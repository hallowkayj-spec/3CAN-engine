from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"


def _load_graph_engine(monkeypatch, graph_dir: Path):
    monkeypatch.setenv("THREECAN_GRAPH_DIR", str(graph_dir))
    monkeypatch.setenv("THREECAN_EMBEDDING_BACKEND", "hashing")
    monkeypatch.setenv("THREECAN_RERANKER_MODE", "off")
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    module_name = f"graph_engine_async_{graph_dir.name}"
    spec = importlib.util.spec_from_file_location(
        module_name,
        BACKEND / "graph_engine.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    module._embed_model = module._HashingEmbeddingModel()
    return module


@pytest.fixture
def graph_runtime(tmp_path, monkeypatch):
    graph_dir = tmp_path / "graph"
    (graph_dir / "nodes").mkdir(parents=True)
    (graph_dir / "edges.json").write_text("[]", encoding="utf-8")
    module = _load_graph_engine(monkeypatch, graph_dir)
    engine = module.GraphEngine()
    try:
        for index in range(20):
            engine.create_node(
                module.NodeCreate(
                    id=f"DOC-async-{index:02d}",
                    name=f"Async client {index}",
                    cluster="async-concurrency",
                    content=module.NodeContent(
                        description=f"independent semantic region {index}",
                        current_state="baseline",
                    ),
                    activation_keywords=[f"client-{index}", "async"],
                )
            )
        yield module, engine, graph_dir
    finally:
        engine.close()


def _route(module, engine, index: int):
    return engine.route(
        module.RoutingRequest(
            task=f"independent semantic region {index}",
            max_nodes=4,
            agent_id=f"codex-async-{index}",
            session_instance_id=f"session-async-{index}",
            workorder_id=f"workorder-async-{index}",
            mode="skeleton",
            confirm_low_confidence=True,
        )
    )


def _assert_embedding_integrity(engine) -> None:
    assert engine._node_id_order == sorted(engine.nodes)
    assert set(engine._node_embeddings) == set(engine.nodes)
    assert engine._emb_matrix.shape == (len(engine.nodes), 1024)
    for index, node_id in enumerate(engine._node_id_order):
        assert engine._node_embeddings[node_id] is not None
        assert (engine._emb_matrix[index] == engine._node_embeddings[node_id]).all()


def test_four_routes_are_serialized_without_activity_chain_forks(graph_runtime):
    module, engine, _graph_dir = graph_runtime
    start = threading.Barrier(4)

    def run(index: int):
        start.wait()
        return _route(module, engine, index)

    before = len(engine.get_activity(action="route", limit=500))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(run, range(4)))

    assert all(result.activated_nodes for result in results)
    routes = engine.get_activity(action="route", limit=500)
    assert len(routes) - before == 4
    assert engine.verify_activity_chain()["valid"] is True
    assert len({entry.self_hash for entry in engine.get_activity(limit=500)}) == len(
        engine.get_activity(limit=500)
    )


def test_caller_inputs_cannot_mutate_owned_graph_state(graph_runtime):
    module, engine, _graph_dir = graph_runtime
    content = module.NodeContent(
        notes="owned",
        extra={"nested": {"values": ["original"]}},
    )
    keywords = ["owned-keyword"]
    created = engine.create_node(
        module.NodeCreate(
            id="DOC-async-owned-input",
            name="Owned input boundary",
            cluster="async-concurrency",
            content=content,
            activation_keywords=keywords,
        )
    )
    meta = {"nested": {"values": ["original"]}}
    affected = [created.id]
    activity = engine.log_activity(
        "codex-owned-input",
        "test",
        affected_nodes=affected,
        meta=meta,
    )
    capabilities = ["code"]
    agent_meta = {"nested": {"values": ["original"]}}
    engine.agent_checkin(
        "codex-owned-input",
        capabilities=capabilities,
        meta=agent_meta,
    )

    content.notes = "caller-mutated"
    content.extra["nested"]["values"].append("caller-mutated")
    keywords.append("caller-mutated")
    meta["nested"]["values"].append("caller-mutated")
    affected.append("DOC-caller-mutated")
    capabilities.append("admin")
    agent_meta["nested"]["values"].append("caller-mutated")

    stored = engine.get_node(created.id)
    assert stored.content.notes == "owned"
    assert stored.content.extra == {"nested": {"values": ["original"]}}
    assert stored.activation_keywords == ["owned-keyword"]
    stored_activity = next(
        entry
        for entry in engine.get_activity(limit=500)
        if entry.self_hash == activity.self_hash
    )
    assert stored_activity.affected_nodes == [created.id]
    assert stored_activity.meta == {"nested": {"values": ["original"]}}
    assert engine.verify_activity_chain()["valid"] is True
    stored_agent = next(
        agent
        for agent in engine.list_agents()
        if agent.agent_id == "codex-owned-input"
    )
    assert stored_agent.capabilities == ["code"]
    assert stored_agent.meta == {"nested": {"values": ["original"]}}
    _assert_embedding_integrity(engine)


