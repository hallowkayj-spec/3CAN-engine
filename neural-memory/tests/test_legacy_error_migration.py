from __future__ import annotations

import importlib.util
import json
import os
import socket
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "maintenance"
    / "migrate_legacy_errors.py"
)
SPEC = importlib.util.spec_from_file_location("migrate_legacy_errors", MODULE_PATH)
assert SPEC and SPEC.loader
MIGRATION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MIGRATION)


REGISTRY_ID = "MEM-3can-core-memory-lane-registry-20260523"


class _QuietHandler(BaseHTTPRequestHandler):
    def do_HEAD(self) -> None:
        self.send_response(204)
        self.end_headers()

    def log_message(self, _format: str, *_args: object) -> None:
        return


@contextmanager
def _active_engine_endpoint():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _QuietHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _silent_engine_endpoint() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
        candidate.bind(("127.0.0.1", 0))
        _host, port = candidate.getsockname()
    return f"http://127.0.0.1:{port}"


def _offline_defaults(monkeypatch) -> str:
    endpoint = _silent_engine_endpoint()
    monkeypatch.setattr(MIGRATION, "DEFAULT_ENGINE_ENDPOINTS", (endpoint,))
    for name in (
        *MIGRATION.ENGINE_ENDPOINT_ENV_KEYS,
        *MIGRATION.ENGINE_PORT_ENV_KEYS,
    ):
        monkeypatch.delenv(name, raising=False)
    return endpoint


def _node(
    node_id: str,
    *,
    count: int | None = None,
    diagnosis: str = "",
    solution: str = "",
    case_status: str = "",
    promoted: bool = False,
) -> dict:
    extra: dict[str, object] = {}
    if count is not None:
        extra["occurrence_count"] = count
    if diagnosis:
        extra["diagnosis"] = diagnosis
    if solution:
        extra["solution_summary"] = solution
        extra["verification_evidence"] = ["pytest -q passed"]
    if case_status:
        extra["case_status"] = case_status
    if promoted:
        extra["promoted"] = True
    return {
        "id": node_id,
        "name": node_id,
        "cluster": "errors" if node_id.startswith("ERR-") else "system",
        "layer": "L0",
        "type": "feedback" if node_id.startswith("ERR-") else "knowledge",
        "status": "active",
        "content": {
            "description": "fixture",
            "current_state": case_status or "recorded",
            "blockers": ["A matching second failure blocks blind retry until diagnosis is written."],
            "notes": "",
            "extra": extra,
        },
        "activation_keywords": [],
        "priority": "high",
    }


