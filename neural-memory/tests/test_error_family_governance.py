from __future__ import annotations

import importlib.util
import json
import socket
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import error_family as FAMILY  # noqa: E402
import graph_engine as ENGINE  # noqa: E402
from models import Node  # noqa: E402


TOOL_PATH = ROOT / "maintenance" / "govern_error_families.py"
SPEC = importlib.util.spec_from_file_location("govern_error_families", TOOL_PATH)
assert SPEC and SPEC.loader
GOVERN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GOVERN)


def _node(
    node_id: str,
    *,
    project_id: str = "",
    operation: str = "",
    component: str = "",
    error_type: str = "",
) -> dict:
    extra = {}
    if project_id:
        extra = {
            "error_knowledge_schema_version": "3can.error-knowledge/v2",
            "error_case": {
                "schema_version": "3can.error-case/v1",
                "case_id": node_id,
                "project_id": project_id,
                "operation": operation,
                "component": component,
                "error_type": error_type,
            },
        }
    return {
        "id": node_id,
        "name": node_id,
        "cluster": "ErrorKnowledge",
        "layer": "L0",
        "type": "feedback",
        "status": "active",
        "content": {
            "description": "fixture error",
            "current_state": "observed",
            "extra": extra,
        },
        "activation_keywords": [component] if component else [],
        "priority": "high",
    }


