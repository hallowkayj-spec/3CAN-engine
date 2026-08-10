from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"


def load_app():
    sys.path.insert(0, str(BACKEND))
    spec = importlib.util.spec_from_file_location("app_under_test", BACKEND / "app.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["app_under_test"] = module
    spec.loader.exec_module(module)
    return module


def load_graph_engine():
    sys.path.insert(0, str(BACKEND))
    spec = importlib.util.spec_from_file_location("graph_engine_under_test", BACKEND / "graph_engine.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["graph_engine_under_test"] = module
    spec.loader.exec_module(module)
    return module


def test_route_feedback_keywords_are_bounded_and_deduped_case_insensitive():
    app = load_app()

    query_words = app._feedback_keywords_from_query(
        "3CAN route ROUTE /mnt/c/Users advisor-v3 Advisor-V3 RunningHub 9700 http api node"
    )
    added = app._append_route_feedback_keywords(
        ["Advisor-V3"],
        query_words + ["timeline", "asset-panel", "extra"],
    )

    assert app.LOW_CONF_THRESHOLD == app.DUPLICATE_NODE_THRESHOLD == 0.030
    assert "3CAN" not in query_words
    assert "route" not in query_words
    assert "mnt" not in query_words
    assert "Users" not in query_words
    assert "9700" not in query_words
    assert query_words == ["advisor-v3", "RunningHub"]
    assert added == ["RunningHub", "timeline", "asset-panel"]


def test_route_feedback_does_not_exceed_keyword_cap():
    app = load_app()

    existing = [f"kw{i}" for i in range(app.ROUTE_FEEDBACK_MAX_KEYWORDS)]

    assert app._append_route_feedback_keywords(existing, ["new-keyword"]) == []


def test_route_api_budget_preserves_must_consume_nodes():
    app = load_app()
    route_meta = {
        "core_memory_graph": {
            "must_consume_node_ids": ["DOC-a", "MEM-b"],
            "missing_must_consume_node_ids": [],
            "pack_status": "complete",
        }
    }
    packed = [
        {"id": "DOC-a", "summary": "x" * 300},
        {"id": "MEM-b", "summary": "y" * 300},
        {"id": "OPT-c", "summary": "z" * 300},
    ]

    kept, truncated = app._enforce_budget(
        packed,
        50,
        protected_ids=set(app._must_consume_node_ids(route_meta)),
    )
    kept_ids = {item["id"] for item in kept}
    post_budget_tokens = app._estimate_packed_tokens(kept)
    app._sync_core_memory_delivery(
        route_meta,
        kept_ids,
        budget_tokens=50,
        post_budget_tokens=post_budget_tokens,
    )

    assert truncated is True
    assert {"DOC-a", "MEM-b"} <= kept_ids
    assert "OPT-c" not in kept_ids
    core = route_meta["core_memory_graph"]
    assert core["missing_must_consume_node_ids"] == []
    assert core["delivered_must_consume_node_ids"] == ["DOC-a", "MEM-b"]
    assert core["pack_status"] == "complete"
    assert core["budget_policy"]["hard_gate_overrode_budget"] is False


def test_route_budget_limits_the_complete_slim_response():
    app = load_app()
    route_meta = {
        "core_memory_graph": {
            "must_consume_node_ids": ["DOC-a", "MEM-b"],
            "missing_must_consume_node_ids": [],
            "pack_status": "complete",
            "large_internal_trace": "m" * 4000,
        },
        "large_debug_payload": "d" * 6000,
    }
    payload = {
        "mode": "slim",
        "nodes": [
            {"id": "DOC-a", "summary": "x" * 1000},
            {"id": "MEM-b", "summary": "y" * 1000},
            {"id": "OPT-c", "summary": "z" * 1000},
        ],
        "scores": {"DOC-a": 0.9, "MEM-b": 0.8, "OPT-c": 0.7},
        "total_nodes": 300,
        "total_edges": 900,
        "budget_truncated": False,
        "route_meta": route_meta,
        "confidence": "high",
        "confidence_meta": {"trace": "c" * 3000},
    }

    result, truncated = app._enforce_route_response_budget(
        payload,
        180,
        node_key="nodes",
        protected_ids={"DOC-a", "MEM-b"},
    )

    assert truncated is True
    assert app._estimate_json_tokens(result) <= 180
    assert "large_debug_payload" not in json.dumps(result)
    assert {"DOC-a", "MEM-b"} <= {
        item["id"] for item in result["nodes"]
    }


def test_budget_compaction_preserves_route_contract_metadata():
    app = load_app()
    compact = app._compact_route_meta_for_budget({
        "core_memory_graph": {
            "must_consume_node_ids": ["DOC-a"],
            "selected_must_consume_node_ids": ["DOC-a"],
            "injected_node_ids": ["DOC-a"],
            "delivered_must_consume_node_ids": ["DOC-a"],
            "missing_must_consume_node_ids": [],
            "pack_status": "complete",
        },
        "temporal_route_policy": {
            "enabled": True,
            "triggered_terms": ["latest"],
            "freshness_required": True,
            "validity_focus": False,
            "error_focus": False,
            "half_life_days": 30,
            "boosted_node_count": 2,
            "penalized_node_count": 1,
            "top_boosts": [{"node_id": "DOC-a", "trace": "x" * 2000}],
        },
    })

    core = compact["core_memory_graph"]
    assert core["selected_must_consume_node_ids"] == ["DOC-a"]
    assert core["injected_node_ids"] == ["DOC-a"]
    assert compact["temporal_route_policy"] == {
        "enabled": True,
        "triggered_terms": ["latest"],
        "freshness_required": True,
        "validity_focus": False,
        "error_focus": False,
        "half_life_days": 30,
        "boosted_node_count": 2,
        "penalized_node_count": 1,
    }


def test_route_token_estimate_keeps_compatible_token_names():
    app = load_app()
    result, truncated = app._enforce_route_response_budget(
        {
            "route_response_schema": app.ROUTE_RESPONSE_SCHEMA,
            "mode": "slim",
            "nodes": [{"id": "DOC-a", "summary": "small"}],
            "scores": {"DOC-a": 1.0},
        },
        400,
        node_key="nodes",
    )

    assert truncated is False
    assert result["route_response_schema"] == "3can.route-response/v1"
    estimate = result["route_token_estimate"]
    assert estimate["response_tokens"] == estimate["post_budget_tokens"]
    assert estimate["response_tokens"] <= 400


def test_route_budget_limits_full_nodes_edges_and_metadata():
    app = load_app()
    route_meta = {
        "error_route_policy": {
            "explicit_error_requested": True,
            "attached_solution_node_ids": ["FIX-a"],
            "attached_evidence_node_ids": ["EVD-a"],
            "verified_solution_bundles": [
                {
                    "case_id": "ERR-case-a",
                    "resolution_id": "FIX-a",
                    "evidence_id": "EVD-a",
                    "required_node_ids": ["ERR-case-a", "FIX-a", "EVD-a"],
                    "selection_status": "complete",
                    "missing_node_ids": [],
                }
            ],
            "private_trace": "p" * 6000,
        }
    }
    payload = {
        "activated_nodes": [
            {"id": "ERR-case-a", "content": {"description": "x" * 4000}},
            {"id": "FIX-a", "content": {"description": "y" * 4000}},
            {"id": "EVD-a", "content": {"description": "z" * 4000}},
        ],
        "relevant_edges": [
            {
                "source": "FIX-a",
                "target": "ERR-case-a",
                "description": "e" * 3000,
            },
            {
                "source": "FIX-a",
                "target": "EVD-a",
                "description": "v" * 3000,
            },
        ],
        "scores": {"ERR-case-a": 1.0, "FIX-a": 0.9, "EVD-a": 0.8},
        "total_nodes": 3,
        "total_edges": 2,
        "route_meta": route_meta,
    }
    protected_ids = set(app._verified_solution_bundle_node_ids(route_meta))

    result, truncated = app._enforce_route_response_budget(
        payload,
        260,
        node_key="activated_nodes",
        protected_ids=protected_ids,
    )

    assert truncated is True
    assert app._estimate_json_tokens(result) <= 260
    assert {item["id"] for item in result["activated_nodes"]} == {
        "ERR-case-a",
        "FIX-a",
        "EVD-a",
    }
    assert "private_trace" not in json.dumps(result)
    assert all(
        edge["source"] in {"ERR-case-a", "FIX-a", "EVD-a"}
        and edge["target"] in {"ERR-case-a", "FIX-a", "EVD-a"}
        for edge in result.get("relevant_edges", [])
    )


def test_route_budget_rejects_complete_bundle_with_missing_member():
    app = load_app()
    route_meta = {
        "error_route_policy": {
            "verified_solution_bundles": [
                {
                    "case_id": "ERR-case-a",
                    "resolution_id": "FIX-a",
                    "evidence_id": "EVD-a",
                    "required_node_ids": ["ERR-case-a", "FIX-a", "EVD-a"],
                    "selection_status": "complete",
                    "missing_node_ids": [],
                }
            ]
        }
    }
    payload = {
        "mode": "slim",
        "nodes": [{"id": "ERR-case-a"}, {"id": "FIX-a"}],
        "route_meta": route_meta,
    }

    with pytest.raises(app.HTTPException) as rejected:
        app._enforce_route_response_budget(
            payload,
            500,
            node_key="nodes",
        )

    assert rejected.value.status_code == 500
    assert rejected.value.detail == {
        "error": "verified_solution_bundle_incomplete",
        "missing_node_ids": ["EVD-a"],
        "required_node_ids": ["ERR-case-a", "EVD-a", "FIX-a"],
    }


def test_route_budget_rejects_tampered_complete_bundle_membership():
    app = load_app()
    route_meta = {
        "error_route_policy": {
            "verified_solution_bundles": [
                {
                    "case_id": "ERR-case-a",
                    "resolution_id": "FIX-a",
                    "evidence_id": "EVD-a",
                    "required_node_ids": ["ERR-case-a", "FIX-a"],
                    "selection_status": "complete",
                    "missing_node_ids": [],
                }
            ]
        }
    }
    payload = {
        "mode": "slim",
        "nodes": [{"id": "ERR-case-a"}, {"id": "FIX-a"}],
        "route_meta": route_meta,
    }

    with pytest.raises(app.HTTPException) as rejected:
        app._enforce_route_response_budget(
            payload,
            500,
            node_key="nodes",
        )

    assert rejected.value.status_code == 500
    assert rejected.value.detail == {
        "error": "verified_solution_bundle_metadata_invalid",
        "bundle_index": 0,
        "structured_node_ids": ["ERR-case-a", "FIX-a", "EVD-a"],
        "required_node_ids": ["ERR-case-a", "FIX-a"],
    }


def test_route_budget_rejects_tampered_partial_bundle_membership():
    app = load_app()
    route_meta = {
        "error_route_policy": {
            "verified_solution_bundles": [
                {
                    "case_id": "ERR-case-a",
                    "resolution_id": "FIX-a",
                    "evidence_id": "EVD-a",
                    "required_node_ids": ["ERR-case-a"],
                    "selection_status": "partial",
                    "missing_node_ids": [],
                }
            ]
        }
    }
    payload = {
        "mode": "slim",
        "nodes": [{"id": "ERR-case-a"}],
        "route_meta": route_meta,
    }

    with pytest.raises(app.HTTPException) as rejected:
        app._enforce_route_response_budget(
            payload,
            500,
            node_key="nodes",
        )

    assert rejected.value.status_code == 500
    assert rejected.value.detail["error"] == (
        "verified_solution_bundle_metadata_invalid"
    )
    assert rejected.value.detail["structured_node_ids"] == [
        "ERR-case-a",
        "FIX-a",
        "EVD-a",
    ]


def test_route_budget_preserves_partial_bundle_anchor_and_syncs_metadata():
    app = load_app()
    route_meta = {
        "error_route_policy": {
            "attached_solution_node_ids": [],
            "attached_evidence_node_ids": [],
            "verified_solution_bundles": [
                {
                    "case_id": "ERR-case-a",
                    "resolution_id": "FIX-a",
                    "evidence_id": "EVD-a",
                    "required_node_ids": ["ERR-case-a", "FIX-a", "EVD-a"],
                    "selection_status": "partial",
                    "missing_node_ids": ["FIX-a", "EVD-a"],
                }
            ],
            "private_trace": "x" * 6000,
        }
    }
    payload = {
        "mode": "slim",
        "nodes": [
            {"id": "ERR-case-a", "summary": "e" * 3000},
            {"id": "OPTIONAL-a", "summary": "o" * 3000},
        ],
        "route_meta": route_meta,
    }

    result, truncated = app._enforce_route_response_budget(
        payload,
        180,
        node_key="nodes",
    )

    assert truncated is True
    assert app._estimate_json_tokens(result) <= 180
    assert [item["id"] for item in result["nodes"]] == ["ERR-case-a"]
    bundle = result["route_meta"]["error_route_policy"][
        "verified_solution_bundles"
    ][0]
    assert bundle["selection_status"] == "partial"
    assert bundle["missing_node_ids"] == ["FIX-a", "EVD-a"]


def test_route_budget_rejects_an_impossibly_small_envelope():
    app = load_app()
    with pytest.raises(app.HTTPException) as rejected:
        app._enforce_route_response_budget(
            {"mode": "slim", "nodes": [], "scores": {}},
            1,
            node_key="nodes",
        )
    assert rejected.value.status_code == 413
    assert rejected.value.detail["error"] == "route_budget_too_small"


def test_full_route_cannot_bypass_low_confidence_gate(monkeypatch):
    app = load_app()
    low_confidence_result = sys.modules["models"].RoutingResponse(
        activated_nodes=[],
        relevant_edges=[],
        scores={},
        total_nodes=0,
        total_edges=0,
    )

    async def fake_route(_request):
        return low_confidence_result

    monkeypatch.setattr(app, "_route_in_worker", fake_route)
    request = app.RoutingRequest(task="unmatched query", mode="full")

    with pytest.raises(app.HTTPException) as rejected:
        asyncio.run(app.route_task(request))
    assert rejected.value.status_code == 428
    assert rejected.value.detail["error"] == "low_confidence_requires_confirmation"

    request.confirm_low_confidence = True
    response = asyncio.run(app.route_task(request))
    assert response["confidence"] == "low"
    assert response["low_confidence_acknowledged"] is True


@pytest.mark.parametrize(
    "node_id",
    ["CON", "con.txt", "NUL.json", "COM1", "lpt9.cache"],
)
def test_node_ids_reject_windows_device_names(node_id):
    app = load_app()
    with pytest.raises(ValueError, match="node_id_windows_device_name_forbidden"):
        app.NodeCreate(id=node_id, name="invalid", cluster="test")


def test_auto_create_node_skips_non_utf8_file_without_replacement(tmp_path, monkeypatch):
    graph_dir = tmp_path / "graph"
    nodes_dir = graph_dir / "nodes"
    nodes_dir.mkdir(parents=True)
    (graph_dir / "edges.json").write_text("[]", encoding="utf-8")
    bad_file = tmp_path / "bad.md"
    bad_file.write_bytes(b"# bad\n\xff\xfe")

    monkeypatch.setenv("THREECAN_GRAPH_DIR", str(graph_dir))
    graph_engine = load_graph_engine()
    graph_engine._embed_model = graph_engine._HashingEmbeddingModel()
    engine = graph_engine.GraphEngine()

    engine._auto_create_node_for_file(bad_file)

    assert engine.nodes == {}
    assert list(nodes_dir.glob("*.json")) == []


def _write_route_node(nodes_dir: Path, node_id: str, *, content: dict | None = None, **extra) -> None:
    payload = {
        "id": node_id,
        "name": extra.pop("name", node_id),
        "cluster": "unit",
        "type": extra.pop("type", "knowledge"),
        "status": extra.pop("status", "active"),
        "priority": extra.pop("priority", "high"),
        "content": content or {"description": f"{node_id} route test"},
        "activation_keywords": extra.pop("activation_keywords", [node_id.lower()]),
        "activation_count": extra.pop("activation_count", 2),
    }
    payload.update(extra)
    (nodes_dir / f"{node_id}.json").write_text(json.dumps(payload), encoding="utf-8")


def _make_core_memory_engine(tmp_path: Path, monkeypatch, repeated_nodes: list[dict]) -> object:
    graph_dir = tmp_path / "graph"
    nodes_dir = graph_dir / "nodes"
    nodes_dir.mkdir(parents=True)
    registry_id = "MEM-3can-core-memory-lane-registry-20260523"
    registry_content = {
        "description": "core registry",
        "extra": {
            "required_default_lanes": ["user_preferences", "environment_constraints", "error_warnings"],
            "lane_weights": {
                "user_preferences": 100,
                "environment_constraints": 95,
                "error_warnings": 100,
                "project_constitution": 90,
            },
            "memory_lanes": {
                "user_preferences": ["USR-PUBLIC-preferences"],
                "environment_constraints": ["ENV-PUBLIC-runtime"],
                "error_warnings": ["ERR-PUBLIC-policy"],
                "project_constitution": ["PRJ-PUBLIC-constitution"],
                "project_file_system": ["DOC-PUBLIC-file-system-contract"],
            },
            "required_edges": [],
        },
    }
    _write_route_node(nodes_dir, registry_id, content=registry_content, type="knowledge", priority="critical")
    _write_route_node(nodes_dir, "USR-PUBLIC-preferences", type="knowledge", priority="critical")
    _write_route_node(
        nodes_dir,
        "ENV-PUBLIC-runtime",
        content={
            "description": "public demo runtime",
            "extra": {
                "project_id": "public-demo",
                "project_namespace": "public-demo",
            },
        },
        type="config",
        priority="critical",
    )
    _write_route_node(nodes_dir, "ERR-PUBLIC-policy", type="feedback", priority="critical")
    _write_route_node(
        nodes_dir,
        "PRJ-PUBLIC-constitution",
        content={
            "description": "demo SaaS project constitution",
            "extra": {
                "project_id": "public-demo",
                "project_namespace": "public-demo",
            },
        },
        activation_keywords=["demo-product", "saas", "project"],
        type="knowledge",
        priority="critical",
    )
    _write_route_node(
        nodes_dir,
        "DOC-PUBLIC-file-system-contract",
        content={
            "description": "Project file-system contract for Desktop media artifacts archive quarantine",
            "extra": {
                "project_id": "public-demo",
                "project_namespace": "public-demo",
            },
        },
        activation_keywords=["project-file-system", "Desktop", "artifact", "archive", "quarantine", "project_fs_guard"],
        type="knowledge",
        priority="high",
    )
    edges = []
    for item in repeated_nodes:
        _write_route_node(
            nodes_dir,
            item["id"],
            content={"description": item["description"]},
            activation_keywords=item.get("activation_keywords", ["advisor-v3", "9711"]),
            type="feedback",
            priority="critical",
            activation_count=item.get("activation_count", 5),
        )
        edges.append({
            "source": registry_id,
            "target": item["id"],
            "type": "requires",
            "weight": 1.0,
            "description": "dynamic repeated failure gate",
        })
    (graph_dir / "edges.json").write_text(json.dumps(edges), encoding="utf-8")
    monkeypatch.setenv("THREECAN_GRAPH_DIR", str(graph_dir))
    graph_engine = load_graph_engine()
    graph_engine._embed_model = graph_engine._HashingEmbeddingModel()
    return graph_engine.GraphEngine()


def test_core_memory_product_route_does_not_pull_unrelated_repeated_errors(tmp_path, monkeypatch):
    engine = _make_core_memory_engine(
        tmp_path,
        monkeypatch,
        [
            {
                "id": "ERR-repeated-PUBLIC-ui-evidence-1111",
                "description": "demo UI evidence test failure",
            },
            {
                "id": "ERR-repeated-PUBLIC-done-evidence-2222",
                "description": "demo done validation failure",
            },
        ],
    )

    graph = engine._build_core_memory_route_graph(
        "current demo-product SaaS project constitution and product brief",
        ["PRJ-PUBLIC-constitution"],
        project_id="public-demo",
        project_namespace="public-demo",
    )

    repeated = [node_id for node_id in graph["must_consume_node_ids"] if node_id.startswith("ERR-repeated-")]
    assert repeated == []
    assert graph["repeated_error_policy"]["explicit_error_requested"] is False


def test_core_memory_repeated_errors_are_capped_for_error_focused_routes(tmp_path, monkeypatch):
    repeated_nodes = [
        {
            "id": "ERR-repeated-PUBLIC-project-isolation-primary",
            "description": "demo project isolation failure and proxy contamination",
            "activation_keywords": ["demo-project", "project-isolation", "contamination"],
            "activation_count": 20,
        }
    ]
    repeated_nodes.extend(
        {
            "id": f"ERR-repeated-PUBLIC-project-isolation-extra-{index}",
            "description": "demo cross-project isolation contamination failure",
            "activation_keywords": ["demo-project", "project-isolation", "contamination"],
        }
        for index in range(6)
    )
    engine = _make_core_memory_engine(tmp_path, monkeypatch, repeated_nodes)

    graph = engine._build_core_memory_route_graph(
        "demo-project cross project isolation contamination failure",
        [],
    )

    repeated = [node_id for node_id in graph["must_consume_node_ids"] if node_id.startswith("ERR-repeated-")]
    assert len(repeated) <= 3
    assert graph["repeated_error_policy"]["max_repeated_must_consume"] == 3
    assert graph["repeated_error_policy"]["over_cap"] is False


def test_core_memory_file_system_route_requires_contract_lane(tmp_path, monkeypatch):
    engine = _make_core_memory_engine(tmp_path, monkeypatch, [])

    graph = engine._build_core_memory_route_graph(
        "Desktop video export pollution project file-system guard generated artifacts archive quarantine",
        [],
        project_id="public-demo",
        project_namespace="public-demo",
    )

    assert "project_file_system" in graph["required_lanes"]
    assert graph["lane_selected_nodes"]["project_file_system"] == ["DOC-PUBLIC-file-system-contract"]
    assert "DOC-PUBLIC-file-system-contract" in graph["must_consume_node_ids"]


def test_core_memory_attach_is_bounded_when_must_consume_exceeds_route_limit(tmp_path, monkeypatch):
    engine = _make_core_memory_engine(tmp_path, monkeypatch, [])
    selected = ["PRJ-PUBLIC-constitution"]
    graph = engine._build_core_memory_route_graph(
        "3CAN Desktop file-system memory graph node edge weight guard",
        selected,
        project_id="public-demo",
        project_namespace="public-demo",
    )

    scores = {node_id: 1.0 for node_id in selected}
    rrf_scores = {node_id: 0.5 for node_id in graph["must_consume_node_ids"]}
    packed = engine._attach_core_memory_nodes(selected, scores, rrf_scores, graph, max_nodes=3)

    delivered = set(graph["must_consume_node_ids"]) & set(packed)
    missing = set(graph["must_consume_node_ids"]) - set(packed)
    assert delivered
    assert set(graph["missing_must_consume_node_ids"]) == missing
    assert graph["pack_status"] == ("partial" if missing else "complete")
    assert graph["injection_policy"]["mode"] == "bounded_must_consume"
    assert graph["injection_policy"]["hard_gate_overrode_max_nodes"] is False
    assert len(packed) <= 3


def test_core_memory_project_mismatch_falls_back_to_optional_semantic_retrieval(
    tmp_path,
    monkeypatch,
):
    engine = _make_core_memory_engine(tmp_path, monkeypatch, [])
    stale = engine.nodes["ENV-PUBLIC-runtime"]
    stale.content.extra["project_id"] = "legacy-project"
    stale.content.extra["project_namespace"] = "legacy-project"

    graph = engine._build_core_memory_route_graph(
        "current runtime and project environment",
        ["ENV-PUBLIC-runtime"],
        project_id="public-demo",
        project_namespace="public-demo",
    )

    assert "ENV-PUBLIC-runtime" not in graph["must_consume_node_ids"]
    assert graph["lane_selected_nodes"]["environment_constraints"] == []
    assert {
        "node_id": "ENV-PUBLIC-runtime",
        "lane": "environment_constraints",
        "reason": "project_mismatch",
    } in graph["optional_semantic_nodes"]


def _make_current_reality_engine(tmp_path: Path, monkeypatch):
    graph_dir = tmp_path / "current-reality-graph"
    nodes_dir = graph_dir / "nodes"
    nodes_dir.mkdir(parents=True)
    common = {
        "activation_keywords": ["runninghub", "canonical", "path", "owner"],
        "priority": "high",
        "activation_count": 2,
    }
    _write_route_node(
        nodes_dir,
        "INTF-PUBLIC-current",
        content={
            "description": "Current canonical RunningHub interface and owner path",
            "extra": {
                "project_id": "public-demo",
                "project_namespace": "public-demo",
            },
        },
        **common,
    )
    _write_route_node(
        nodes_dir,
        "SES-PUBLIC-old",
        content={"description": "Old session narrative about RunningHub path"},
        type="session",
        **common,
    )
    _write_route_node(
        nodes_dir,
        "HO-PUBLIC-old",
        content={"description": "Old handoff narrative about RunningHub owner"},
        type="session",
        **common,
    )
    _write_route_node(
        nodes_dir,
        "DEC-PUBLIC-old",
        content={
            "description": "Old canonical decision",
            "extra": {"project_id": "public-demo", "project_namespace": "public-demo"},
        },
        type="decision",
        **common,
    )
    _write_route_node(
        nodes_dir,
        "DEC-PUBLIC-new",
        content={
            "description": "Current canonical decision",
            "extra": {"project_id": "public-demo", "project_namespace": "public-demo"},
        },
        type="decision",
        **common,
    )
    _write_route_node(
        nodes_dir,
        "PRJ-OTHER-current",
        content={
            "description": "Current path for a different project",
            "extra": {"project_id": "other-project", "project_namespace": "other-project"},
        },
        **common,
    )
    (graph_dir / "edges.json").write_text(
        json.dumps(
            [
                {
                    "source": "DEC-PUBLIC-new",
                    "target": "DEC-PUBLIC-old",
                    "type": "supersedes",
                    "weight": 1.0,
                    "description": "verified replacement",
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("THREECAN_GRAPH_DIR", str(graph_dir))
    monkeypatch.setenv("THREECAN_RERANKER_MODE", "off")
    graph_engine = load_graph_engine()
    graph_engine._embed_model = graph_engine._HashingEmbeddingModel()
    return graph_engine, graph_engine.GraphEngine()


def test_current_reality_prefers_durable_facts_and_excludes_stale_or_wrong_project(
    tmp_path,
    monkeypatch,
):
    graph_engine, engine = _make_current_reality_engine(tmp_path, monkeypatch)

    response = engine.route(
        graph_engine.RoutingRequest(
            task="current canonical RunningHub path and owner",
            max_nodes=5,
            agent_id="PUBLIC-agent",
            mode="skeleton",
            project_id="public-demo",
            project_namespace="public-demo",
            workspace_id="git-family-worktree",
        )
    )
    ids = [node.id for node in response.activated_nodes]
    policy = response.route_meta["current_reality_policy"]

    assert ids[0] == "INTF-PUBLIC-current"
    assert "DEC-PUBLIC-old" not in ids
    assert "PRJ-OTHER-current" not in ids
    assert ids.index("INTF-PUBLIC-current") < min(
        ids.index(node_id)
        for node_id in ids
        if node_id.startswith(("SES-", "HO-"))
    )
    assert policy["enabled"] is True
    assert policy["excluded_superseded_count"] == 1
    assert policy["excluded_project_mismatch_count"] == 1

    history = engine._current_reality_policy(
        graph_engine.RoutingRequest(task="RunningHub history and handoff continuation"),
        "RunningHub history and handoff continuation",
        explicit_error=False,
        exact_code=False,
    )
    assert history["enabled"] is False

    history_with_evidence = engine._current_reality_policy(
        graph_engine.RoutingRequest(
            task="以前如何验证成功，证据和边界是什么？"
        ),
        "以前如何验证成功，证据和边界是什么？",
        explicit_error=False,
        exact_code=False,
    )
    assert history_with_evidence["enabled"] is True
    assert history_with_evidence["intent"] == "durable_source_evidence"

    for held_out in (
        "目前默认能力是什么？",
        "这个分支是否仍是 Draft PR？",
        "哪个系统拥有业务真相？",
    ):
        held_out_policy = engine._current_reality_policy(
            graph_engine.RoutingRequest(task=held_out),
            held_out,
            explicit_error=False,
            exact_code=False,
        )
        assert held_out_policy["enabled"] is True

    expansion_cannot_override = engine._current_reality_policy(
        graph_engine.RoutingRequest(task="current canonical owner"),
        "current canonical owner plus old handoff history",
        explicit_error=False,
        exact_code=False,
    )
    expansion_cannot_enable = engine._current_reality_policy(
        graph_engine.RoutingRequest(task="ordinary module question"),
        "current canonical owner",
        explicit_error=False,
        exact_code=False,
    )
    assert expansion_cannot_override["enabled"] is True
    assert expansion_cannot_enable["enabled"] is False


def test_project_reality_diagnostics_separate_raw_hot_and_history(
    tmp_path,
    monkeypatch,
):
    _graph_engine, engine = _make_current_reality_engine(tmp_path, monkeypatch)
    historical = engine.nodes["SES-PUBLIC-old"]
    historical.content.extra.update(
        {"knowledge_tier": "historical", "route_visibility": "explicit_error_only"}
    )

    diagnostics = engine.project_reality_diagnostics()

    assert diagnostics["status"] == "observed"
    assert diagnostics["hard_gate"] is False
    assert diagnostics["raw_node_count"] == 6
    assert diagnostics["raw_edge_count"] == 1
    assert diagnostics["historical_archive_node_count"] == 1
    assert diagnostics["hot_route_eligible_node_count"] < diagnostics["raw_node_count"]
    assert diagnostics["raw_edge_count"] == (
        diagnostics["hot_relation_count"] + diagnostics["non_hot_relation_count"]
    )
    assert diagnostics["historical_only_relation_count"] == 0
    assert diagnostics["semantic_quality"]["status"] == "validating"


def test_durable_writeback_inherits_known_project_identity(tmp_path, monkeypatch):
    graph_engine, engine = _make_current_reality_engine(tmp_path, monkeypatch)

    updated = engine.session_writeback(
        [
            {
                "node_id": "INTF-PUBLIC-current",
                "field": "current_state",
                "action": "set",
                "value": "verified current",
            }
        ],
        agent_id="PUBLIC-agent",
        execution_context={
            "project_id": "public-demo",
            "project_namespace": "public-demo",
            "workspace_id": "git-family-worktree",
        },
    )

    assert updated == ["INTF-PUBLIC-current"]
    assert engine.nodes["INTF-PUBLIC-current"].content.extra["project_id"] == "public-demo"
    assert "workorder_id" not in engine.nodes["INTF-PUBLIC-current"].content.extra


def test_durable_writeback_rejects_project_identity_takeover(tmp_path, monkeypatch):
    _graph_engine, engine = _make_current_reality_engine(tmp_path, monkeypatch)
    before = engine.nodes["INTF-PUBLIC-current"].content.current_state

    with pytest.raises(ValueError, match="writeback_project_id_mismatch"):
        engine.session_writeback(
            [
                {
                    "node_id": "INTF-PUBLIC-current",
                    "field": "current_state",
                    "action": "set",
                    "value": "wrong project takeover",
                }
            ],
            agent_id="OTHER-agent",
            execution_context={
                "project_id": "other-project",
                "project_namespace": "other-project",
                "workspace_id": "git-other-worktree",
            },
        )

    assert engine.nodes["INTF-PUBLIC-current"].content.current_state == before
    assert engine.nodes["INTF-PUBLIC-current"].content.extra["project_id"] == "public-demo"


def test_handoff_context_edge_uses_existing_informs_contract(monkeypatch):
    app = load_app()
    created_edges = []

    class FakeHandoffEngine:
        nodes = {"DOC-PUBLIC-context": object()}

        @staticmethod
        def create_node(req):
            return type(
                "CreatedNode",
                (),
                {"model_dump": lambda self: {"id": req.id}},
            )()

        @staticmethod
        def create_edge(req, *, internal_owner=None):
            created_edges.append((req, internal_owner))
            return req

    async def discard(_message):
        return None

    monkeypatch.setattr(app, "engine", FakeHandoffEngine())
    monkeypatch.setattr(app.manager, "broadcast", discard)

    asyncio.run(
        app.handoff_create(
            {
                "from_agent": "PUBLIC-agent",
                "to_agent": "PUBLIC-next",
                "context_node_ids": ["DOC-PUBLIC-context"],
                "project_id": "public-demo",
                "project_namespace": "public-demo",
                "workspace_id": "git-family-worktree",
            }
        )
    )

    assert len(created_edges) == 1
    assert created_edges[0][0].type == app.EdgeType.informs
    assert created_edges[0][1] is None


def test_handoff_rejects_reserved_context_before_writing(monkeypatch):
    app = load_app()
    created_nodes = []
    created_edges = []

    class FakeHandoffEngine:
        nodes = {"DOC-PUBLIC-context": object(), "ERR-PUBLIC-context": object()}

        @staticmethod
        def create_node(req):
            created_nodes.append(req)

        @staticmethod
        def create_edge(req, *, internal_owner=None):
            created_edges.append((req, internal_owner))

    monkeypatch.setattr(app, "engine", FakeHandoffEngine())

    with pytest.raises(app.HTTPException) as exc:
        asyncio.run(
            app.handoff_create(
                {
                    "from_agent": "PUBLIC-agent",
                    "to_agent": "PUBLIC-next",
                    "context_node_ids": [
                        "DOC-PUBLIC-context",
                        "ERR-PUBLIC-context",
                    ],
                }
            )
        )

    assert exc.value.status_code == 403
    assert exc.value.detail == {
        "error": "handoff_reserved_context_not_allowed",
        "node_ids": ["ERR-PUBLIC-context"],
    }
    assert created_nodes == []
    assert created_edges == []


def test_ticketed_error_binding_replays_complete_project_identity(monkeypatch):
    app = load_app()
    captured = {}

    def capture_digest(target_files, **identity):
        captured["target_files"] = target_files
        captured["identity"] = identity
        return "a" * 64

    def stop_after_digest(_ticket):
        raise RuntimeError("digest verified")

    monkeypatch.setattr(app, "_target_digest", capture_digest)
    monkeypatch.setattr(app, "_ticketed_error_target_path", stop_after_digest)
    ticket = {
        "target_digest": "a" * 64,
        "scope_digest": "b" * 64,
        "project_id": "public-demo",
        "project_namespace": "public-demo",
        "workspace_id": "git-family-worktree",
        "scope": {"target_files": ["C:/work/codex-error-event.json"]},
    }

    with pytest.raises(RuntimeError, match="digest verified"):
        app._ticketed_error_spool_binding(
            {"target_digest": "a" * 64, "scope_digest": "b" * 64},
            ticket,
            {},
        )

    assert captured == {
        "target_files": ["C:/work/codex-error-event.json"],
        "identity": {
            "project_id": "public-demo",
            "project_namespace": "public-demo",
            "workspace_id": "git-family-worktree",
        },
    }


def test_ticketed_error_binding_preserves_legacy_unbound_identity(monkeypatch):
    app = load_app()
    captured = {}

    def capture_digest(_target_files, **identity):
        captured.update(identity)
        return "a" * 64

    def stop_after_digest(_ticket):
        raise RuntimeError("digest verified")

    monkeypatch.setattr(app, "_target_digest", capture_digest)
    monkeypatch.setattr(app, "_ticketed_error_target_path", stop_after_digest)
    ticket = {
        "target_digest": "a" * 64,
        "scope_digest": "b" * 64,
        "project_id": "unspecified",
        "project_namespace": "unspecified",
        "workspace_id": "unspecified",
        "scope": {"target_files": ["C:/work/codex-error-event.json"]},
    }

    with pytest.raises(RuntimeError, match="digest verified"):
        app._ticketed_error_spool_binding(
            {"target_digest": "a" * 64, "scope_digest": "b" * 64},
            ticket,
            {},
        )

    assert captured == {
        "project_id": "",
        "project_namespace": "",
        "workspace_id": "",
    }


def _make_error_solution_engine(tmp_path: Path, monkeypatch):
    graph_dir = tmp_path / "error-solution-graph"
    nodes_dir = graph_dir / "nodes"
    nodes_dir.mkdir(parents=True)
    monkeypatch.setenv("THREECAN_GRAPH_DIR", str(graph_dir))
    monkeypatch.setenv("THREECAN_RERANKER_MODE", "off")
    graph_engine = load_graph_engine()
    fingerprint = graph_engine.deterministic_fingerprint(
        project_id="public-demo",
        operation="test",
        component="ticket-ledger",
        error_type="timeout",
    )
    resolved_id = f"ERR-case-{fingerprint.split(':', 1)[1][:24]}"
    resolution_id = "FIX-PUBLIC-timeout"
    evidence_id = "EVD-PUBLIC-timeout"
    _write_route_node(
        nodes_dir,
        resolved_id,
        content={
            "description": "Resolved timeout in the public demo ticket ledger",
            "current_state": "resolved",
            "extra": {
                "kind": "error_case",
                "fingerprint": fingerprint,
                "component": "ticket-ledger",
                "error_type": "timeout",
                "case_status": "resolved",
                "occurrence_count": 4,
                "solution_summary": "Use an idempotent ledger transaction.",
                "verification_evidence": [
                    {
                        "kind": "pytest",
                        "ref": "tests/test_ticket_ledger.py",
                        "verifier": "PUBLIC-verifier",
                        "verified": True,
                        "digest": "sha256:" + ("a" * 64),
                        "verification_status": "signed_attestation_verified",
                    }
                ],
                "current_resolution_id": resolution_id,
                "error_case": {
                    "schema_version": "3can.error-case/v1",
                    "case_id": resolved_id,
                    "fingerprint": fingerprint,
                    "project_id": "public-demo",
                    "operation": "test",
                    "component": "ticket-ledger",
                    "error_type": "timeout",
                    "state": "resolved",
                    "occurrence_count": 4,
                },
            },
        },
        activation_keywords=["timeout", "ticket-ledger", "public-demo", fingerprint],
        type="feedback",
        priority="critical",
    )
    _write_route_node(
        nodes_dir,
        resolution_id,
        content={
            "description": "Verified idempotent ledger transaction",
            "extra": {
                "kind": "error_resolution",
                "error_id": resolved_id,
                "evidence_id": evidence_id,
            },
        },
        activation_keywords=["ticket-ledger", "timeout", "solution"],
        type="knowledge",
        priority="high",
    )
    _write_route_node(
        nodes_dir,
        evidence_id,
        content={
            "description": "Public verification receipt",
            "extra": {
                "schema_version": "3can.resolution-evidence/v1",
                "kind": "resolution_evidence",
                "error_id": resolved_id,
                "resolution_id": resolution_id,
                "verified_at": "2026-07-30T00:00:00+00:00",
                "verified_by": "PUBLIC-verifier",
                "evidence": [
                    {
                        "kind": "pytest",
                        "reference": "tests/test_ticket_ledger.py",
                        "verified": True,
                        "digest": "sha256:" + ("a" * 64),
                        "metadata": {
                            "verifier": "PUBLIC-verifier",
                            "verification_status": (
                                "signed_attestation_verified"
                            ),
                        },
                    }
                ],
            },
        },
        activation_keywords=["verification", "ticket-ledger"],
        type="reference",
        priority="high",
    )
    for index in range(7):
        _write_route_node(
            nodes_dir,
            f"ERR-case-{index:024x}",
            content={
                "description": "Unresolved ticket ledger timeout candidate",
                "extra": {
                    "kind": "error_case",
                    "fingerprint": f"ek2:{index:064x}",
                    "project_identity": {"project_id": "public-demo"},
                    "operation_class": "test",
                    "component": f"ticket-ledger-{index}",
                    "error_type": "timeout",
                    "case_status": "open",
                    "occurrence_count": 3,
                },
            },
            activation_keywords=["timeout", "ticket-ledger", "public-demo"],
            type="feedback",
            priority="critical",
        )
    _write_route_node(
        nodes_dir,
        "DOC-PUBLIC-release-notes",
        content={"description": "Public demo release notes and roadmap"},
        activation_keywords=["public-demo", "release", "notes", "roadmap"],
        type="knowledge",
        priority="high",
    )
    _write_route_node(
        nodes_dir,
        "ERR-PUBLIC-legacy-release-noise",
        content={
            "description": "Historical release-note failure with no canonical identity",
        },
        activation_keywords=["public-demo", "release", "notes", "roadmap"],
        type="feedback",
        priority="critical",
    )
    _write_route_node(
        nodes_dir,
        "MEM-3can-core-memory-lane-registry-20260523",
        content={
            "description": "Public error-memory lane registry",
            "extra": {
                "required_default_lanes": ["error_warnings"],
                "memory_lanes": {
                    "error_warnings": [
                        resolved_id,
                        "ERR-case-000000000000000000000000",
                        "ERR-PUBLIC-legacy-release-noise",
                    ]
                },
                "required_edges": [],
            },
        },
        activation_keywords=["3can", "memory", "registry"],
        type="knowledge",
        priority="critical",
    )
    (graph_dir / "edges.json").write_text(
        json.dumps(
            [
                {
                    "source": resolution_id,
                    "target": resolved_id,
                    "type": "resolves",
                    "weight": 1.0,
                    "description": "verified resolution",
                },
                {
                    "source": resolution_id,
                    "target": evidence_id,
                    "type": "verified_by",
                    "weight": 1.0,
                    "description": "verification receipt",
                },
            ]
        ),
        encoding="utf-8",
    )
    graph_engine._embed_model = graph_engine._HashingEmbeddingModel()
    return graph_engine, graph_engine.GraphEngine(), resolved_id, resolution_id


def test_graph_route_uses_canonical_error_intent_and_keeps_ordinary_route_clean(
    tmp_path,
    monkeypatch,
):
    graph_engine, engine, _resolved_id, _resolution_id = _make_error_solution_engine(
        tmp_path,
        monkeypatch,
    )

    for query in (
        "run pytest for the new feature",
        "fix README formatting",
        "failure rate dashboard",
        "the worker failed with a traceback",
        "任务执行报错需要排错",
    ):
        assert engine._core_repeated_error_requested(query, set()) is graph_engine.is_error_intent(query)

    response = engine.route(
        graph_engine.RoutingRequest(
            task="review 3can public demo release notes",
            max_nodes=4,
            agent_id="PUBLIC-agent",
            mode="skeleton",
        )
    )
    assert len(response.activated_nodes) <= 4
    assert all(
        not node.id.startswith(("ERR-", "ERRCASE-"))
        and not engine._is_error_case_node(node.id, node)
        for node in response.activated_nodes
    )
    assert response.route_meta["error_route_policy"]["explicit_error_requested"] is False


def test_graph_route_recalls_exact_verified_solution_with_strict_caps(tmp_path, monkeypatch):
    graph_engine, engine, resolved_id, resolution_id = _make_error_solution_engine(
        tmp_path,
        monkeypatch,
    )
    evidence_id = "EVD-PUBLIC-timeout"
    query = (
        "worker failed with timeout "
        "[project_id=public-demo][operation=test]"
        "[component=ticket-ledger][error_type=timeout]"
    )
    response = engine.route(
        graph_engine.RoutingRequest(
            task=query,
            max_nodes=4,
            agent_id="PUBLIC-agent",
            mode="skeleton",
        )
    )
    ids = [node.id for node in response.activated_nodes]
    error_case_ids = [
        node.id
        for node in response.activated_nodes
        if engine._is_error_case_node(node.id, node)
    ]

    assert ids[0] == resolved_id
    assert resolution_id in ids
    assert evidence_id in ids
    assert len(ids) <= 4
    assert len(error_case_ids) <= 3
    assert len(response.relevant_edges) <= engine._ROUTE_RELEVANT_EDGE_MAX
    policy = response.route_meta["error_route_policy"]
    assert policy["verified_solution_ranking"]["reason"] == "exact_verified_solution"
    assert policy["attached_solution_node_ids"] == [resolution_id]
    assert policy["attached_evidence_node_ids"] == [evidence_id]
    assert policy["verified_solution_bundles"] == [
        {
            "case_id": resolved_id,
            "resolution_id": resolution_id,
            "evidence_id": evidence_id,
            "required_node_ids": [resolved_id, resolution_id, evidence_id],
            "selection_status": "complete",
            "missing_node_ids": [],
        }
    ]


def test_graph_route_preserves_exact_unresolved_case_ahead_of_core_context(
    tmp_path,
    monkeypatch,
):
    graph_engine, engine, _resolved_id, _resolution_id = _make_error_solution_engine(
        tmp_path,
        monkeypatch,
    )
    registry = engine.nodes["MEM-3can-core-memory-lane-registry-20260523"]
    registry.content.extra["required_default_lanes"] = ["user_preferences"]
    registry.content.extra["memory_lanes"]["user_preferences"] = [
        "DOC-PUBLIC-release-notes"
    ]
    unresolved_id = "ERR-case-000000000000000000000000"
    response = engine.route(
        graph_engine.RoutingRequest(
            task=f"inspect 3can failure {unresolved_id}",
            max_nodes=1,
            agent_id="PUBLIC-agent",
            mode="skeleton",
        )
    )
    ids = [node.id for node in response.activated_nodes]
    policy = response.route_meta["error_route_policy"]

    assert ids == [unresolved_id]
    assert policy["exact_error_case_ranking"]["reason"] == "exact_error_case"
    assert policy["exact_error_case_ranking"]["boosted_case_ids"] == [unresolved_id]
    assert policy["verified_solution_ranking"]["reason"] == "no_exact_verified_solution"
    assert policy["attached_solution_node_ids"] == []
    assert response.route_meta["core_memory_graph"]["triggered"] is True
    assert response.route_meta["core_memory_graph"]["injection_policy"][
        "protected_node_ids"
    ] == [unresolved_id]


def test_exact_error_case_id_does_not_match_a_shorter_prefix(
    tmp_path,
    monkeypatch,
):
    _graph_engine, engine, _resolved_id, _resolution_id = _make_error_solution_engine(
        tmp_path,
        monkeypatch,
    )
    long_id = "ERR-case-000000000000000000000000"
    short_id = "ERR-case-000000000000"
    short_node = engine.nodes[long_id].model_copy(deep=True)
    short_node.id = short_id
    engine.nodes[short_id] = short_node
    scores = {short_id: 0.9, long_id: 0.1}

    policy = engine._apply_exact_error_case_route_boost(
        f"inspect {long_id}",
        scores,
        explicit_error=True,
    )

    assert policy["boosted_case_ids"] == [long_id]
    assert policy["match_kinds"] == {long_id: "case_id"}
    assert scores[long_id] > scores[short_id]


def test_direct_case_id_survives_cap_against_verified_canonical_siblings(
    tmp_path,
    monkeypatch,
):
    graph_engine, engine, _resolved_id, _resolution_id = _make_error_solution_engine(
        tmp_path,
        monkeypatch,
    )
    target_id = "ERR-case-000000000000000000000000"
    verified_siblings = {
        "ERR-case-000000000000000000000001",
        "ERR-case-000000000000000000000002",
        "ERR-case-000000000000000000000003",
    }
    original_match = engine._error_case_exact_match_kind

    def match_kind(task, node_id, node):
        if node_id in verified_siblings:
            return "canonical_identity"
        return original_match(task, node_id, node)

    monkeypatch.setattr(engine, "_error_case_exact_match_kind", match_kind)
    monkeypatch.setattr(
        engine,
        "_error_case_has_verified_solution",
        lambda node: node.id in verified_siblings,
    )

    response = engine.route(
        graph_engine.RoutingRequest(
            task=f"inspect 3can failure {target_id}",
            max_nodes=1,
            agent_id="PUBLIC-agent",
            mode="skeleton",
        )
    )
    policy = response.route_meta["error_route_policy"]

    assert [node.id for node in response.activated_nodes] == [target_id]
    assert policy["exact_error_case_ranking"]["match_kinds"][target_id] == "case_id"
    assert target_id in policy["exact_error_case_ranking"]["boosted_case_ids"]


def test_solution_attachment_does_not_evict_directly_named_unresolved_case(
    tmp_path,
    monkeypatch,
):
    _graph_engine, engine, resolved_id, resolution_id = _make_error_solution_engine(
        tmp_path,
        monkeypatch,
    )
    unresolved_id = "ERR-case-000000000000000000000000"
    selected = [unresolved_id, resolved_id]
    scores = {unresolved_id: 2.0, resolved_id: 1.0}

    result, attached, attached_evidence, bundles = (
        engine._attach_verified_solution_nodes(
        selected,
        scores,
        {unresolved_id: 2.0, resolved_id: 1.0, resolution_id: 0.9},
        prioritized_case_ids=[resolved_id],
        protected_case_ids=[unresolved_id],
        max_nodes=2,
        )
    )

    assert result == selected
    assert attached == []
    assert attached_evidence == []
    assert resolution_id not in result
    assert bundles == [
        {
            "case_id": resolved_id,
            "resolution_id": resolution_id,
            "evidence_id": "EVD-PUBLIC-timeout",
            "required_node_ids": [
                resolved_id,
                resolution_id,
                "EVD-PUBLIC-timeout",
            ],
            "selection_status": "partial",
            "missing_node_ids": [resolution_id, "EVD-PUBLIC-timeout"],
        }
    ]


def test_graph_route_rejects_untyped_or_unverified_solution_evidence(
    tmp_path,
    monkeypatch,
):
    _graph_engine, engine, resolved_id, _resolution_id = _make_error_solution_engine(
        tmp_path,
        monkeypatch,
    )
    node = engine.nodes[resolved_id]
    node.content.extra["verification_evidence"] = ["pytest passed"]
    assert engine._error_case_has_verified_solution(node) is False

    node.content.extra["verification_evidence"] = [
        {
            "kind": "pytest",
            "ref": "tests/test_ticket_ledger.py",
            "verifier": "PUBLIC-verifier",
            "verified": True,
            "self_hash": "b" * 64,
            "verification_status": "claim_not_verified",
        }
    ]
    assert engine._error_case_has_verified_solution(node) is False

    node.content.extra["verification_evidence"] = [
        {
            "kind": "activity",
            "ref": "activity-log",
            "verifier": "PUBLIC-verifier",
            "verified": True,
            "self_hash": "b" * 64,
            "verification_status": "activity_self_hash_verified",
        }
    ]
    assert engine._error_case_has_verified_solution(node) is False


@pytest.mark.parametrize(
    "corruption",
    ["missing_receipts", "missing_digest", "missing_verifier"],
)
def test_verified_solution_bundle_rejects_corrupt_evidence_node(
    tmp_path,
    monkeypatch,
    corruption,
):
    _graph_engine, engine, resolved_id, resolution_id = (
        _make_error_solution_engine(tmp_path, monkeypatch)
    )
    evidence = engine.nodes["EVD-PUBLIC-timeout"]
    extra = evidence.content.extra
    if corruption == "missing_receipts":
        extra["evidence"] = []
    elif corruption == "missing_digest":
        extra["evidence"][0]["digest"] = ""
    else:
        extra["evidence"][0]["metadata"]["verifier"] = ""

    assert engine._verified_solution_bundle_for_case(
        resolved_id,
        resolution_id,
    ) is None
    assert engine._verified_solution_node_for_case(
        resolved_id,
        resolution_id,
    ) is False


def test_preference_profile_is_registry_configured_and_public_safe(tmp_path, monkeypatch):
    graph_dir = tmp_path / "profile-graph"
    nodes_dir = graph_dir / "nodes"
    nodes_dir.mkdir(parents=True)
    registry_id = "MEM-3can-core-memory-lane-registry-20260523"
    _write_route_node(
        nodes_dir,
        registry_id,
        content={
            "description": "public registry",
            "extra": {
                "preference_profile": {
                    "node_id": "USR-PUBLIC-profile",
                    "name": "Public demo profile",
                    "cluster": "Public preferences",
                }
            },
        },
        type="knowledge",
    )
    (graph_dir / "edges.json").write_text("[]", encoding="utf-8")
    monkeypatch.setenv("THREECAN_GRAPH_DIR", str(graph_dir))
    graph_engine = load_graph_engine()
    graph_engine._embed_model = graph_engine._HashingEmbeddingModel()
    engine = graph_engine.GraphEngine()

    node = engine.learn_preference("response_style", "concise", "unit test")

    assert node.id == "USR-PUBLIC-profile"
    assert node.content.extra["preferences"]["response_style"][-1]["value"] == "concise"
