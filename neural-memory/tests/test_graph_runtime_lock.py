from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
MAINTENANCE = ROOT / "maintenance"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from graph_runtime_lock import (  # noqa: E402
    GraphRuntimeLockError,
    acquire_graph_runtime_lock,
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "runtime_lock_migration_under_test",
        MAINTENANCE / "migrate_legacy_errors.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_graph_engine(monkeypatch, graph_dir: Path):
    monkeypatch.setenv("THREECAN_GRAPH_DIR", str(graph_dir))
    spec = importlib.util.spec_from_file_location(
        f"graph_engine_atomic_write_{graph_dir.name}",
        BACKEND / "graph_engine.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_same_owner_is_reentrant_and_other_owner_fails_closed(tmp_path):
    first = acquire_graph_runtime_lock(tmp_path, owner_kind="3can-engine")
    second = acquire_graph_runtime_lock(tmp_path, owner_kind="3can-engine")
    try:
        assert first.active is True
        assert second.active is True
        with pytest.raises(
            GraphRuntimeLockError,
            match="different_in_process_owner",
        ):
            acquire_graph_runtime_lock(
                tmp_path,
                owner_kind="legacy-error-migration:apply",
            )
    finally:
        second.release()
        first.release()


def test_release_hands_graph_ownership_to_a_different_owner(tmp_path):
    engine_lease = acquire_graph_runtime_lock(tmp_path, owner_kind="3can-engine")
    engine_lease.release()

    migration_lease = acquire_graph_runtime_lock(
        tmp_path,
        owner_kind="legacy-error-migration:apply",
    )
    try:
        assert migration_lease.active is True
    finally:
        migration_lease.release()


def test_kernel_lock_blocks_another_process_and_recovers_on_exit(tmp_path):
    script = (
        "import sys,time;"
        f"sys.path.insert(0,{str(BACKEND)!r});"
        "from graph_runtime_lock import acquire_graph_runtime_lock;"
        f"lease=acquire_graph_runtime_lock(__import__('pathlib').Path({str(tmp_path)!r}),"
        "owner_kind='child-engine');"
        "print('READY',flush=True);"
        "sys.stdin.readline()"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "READY"
        with pytest.raises(GraphRuntimeLockError, match="graph_runtime_lock_busy"):
            acquire_graph_runtime_lock(tmp_path, owner_kind="parent-engine")
    finally:
        if child.stdin:
            child.stdin.write("\n")
            child.stdin.flush()
        child.wait(timeout=10)
    assert child.returncode == 0

    recovered = acquire_graph_runtime_lock(tmp_path, owner_kind="parent-engine")
    recovered.release()


def test_migration_uses_the_same_runtime_lock(tmp_path):
    migration = _load_migration()
    engine_lease = acquire_graph_runtime_lock(tmp_path, owner_kind="3can-engine")
    try:
        with pytest.raises(
            migration.MigrationError,
            match="runtime or another maintenance writer",
        ):
            with migration._mutation_lock(
                tmp_path,
                operation="apply",
                plan_hash="a" * 64,
            ):
                pytest.fail("migration must not enter while the engine owns the graph")
    finally:
        engine_lease.release()

    with migration._mutation_lock(
        tmp_path,
        operation="apply",
        plan_hash="a" * 64,
    ) as owner:
        assert owner["operation"] == "apply"


def test_node_fsync_failure_preserves_authoritative_file(
    monkeypatch,
    tmp_path,
):
    graph_dir = tmp_path / "node-fsync-graph"
    nodes_dir = graph_dir / "nodes"
    nodes_dir.mkdir(parents=True)
    graph_engine = _load_graph_engine(monkeypatch, graph_dir)
    engine = graph_engine.GraphEngine.__new__(graph_engine.GraphEngine)
    node = graph_engine.Node(
        id="DOC-atomic-node",
        name="Atomic node",
        cluster="Tests",
        content=graph_engine.NodeContent(description="new payload"),
    )
    engine.nodes = {node.id: node}
    target = nodes_dir / f"{node.id}.json"
    original = b'{"id":"DOC-atomic-node","sentinel":"old"}'
    target.write_bytes(original)

    def fail_fsync(descriptor):
        raise OSError("injected_file_fsync_failure")

    monkeypatch.setattr(graph_engine._os, "fsync", fail_fsync)

    with pytest.raises(OSError, match="injected_file_fsync_failure"):
        engine._save_node(node)

    assert target.read_bytes() == original
    assert list(nodes_dir.glob(f".{target.name}.*.tmp")) == []


def test_edge_replace_failure_preserves_authoritative_file(
    monkeypatch,
    tmp_path,
):
    graph_dir = tmp_path / "edge-replace-graph"
    graph_dir.mkdir()
    graph_engine = _load_graph_engine(monkeypatch, graph_dir)
    engine = graph_engine.GraphEngine.__new__(graph_engine.GraphEngine)
    engine.edges = [
        graph_engine.Edge(
            source="DOC-source",
            target="DOC-target",
            description="new edge",
        )
    ]
    target = graph_dir / "edges.json"
    original = b'[{"source":"DOC-old","target":"DOC-existing"}]'
    target.write_bytes(original)

    def fail_replace(source, destination):
        source_path = Path(source)
        destination_path = Path(destination)
        assert source_path.parent == target.parent
        assert destination_path == target
        staged = json.loads(source_path.read_text(encoding="utf-8"))
        assert staged[0]["source"] == "DOC-source"
        raise OSError("injected_replace_failure")

    monkeypatch.setattr(graph_engine._os, "replace", fail_replace)

    with pytest.raises(OSError, match="injected_replace_failure"):
        engine._save_edges()

    assert target.read_bytes() == original
    assert list(graph_dir.glob(f".{target.name}.*.tmp")) == []


def test_all_stable_graph_json_writes_share_atomic_helper(
    monkeypatch,
    tmp_path,
):
    graph_dir = tmp_path / "shared-helper-graph"
    nodes_dir = graph_dir / "nodes"
    nodes_dir.mkdir(parents=True)
    graph_engine = _load_graph_engine(monkeypatch, graph_dir)
    monkeypatch.setattr(graph_engine, "NODES_DIR", nodes_dir)
    monkeypatch.setattr(graph_engine, "EDGES_FILE", graph_dir / "edges.json")
    monkeypatch.setattr(graph_engine, "AGENTS_FILE", graph_dir / "agents.json")
    monkeypatch.setattr(
        graph_engine,
        "ACTIVITY_FILE",
        graph_dir / "activity_log.json",
    )

    engine = graph_engine.GraphEngine.__new__(graph_engine.GraphEngine)
    node = graph_engine.Node(
        id="DOC-shared-helper",
        name="Shared helper",
        cluster="Tests",
    )
    engine.nodes = {node.id: node}
    engine.edges = [
        graph_engine.Edge(source=node.id, target="DOC-target")
    ]
    engine.agents = {
        "agent-test": graph_engine.AgentInfo(agent_id="agent-test")
    }
    engine.activity_log = [
        graph_engine.ActivityEntry(agent_id="agent-test", action="test")
    ]
    engine._click_log = {"QUERY": {node.id: 1}}
    engine._pending_keywords = {node.id: {"atomic": 1}}
    engine._CLICK_LOG_FILE = graph_dir / "route_click_log.json"
    engine._PENDING_KW_FILE = graph_dir / "pending_keywords.json"

    calls = []

    def record(path, payload):
        calls.append((Path(path), payload))

    monkeypatch.setattr(graph_engine, "_atomic_write_json", record)

    engine._ensure_dirs()
    engine._save_node(node)
    engine._save_edges()
    engine._save_agents()
    engine._save_activity_log()
    engine._save_click_log()
    engine._save_pending_keywords()

    assert [path for path, _ in calls] == [
        graph_dir / "edges.json",
        nodes_dir / f"{node.id}.json",
        graph_dir / "edges.json",
        graph_dir / "agents.json",
        graph_dir / "activity_log.json",
        graph_dir / "route_click_log.json",
        graph_dir / "pending_keywords.json",
    ]
    assert calls[0][1] == []