def _graph(tmp_path: Path) -> Path:
    graph = tmp_path / "graph"
    nodes = graph / "nodes"
    nodes.mkdir(parents=True)
    fixtures = {
        "ERR-case-a": _node(
            "ERR-case-a",
            project_id="project-a",
            operation="read",
            component="adapter",
            error_type="timeout",
        ),
        "ERR-case-b": _node(
            "ERR-case-b",
            project_id="project-a",
            operation="write",
            component="adapter",
            error_type="timeout",
        ),
        "ERR-legacy-incomplete": _node("ERR-legacy-incomplete"),
        "DOC-unrelated": {
            **_node("DOC-unrelated"),
            "cluster": "docs",
            "type": "knowledge",
        },
    }
    for node_id, payload in fixtures.items():
        (nodes / f"{node_id}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    (graph / "edges.json").write_text("[]\n", encoding="utf-8")
    (graph / "embeddings.npz").write_bytes(b"fixture-cache")
    return graph


def _decisions(candidates: dict) -> dict:
    return {
        "schema_version": FAMILY.DECISION_SCHEMA,
        "candidate_manifest_sha256": candidates["manifest_sha256"],
        "decided_by": "fixture-reviewer",
        "decided_at": "2026-08-23T00:00:00+00:00",
        "decisions": [
            {
                "case_id": item["case_id"],
                "decision": "accept",
                "reason_code": "reviewed_exact_identity",
                "additional_aliases": [f"reviewed alias {item['case_id']}"],
            }
            for item in candidates["candidates"]
        ],
    }


def _silent_endpoint() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
        candidate.bind(("127.0.0.1", 0))
        _host, port = candidate.getsockname()
    return f"http://127.0.0.1:{port}"


def _offline_defaults(monkeypatch) -> str:
    endpoint = _silent_endpoint()
    monkeypatch.setitem(
        GOVERN._require_engine_quiescence.__globals__,
        "DEFAULT_ENGINE_ENDPOINTS",
        (endpoint,),
    )
    return endpoint


def _mutable_graph_bytes(graph: Path) -> dict[str, bytes]:
    return {
        path.relative_to(graph).as_posix(): path.read_bytes()
        for path in sorted((graph / "nodes").glob("*.json"))
    } | {"edges.json": (graph / "edges.json").read_bytes()}


def test_candidates_group_only_complete_deterministic_identities(tmp_path: Path) -> None:
    graph = _graph(tmp_path)
    manifest = GOVERN.build_candidate_manifest(graph)

    assert manifest["candidate_count"] == 2
    assert manifest["deferred_count"] == 1
    assert manifest["family_count"] == 1
    assert {
        item["family_id"] for item in manifest["candidates"]
    } == {manifest["candidates"][0]["family_id"]}
    assert manifest["deferred"][0]["case_id"] == "ERR-legacy-incomplete"
    assert manifest["policy"]["automatic_semantic_merge"] is False


def test_compile_requires_an_explicit_decision_for_every_candidate(
    tmp_path: Path,
) -> None:
    graph = _graph(tmp_path)
    candidates = GOVERN.build_candidate_manifest(graph)
    decisions = _decisions(candidates)
    decisions["decisions"].pop()

    with pytest.raises(GOVERN.MigrationError, match="every deterministic"):
        GOVERN.compile_active_manifest(graph, candidates, decisions)


def test_compile_keeps_reviewer_aliases_distinct_from_proposals(
    tmp_path: Path,
) -> None:
    graph = _graph(tmp_path)
    candidates = GOVERN.build_candidate_manifest(graph)
    active = GOVERN.compile_active_manifest(graph, candidates, _decisions(candidates))

    candidate_by_id = {item["case_id"]: item for item in candidates["candidates"]}
    for assignment in active["assignments"]:
        assert assignment["reviewed_aliases"] == [
            f"reviewed alias {assignment['case_id']}"
        ]
        assert set(candidate_by_id[assignment["case_id"]]["proposed_aliases"]).isdisjoint(
            assignment["reviewed_aliases"]
        )


def test_activation_is_sidecar_only_and_rollback_is_exact(
    tmp_path: Path,
    monkeypatch,
) -> None:
    graph = _graph(tmp_path)
    before = _mutable_graph_bytes(graph)
    candidates = GOVERN.build_candidate_manifest(graph)
    endpoint = _offline_defaults(monkeypatch)

    activated = GOVERN.activate(
        graph,
        candidates,
        _decisions(candidates),
        confirm_engine_stopped=True,
        engine_endpoints=[endpoint],
        engine_probe_timeout_sec=0.05,
    )

    active_path = graph / "maintenance" / "error_families" / "active.json"
    assert active_path.is_file()
    assert activated["assignment_count"] == 2
    assert activated["family_count"] == 1
    assert _mutable_graph_bytes(graph) == before
    assert (graph / "embeddings.npz").read_bytes() == b"fixture-cache"
    assert not (graph / "embeddings.rebuild_required.json").exists()
    active = json.loads(active_path.read_text(encoding="utf-8"))
    assignments, diagnostics = FAMILY.validate_active_manifest(
        active,
        {
            path.stem: json.loads(path.read_text(encoding="utf-8"))
            for path in (graph / "nodes").glob("*.json")
        },
    )
    assert diagnostics["status"] == "verified"
    assert len(assignments) == 2

    rolled_back = GOVERN.rollback(
        graph,
        activated["receipt_path"],
        confirm_engine_stopped=True,
        engine_endpoints=[endpoint],
        engine_probe_timeout_sec=0.05,
    )
    assert rolled_back["status"] == "completed"
    assert not active_path.exists()
    assert _mutable_graph_bytes(graph) == before


def test_rollback_refuses_a_later_active_manifest(
    tmp_path: Path,
    monkeypatch,
) -> None:
    graph = _graph(tmp_path)
    candidates = GOVERN.build_candidate_manifest(graph)
    endpoint = _offline_defaults(monkeypatch)
    activated = GOVERN.activate(
        graph,
        candidates,
        _decisions(candidates),
        confirm_engine_stopped=True,
        engine_endpoints=[endpoint],
        engine_probe_timeout_sec=0.05,
    )
    active_path = graph / "maintenance" / "error_families" / "active.json"
    active_path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(GOVERN.MigrationError, match="changed after activation"):
        GOVERN.rollback(
            graph,
            activated["receipt_path"],
            confirm_engine_stopped=True,
            engine_endpoints=[endpoint],
            engine_probe_timeout_sec=0.05,
        )
    assert active_path.read_text(encoding="utf-8") == "{}\n"


def test_graph_engine_uses_verified_aliases_without_mutating_node() -> None:
    payload = _node(
        "ERR-case-a",
        project_id="project-a",
        operation="read",
        component="adapter",
        error_type="timeout",
    )
    node = Node(**payload)
    engine = ENGINE.GraphEngine.__new__(ENGINE.GraphEngine)
    engine._error_family_assignments = {
        node.id: {
            "family_id": "ERF-fixture",
            "aliases": ["adapter request deadline exceeded"],
            "reviewed_aliases": ["adapter request deadline exceeded"],
        }
    }
    engine._kw_df = {"adapter request deadline exceeded": 1}
    engine._kw_N = 1

    assert "adapter request deadline exceeded" not in engine._node_to_text(node)
    score, _intent, _tier, _exact = engine._score_keyword(
        node,
        node.id,
        "adapter request deadline exceeded",
        {"adapter", "request", "deadline", "exceeded"},
        [],
        False,
    )
    assert score > 0
    assert node.activation_keywords == ["adapter"]


def test_reviewed_alias_boost_is_explicit_unique_and_fail_closed() -> None:
    node_a = Node(
        **_node(
            "ERR-case-a",
            project_id="project-a",
            component="adapter",
            error_type="timeout",
        )
    )
    node_b = Node(
        **_node(
            "ERR-case-b",
            project_id="project-a",
            component="worker",
            error_type="crash",
        )
    )
    document = Node(
        **{**_node("DOC-unrelated"), "cluster": "docs", "type": "knowledge"}
    )
    engine = ENGINE.GraphEngine.__new__(ENGINE.GraphEngine)
    engine.nodes = {node.id: node for node in (node_a, node_b, document)}
    engine._error_family_assignments = {
        node_a.id: {
            "aliases": ["adapter timeout", "reviewed request deadline"],
            "reviewed_aliases": ["reviewed request deadline"],
        },
        node_b.id: {
            "aliases": ["worker crash"],
            "reviewed_aliases": [],
        },
    }
    scores = {node_a.id: 0.01, node_b.id: 0.02, document.id: 0.03}
    assert engine._core_repeated_error_requested(
        "publish adapter cannot produce a durable receipt",
        set(),
    )

    ordinary = engine._apply_reviewed_error_family_route_boost(
        "reviewed request deadline",
        scores.copy(),
        explicit_error=False,
    )
    assert ordinary["reason"] == "ordinary_route"

    proposed_only = engine._apply_reviewed_error_family_route_boost(
        "error adapter timeout",
        scores.copy(),
        explicit_error=True,
    )
    assert proposed_only["reason"] == "no_unique_reviewed_alias"

    boosted_scores = scores.copy()
    unique = engine._apply_reviewed_error_family_route_boost(
        "error reviewed request deadline",
        boosted_scores,
        explicit_error=True,
    )
    assert unique["boosted_case_ids"] == [node_a.id]
    assert boosted_scores[node_a.id] > max(scores.values())

    engine._error_family_assignments[node_b.id]["reviewed_aliases"] = [
        "reviewed request deadline"
    ]
    ambiguous = engine._apply_reviewed_error_family_route_boost(
        "error reviewed request deadline",
        scores.copy(),
        explicit_error=True,
    )
    assert ambiguous["reason"] == "no_unique_reviewed_alias"