@pytest.mark.parametrize("client_count", [8, 16])
def test_bounded_independent_client_load_keeps_graph_coherent(
    graph_runtime,
    client_count,
):
    module, engine, graph_dir = graph_runtime
    start = threading.Barrier(client_count)

    def run(index: int):
        start.wait()
        node_id = f"DOC-async-{index:02d}"
        updated = engine.session_writeback(
            [
                {
                    "node_id": node_id,
                    "field": "current_state",
                    "action": "set",
                    "value": f"committed-by-{index}",
                }
            ],
            agent_id=f"codex-load-{index}",
            execution_context={
                "project_id": "async-public",
                "project_namespace": "async-public",
                "workspace_id": f"workspace-{index}",
                "workorder_id": f"workorder-{index}",
            },
        )
        route = _route(module, engine, index)
        return node_id, updated, route

    with ThreadPoolExecutor(max_workers=client_count) as pool:
        results = list(pool.map(run, range(client_count)))

    for index, (node_id, updated, route) in enumerate(results):
        assert updated == [node_id]
        assert route.activated_nodes
        node = engine.get_node(node_id)
        assert node and node.content.current_state == f"committed-by-{index}"
        stored = json.loads(
            (graph_dir / "nodes" / f"{node_id}.json").read_text(encoding="utf-8")
        )
        assert stored["content"]["current_state"] == f"committed-by-{index}"
    _assert_embedding_integrity(engine)
    assert engine.verify_activity_chain()["valid"] is True


def test_route_waits_for_writeback_commit_boundary(graph_runtime, monkeypatch):
    module, engine, _graph_dir = graph_runtime
    node_id = "DOC-async-00"
    saved = threading.Event()
    release = threading.Event()
    route_started = threading.Event()
    original = engine._save_node

    def paused_save(node):
        original(node)
        if node.id == node_id and node.content.current_state == "committed-state":
            saved.set()
            assert release.wait(timeout=5)

    monkeypatch.setattr(engine, "_save_node", paused_save)
    with ThreadPoolExecutor(max_workers=2) as pool:
        mutation = pool.submit(
            engine.session_writeback,
            [
                {
                    "node_id": node_id,
                    "field": "current_state",
                    "action": "set",
                    "value": "committed-state",
                }
            ],
            "codex-writer",
            {
                "project_id": "async-public",
                "project_namespace": "async-public",
                "workspace_id": "workspace-writer",
            },
        )
        assert saved.wait(timeout=5)
        def start_route():
            route_started.set()
            return _route(module, engine, 0)

        route = pool.submit(start_route)
        assert route_started.wait(timeout=5)
        assert route.done() is False
        release.set()
        assert mutation.result(timeout=10) == [node_id]
        routed = route.result(timeout=10)

    assert engine.get_node(node_id).content.current_state == "committed-state"
    routed_node = next(node for node in routed.activated_nodes if node.id == node_id)
    assert routed_node.content.current_state == "committed-state"
    _assert_embedding_integrity(engine)