def _write_graph(root: Path) -> Path:
    graph = root / "graph"
    nodes = graph / "nodes"
    nodes.mkdir(parents=True)
    fixtures = {
        REGISTRY_ID: _node(REGISTRY_ID),
        "ERR-repeated-remove": _node("ERR-repeated-remove", count=1),
        "ERR-repeated-diagnosed": _node(
            "ERR-repeated-diagnosed",
            count=1,
            diagnosis="PowerShell needs a native separator.",
        ),
        "ERR-repeated-promoted": _node("ERR-repeated-promoted", count=2),
        "ERR-repeated-explicit-promoted": _node(
            "ERR-repeated-explicit-promoted",
            count=1,
            promoted=True,
        ),
        "ERR-repeated-resolved": _node(
            "ERR-repeated-resolved",
            count=1,
            solution="Use the verified bounded command.",
            case_status="resolved",
        ),
        "ERR-repeated-edge-resolved": _node("ERR-repeated-edge-resolved", count=1),
        "FIX-edge-resolution": _node("FIX-edge-resolution"),
        "DOC-unrelated": _node("DOC-unrelated"),
        "DOC-neighbor": _node("DOC-neighbor"),
    }
    for node_id, payload in fixtures.items():
        (nodes / f"{node_id}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    edges = [
        {
            "source": REGISTRY_ID,
            "target": node_id,
            "type": "requires",
            "weight": 1.0,
        }
        for node_id in fixtures
        if node_id.startswith("ERR-repeated-")
    ]
    edges.extend(
        [
            {
                "source": "ERR-repeated-remove",
                "target": "DOC-neighbor",
                "type": "informs",
                "weight": 0.5,
            },
            {
                "source": "FIX-edge-resolution",
                "target": "ERR-repeated-edge-resolved",
                "type": "resolves",
                "weight": 1.0,
            },
            {
                "source": "DOC-unrelated",
                "target": "DOC-neighbor",
                "type": "informs",
                "weight": 0.8,
            },
        ]
    )
    (graph / "edges.json").write_text(
        json.dumps(edges, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (graph / "embeddings.npz").write_bytes(b"fixture-embedding-cache")
    return graph


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _read_edges(graph: Path) -> list[dict]:
    return json.loads((graph / "edges.json").read_text(encoding="utf-8"))


def test_dry_run_is_default_and_does_not_mutate(tmp_path: Path) -> None:
    graph = _write_graph(tmp_path)
    before = _tree_bytes(graph)

    result = MIGRATION.migrate(graph)

    assert result["mode"] == "dry-run"
    assert result["applied"] is False
    assert result["candidate_node_ids"] == [
        "ERR-repeated-diagnosed",
        "ERR-repeated-remove",
    ]
    assert result["before"]["core_registry_requires_to_repeated_count"] == 6
    assert result["after"]["core_registry_requires_to_repeated_count"] == 0
    assert _tree_bytes(graph) == before
    assert not (graph / "maintenance").exists()


def test_unknown_count_requires_explicit_promotion_or_solution_to_remain(
    tmp_path: Path,
) -> None:
    graph = _write_graph(tmp_path)
    nodes = graph / "nodes"
    unknown = "ERR-repeated-unknown-low-value"
    diagnosed = "ERR-repeated-unknown-diagnosed"
    promoted = "ERR-repeated-unknown-promoted"
    (nodes / f"{unknown}.json").write_text(
        json.dumps(_node(unknown), ensure_ascii=False),
        encoding="utf-8",
    )
    (nodes / f"{diagnosed}.json").write_text(
        json.dumps(
            _node(diagnosed, diagnosis="The durable root cause is known."),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (nodes / f"{promoted}.json").write_text(
        json.dumps(
            _node(
                promoted,
                diagnosis="The durable root cause is known.",
                promoted=True,
            ),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = MIGRATION.migrate(graph)

    assert unknown in result["candidate_node_ids"]
    assert diagnosed in result["candidate_node_ids"]
    assert promoted not in result["candidate_node_ids"]
    assert promoted in result["preserved"]["unknown_count_node_ids"]


def test_public_manifest_bounds_large_node_and_edge_lists(
    tmp_path: Path,
) -> None:
    graph = _write_graph(tmp_path)
    nodes = graph / "nodes"
    edges = _read_edges(graph)
    for index in range(MIGRATION.PUBLIC_MANIFEST_LIST_LIMIT + 7):
        node_id = f"ERR-repeated-generated-{index:03d}"
        (nodes / f"{node_id}.json").write_text(
            json.dumps(_node(node_id), ensure_ascii=False),
            encoding="utf-8",
        )
        edges.append(
            {
                "source": REGISTRY_ID,
                "target": node_id,
                "type": "requires",
                "weight": 1.0,
            }
        )
    (graph / "edges.json").write_text(
        json.dumps(edges, ensure_ascii=False),
        encoding="utf-8",
    )

    result = MIGRATION.migrate(graph)

    assert len(result["candidate_node_ids"]) == MIGRATION.PUBLIC_MANIFEST_LIST_LIMIT
    assert result["candidate_node_ids_count"] == MIGRATION.PUBLIC_MANIFEST_LIST_LIMIT + 9
    assert result["candidate_node_ids_truncated"] is True
    assert len(result["removed_edges"]) == MIGRATION.PUBLIC_MANIFEST_LIST_LIMIT
    assert result["removed_edges_count"] > MIGRATION.PUBLIC_MANIFEST_LIST_LIMIT
    assert result["removed_edges_truncated"] is True


def test_corrupt_node_is_backed_up_removed_and_exactly_restored(
    tmp_path: Path,
    monkeypatch,
) -> None:
    graph = _write_graph(tmp_path)
    corrupt = graph / "nodes" / "ERR-repeated-zero-byte.json"
    corrupt.write_bytes(b"")
    original = _tree_bytes(graph)
    silent_endpoint = _offline_defaults(monkeypatch)

    dry_run = MIGRATION.migrate(graph)
    assert dry_run["corrupt_node_ids"] == ["ERR-repeated-zero-byte"]
    assert dry_run["before"]["corrupt_node_file_count"] == 1

    applied = MIGRATION.migrate(
        graph,
        apply=True,
        confirm_engine_stopped=True,
        engine_endpoints=[silent_endpoint],
        engine_probe_timeout_sec=0.05,
    )

    assert not corrupt.exists()
    backup = Path(applied["paths"]["backup"])
    assert (backup / "nodes" / corrupt.name).read_bytes() == b""
    archive_records = [
        json.loads(line)
        for line in Path(applied["paths"]["archive"])
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    corrupt_record = next(
        item for item in archive_records if item["node_id"] == "ERR-repeated-zero-byte"
    )
    assert corrupt_record["record_type"] == "corrupt_graph_node"
    assert corrupt_record["source_size"] == 0

    MIGRATION.rollback(
        graph,
        backup,
        confirm_engine_stopped=True,
        engine_endpoints=[silent_endpoint],
        engine_probe_timeout_sec=0.05,
    )
    restored_mutable = {
        name: payload
        for name, payload in _tree_bytes(graph).items()
        if not name.startswith("maintenance/")
    }
    assert restored_mutable == original


def test_apply_requires_explicit_engine_stopped_confirmation(tmp_path: Path) -> None:
    graph = _write_graph(tmp_path)
    before = _tree_bytes(graph)

    with pytest.raises(MIGRATION.MigrationError, match="confirm-engine-stopped"):
        MIGRATION.migrate(graph, apply=True)

    assert _tree_bytes(graph) == before


def test_apply_rejects_a_live_engine_endpoint_before_mutating(tmp_path: Path) -> None:
    graph = _write_graph(tmp_path)
    before = _tree_bytes(graph)

    with _active_engine_endpoint() as endpoint:
        with pytest.raises(MIGRATION.MigrationError, match="still active"):
            MIGRATION.migrate(
                graph,
                apply=True,
                confirm_engine_stopped=True,
                engine_endpoints=[endpoint],
                engine_probe_timeout_sec=0.1,
            )

    assert _tree_bytes(graph) == before


def test_empty_endpoint_override_cannot_skip_default_probes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    graph = _write_graph(tmp_path)
    before = _tree_bytes(graph)

    with _active_engine_endpoint() as endpoint:
        monkeypatch.setattr(MIGRATION, "DEFAULT_ENGINE_ENDPOINTS", (endpoint,))
        with pytest.raises(MIGRATION.MigrationError, match="still active"):
            MIGRATION.migrate(
                graph,
                apply=True,
                confirm_engine_stopped=True,
                engine_endpoints=[],
                engine_probe_timeout_sec=0.1,
            )

    assert _tree_bytes(graph) == before


def test_default_probe_set_covers_every_public_local_profile() -> None:
    assert MIGRATION.DEFAULT_ENGINE_ENDPOINTS == (
        "http://127.0.0.1:9700",
        "http://127.0.0.1:9701",
        "http://127.0.0.1:9702",
        "http://127.0.0.1:9711",
    )


def test_apply_rejects_endpoint_discovered_from_graph_config(
    tmp_path: Path,
    monkeypatch,
) -> None:
    graph = _write_graph(tmp_path)
    before = _tree_bytes(graph)
    _offline_defaults(monkeypatch)

    with _active_engine_endpoint() as endpoint:
        (graph / "engine_endpoints.json").write_text(
            json.dumps({"engine_endpoints": [endpoint]}),
            encoding="utf-8",
        )
        before = _tree_bytes(graph)
        with pytest.raises(MIGRATION.MigrationError, match="still active"):
            MIGRATION.migrate(
                graph,
                apply=True,
                confirm_engine_stopped=True,
                engine_probe_timeout_sec=0.05,
            )

    assert _tree_bytes(graph) == before


def test_apply_backs_up_archives_cleans_edges_and_preserves_knowledge(
    tmp_path: Path,
    monkeypatch,
) -> None:
    graph = _write_graph(tmp_path)
    original_edges = (graph / "edges.json").read_bytes()
    original_candidate = (graph / "nodes" / "ERR-repeated-remove.json").read_bytes()
    silent_endpoint = _offline_defaults(monkeypatch)

    result = MIGRATION.migrate(
        graph,
        apply=True,
        confirm_engine_stopped=True,
        engine_endpoints=[silent_endpoint],
        engine_probe_timeout_sec=0.05,
    )

    assert result["applied"] is True
    assert result["no_op"] is False
    assert result["engine_quiescence"]["confirmed_engine_stopped"] is True
    assert result["engine_quiescence"]["checked_endpoints"] == [f"{silent_endpoint}/"]
    assert result["engine_quiescence"]["checked_immediately_before_mutation"] is True
    assert not (graph / "nodes" / "ERR-repeated-remove.json").exists()
    assert not (graph / "nodes" / "ERR-repeated-diagnosed.json").exists()
    for preserved_id in (
        "ERR-repeated-promoted",
        "ERR-repeated-explicit-promoted",
        "ERR-repeated-resolved",
        "ERR-repeated-edge-resolved",
        "DOC-unrelated",
    ):
        assert (graph / "nodes" / f"{preserved_id}.json").is_file()

    remaining_edges = _read_edges(graph)
    assert not any(
        edge["source"] == REGISTRY_ID
        and edge["target"].startswith("ERR-repeated-")
        and edge["type"] == "requires"
        for edge in remaining_edges
    )
    assert not any(
        "ERR-repeated-remove" in {edge["source"], edge["target"]}
        for edge in remaining_edges
    )
    assert any(
        edge["source"] == "FIX-edge-resolution"
        and edge["target"] == "ERR-repeated-edge-resolved"
        and edge["type"] == "resolves"
        for edge in remaining_edges
    )
    assert any(
        edge["source"] == "DOC-unrelated"
        and edge["target"] == "DOC-neighbor"
        for edge in remaining_edges
    )

    promoted = json.loads(
        (graph / "nodes" / "ERR-repeated-promoted.json").read_text(encoding="utf-8")
    )
    promoted_extra = promoted["content"]["extra"]
    assert promoted_extra["error_knowledge_schema_version"] == "3can.error-knowledge/v1"
    assert promoted_extra["promoted"] is True
    assert promoted_extra["case_status"] == "observed"
    assert promoted_extra["route_blocking"] is True

    backup = Path(result["paths"]["backup"])
    assert (backup / "backup_metadata.json").is_file()
    assert (backup / "nodes" / "ERR-repeated-remove.json").read_bytes() == original_candidate
    assert (backup / "edges.json").read_bytes() == original_edges
    assert (backup / "embeddings.npz").read_bytes() == b"fixture-embedding-cache"

    archive = Path(result["paths"]["archive"])
    records = [
        json.loads(line)
        for line in archive.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [record["node_id"] for record in records] == [
        "ERR-repeated-diagnosed",
        "ERR-repeated-remove",
    ]
    remove_record = next(
        record for record in records if record["node_id"] == "ERR-repeated-remove"
    )
    assert remove_record["node"]["id"] == "ERR-repeated-remove"
    assert {
        (edge["source"], edge["target"], edge["type"])
        for edge in remove_record["connected_edges"]
    } == {
        (REGISTRY_ID, "ERR-repeated-remove", "requires"),
        ("ERR-repeated-remove", "DOC-neighbor", "informs"),
    }

    assert not (graph / "embeddings.npz").exists()
    marker = json.loads(
        (graph / "embeddings.rebuild_required.json").read_text(encoding="utf-8")
    )
    assert marker["run_id"] == result["run_id"]
    assert marker["removed_node_ids"] == [
        "ERR-repeated-diagnosed",
        "ERR-repeated-remove",
    ]
    assert Path(result["paths"]["manifest"]).is_file()


def test_second_apply_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    graph = _write_graph(tmp_path)
    silent_endpoint = _offline_defaults(monkeypatch)
    first = MIGRATION.migrate(
        graph,
        apply=True,
        confirm_engine_stopped=True,
        engine_endpoints=[silent_endpoint],
        engine_probe_timeout_sec=0.05,
    )
    maintenance_before = _tree_bytes(graph / "maintenance")
    graph_before = _tree_bytes(graph)

    second = MIGRATION.migrate(
        graph,
        apply=True,
        confirm_engine_stopped=True,
        engine_endpoints=[silent_endpoint],
        engine_probe_timeout_sec=0.05,
    )

    assert first["applied"] is True
    assert second["applied"] is False
    assert second["no_op"] is True
    assert second["candidate_node_ids"] == []
    assert second["removed_core_registry_requires_edges"] == []
    assert _tree_bytes(graph) == graph_before
    assert _tree_bytes(graph / "maintenance") == maintenance_before


def test_rollback_restores_exact_graph_snapshot(tmp_path: Path, monkeypatch) -> None:
    graph = _write_graph(tmp_path)
    original = _tree_bytes(graph)
    silent_endpoint = _offline_defaults(monkeypatch)
    applied = MIGRATION.migrate(
        graph,
        apply=True,
        confirm_engine_stopped=True,
        engine_endpoints=[silent_endpoint],
        engine_probe_timeout_sec=0.05,
    )
    backup = Path(applied["paths"]["backup"])
    migrated = _tree_bytes(graph)

    with pytest.raises(MIGRATION.MigrationError, match="confirm-engine-stopped"):
        MIGRATION.rollback(
            graph,
            backup,
            engine_endpoints=[silent_endpoint],
            engine_probe_timeout_sec=0.05,
        )
    assert _tree_bytes(graph) == migrated

    with _active_engine_endpoint() as active_endpoint:
        with pytest.raises(MIGRATION.MigrationError, match="still active"):
            MIGRATION.rollback(
                graph,
                backup,
                confirm_engine_stopped=True,
                engine_endpoints=[active_endpoint],
                engine_probe_timeout_sec=0.1,
            )
    assert _tree_bytes(graph) == migrated

    result = MIGRATION.rollback(
        graph,
        backup,
        confirm_engine_stopped=True,
        engine_endpoints=[silent_endpoint],
        engine_probe_timeout_sec=0.05,
    )

    assert result["restored"] is True
    assert result["engine_quiescence"]["checked_immediately_before_mutation"] is True
    restored_mutable = {
        name: payload
        for name, payload in _tree_bytes(graph).items()
        if not name.startswith("maintenance/")
    }
    assert restored_mutable == original


def test_rollback_rejects_corrupt_backup_before_mutating(
    tmp_path: Path,
    monkeypatch,
) -> None:
    graph = _write_graph(tmp_path)
    silent_endpoint = _offline_defaults(monkeypatch)
    applied = MIGRATION.migrate(
        graph,
        apply=True,
        confirm_engine_stopped=True,
        engine_endpoints=[silent_endpoint],
        engine_probe_timeout_sec=0.05,
    )
    backup = Path(applied["paths"]["backup"])
    (backup / "edges.json").write_text("[]", encoding="utf-8")
    before = _tree_bytes(graph)

    with pytest.raises(MIGRATION.MigrationError, match="edges checksum"):
        MIGRATION.rollback(
            graph,
            backup,
            confirm_engine_stopped=True,
            engine_endpoints=[silent_endpoint],
            engine_probe_timeout_sec=0.05,
        )

    assert _tree_bytes(graph) == before


def test_live_local_lock_is_refused_without_mutation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    graph = _write_graph(tmp_path)
    silent_endpoint = _offline_defaults(monkeypatch)
    lock = graph / ".legacy_error_migration.lock"
    lock.write_text(
        json.dumps(
            {
                "schema_version": MIGRATION.LOCK_VERSION,
                "lock_id": "live-owner",
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "started_at": "2026-07-29T00:00:00+00:00",
                "operation": "apply",
                "plan_hash": "a" * 64,
            }
        ),
        encoding="utf-8",
    )
    before = _tree_bytes(graph)

    with pytest.raises(MIGRATION.MigrationError, match="active or remote"):
        MIGRATION.migrate(
            graph,
            apply=True,
            confirm_engine_stopped=True,
            engine_endpoints=[silent_endpoint],
            engine_probe_timeout_sec=0.05,
        )

    assert _tree_bytes(graph) == before


def test_dead_local_lock_is_archived_and_apply_recovers(
    tmp_path: Path,
    monkeypatch,
) -> None:
    graph = _write_graph(tmp_path)
    silent_endpoint = _offline_defaults(monkeypatch)
    lock = graph / ".legacy_error_migration.lock"
    lock.write_text(
        json.dumps(
            {
                "schema_version": MIGRATION.LOCK_VERSION,
                "lock_id": "dead-owner",
                "pid": 2_147_483_647,
                "host": socket.gethostname(),
                "started_at": "2026-07-28T00:00:00+00:00",
                "operation": "apply",
                "plan_hash": "b" * 64,
            }
        ),
        encoding="utf-8",
    )

    result = MIGRATION.migrate(
        graph,
        apply=True,
        confirm_engine_stopped=True,
        engine_endpoints=[silent_endpoint],
        engine_probe_timeout_sec=0.05,
    )

    assert result["applied"] is True
    assert not lock.exists()
    stale_locks = list(
        (
            graph
            / "maintenance"
            / "legacy_error_migration"
            / "stale_locks"
        ).glob("*.lock")
    )
    assert len(stale_locks) == 1
    assert json.loads(stale_locks[0].read_text(encoding="utf-8"))["lock_id"] == "dead-owner"


def test_dead_legacy_pid_lock_does_not_permanently_block_apply(
    tmp_path: Path,
    monkeypatch,
) -> None:
    graph = _write_graph(tmp_path)
    silent_endpoint = _offline_defaults(monkeypatch)
    lock = graph / ".legacy_error_migration.lock"
    lock.write_text("pid=2147483647\n", encoding="utf-8")

    result = MIGRATION.migrate(
        graph,
        apply=True,
        confirm_engine_stopped=True,
        engine_endpoints=[silent_endpoint],
        engine_probe_timeout_sec=0.05,
    )

    assert result["applied"] is True
    assert not lock.exists()
    archived = list(
        (
            graph
            / "maintenance"
            / "legacy_error_migration"
            / "stale_locks"
        ).glob("*.lock")
    )
    assert len(archived) == 1
    assert archived[0].read_text(encoding="utf-8") == "pid=2147483647\n"


def test_interrupted_apply_resumes_from_verified_journal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    graph = _write_graph(tmp_path)
    silent_endpoint = _offline_defaults(monkeypatch)
    original_save = MIGRATION._save_journal
    crashed = False

    def crash_after_edges(path: Path, journal: dict) -> None:
        nonlocal crashed
        if journal.get("phase") == "edges_written" and not crashed:
            crashed = True
            raise RuntimeError("simulated process loss after edges replace")
        original_save(path, journal)

    monkeypatch.setattr(MIGRATION, "_save_journal", crash_after_edges)
    with pytest.raises(RuntimeError, match="simulated process loss"):
        MIGRATION.migrate(
            graph,
            apply=True,
            confirm_engine_stopped=True,
            engine_endpoints=[silent_endpoint],
            engine_probe_timeout_sec=0.05,
        )

    assert not (graph / ".legacy_error_migration.lock").exists()
    partial_before_dry_run = _tree_bytes(graph)
    dry_run = MIGRATION.migrate(graph)
    assert dry_run["resume_required"] is True
    assert _tree_bytes(graph) == partial_before_dry_run

    monkeypatch.setattr(MIGRATION, "_save_journal", original_save)
    resumed = MIGRATION.migrate(
        graph,
        apply=True,
        confirm_engine_stopped=True,
        engine_endpoints=[silent_endpoint],
        engine_probe_timeout_sec=0.05,
    )
    repeated = MIGRATION.migrate(
        graph,
        apply=True,
        confirm_engine_stopped=True,
        engine_endpoints=[silent_endpoint],
        engine_probe_timeout_sec=0.05,
    )

    assert resumed["applied"] is True
    assert resumed["resumed_from_journal"] is True
    assert resumed["journal_phase"] == "completed"
    assert repeated["applied"] is False
    assert repeated["no_op"] is True


def test_partial_apply_never_replaces_a_missing_original_backup(
    tmp_path: Path,
    monkeypatch,
) -> None:
    graph = _write_graph(tmp_path)
    silent_endpoint = _offline_defaults(monkeypatch)
    original_save = MIGRATION._save_journal
    crashed = False

    def crash_after_edges(path: Path, journal: dict) -> None:
        nonlocal crashed
        if journal.get("phase") == "edges_written" and not crashed:
            crashed = True
            raise RuntimeError("simulated partial apply before backup loss")
        original_save(path, journal)

    monkeypatch.setattr(MIGRATION, "_save_journal", crash_after_edges)
    with pytest.raises(RuntimeError, match="before backup loss"):
        MIGRATION.migrate(
            graph,
            apply=True,
            confirm_engine_stopped=True,
            engine_endpoints=[silent_endpoint],
            engine_probe_timeout_sec=0.05,
        )
    monkeypatch.setattr(MIGRATION, "_save_journal", original_save)

    recovery = MIGRATION.migrate(graph)
    backup = Path(recovery["paths"]["backup"])
    MIGRATION.shutil.rmtree(backup)
    before = _tree_bytes(graph)
    with pytest.raises(MIGRATION.MigrationError, match="original rollback backup"):
        MIGRATION.migrate(
            graph,
            apply=True,
            confirm_engine_stopped=True,
            engine_endpoints=[silent_endpoint],
            engine_probe_timeout_sec=0.05,
        )
    assert _tree_bytes(graph) == before
    assert not backup.exists()


def test_interrupted_rollback_resumes_after_atomic_node_swap(
    tmp_path: Path,
    monkeypatch,
) -> None:
    graph = _write_graph(tmp_path)
    original = _tree_bytes(graph)
    silent_endpoint = _offline_defaults(monkeypatch)
    applied = MIGRATION.migrate(
        graph,
        apply=True,
        confirm_engine_stopped=True,
        engine_endpoints=[silent_endpoint],
        engine_probe_timeout_sec=0.05,
    )
    backup = Path(applied["paths"]["backup"])
    original_save = MIGRATION._save_journal
    crashed = False

    def crash_after_node_swap(path: Path, journal: dict) -> None:
        nonlocal crashed
        if journal.get("phase") == "nodes_swapped" and not crashed:
            crashed = True
            raise RuntimeError("simulated process loss after node swap")
        original_save(path, journal)

    monkeypatch.setattr(MIGRATION, "_save_journal", crash_after_node_swap)
    with pytest.raises(RuntimeError, match="simulated process loss"):
        MIGRATION.rollback(
            graph,
            backup,
            confirm_engine_stopped=True,
            engine_endpoints=[silent_endpoint],
            engine_probe_timeout_sec=0.05,
        )
    assert not (graph / ".legacy_error_migration.lock").exists()

    monkeypatch.setattr(MIGRATION, "_save_journal", original_save)
    resumed = MIGRATION.rollback(
        graph,
        backup,
        confirm_engine_stopped=True,
        engine_endpoints=[silent_endpoint],
        engine_probe_timeout_sec=0.05,
    )

    restored_mutable = {
        name: payload
        for name, payload in _tree_bytes(graph).items()
        if not name.startswith("maintenance/")
    }
    assert resumed["restored"] is True
    assert resumed["resumed_from_journal"] is True
    assert restored_mutable == original


def test_interrupted_rollback_rebuilds_partial_restore_copy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    graph = _write_graph(tmp_path)
    original = _tree_bytes(graph)
    silent_endpoint = _offline_defaults(monkeypatch)
    applied = MIGRATION.migrate(
        graph,
        apply=True,
        confirm_engine_stopped=True,
        engine_endpoints=[silent_endpoint],
        engine_probe_timeout_sec=0.05,
    )
    backup = Path(applied["paths"]["backup"])
    original_copytree = MIGRATION.shutil.copytree
    crashed = False

    def partial_copy(source: Path, destination: Path, *args, **kwargs):
        nonlocal crashed
        destination_path = Path(destination)
        if (
            Path(source) == backup / "nodes"
            and destination_path.name.startswith(".legacy-error-restore-")
            and not crashed
        ):
            crashed = True
            destination_path.mkdir(parents=True)
            first = next((backup / "nodes").glob("*.json"))
            MIGRATION.shutil.copy2(first, destination_path / first.name)
            raise RuntimeError("simulated interrupted restore copy")
        return original_copytree(source, destination, *args, **kwargs)

    monkeypatch.setattr(MIGRATION.shutil, "copytree", partial_copy)
    with pytest.raises(RuntimeError, match="interrupted restore copy"):
        MIGRATION.rollback(
            graph,
            backup,
            confirm_engine_stopped=True,
            engine_endpoints=[silent_endpoint],
            engine_probe_timeout_sec=0.05,
        )
    monkeypatch.setattr(MIGRATION.shutil, "copytree", original_copytree)

    resumed = MIGRATION.rollback(
        graph,
        backup,
        confirm_engine_stopped=True,
        engine_endpoints=[silent_endpoint],
        engine_probe_timeout_sec=0.05,
    )
    restored_mutable = {
        name: payload
        for name, payload in _tree_bytes(graph).items()
        if not name.startswith("maintenance/")
    }
    assert resumed["resumed_from_journal"] is True
    assert restored_mutable == original


def test_interrupted_apply_can_be_explicitly_rolled_back(
    tmp_path: Path,
    monkeypatch,
) -> None:
    graph = _write_graph(tmp_path)
    original = _tree_bytes(graph)
    silent_endpoint = _offline_defaults(monkeypatch)
    original_save = MIGRATION._save_journal
    crashed = False

    def crash_after_edges(path: Path, journal: dict) -> None:
        nonlocal crashed
        if journal.get("phase") == "edges_written" and not crashed:
            crashed = True
            raise RuntimeError("simulated interrupted apply for rollback")
        original_save(path, journal)

    monkeypatch.setattr(MIGRATION, "_save_journal", crash_after_edges)
    with pytest.raises(RuntimeError, match="interrupted apply"):
        MIGRATION.migrate(
            graph,
            apply=True,
            confirm_engine_stopped=True,
            engine_endpoints=[silent_endpoint],
            engine_probe_timeout_sec=0.05,
        )
    monkeypatch.setattr(MIGRATION, "_save_journal", original_save)

    dry_run = MIGRATION.migrate(graph)
    backup = Path(dry_run["paths"]["backup"])
    rolled_back = MIGRATION.rollback(
        graph,
        backup,
        confirm_engine_stopped=True,
        engine_endpoints=[silent_endpoint],
        engine_probe_timeout_sec=0.05,
    )

    apply_journal = json.loads(
        Path(dry_run["journal_path"]).read_text(encoding="utf-8")
    )
    restored_mutable = {
        name: payload
        for name, payload in _tree_bytes(graph).items()
        if not name.startswith("maintenance/")
    }
    assert rolled_back["restored"] is True
    assert apply_journal["phase"] == "rolled_back"
    assert restored_mutable == original


def test_removal_checkpoints_are_batched(tmp_path: Path, monkeypatch) -> None:
    graph = _write_graph(tmp_path)
    nodes = graph / "nodes"
    for index in range(205):
        node_id = f"ERR-repeated-batch-{index:03d}"
        (nodes / f"{node_id}.json").write_text(
            json.dumps(_node(node_id, count=1), ensure_ascii=False),
            encoding="utf-8",
        )
    silent_endpoint = _offline_defaults(monkeypatch)
    original_save = MIGRATION._save_journal
    removal_checkpoint_sizes: list[int] = []

    def record_removal_checkpoints(path: Path, journal: dict) -> None:
        if journal.get("phase") == "removing_candidates":
            checkpoints = journal["checkpoints"]
            removal_checkpoint_sizes.append(
                len(checkpoints["removed_node_ids"])
                + len(checkpoints["removed_corrupt_node_ids"])
            )
        original_save(path, journal)

    monkeypatch.setattr(MIGRATION, "JOURNAL_CHECKPOINT_BATCH_SIZE", 100)
    monkeypatch.setattr(MIGRATION, "_save_journal", record_removal_checkpoints)

    result = MIGRATION.migrate(
        graph,
        apply=True,
        confirm_engine_stopped=True,
        engine_endpoints=[silent_endpoint],
        engine_probe_timeout_sec=0.05,
    )

    assert result["applied"] is True
    assert removal_checkpoint_sizes == [100, 200, 207]


def test_removal_batch_resume_replays_only_uncheckpointed_tail(
    tmp_path: Path,
    monkeypatch,
) -> None:
    graph = _write_graph(tmp_path)
    nodes = graph / "nodes"
    extra_id = "ERR-repeated-uncheckpointed-tail"
    (nodes / f"{extra_id}.json").write_text(
        json.dumps(_node(extra_id, count=1), ensure_ascii=False),
        encoding="utf-8",
    )
    silent_endpoint = _offline_defaults(monkeypatch)
    original_save = MIGRATION._save_journal
    removal_saves = 0

    def crash_on_tail_flush(path: Path, journal: dict) -> None:
        nonlocal removal_saves
        if journal.get("phase") == "removing_candidates":
            removal_saves += 1
            if removal_saves == 2:
                raise RuntimeError("simulated process loss before tail checkpoint")
        original_save(path, journal)

    monkeypatch.setattr(MIGRATION, "JOURNAL_CHECKPOINT_BATCH_SIZE", 2)
    monkeypatch.setattr(MIGRATION, "_save_journal", crash_on_tail_flush)
    with pytest.raises(RuntimeError, match="before tail checkpoint"):
        MIGRATION.migrate(
            graph,
            apply=True,
            confirm_engine_stopped=True,
            engine_endpoints=[silent_endpoint],
            engine_probe_timeout_sec=0.05,
        )

    recovery = MIGRATION.migrate(graph)
    journal_path = Path(recovery["journal_path"])
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    checkpointed = set(journal["checkpoints"]["removed_node_ids"])
    assert len(checkpointed) == 2
    assert not (nodes / "ERR-repeated-remove.json").exists()
    assert not (nodes / "ERR-repeated-diagnosed.json").exists()
    assert not (nodes / f"{extra_id}.json").exists()

    resumed_removal_saves = 0
    resumed_node_unlinks: list[str] = []
    original_unlink = Path.unlink

    def record_resume(path: Path, current: dict) -> None:
        nonlocal resumed_removal_saves
        if current.get("phase") == "removing_candidates":
            resumed_removal_saves += 1
        original_save(path, current)

    def record_unlink(path: Path, *args, **kwargs) -> None:
        if path.parent.resolve() == nodes.resolve():
            resumed_node_unlinks.append(path.stem)
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(MIGRATION, "_save_journal", record_resume)
    monkeypatch.setattr(Path, "unlink", record_unlink)
    resumed = MIGRATION.migrate(
        graph,
        apply=True,
        confirm_engine_stopped=True,
        engine_endpoints=[silent_endpoint],
        engine_probe_timeout_sec=0.05,
    )

    assert resumed["applied"] is True
    assert resumed["resumed_from_journal"] is True
    assert resumed_removal_saves == 1
    expected_tail = {
        "ERR-repeated-remove",
        "ERR-repeated-diagnosed",
        extra_id,
    } - checkpointed
    assert set(resumed_node_unlinks) == expected_tail
    assert checkpointed.isdisjoint(resumed_node_unlinks)


def test_normalization_resume_skips_checkpointed_node_writes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    graph = _write_graph(tmp_path)
    nodes = graph / "nodes"
    expected_normalized = set(MIGRATION.migrate(graph)["normalized_node_ids"])
    assert len(expected_normalized) > 1
    silent_endpoint = _offline_defaults(monkeypatch)
    original_save = MIGRATION._save_journal
    crashed = False

    def persist_first_normalization_then_crash(
        path: Path,
        journal: dict,
    ) -> None:
        nonlocal crashed
        original_save(path, journal)
        if journal.get("phase") == "normalizing_nodes" and not crashed:
            crashed = True
            raise RuntimeError("simulated loss after normalization checkpoint")

    monkeypatch.setattr(MIGRATION, "JOURNAL_CHECKPOINT_BATCH_SIZE", 1)
    monkeypatch.setattr(
        MIGRATION,
        "_save_journal",
        persist_first_normalization_then_crash,
    )
    with pytest.raises(RuntimeError, match="normalization checkpoint"):
        MIGRATION.migrate(
            graph,
            apply=True,
            confirm_engine_stopped=True,
            engine_endpoints=[silent_endpoint],
            engine_probe_timeout_sec=0.05,
        )

    recovery = MIGRATION.migrate(graph)
    journal = json.loads(
        Path(recovery["journal_path"]).read_text(encoding="utf-8")
    )
    checkpointed = set(journal["checkpoints"]["normalized_node_ids"])
    assert len(checkpointed) == 1

    resumed_node_writes: list[str] = []
    original_atomic_write_json = MIGRATION._atomic_write_json

    def record_node_write(path: Path, payload: object) -> None:
        if path.parent.resolve() == nodes.resolve():
            resumed_node_writes.append(path.stem)
        original_atomic_write_json(path, payload)

    monkeypatch.setattr(MIGRATION, "_save_journal", original_save)
    monkeypatch.setattr(MIGRATION, "_atomic_write_json", record_node_write)
    resumed = MIGRATION.migrate(
        graph,
        apply=True,
        confirm_engine_stopped=True,
        engine_endpoints=[silent_endpoint],
        engine_probe_timeout_sec=0.05,
    )

    assert resumed["applied"] is True
    assert resumed["resumed_from_journal"] is True
    assert set(resumed_node_writes) == expected_normalized - checkpointed
    assert checkpointed.isdisjoint(resumed_node_writes)


def test_atomic_write_retries_transient_windows_replace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    destination = tmp_path / "journal.json"
    destination.write_bytes(b"old")
    original_replace = MIGRATION.os.replace
    attempts = 0

    def transient_replace(source: Path, target: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            error = PermissionError("simulated Windows sharing violation")
            error.winerror = 5
            raise error
        original_replace(source, target)

    monkeypatch.setattr(MIGRATION, "ATOMIC_REPLACE_RETRY_DELAYS_SEC", (0.0, 0.0))
    monkeypatch.setattr(MIGRATION.os, "replace", transient_replace)
    monkeypatch.setattr(MIGRATION.time, "sleep", lambda _seconds: None)

    MIGRATION._atomic_write_bytes(destination, b"new")

    assert attempts == 3
    assert destination.read_bytes() == b"new"
    assert list(tmp_path.glob(".journal.json.*.tmp")) == []


def test_atomic_write_does_not_retry_nontransient_replace_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    destination = tmp_path / "journal.json"
    destination.write_bytes(b"old")
    attempts = 0

    def denied_replace(_source: Path, _target: Path) -> None:
        nonlocal attempts
        attempts += 1
        raise PermissionError("non-Windows permission failure")

    monkeypatch.setattr(MIGRATION.os, "replace", denied_replace)

    with pytest.raises(PermissionError, match="non-Windows"):
        MIGRATION._atomic_write_bytes(destination, b"new")

    assert attempts == 1
    assert destination.read_bytes() == b"old"
    assert list(tmp_path.glob(".journal.json.*.tmp")) == []
