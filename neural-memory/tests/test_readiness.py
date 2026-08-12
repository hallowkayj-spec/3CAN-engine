from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path


BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import readiness  # noqa: E402


TEST_SOURCE_MANIFEST_SHA256 = "1" * 64
TEST_BACKEND_ID = readiness.HASHING_BACKEND_ID


def _load_backend_app():
    module_name = "backend_app_deep_readiness_test"
    spec = importlib.util.spec_from_file_location(module_name, BACKEND / "app.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class _FakeEngine:
    def __init__(self, node_ids: list[str], edges: list[dict[str, str]]) -> None:
        self.nodes = {node_id: object() for node_id in node_ids}
        self.edges = edges
        self._embedding_cache_source_manifest = TEST_SOURCE_MANIFEST_SHA256


def _write_graph(graph: Path, node_ids: list[str], edges: list[dict[str, str]]) -> None:
    nodes = graph / "nodes"
    nodes.mkdir(parents=True)
    for node_id in node_ids:
        (nodes / f"{node_id}.json").write_text(
            json.dumps({"id": node_id}),
            encoding="utf-8",
        )
    (graph / "edges.json").write_text(json.dumps(edges), encoding="utf-8")
    (graph / "embeddings.npz").write_bytes(b"readiness-test-cache-v1")
    (graph / "embeddings.meta.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "backend_id": TEST_BACKEND_ID,
                "source_manifest": TEST_SOURCE_MANIFEST_SHA256,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _embedding_contract(graph: Path, row_count: int) -> dict:
    return {
        "requested_backend": "hashing",
        "active_backend_id": TEST_BACKEND_ID,
        "source_manifest_sha256": TEST_SOURCE_MANIFEST_SHA256,
        "cache_sha256": hashlib.sha256(
            (graph / "embeddings.npz").read_bytes()
        ).hexdigest(),
        "meta_sha256": hashlib.sha256(
            (graph / "embeddings.meta.json").read_bytes()
        ).hexdigest(),
        "row_count": row_count,
        "dimension": readiness.EMBEDDING_DIMENSION,
    }


def _embedding_status(contract: dict, *, deep: bool = True) -> dict:
    status = {
        "requested_backend": contract["requested_backend"],
        "active_backend": contract["active_backend_id"],
        "active_backend_id": contract["active_backend_id"],
        "degraded": False,
        "fallback_policy": (
            "error"
            if contract["requested_backend"] == "bge-m3"
            else "not_applicable"
        ),
        "model_revision": "algorithm-v1",
        "max_sequence_length": 768,
        "cache_backend_id": contract["active_backend_id"],
        "cache_source_manifest": contract["source_manifest_sha256"],
        "matrix_rows": contract["row_count"],
        "matrix_dimension": contract["dimension"],
        "row_count": contract["row_count"],
        "dimension": contract["dimension"],
        "cache_structurally_ready": True,
        "source_manifest_check": "verified" if deep else "not_requested",
        "source_manifest_match": True if deep else None,
        "cache_synchronized": True if deep else None,
        "deep_cache_check": "verified" if deep else "not_requested",
        "all_rows_finite": True if deep else None,
        "all_rows_nonzero": True if deep else None,
        "all_rows_unit_norm": True if deep else None,
        "cache_ids_match": True if deep else None,
        "cache_backend_match": True if deep else None,
        "meta_backend_match": True if deep else None,
        "source_manifest_sha256": (
            contract["source_manifest_sha256"] if deep else ""
        ),
        "cache_sha256": contract["cache_sha256"] if deep else "",
        "meta_sha256": contract["meta_sha256"] if deep else "",
    }
    if contract["requested_backend"] == "bge-m3":
        match = readiness._BGE_M3_ACTIVE_BACKEND_RE.fullmatch(
            contract["active_backend_id"]
        )
        assert match is not None
        status["model_revision"] = match.group(1)
        status["max_sequence_length"] = int(match.group(2))
    return status


def _identity(engine_root: Path, graph: Path) -> dict[str, str]:
    return {
        "schema": readiness.RUNTIME_IDENTITY_SCHEMA,
        "engine_root_sha256": readiness.runtime_path_sha256(engine_root),
        "graph_root_sha256": readiness.runtime_path_sha256(graph),
    }


def _write_profile(
    path: Path,
    *,
    engine_root: Path,
    graph: Path,
    baseline: dict,
    **extra,
) -> str:
    payload = {
        "schema_version": readiness.PROFILE_SCHEMA,
        "readiness_mode": readiness.READINESS_MODE_PRODUCTION,
        "profile_id": "prod-test",
        "runtime_identity": _identity(engine_root, graph),
        "baseline": baseline,
        "previous_baseline": {
            "valid_nodes": baseline["min_valid_nodes"],
            "edges": baseline["min_edges"],
        },
        "migration_lineage": [],
    }
    if "embedding_contract" not in extra:
        payload["embedding_contract"] = _embedding_contract(
            graph,
            len(list((graph / "nodes").glob("*.json"))),
        )
    payload.update(extra)
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _evaluate(
    engine: _FakeEngine,
    *,
    engine_root: Path,
    graph: Path,
    profile: Path,
    approved: set[str] | None = None,
    profile_pin: str | None = None,
    embedding_status: dict | None = None,
) -> dict:
    resolved_pin = profile_pin
    if resolved_pin is None:
        resolved_pin = (
            hashlib.sha256(profile.read_bytes()).hexdigest()
            if profile.exists()
            else "0" * 64
        )
    try:
        profile_payload = json.loads(profile.read_text(encoding="utf-8"))
        contract = profile_payload.get("embedding_contract")
    except (OSError, UnicodeError, json.JSONDecodeError):
        contract = None
    if not isinstance(contract, dict):
        contract = _embedding_contract(graph, len(engine.nodes))
    return readiness.evaluate_readiness(
        engine,
        engine_root=engine_root,
        graph_root=graph,
        runtime_identity=_identity(engine_root, graph),
        embedding_status=(
            embedding_status
            if embedding_status is not None
            else _embedding_status(contract)
        ),
        profile_path=profile,
        approved_waiver_ids=approved or set(),
        readiness_mode=readiness.READINESS_MODE_PRODUCTION,
        profile_sha256_pin=resolved_pin,
    )


def test_profile_backed_graph_is_ready(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    graph = engine_root / "graph"
    node_ids = ["DOC-sentinel", "DOC-other"]
    edges = [{
        "source": "DOC-sentinel",
        "target": "DOC-other",
        "type": "informs",
    }]
    _write_graph(graph, node_ids, edges)
    profile = graph / "readiness-profile.json"
    _write_profile(
        profile,
        engine_root=engine_root,
        graph=graph,
        baseline={
            "min_valid_nodes": 2,
            "min_edges": 1,
            "sentinel_node_ids": ["DOC-sentinel"],
            "require_embedding_cache_ready": True,
        },
    )

    result = _evaluate(
        _FakeEngine(node_ids, edges),
        engine_root=engine_root,
        graph=graph,
        profile=profile,
    )

    assert result["ready"] is True
    assert result["reasons"] == []
    assert result["metrics"]["invalid_node_files"] == 0


def test_required_embedding_evidence_fails_closed_when_not_deep_verified(
    tmp_path: Path,
) -> None:
    engine_root = tmp_path / "engine"
    graph = engine_root / "graph"
    node_ids = ["DOC-sentinel"]
    _write_graph(graph, node_ids, [])
    profile = graph / "readiness-profile.json"
    _write_profile(
        profile,
        engine_root=engine_root,
        graph=graph,
        baseline={
            "min_valid_nodes": 1,
            "min_edges": 0,
            "sentinel_node_ids": node_ids,
            "require_embedding_cache_ready": True,
        },
    )
    engine = _FakeEngine(node_ids, [])

    cases = [
        (
            {},
            {
                "embedding_cache_not_ready",
                "embedding_source_manifest_unverified",
                "embedding_source_manifest_match_missing",
                "embedding_cache_synchronization_unverified",
            },
        ),
        (
            {
                "cache_structurally_ready": True,
                "source_manifest_check": "not_requested",
                "source_manifest_match": None,
                "cache_synchronized": None,
            },
            {
                "embedding_source_manifest_unverified",
                "embedding_source_manifest_match_missing",
                "embedding_cache_synchronization_unverified",
            },
        ),
        (
            {
                "cache_structurally_ready": True,
                "source_manifest_check": "verified",
                "source_manifest_match": False,
                "cache_synchronized": False,
            },
            {
                "embedding_source_manifest_mismatch",
                "embedding_cache_not_synchronized",
            },
        ),
        (
            {
                "cache_structurally_ready": True,
                "source_manifest_check": "failed",
                "source_manifest_match": None,
                "cache_synchronized": None,
            },
            {
                "embedding_source_manifest_check_failed",
                "embedding_source_manifest_match_missing",
                "embedding_cache_synchronization_unverified",
            },
        ),
    ]
    for status, expected_codes in cases:
        result = _evaluate(
            engine,
            engine_root=engine_root,
            graph=graph,
            profile=profile,
            embedding_status=status,
        )
        assert result["production_ready"] is False
        assert expected_codes <= {item["code"] for item in result["reasons"]}


def test_production_profile_requires_well_formed_embedding_contract(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    graph = engine_root / "graph"
    node_ids = ["DOC-sentinel"]
    _write_graph(graph, node_ids, [])
    profile = graph / "readiness-profile.json"
    baseline = {
        "min_valid_nodes": 1,
        "min_edges": 0,
        "sentinel_node_ids": node_ids,
        "require_embedding_cache_ready": True,
    }

    _write_profile(
        profile,
        engine_root=engine_root,
        graph=graph,
        baseline=baseline,
        embedding_contract=None,
    )
    missing = _evaluate(
        _FakeEngine(node_ids, []),
        engine_root=engine_root,
        graph=graph,
        profile=profile,
    )
    assert {item["code"] for item in missing["reasons"]} == {
        "embedding_contract_missing"
    }

    invalid_contract = _embedding_contract(graph, 1)
    invalid_contract["cache_sha256"] = "A" * 64
    _write_profile(
        profile,
        engine_root=engine_root,
        graph=graph,
        baseline=baseline,
        embedding_contract=invalid_contract,
    )
    invalid = _evaluate(
        _FakeEngine(node_ids, []),
        engine_root=engine_root,
        graph=graph,
        profile=profile,
    )
    assert "embedding_contract_sha256_invalid" in {
        item["code"] for item in invalid["reasons"]
    }


def test_production_readiness_accepts_verified_live_snapshot_growth(
    tmp_path: Path,
) -> None:
    engine_root = tmp_path / "engine"
    graph = engine_root / "graph"
    _write_graph(graph, ["DOC-sentinel"], [])
    profile = graph / "readiness-profile.json"
    contract = _embedding_contract(graph, 1)
    _write_profile(
        profile,
        engine_root=engine_root,
        graph=graph,
        baseline={
            "min_valid_nodes": 1,
            "min_edges": 0,
            "sentinel_node_ids": ["DOC-sentinel"],
            "require_embedding_cache_ready": True,
        },
        embedding_contract=contract,
    )
    live_contract = {
        **contract,
        "source_manifest_sha256": "2" * 64,
        "cache_sha256": "3" * 64,
        "meta_sha256": "4" * 64,
        "row_count": 2,
    }
    (graph / "nodes" / "SES-new.json").write_text(
        json.dumps({"id": "SES-new"}),
        encoding="utf-8",
    )

    result = _evaluate(
        _FakeEngine(["DOC-sentinel", "SES-new"], []),
        engine_root=engine_root,
        graph=graph,
        profile=profile,
        embedding_status=_embedding_status(live_contract),
    )

    assert result["production_ready"] is True
    assert result["reasons"] == []


def test_bge_m3_contract_rejects_hashing_fallback(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    graph = engine_root / "graph"
    node_ids = ["DOC-sentinel"]
    _write_graph(graph, node_ids, [])
    revision = "a" * 40
    contract = _embedding_contract(graph, 1)
    contract.update(
        {
            "requested_backend": "bge-m3",
            "active_backend_id": (
                "sentence-transformers:BAAI/bge-m3@"
                f"{revision}:maxseq=768"
            ),
        }
    )
    profile = graph / "readiness-profile.json"
    _write_profile(
        profile,
        engine_root=engine_root,
        graph=graph,
        baseline={
            "min_valid_nodes": 1,
            "min_edges": 0,
            "sentinel_node_ids": node_ids,
            "require_embedding_cache_ready": True,
        },
        embedding_contract=contract,
    )
    fallback_status = _embedding_status(contract)
    fallback_status.update(
        {
            "active_backend": TEST_BACKEND_ID,
            "active_backend_id": TEST_BACKEND_ID,
            "cache_backend_id": TEST_BACKEND_ID,
            "degraded": True,
            "fallback_policy": "hashing",
            "model_revision": "algorithm-v1",
        }
    )

    result = _evaluate(
        _FakeEngine(node_ids, []),
        engine_root=engine_root,
        graph=graph,
        profile=profile,
        embedding_status=fallback_status,
    )

    codes = {item["code"] for item in result["reasons"]}
    assert result["production_ready"] is False
    assert "embedding_backend_fallback_forbidden" in codes
    assert {
        item.get("field")
        for item in result["reasons"]
        if item["code"] == "embedding_contract_mismatch"
    } >= {"active_backend_id"}


def test_production_profile_cannot_disable_embedding_readiness(
    tmp_path: Path,
) -> None:
    engine_root = tmp_path / "engine"
    graph = engine_root / "graph"
    node_ids = ["DOC-sentinel"]
    _write_graph(graph, node_ids, [])
    profile = graph / "readiness-profile.json"
    _write_profile(
        profile,
        engine_root=engine_root,
        graph=graph,
        baseline={
            "min_valid_nodes": 1,
            "min_edges": 0,
            "sentinel_node_ids": node_ids,
            "require_embedding_cache_ready": False,
        },
    )

    result = _evaluate(
        _FakeEngine(node_ids, []),
        engine_root=engine_root,
        graph=graph,
        profile=profile,
        embedding_status={
            "cache_structurally_ready": True,
            "source_manifest_check": "not_requested",
            "source_manifest_match": None,
            "cache_synchronized": None,
        },
    )

    assert result["production_ready"] is False
    assert [item["code"] for item in result["reasons"]] == [
        "production_embedding_cache_required"
    ]


def test_same_count_node_content_drift_is_deep_embedding_unready(
    tmp_path: Path,
) -> None:
    engine_root = tmp_path / "engine"
    graph = engine_root / "graph"
    node_ids = ["DOC-sentinel"]
    _write_graph(graph, node_ids, [])
    profile = graph / "readiness-profile.json"
    _write_profile(
        profile,
        engine_root=engine_root,
        graph=graph,
        baseline={
            "min_valid_nodes": 1,
            "min_edges": 0,
            "sentinel_node_ids": node_ids,
            "require_embedding_cache_ready": True,
        },
    )
    node_path = graph / "nodes" / "DOC-sentinel.json"
    node_path.write_text('{"id":"DOC-sentinel"}', encoding="utf-8")

    result = _evaluate(
        _FakeEngine(node_ids, []),
        engine_root=engine_root,
        graph=graph,
        profile=profile,
        embedding_status={
            "cache_structurally_ready": True,
            "source_manifest_check": "verified",
            "source_manifest_match": False,
            "cache_synchronized": False,
        },
    )

    assert result["production_ready"] is False
    assert "embedding_source_manifest_mismatch" in {
        item["code"] for item in result["reasons"]
    }


def test_deep_stats_and_ready_endpoints_request_deep_embedding(
    monkeypatch,
    tmp_path: Path,
) -> None:
    backend_app = _load_backend_app()
    deep_calls: list[bool] = []
    cache_calls: list[bool] = []

    class FakeStats:
        @staticmethod
        def model_dump():
            return {"total_nodes": 1, "total_edges": 0}

    class FakeEngine:
        nodes = {"DOC-sentinel": object()}
        edges: list = []

        @staticmethod
        def stats():
            return FakeStats()

        @staticmethod
        def embedding_status(*, deep=False):
            deep_calls.append(deep)
            return {
                "cache_structurally_ready": True,
                "source_manifest_check": "verified" if deep else "not_requested",
                "source_manifest_match": True if deep else None,
                "cache_synchronized": True if deep else None,
            }

    class FakeCache:
        @staticmethod
        def snapshot(_engine, **kwargs):
            cache_calls.append(kwargs["force_refresh"])
            return {
                "ready": False,
                "production_ready": False,
                "reasons": [{"code": "test"}],
            }

    monkeypatch.setattr(backend_app, "engine", FakeEngine())
    monkeypatch.setattr(backend_app, "GRAPH_DIR", tmp_path / "graph")
    monkeypatch.setattr(backend_app, "_READINESS_CACHE", FakeCache())

    asyncio.run(backend_app.get_stats(deep=True))
    asyncio.run(backend_app.get_readiness(deep=True))

    assert deep_calls == [True, True]
    assert cache_calls == [True, True]


def test_missing_profile_and_unloaded_node_file_fail_closed(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    graph = engine_root / "graph"
    node_ids = ["DOC-sentinel"]
    _write_graph(graph, node_ids, [])
    engine = _FakeEngine(node_ids, [])

    missing = _evaluate(
        engine,
        engine_root=engine_root,
        graph=graph,
        profile=graph / "missing.json",
    )
    assert missing["ready"] is False
    assert [item["code"] for item in missing["reasons"]] == [
        "readiness_profile_missing"
    ]

    profile = graph / "readiness-profile.json"
    _write_profile(
        profile,
        engine_root=engine_root,
        graph=graph,
        baseline={
            "min_valid_nodes": 1,
            "min_edges": 0,
            "sentinel_node_ids": ["DOC-sentinel"],
            "require_embedding_cache_ready": True,
        },
    )
    (graph / "nodes" / "BROKEN.json").write_text("{", encoding="utf-8")

    invalid = _evaluate(
        engine,
        engine_root=engine_root,
        graph=graph,
        profile=profile,
    )
    assert invalid["ready"] is False
    assert "node_files_invalid" in {
        item["code"] for item in invalid["reasons"]
    }


def test_orphan_edge_fails_closed(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    graph = engine_root / "graph"
    node_ids = ["DOC-sentinel"]
    edges = [{
        "source": "DOC-sentinel",
        "target": "DOC-missing",
        "type": "informs",
    }]
    _write_graph(graph, node_ids, edges)
    profile = graph / "readiness-profile.json"
    _write_profile(
        profile,
        engine_root=engine_root,
        graph=graph,
        baseline={
            "min_valid_nodes": 1,
            "min_edges": 1,
            "sentinel_node_ids": ["DOC-sentinel"],
            "require_embedding_cache_ready": True,
        },
    )

    result = _evaluate(
        _FakeEngine(node_ids, edges),
        engine_root=engine_root,
        graph=graph,
        profile=profile,
    )

    assert result["ready"] is False
    assert result["metrics"]["orphan_edges"] == 1
    assert "orphan_edges" in {item["code"] for item in result["reasons"]}


def test_development_mode_is_live_but_never_production_ready(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    graph = engine_root / "graph"
    _write_graph(graph, ["DOC-local"], [])

    result = readiness.evaluate_readiness(
        _FakeEngine(["DOC-local"], []),
        engine_root=engine_root,
        graph_root=graph,
        runtime_identity=_identity(engine_root, graph),
        embedding_status={"cache_structurally_ready": False},
        readiness_mode=readiness.READINESS_MODE_DEVELOPMENT,
    )

    assert result["development_ready"] is True
    assert result["production_ready"] is False
    assert result["ready"] is False
    assert [item["code"] for item in result["reasons"]] == [
        "development_mode_not_production"
    ]

    cached = readiness.ReadinessCache().snapshot(
        _FakeEngine(["DOC-local"], []),
        engine_root=engine_root,
        graph_root=graph,
        runtime_identity=_identity(engine_root, graph),
        embedding_status={"cache_structurally_ready": False},
        readiness_mode=readiness.READINESS_MODE_DEVELOPMENT,
        force_refresh=True,
    )
    assert cached["cache"]["verification_state"] == "development"
    assert cached["cache"]["deep_required"] is False


def test_production_requires_external_profile_pin_and_previous_baseline(
    tmp_path: Path,
) -> None:
    engine_root = tmp_path / "engine"
    graph = engine_root / "graph"
    node_ids = ["DOC-sentinel"]
    _write_graph(graph, node_ids, [])
    profile = graph / "readiness-profile.json"
    _write_profile(
        profile,
        engine_root=engine_root,
        graph=graph,
        baseline={
            "min_valid_nodes": 1,
            "min_edges": 0,
            "sentinel_node_ids": node_ids,
            "require_embedding_cache_ready": True,
        },
    )
    engine = _FakeEngine(node_ids, [])

    missing_pin = readiness.evaluate_readiness(
        engine,
        engine_root=engine_root,
        graph_root=graph,
        runtime_identity=_identity(engine_root, graph),
        embedding_status={"cache_structurally_ready": True},
        profile_path=profile,
        readiness_mode=readiness.READINESS_MODE_PRODUCTION,
        profile_sha256_pin="",
    )
    assert missing_pin["production_ready"] is False
    assert missing_pin["reasons"][0]["code"] == (
        "readiness_profile_sha256_pin_missing"
    )

    invalid_pin = _evaluate(
        engine,
        engine_root=engine_root,
        graph=graph,
        profile=profile,
        profile_pin="not-a-sha256",
    )
    assert invalid_pin["production_ready"] is False
    assert invalid_pin["reasons"][0]["code"] == (
        "readiness_profile_sha256_pin_invalid"
    )

    mismatch = _evaluate(
        engine,
        engine_root=engine_root,
        graph=graph,
        profile=profile,
        profile_pin="f" * 64,
    )
    assert mismatch["production_ready"] is False
    assert mismatch["reasons"][0]["code"] == (
        "readiness_profile_sha256_pin_mismatch"
    )

    _write_profile(
        profile,
        engine_root=engine_root,
        graph=graph,
        baseline={
            "min_valid_nodes": 1,
            "min_edges": 0,
            "sentinel_node_ids": node_ids,
            "require_embedding_cache_ready": True,
        },
        previous_baseline=None,
    )
    missing_previous = _evaluate(
        engine,
        engine_root=engine_root,
        graph=graph,
        profile=profile,
    )
    assert missing_previous["production_ready"] is False
    assert "previous_baseline_required" in {
        item["code"] for item in missing_previous["reasons"]
    }


def test_on_disk_node_corruption_fails_even_when_file_count_is_unchanged(
    tmp_path: Path,
) -> None:
    engine_root = tmp_path / "engine"
    graph = engine_root / "graph"
    node_ids = ["DOC-sentinel", "DOC-other"]
    _write_graph(graph, node_ids, [])
    profile = graph / "readiness-profile.json"
    _write_profile(
        profile,
        engine_root=engine_root,
        graph=graph,
        baseline={
            "min_valid_nodes": 2,
            "min_edges": 0,
            "sentinel_node_ids": ["DOC-sentinel"],
            "require_embedding_cache_ready": True,
        },
    )
    (graph / "nodes" / "DOC-sentinel.json").write_text("{", encoding="utf-8")

    result = _evaluate(
        _FakeEngine(node_ids, []),
        engine_root=engine_root,
        graph=graph,
        profile=profile,
    )

    assert result["production_ready"] is False
    assert result["metrics"]["invalid_node_files"] == 1
    assert "node_files_invalid" in {item["code"] for item in result["reasons"]}


def test_duplicate_self_and_full_edge_payload_drift_fail_closed(
    tmp_path: Path,
) -> None:
    engine_root = tmp_path / "engine"
    graph = engine_root / "graph"
    node_ids = ["DOC-a", "DOC-b"]
    duplicate = {
        "source": "DOC-a",
        "target": "DOC-b",
        "type": "informs",
        "weight": 0.5,
    }
    raw_edges = [duplicate, dict(duplicate), {
        "source": "DOC-a",
        "target": "DOC-a",
        "type": "informs",
        "weight": 1.0,
    }]
    _write_graph(graph, node_ids, raw_edges)
    profile = graph / "readiness-profile.json"
    _write_profile(
        profile,
        engine_root=engine_root,
        graph=graph,
        baseline={
            "min_valid_nodes": 2,
            "min_edges": 3,
            "sentinel_node_ids": ["DOC-a"],
            "require_embedding_cache_ready": True,
        },
    )
    loaded_edges = [dict(edge) for edge in raw_edges]
    loaded_edges[0]["weight"] = 0.75

    result = _evaluate(
        _FakeEngine(node_ids, loaded_edges),
        engine_root=engine_root,
        graph=graph,
        profile=profile,
    )

    codes = {item["code"] for item in result["reasons"]}
    assert result["production_ready"] is False
    assert result["metrics"]["duplicate_edges"] == 1
    assert result["metrics"]["self_edges"] == 1
    assert "duplicate_edges" in codes
    assert "self_edges" in codes
    assert "edge_file_set_not_fully_loaded" in codes


def test_readiness_cache_avoids_repeat_scan_and_invalidates(
    monkeypatch,
    tmp_path: Path,
) -> None:
    engine_root = tmp_path / "engine"
    graph = engine_root / "graph"
    node_ids = ["DOC-a", "DOC-b"]
    edges = [{"source": "DOC-a", "target": "DOC-b", "type": "informs"}]
    _write_graph(graph, node_ids, edges)
    profile = graph / "readiness-profile.json"
    profile_pin = _write_profile(
        profile,
        engine_root=engine_root,
        graph=graph,
        baseline={
            "min_valid_nodes": 2,
            "min_edges": 1,
            "sentinel_node_ids": ["DOC-a"],
            "require_embedding_cache_ready": True,
        },
    )
    engine = _FakeEngine(node_ids, edges)
    calls = 0
    real_evaluate = readiness.evaluate_readiness

    def counted_evaluate(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real_evaluate(*args, **kwargs)

    monkeypatch.setattr(readiness, "evaluate_readiness", counted_evaluate)
    now = [10.0]
    cache = readiness.ReadinessCache(ttl_seconds=5.0, clock=lambda: now[0])
    kwargs = {
        "engine_root": engine_root,
        "graph_root": graph,
        "runtime_identity": _identity(engine_root, graph),
        "embedding_status": {"cache_structurally_ready": True},
        "profile_path": profile,
        "approved_waiver_ids": set(),
        "readiness_mode": readiness.READINESS_MODE_PRODUCTION,
        "profile_sha256_pin": profile_pin,
    }

    first = cache.snapshot(engine, **kwargs)
    second = cache.snapshot(engine, **kwargs)
    assert first["cache"]["hit"] is False
    assert second["cache"]["hit"] is True
    assert calls == 1

    (graph / "edges.json").write_text(
        json.dumps(edges) + "\n",
        encoding="utf-8",
    )
    invalidated = cache.snapshot(engine, **kwargs)
    assert invalidated["cache"]["hit"] is False
    assert calls == 2

    forced = cache.snapshot(engine, force_refresh=True, **kwargs)
    assert forced["cache"]["forced_refresh"] is True
    assert calls == 3

    now[0] += 6.0
    expired = cache.snapshot(engine, **kwargs)
    assert expired["cache"]["hit"] is False
    assert calls == 4


def test_readiness_cache_invalidates_same_count_node_overwrite(
    tmp_path: Path,
) -> None:
    engine_root = tmp_path / "engine"
    graph = engine_root / "graph"
    node_ids = ["DOC-a"]
    _write_graph(graph, node_ids, [])
    profile = graph / "readiness-profile.json"
    profile_pin = _write_profile(
        profile,
        engine_root=engine_root,
        graph=graph,
        baseline={
            "min_valid_nodes": 1,
            "min_edges": 0,
            "sentinel_node_ids": node_ids,
            "require_embedding_cache_ready": True,
        },
    )
    engine = _FakeEngine(node_ids, [])
    cache = readiness.ReadinessCache(ttl_seconds=30.0, clock=lambda: 10.0)
    common = {
        "engine_root": engine_root,
        "graph_root": graph,
        "runtime_identity": _identity(engine_root, graph),
        "profile_path": profile,
        "approved_waiver_ids": set(),
        "readiness_mode": readiness.READINESS_MODE_PRODUCTION,
        "profile_sha256_pin": profile_pin,
    }
    contract = json.loads(profile.read_text(encoding="utf-8"))["embedding_contract"]
    deep_status = _embedding_status(contract)
    shallow_status = _embedding_status(contract, deep=False)

    initial = cache.snapshot(
        engine,
        embedding_status=deep_status,
        force_refresh=True,
        **common,
    )
    shallow_hit = cache.snapshot(
        engine,
        embedding_status=shallow_status,
        **common,
    )
    assert initial["production_ready"] is True
    assert shallow_hit["production_ready"] is True
    assert shallow_hit["cache"]["hit"] is True

    nodes_dir = graph / "nodes"
    node_path = nodes_dir / "DOC-a.json"
    directory_mtime_before = nodes_dir.stat().st_mtime_ns
    node_stat_before = node_path.stat()
    node_path.write_text(json.dumps({"id": "DOC-z"}), encoding="utf-8")
    os.utime(
        node_path,
        ns=(node_stat_before.st_atime_ns, node_stat_before.st_mtime_ns + 1_000_000_000),
    )
    assert node_path.stat().st_size == node_stat_before.st_size
    assert nodes_dir.stat().st_mtime_ns == directory_mtime_before

    invalidated = cache.snapshot(
        engine,
        embedding_status=shallow_status,
        **common,
    )
    assert invalidated["cache"]["hit"] is False
    assert invalidated["production_ready"] is False
    assert "embedding_source_manifest_unverified" in {
        item["code"] for item in invalidated["reasons"]
    }

    forced_deep = cache.snapshot(
        engine,
        embedding_status=deep_status,
        force_refresh=True,
        **common,
    )
    assert forced_deep["production_ready"] is False
    assert "node_files_invalid" in {
        item["code"] for item in forced_deep["reasons"]
    }


def test_readiness_cache_invalidates_same_count_embedding_overwrite(
    tmp_path: Path,
) -> None:
    engine_root = tmp_path / "engine"
    graph = engine_root / "graph"
    node_ids = ["DOC-a"]
    _write_graph(graph, node_ids, [])
    profile = graph / "readiness-profile.json"
    profile_pin = _write_profile(
        profile,
        engine_root=engine_root,
        graph=graph,
        baseline={
            "min_valid_nodes": 1,
            "min_edges": 0,
            "sentinel_node_ids": node_ids,
            "require_embedding_cache_ready": True,
        },
    )
    contract = json.loads(profile.read_text(encoding="utf-8"))["embedding_contract"]
    deep_status = _embedding_status(contract)
    shallow_status = _embedding_status(contract, deep=False)
    engine = _FakeEngine(node_ids, [])
    cache = readiness.ReadinessCache(ttl_seconds=30.0, clock=lambda: 10.0)
    common = {
        "engine_root": engine_root,
        "graph_root": graph,
        "runtime_identity": _identity(engine_root, graph),
        "profile_path": profile,
        "approved_waiver_ids": set(),
        "readiness_mode": readiness.READINESS_MODE_PRODUCTION,
        "profile_sha256_pin": profile_pin,
    }

    deep = cache.snapshot(
        engine,
        embedding_status=deep_status,
        force_refresh=True,
        **common,
    )
    shallow_hit = cache.snapshot(
        engine,
        embedding_status=shallow_status,
        **common,
    )
    assert deep["production_ready"] is True
    assert shallow_hit["cache"]["hit"] is True

    embeddings = graph / "embeddings.npz"
    before = embeddings.stat()
    tampered = bytearray(embeddings.read_bytes())
    tampered[-1] ^= 1
    embeddings.write_bytes(tampered)
    os.utime(
        embeddings,
        ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000),
    )
    after = embeddings.stat()
    assert after.st_size == before.st_size
    assert after.st_mtime_ns != before.st_mtime_ns

    invalidated = cache.snapshot(
        engine,
        embedding_status=shallow_status,
        **common,
    )
    assert invalidated["cache"]["hit"] is False
    assert invalidated["production_ready"] is False
    assert "embedding_deep_cache_unverified" in {
        item["code"] for item in invalidated["reasons"]
    }


def test_rebuild_marker_blocks_external_profile_and_invalidates_cache(
    tmp_path: Path,
) -> None:
    engine_root = tmp_path / "engine"
    graph = engine_root / "graph"
    node_ids = ["DOC-a"]
    _write_graph(graph, node_ids, [])
    external_profile = tmp_path / "previous-approved-readiness-profile.json"
    profile_pin = _write_profile(
        external_profile,
        engine_root=engine_root,
        graph=graph,
        baseline={
            "min_valid_nodes": 1,
            "min_edges": 0,
            "sentinel_node_ids": node_ids,
            "require_embedding_cache_ready": True,
        },
    )
    engine = _FakeEngine(node_ids, [])
    cache = readiness.ReadinessCache(ttl_seconds=30.0, clock=lambda: 10.0)
    common = {
        "engine_root": engine_root,
        "graph_root": graph,
        "runtime_identity": _identity(engine_root, graph),
        "profile_path": external_profile,
        "approved_waiver_ids": set(),
        "readiness_mode": readiness.READINESS_MODE_PRODUCTION,
        "profile_sha256_pin": profile_pin,
    }
    contract = json.loads(
        external_profile.read_text(encoding="utf-8")
    )["embedding_contract"]
    deep_status = _embedding_status(contract)
    shallow_status = _embedding_status(contract, deep=False)

    initial = cache.snapshot(
        engine,
        embedding_status=deep_status,
        force_refresh=True,
        **common,
    )
    assert initial["production_ready"] is True

    marker = graph / readiness.READINESS_PROFILE_REBUILD_MARKER_NAME
    marker.write_text('{"required":true}', encoding="utf-8")
    blocked = cache.snapshot(
        engine,
        embedding_status=shallow_status,
        **common,
    )
    assert blocked["cache"]["hit"] is False
    assert blocked["production_ready"] is False
    assert [item["code"] for item in blocked["reasons"]] == [
        "readiness_profile_rebuild_required"
    ]


def test_shallow_stats_reuse_unchanged_deep_verified_readiness(
    tmp_path: Path,
) -> None:
    engine_root = tmp_path / "engine"
    graph = engine_root / "graph"
    node_ids = ["DOC-a"]
    _write_graph(graph, node_ids, [])
    profile = graph / "readiness-profile.json"
    profile_pin = _write_profile(
        profile,
        engine_root=engine_root,
        graph=graph,
        baseline={
            "min_valid_nodes": 1,
            "min_edges": 0,
            "sentinel_node_ids": node_ids,
            "require_embedding_cache_ready": True,
        },
    )
    now = [10.0]
    cache = readiness.ReadinessCache(ttl_seconds=5.0, clock=lambda: now[0])
    engine = _FakeEngine(node_ids, [])
    common = {
        "engine_root": engine_root,
        "graph_root": graph,
        "runtime_identity": _identity(engine_root, graph),
        "profile_path": profile,
        "approved_waiver_ids": set(),
        "readiness_mode": readiness.READINESS_MODE_PRODUCTION,
        "profile_sha256_pin": profile_pin,
    }
    contract = json.loads(profile.read_text(encoding="utf-8"))["embedding_contract"]
    deep_status = _embedding_status(contract)
    shallow_status = _embedding_status(contract, deep=False)

    deep = cache.snapshot(
        engine,
        embedding_status=deep_status,
        force_refresh=True,
        **common,
    )
    shallow_hit = cache.snapshot(
        engine,
        embedding_status=shallow_status,
        **common,
    )
    assert deep["production_ready"] is True
    assert shallow_hit["production_ready"] is True
    assert shallow_hit["cache"]["hit"] is True
    assert shallow_hit["cache"]["reused_verified_deep_evidence"] is True
    assert shallow_hit["cache"]["verification_state"] == "cached_verified"
    assert shallow_hit["embedding_evidence"]["source_manifest_check"] == "verified"

    now[0] += 6.0
    expired_shallow = cache.snapshot(
        engine,
        embedding_status=shallow_status,
        **common,
    )
    assert expired_shallow["production_ready"] is True
    assert expired_shallow["cache"]["hit"] is False
    assert expired_shallow["cache"]["reused_verified_deep_evidence"] is True
    assert expired_shallow["cache"]["verification_state"] == "stale_verified"
    assert expired_shallow["cache"]["deep_required"] is False
    assert expired_shallow["cache"]["evidence_age_seconds"] == 6.0


def test_regressed_baseline_requires_matching_explicit_waiver(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    graph = engine_root / "graph"
    node_ids = ["DOC-sentinel", "DOC-current"]
    edges = [{
        "source": "DOC-sentinel",
        "target": "DOC-current",
        "type": "informs",
    }]
    _write_graph(graph, node_ids, edges)
    manifest_path = graph / "maintenance" / "migrations" / "legacy-errors.json"
    manifest_path.parent.mkdir(parents=True)
    manifest = {
        "run_id": "legacy-errors-test",
        "applied": True,
        "journal_phase": "completed",
        "before": {"node_count": 4, "edge_count": 3},
        "after": {"node_count": 2, "edge_count": 1},
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    manifest_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    profile = graph / "readiness-profile.json"
    _write_profile(
        profile,
        engine_root=engine_root,
        graph=graph,
        baseline={
            "min_valid_nodes": 2,
            "min_edges": 1,
            "sentinel_node_ids": ["DOC-sentinel"],
            "require_embedding_cache_ready": True,
        },
        previous_baseline={"valid_nodes": 4, "edges": 3},
        migration_lineage=[{
            "run_id": "legacy-errors-test",
            "manifest_path": "maintenance/migrations/legacy-errors.json",
            "manifest_sha256": manifest_hash,
        }],
        migration_waiver={
            "schema_version": readiness.WAIVER_SCHEMA,
            "waiver_id": "MW-approved-test",
            "run_id": "legacy-errors-test",
            "manifest_sha256": manifest_hash,
            "approved_by": "test-operator",
            "approved_at": "2026-07-31T00:00:00+00:00",
            "reason": "Reviewed intentional canonicalization.",
        },
    )
    engine = _FakeEngine(node_ids, edges)

    denied = _evaluate(
        engine,
        engine_root=engine_root,
        graph=graph,
        profile=profile,
    )
    assert denied["ready"] is False
    assert "migration_waiver_not_approved" in {
        item["code"] for item in denied["reasons"]
    }

    approved = _evaluate(
        engine,
        engine_root=engine_root,
        graph=graph,
        profile=profile,
        approved={"MW-approved-test"},
    )
    assert approved["ready"] is True
    assert approved["verified_migrations"] == [{
        "run_id": "legacy-errors-test",
        "manifest_sha256": manifest_hash,
    }]


def test_stats_source_never_assigns_unconditional_healthy() -> None:
    source = (BACKEND / "app.py").read_text(encoding="utf-8")
    assert 'stats["healthy"] = True' not in source
    assert 'stats["healthy"] = readiness["production_ready"]' in source