def test_watcher_is_serialized_and_cannot_modify_protected_authority(
    graph_runtime,
    tmp_path,
    monkeypatch,
):
    module, engine, graph_dir = graph_runtime
    protected_nodes = []
    protected_files = []
    for family in ("PRJ", "INTF", "PROC", "DEC"):
        filename = f"{family}-async-protected.md"
        protected_nodes.append(
            engine.create_node(
                module.NodeCreate(
                    id=f"{family}-async-protected",
                    name=f"Protected {family} reality",
                    cluster="async-concurrency",
                    content=module.NodeContent(
                        notes="owner-authored",
                        key_files=[filename],
                        extra={
                            "project_id": "async-public",
                            "project_namespace": "async-public",
                        },
                    ),
                ),
                internal_owner="durable-seed",
            )
        )
        path = tmp_path / filename
        path.write_text("# protected\nowner-authored", encoding="utf-8")
        protected_files.append(path)
    ordinary = engine.get_node("DOC-async-01")
    ordinary.content.key_files = ["DOC-async-01.md"]
    engine.update_node(
        ordinary.id,
        module.NodeUpdate(content=ordinary.content),
    )
    ordinary_file = tmp_path / "DOC-async-01.md"
    ordinary_file.write_text("# ordinary\nbaseline", encoding="utf-8")
    protected_before = {
        node.id: engine.get_node(node.id) for node in protected_nodes
    }
    protected_disk_before = {
        node.id: (graph_dir / "nodes" / f"{node.id}.json").read_bytes()
        for node in protected_nodes
    }
    watcher_saved = threading.Event()
    release_watcher = threading.Event()
    original_save = engine._save_node

    def paused_watcher_save(node):
        original_save(node)
        if (
            node.id == ordinary.id
            and "watcher committed" in node.content.notes
            and threading.current_thread().name == "3can-sync"
        ):
            watcher_saved.set()
            assert release_watcher.wait(timeout=5)

    monkeypatch.setattr(engine, "_save_node", paused_watcher_save)
    engine.start_sync_watcher([tmp_path], interval=0.01)

    try:
        ordinary_file.write_text("# ordinary\nwatcher committed", encoding="utf-8")
        for path in protected_files:
            path.write_text("# protected\nwatcher must skip", encoding="utf-8")
        assert watcher_saved.wait(timeout=5)
        route_started = threading.Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            def start_route():
                route_started.set()
                return _route(module, engine, 1)

            route = pool.submit(start_route)
            assert route_started.wait(timeout=5)
            assert route.done() is False
            release_watcher.set()
            assert route.result(timeout=10).activated_nodes
    finally:
        release_watcher.set()
        engine.stop_sync_watcher()

    assert "watcher committed" in engine.get_node(ordinary.id).content.notes
    for node in protected_nodes:
        assert engine.get_node(node.id) == protected_before[node.id]
        assert (
            graph_dir / "nodes" / f"{node.id}.json"
        ).read_bytes() == protected_disk_before[node.id]
    _assert_embedding_integrity(engine)


def test_same_protected_node_collision_is_localized_by_version(graph_runtime):
    module, engine, _graph_dir = graph_runtime
    protected = engine.create_node(
        module.NodeCreate(
            id="DEC-async-collision",
            name="Protected collision control",
            cluster="async-concurrency",
            content=module.NodeContent(
                current_state="baseline",
                extra={
                    "project_id": "async-public",
                    "project_namespace": "async-public",
                },
            ),
        ),
        internal_owner="durable-seed",
    )
    start = threading.Barrier(2)
    provenance = module.DurableProvenance(
        source_provenance="user_authoritative",
        authorized_by="user",
    )

    def write(value: str):
        start.wait()
        return engine.session_writeback(
            [
                {
                    "node_id": protected.id,
                    "field": "current_state",
                    "action": "set",
                    "value": value,
                    "expected_updated_at": protected.updated_at,
                }
            ],
            agent_id=f"codex-{value}",
            execution_context={
                "project_id": "async-public",
                "project_namespace": "async-public",
                "workspace_id": f"workspace-{value}",
            },
            provenance=provenance,
        )

    outcomes = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(write, value) for value in ("first", "second")]
        for future in futures:
            try:
                outcomes.append(("ok", future.result(timeout=10)))
            except ValueError as exc:
                outcomes.append(("conflict", str(exc)))

    assert [kind for kind, _ in outcomes].count("ok") == 1
    assert [kind for kind, _ in outcomes].count("conflict") == 1
    assert "writeback_node_version_conflict:DEC-async-collision" in str(outcomes)
    assert engine.get_node(protected.id).content.current_state in {"first", "second"}
    assert engine.get_node("DOC-async-02").content.current_state == "baseline"
    _assert_embedding_integrity(engine)


def test_engine_close_joins_an_already_stopping_real_watcher(
    graph_runtime,
    monkeypatch,
):
    _module, engine, graph_dir = graph_runtime
    detect_entered = threading.Event()
    release_detect = threading.Event()

    def blocked_detect(_watch_dirs):
        detect_entered.set()
        assert release_detect.wait(timeout=5)
        return []

    monkeypatch.setattr(engine, "_detect_changes", blocked_detect)
    engine.start_sync_watcher([graph_dir], interval=60)
    assert detect_entered.wait(timeout=5)

    with ThreadPoolExecutor(max_workers=2) as pool:
        stopping = pool.submit(engine.stop_sync_watcher)
        deadline = time.monotonic() + 5
        while not engine._sync_stopping and time.monotonic() < deadline:
            time.sleep(0.005)
        assert engine._sync_stopping is True
        closing = pool.submit(engine.close)
        deadline = time.monotonic() + 5
        while not engine._closing and time.monotonic() < deadline:
            time.sleep(0.005)
        assert engine._closing is True
        assert closing.done() is False
        release_detect.set()
        stopping.result(timeout=10)
        closing.result(timeout=10)

    assert engine._sync_thread is None
    assert engine._graph_runtime_lease is None
    with pytest.raises(RuntimeError, match="graph_engine_closed"):
        engine.get_node("DOC-async-00")


def test_watcher_cannot_restart_while_previous_thread_is_stopping(
    graph_runtime,
    monkeypatch,
):
    _module, engine, graph_dir = graph_runtime
    detect_entered = threading.Event()
    release_detect = threading.Event()

    def blocked_detect(_watch_dirs):
        detect_entered.set()
        assert release_detect.wait(timeout=5)
        return []

    monkeypatch.setattr(engine, "_detect_changes", blocked_detect)
    engine.start_sync_watcher([graph_dir], interval=60)
    assert detect_entered.wait(timeout=5)

    with ThreadPoolExecutor(max_workers=1) as pool:
        stopping = pool.submit(engine.stop_sync_watcher)
        deadline = time.monotonic() + 5
        while not engine._sync_stopping and time.monotonic() < deadline:
            time.sleep(0.005)
        assert engine._sync_stopping is True
        with pytest.raises(RuntimeError, match="sync_watcher_stopping"):
            engine.start_sync_watcher([graph_dir], interval=60)
        release_detect.set()
        stopping.result(timeout=10)

    assert engine._sync_thread is None
    assert engine._sync_running is False
    assert engine._sync_stopping is False

    monkeypatch.setattr(engine, "_detect_changes", lambda _watch_dirs: [])
    engine.start_sync_watcher([graph_dir], interval=60)
    engine.stop_sync_watcher()
    assert engine._sync_thread is None


def test_fastapi_worker_boundary_keeps_event_loop_responsive(
    graph_runtime,
    monkeypatch,
    tmp_path,
):
    _module, engine, _graph_dir = graph_runtime
    app_graph = tmp_path / "app-graph"
    (app_graph / "nodes").mkdir(parents=True)
    (app_graph / "edges.json").write_text("[]", encoding="utf-8")
    monkeypatch.setenv("THREECAN_GRAPH_DIR", str(app_graph))
    module_name = f"backend_app_async_{tmp_path.name}"
    spec = importlib.util.spec_from_file_location(module_name, BACKEND / "app.py")
    assert spec and spec.loader
    backend_app = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = backend_app
    spec.loader.exec_module(backend_app)
    backend_app.engine = engine

    async def scenario():
        entered = threading.Event()
        release = threading.Event()
        ticks = 0

        def blocked_read():
            entered.set()
            assert release.wait(timeout=5)

        holder = threading.Thread(
            target=lambda: engine.run_consistent(blocked_read),
        )
        holder.start()
        assert entered.wait(timeout=5)

        async def ticker():
            nonlocal ticks
            for _ in range(5):
                await asyncio.sleep(0.01)
                ticks += 1

        read = asyncio.create_task(
            backend_app._engine_in_worker(engine.get_node, "DOC-async-00")
        )
        await ticker()
        assert read.done() is False
        release.set()
        assert await read is not None
        holder.join(timeout=5)
        return ticks

    assert asyncio.run(scenario()) == 5
