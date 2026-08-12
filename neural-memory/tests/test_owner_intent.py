from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
RELEASE_ROOT = ROOT.parent
PROJECT_KIT = (
    RELEASE_ROOT
    / "examples"
    / "codex-cli-project-kit"
    / "scripts"
    / "3can_codex.py"
)


def _load(name: str, path: Path):
    sys.path.insert(0, str(BACKEND))
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _project(tmp_path: Path, *, project_id: str = "project-a") -> Path:
    root = tmp_path / project_id
    root.mkdir()
    capsule = root / ".agents" / "project.json"
    capsule.parent.mkdir()
    capsule.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "project_id": project_id,
                "project_namespace": project_id,
                "project_name": project_id,
                "project_root": ".",
                "git_repository": f"github.com/example/{project_id}",
            }
        ),
        encoding="utf-8",
    )
    (root / "3CAN.md").write_text(
        """---
version: 1
caution: balanced
autonomy: bounded
external_changes: confirm
context: compact
history: applicable
review: risk_based
writeback: meaningful_only
---

# Private body must never enter a route response

PRIVATE-BODY-SENTINEL
""",
        encoding="utf-8",
    )
    return root


def test_owner_intent_loads_compact_projection_and_reuses_cache(tmp_path, monkeypatch):
    owner_intent = _load("owner_intent_cache_test", BACKEND / "owner_intent.py")
    root = _project(tmp_path)
    calls = 0
    original = owner_intent._parse_front_matter

    def counted(raw):
        nonlocal calls
        calls += 1
        return original(raw)

    monkeypatch.setattr(owner_intent, "_parse_front_matter", counted)
    first = owner_intent.load_owner_intent(
        root,
        project_id="project-a",
        project_namespace="project-a",
    )
    second = owner_intent.load_owner_intent(
        root,
        project_id="project-a",
        project_namespace="project-a",
    )

    assert first == second
    assert calls == 1
    assert first["status"] == "applied"
    assert first["source"] == "3CAN.md"
    assert first["digest"].startswith("sha256:")
    assert first["defaults"]["context"] == "compact"
    assert "PRIVATE-BODY-SENTINEL" not in json.dumps(first)

    path = root / "3CAN.md"
    path.write_text(path.read_text(encoding="utf-8") + "\nchanged\n", encoding="utf-8")
    changed = owner_intent.load_owner_intent(
        root,
        project_id="project-a",
        project_namespace="project-a",
    )
    assert calls == 2
    assert changed["digest"] != first["digest"]


@pytest.mark.parametrize(
    "replacement,error",
    [
        ("unknown: value\n", "owner_intent_key_unsupported"),
        ("context: compact\ncontext: full\n", "owner_intent_key_duplicate"),
        ("context: enormous\n", "owner_intent_value_unsupported:context"),
        ("nested:\n  value: bad\n", "owner_intent_key_unsupported:nested"),
    ],
)
def test_owner_intent_invalid_front_matter_fails_without_fallback(
    tmp_path,
    replacement,
    error,
):
    owner_intent = _load("owner_intent_invalid_test", BACKEND / "owner_intent.py")
    root = _project(tmp_path)
    path = root / "3CAN.md"
    text = path.read_text(encoding="utf-8")
    if replacement.startswith("context:"):
        text = text.replace("context: compact\n", replacement)
    else:
        text = text.replace("version: 1\n", f"version: 1\n{replacement}")
    path.write_text(text, encoding="utf-8")

    with pytest.raises(owner_intent.OwnerIntentError, match=error):
        owner_intent.load_owner_intent(
            root,
            project_id="project-a",
            project_namespace="project-a",
        )


def test_owner_intent_is_project_bound(tmp_path):
    owner_intent = _load("owner_intent_isolation_test", BACKEND / "owner_intent.py")
    root = _project(tmp_path, project_id="project-a")

    result = owner_intent.load_owner_intent(
        root,
        project_id="project-b",
        project_namespace="project-b",
    )

    assert result == {
        "schema": "3can.owner-intent/v1",
        "status": "not_applicable",
        "source": "3CAN.md",
        "project_id": "project-b",
        "project_namespace": "project-b",
        "reason": "project_identity_mismatch",
    }
    assert "defaults" not in result


def test_routing_request_validates_owner_intent_shape_and_project_binding(tmp_path):
    owner_intent = _load("owner_intent_model_test", BACKEND / "owner_intent.py")
    models = importlib.import_module("models")
    root = _project(tmp_path)
    projection = owner_intent.load_owner_intent(
        root,
        project_id="project-a",
        project_namespace="project-a",
    )

    accepted = models.RoutingRequest(
        task="current module state",
        project_id="project-a",
        project_namespace="project-a",
        owner_intent=projection,
    )
    assert accepted.owner_intent["defaults"]["writeback"] == "meaningful_only"
    with pytest.raises(ValueError, match="owner_intent_project_identity_required"):
        models.RoutingRequest(task="unbound", owner_intent=projection)
    with pytest.raises(ValueError, match="owner_intent_project_identity_mismatch"):
        models.RoutingRequest(
            task="wrong project",
            project_id="project-b",
            project_namespace="project-b",
            owner_intent=projection,
        )
    forged = json.loads(json.dumps(projection))
    forged["hard_gates_unchanged"] = False
    with pytest.raises(ValueError, match="owner_intent_boundary_invalid"):
        models.RoutingRequest(
            task="weaken hard gate",
            project_id="project-a",
            project_namespace="project-a",
            owner_intent=forged,
        )


def test_route_projects_owner_defaults_without_markdown_body(tmp_path, monkeypatch):
    app = _load("owner_intent_app_test", BACKEND / "app.py")
    root = _project(tmp_path)
    monkeypatch.setenv("THREECAN_PROJECT_DIR", str(root))
    app.clear_owner_intent_cache = importlib.import_module(
        "owner_intent"
    ).clear_owner_intent_cache
    app.clear_owner_intent_cache()
    captured = []

    async def fake_route(req):
        captured.append(req)
        return SimpleNamespace(
            activated_nodes=[],
            relevant_edges=[],
            scores={},
            total_nodes=0,
            total_edges=0,
            route_meta={
                "current_reality_policy": {
                    "external_verification_required": True,
                }
            },
        )

    monkeypatch.setattr(app, "_route_in_worker", fake_route)
    client_projection = importlib.import_module("owner_intent").load_owner_intent(
        root,
        project_id="project-a",
        project_namespace="project-a",
    )
    client_projection = {
        **client_projection,
        "digest": "sha256:" + "0" * 64,
        "defaults": {**client_projection["defaults"], "autonomy": "high"},
    }
    response = asyncio.run(
        app.route_task(
            app.RoutingRequest(
                task="current exact Git state",
                project_id="project-a",
                project_namespace="project-a",
                owner_intent=client_projection,
                allow_degraded=True,
            ),
            detail=False,
        )
    )

    assert captured[0].owner_intent["defaults"]["context"] == "compact"
    assert captured[0].owner_intent["defaults"]["autonomy"] == "bounded"
    assert captured[0].owner_intent["digest"] != client_projection["digest"]
    assert captured[0].mode == "skeleton"
    assert response["route_meta"]["owner_defaults"]["status"] == "applied"
    assert response["route_meta"]["owner_defaults"]["assertion_origin"] == (
        "server_local_file"
    )
    applicable = response["route_meta"]["applicable_project_reality"]
    assert applicable["selected_current_node_ids"] == []
    assert applicable["constraint_node_ids"] == []
    assert applicable["experience_node_ids"] == []
    assert applicable["external_verification_required"] is True
    assert "PRIVATE-BODY-SENTINEL" not in json.dumps(response)


def test_route_types_unverifiable_client_projection_as_assertion(tmp_path, monkeypatch):
    app = _load("owner_intent_client_assertion_test", BACKEND / "app.py")
    client_root = _project(tmp_path)
    projection = importlib.import_module("owner_intent").load_owner_intent(
        client_root,
        project_id="project-a",
        project_namespace="project-a",
    )
    projection = {
        **projection,
        "digest": "sha256:" + "0" * 64,
    }
    authority_root = tmp_path / "authority"
    authority_root.mkdir()
    monkeypatch.setenv("THREECAN_PROJECT_DIR", str(authority_root))
    captured = []

    async def fake_route(req):
        captured.append(req)
        return SimpleNamespace(
            activated_nodes=[],
            relevant_edges=[],
            scores={},
            total_nodes=0,
            total_edges=0,
            route_meta={},
        )

    monkeypatch.setattr(app, "_route_in_worker", fake_route)
    response = asyncio.run(
        app.route_task(
            app.RoutingRequest(
                task="bounded local experiment",
                project_id="project-a",
                project_namespace="project-a",
                owner_intent=projection,
                allow_degraded=True,
            ),
            detail=False,
        )
    )

    assert captured[0].mode == "skeleton"
    owner = response["route_meta"]["owner_defaults"]
    assert owner["digest"] == "sha256:" + "0" * 64
    assert owner["assertion_origin"] == "client_asserted"
    assert owner["hard_gates_unchanged"] is True


def test_route_budget_never_silently_drops_project_reality():
    app = _load("owner_intent_budget_test", BACKEND / "app.py")
    owner_defaults = {
        "schema": "3can.owner-intent/v1",
        "status": "applied",
        "source": "3CAN.md",
        "digest": "sha256:" + "a" * 64,
        "project_id": "project-a",
        "project_namespace": "project-a",
        "defaults": {
            "caution": "balanced",
            "autonomy": "bounded",
            "external_changes": "confirm",
            "context": "compact",
            "history": "applicable",
            "review": "risk_based",
            "writeback": "meaningful_only",
        },
        "precedence": "current_explicit_owner_instruction_over_default",
        "hard_gates_unchanged": True,
    }
    route_meta = {
        "owner_defaults": owner_defaults,
        "applicable_project_reality": {
            "selected_current_node_ids": ["PRJ-project-a"],
            "constraint_node_ids": [],
            "experience_node_ids": [],
            "external_verification_required": False,
        },
    }
    payload = {
        "mode": "skeleton",
        "nodes": [{"id": "PRJ-project-a", "summary": "current"}],
        "route_meta": route_meta,
    }

    with pytest.raises(app.HTTPException) as rejected:
        app._enforce_route_response_budget(
            payload,
            100,
            node_key="nodes",
        )
    assert rejected.value.status_code == 413
    assert rejected.value.detail["error"] == (
        "route_budget_too_small_for_project_reality"
    )

    result, truncated = app._enforce_route_response_budget(
        payload,
        1400,
        node_key="nodes",
    )
    assert truncated is False
    assert result["route_meta"]["owner_defaults"] == owner_defaults
    assert result["route_meta"]["applicable_project_reality"] == (
        route_meta["applicable_project_reality"]
    )


def test_project_kit_sends_projection_and_explicit_mode_wins(tmp_path, monkeypatch):
    helper = _load("owner_intent_project_kit_test", PROJECT_KIT)
    root = _project(tmp_path)
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:Example/project-a.git"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    monkeypatch.setattr(helper, "PROJECT_ROOT", root)
    monkeypatch.setattr(helper, "STAGING_ENGINE_ROOT", ROOT)
    monkeypatch.setattr(helper, "_record_local_token_estimate", lambda *a, **k: None)
    monkeypatch.setattr(helper, "_print_json", lambda _payload: None)
    sent = []

    def request(_base_url, path, *, method="GET", payload=None, timeout=8.0):
        sent.append((path, payload))
        return True, {"confidence": "high", "nodes": [], "route_meta": {}}

    monkeypatch.setattr(helper, "_try_json_request", request)
    args = SimpleNamespace(
        base_url="http://3can.test",
        task="current module",
        max_nodes=4,
        agent_id="codex-project-a-test",
        mode=None,
        confirm_low_confidence=True,
        allow_degraded=True,
        allow_project_mismatch=False,
        budget_tokens=None,
        timeout_seconds=5.0,
        session_id="",
    )

    assert helper.route(args) == 0
    compact_payload = sent[-1][1]
    assert "mode" not in compact_payload
    assert compact_payload["owner_intent"]["defaults"]["context"] == "compact"
    assert "PRIVATE-BODY-SENTINEL" not in json.dumps(compact_payload)

    args.mode = "full"
    assert helper.route(args) == 0
    assert sent[-1][1]["mode"] == "full"


def test_project_kit_session_start_preserves_owner_precedence(monkeypatch):
    helper = _load("owner_intent_session_start_test", PROJECT_KIT)
    project_meta = {
        "project_id": "project-a",
        "project_namespace": "project-a",
    }
    client_owner = {
        "status": "applied",
        "source": "3CAN.md",
        **project_meta,
        "defaults": {"autonomy": "high"},
    }
    printed = []

    monkeypatch.setattr(
        helper,
        "ensure_online",
        lambda *_a, **_k: {"online": True, "healthy": True},
    )
    monkeypatch.setattr(helper, "_session_id_for_agent", lambda *_a: "session-a")
    monkeypatch.setattr(helper, "_current_project_metadata", lambda **_k: project_meta)
    monkeypatch.setattr(helper, "_record_local_token_estimate", lambda *_a, **_k: None)
    monkeypatch.setattr(helper, "_print_json", printed.append)
    args = SimpleNamespace(
        base_url="http://3can.test",
        engine_root="",
        start_if_offline=False,
        wait_seconds=1,
        min_nodes=1,
        agent_id="agent-a",
        session_id="",
        name="Agent A",
        role="review",
        task="owner precedence",
        capabilities=[],
        meta="",
        max_nodes=4,
    )

    def run(*, server_owner, local_owner=client_owner):
        printed.clear()
        monkeypatch.setattr(helper, "_owner_intent_projection", lambda **_k: local_owner)

        def request(_base_url, path, **_kwargs):
            if path == "/api/agents/checkin":
                return True, {"agent_id": "agent-a"}
            if path.startswith("/api/briefing?"):
                return True, {"owner_defaults": server_owner}
            raise AssertionError(path)

        monkeypatch.setattr(helper, "_try_json_request", request)
        code = helper.session_start(args)
        return code, printed[-1]

    server_owner = {
        "status": "applied",
        "source": "3CAN.md",
        "assertion_origin": "server_local_file",
        **project_meta,
        "defaults": {"autonomy": "bounded"},
    }
    code, result = run(server_owner=server_owner)
    assert code == 0
    assert result["briefing"]["owner_defaults"] == server_owner

    code, result = run(server_owner=None)
    assert code == 0
    assert result["briefing"]["owner_defaults"]["defaults"]["autonomy"] == "high"
    assert result["briefing"]["owner_defaults"]["assertion_origin"] == "client_asserted"

    code, result = run(server_owner={
        **server_owner,
        "project_id": "project-b",
        "project_namespace": "project-b",
    })
    assert code == 1
    assert result["briefing_error"]["kind"] == (
        "owner_intent_project_identity_mismatch"
    )

    code, result = run(server_owner={"status": "not_applicable"})
    assert code == 1
    assert result["briefing_error"]["kind"] == "owner_intent_invalid"

    code, result = run(server_owner=None, local_owner=None)
    assert code == 0
    assert result["briefing"]["owner_defaults"] is None


def test_mcp_formats_compact_owner_defaults():
    mcp = _load("owner_intent_mcp_test", ROOT / "mcp_server.py")
    line = mcp._owner_defaults_line(
        {
            "owner_defaults": {
                "status": "applied",
                "source": "3CAN.md",
                "assertion_origin": "client_asserted",
                "defaults": {
                    "caution": "balanced",
                    "autonomy": "bounded",
                    "context": "compact",
                },
            }
        }
    )
    assert line == (
        "owner defaults (3CAN.md; client_asserted): caution=balanced autonomy=bounded "
        "context=compact"
    )


@pytest.mark.parametrize(
    "scope",
    [
        {"project_id": "project-a"},
        {"project_namespace": "project-a"},
    ],
)
def test_mcp_briefing_rejects_partial_project_identity(monkeypatch, scope):
    mcp = _load("owner_intent_mcp_scope_test", ROOT / "mcp_server.py")
    calls = []
    monkeypatch.setattr(mcp, "_get", lambda *args, **kwargs: calls.append((args, kwargs)))

    with pytest.raises(
        ValueError,
        match="project_id and project_namespace must be supplied together",
    ):
        mcp.briefing(**scope)

    assert calls == []


def test_mcp_briefing_forwards_complete_project_identity(monkeypatch):
    mcp = _load("owner_intent_mcp_complete_scope_test", ROOT / "mcp_server.py")
    calls = []

    def fake_get(path, params):
        calls.append((path, params))
        return {"agent_id": params["agent_id"]}

    monkeypatch.setattr(mcp, "_get", fake_get)

    mcp.briefing(
        agent_id="mcp-project-a",
        project_id="project-a",
        project_namespace="namespace-a",
    )

    assert calls == [
        (
            "/api/briefing",
            {
                "agent_id": "mcp-project-a",
                "max_nodes": 6,
                "project_id": "project-a",
                "project_namespace": "namespace-a",
            },
        )
    ]


def test_project_briefing_filters_activity_and_ranks_applicable_errors_first(
    tmp_path,
    monkeypatch,
):
    app = _load("owner_intent_briefing_test", BACKEND / "app.py")
    monkeypatch.setattr(app, "load_owner_intent", lambda *_a, **_k: None)

    def error_node(node_id, applicability, activation_count):
        return SimpleNamespace(
            id=node_id,
            name=node_id,
            cluster="错误与教训",
            status=SimpleNamespace(value="active"),
            content=SimpleNamespace(
                current_state="active",
                description="bounded fixture",
                extra={},
            ),
            activation_keywords=["error"],
            activation_count=activation_count,
            updated_at="2026-08-11T00:00:00+00:00",
            applicability=applicability,
        )

    nodes = {
        node.id: node
        for node in [
            error_node(f"ERR-other-{index}", "mismatch", 100 - index)
            for index in range(3)
        ]
        + [error_node("ERR-project-a", "exact_project", 1)]
    }
    activity = SimpleNamespace(
        affected_nodes=["ERR-other-0"],
        model_dump=lambda: {
            "detail": "another project's activity",
            "affected_nodes": ["ERR-other-0"],
        },
    )
    fake_engine = SimpleNamespace(
        nodes=nodes,
        list_nodes=lambda: list(nodes.values()),
        _is_error_case_node=lambda node_id, _node: node_id.startswith("ERR-"),
        _is_error_artifact_node=lambda node_id, _node: node_id.startswith("ERR-"),
        _reserved_error_knowledge_id=lambda node_id: node_id.startswith("ERR-"),
        _project_applicability=lambda node, **_scope: node.applicability,
        get_activity=lambda **_query: [activity],
    )
    monkeypatch.setattr(app, "engine", fake_engine)

    result = asyncio.run(
        app.agent_briefing(
            agent_id="shared-agent",
            max_nodes=6,
            role="review",
            compress=True,
            include_error_history=True,
            project_id="project-a",
            project_namespace="project-a",
        )
    )

    assert [node["id"] for node in result["role_nodes"]] == ["ERR-project-a"]
    assert result["agent_history"] == []
    assert "applicable_project_reality" not in result


def test_agent_mediated_semantic_checkpoint_updates_meaning_without_commit_mirroring(
    tmp_path,
    monkeypatch,
):
    graph_dir = tmp_path / "checkpoint-graph"
    nodes_dir = graph_dir / "nodes"
    nodes_dir.mkdir(parents=True)
    node_id = "PRJ-PUBLIC-module-checkpoint"
    (nodes_dir / f"{node_id}.json").write_text(
        json.dumps(
            {
                "id": node_id,
                "name": "Public module semantic checkpoint",
                "cluster": "public",
                "type": "knowledge",
                "status": "active",
                "priority": "high",
                "content": {
                    "description": (
                        "Git and its pull request are the exact authority for "
                        "the public module completion"
                    ),
                    "current_state": "stale: implementation still pending",
                    "extra": {
                        "project_id": "public-demo",
                        "project_namespace": "public-demo",
                    },
                },
                "activation_keywords": [
                    "current",
                    "public",
                    "module",
                    "git",
                    "checkpoint",
                ],
                "activation_count": 1,
            }
        ),
        encoding="utf-8",
    )
    (graph_dir / "edges.json").write_text("[]", encoding="utf-8")
    monkeypatch.setenv("THREECAN_GRAPH_DIR", str(graph_dir))
    monkeypatch.setenv("THREECAN_RERANKER_MODE", "off")
    graph_engine = _load("semantic_checkpoint_graph_engine", BACKEND / "graph_engine.py")
    graph_engine._embed_model = graph_engine._HashingEmbeddingModel()
    engine = graph_engine.GraphEngine()

    before = engine.route(
        graph_engine.RoutingRequest(
            task="current public module Git checkpoint",
            project_id="public-demo",
            project_namespace="public-demo",
            max_nodes=3,
        )
    )
    assert [node.id for node in before.activated_nodes] == [node_id]
    assert "Git" in before.activated_nodes[0].content.description
    assert before.activated_nodes[0].content.current_state.startswith("stale:")

    updated = engine.session_writeback(
        [
            {
                "node_id": node_id,
                "field": "current_state",
                "value": "accepted: owner intent module verified at the current Git PR",
                "expected_updated_at": engine.get_node(node_id).updated_at,
            }
        ],
        agent_id="PUBLIC-checkpoint-agent",
        execution_context={
            "project_id": "public-demo",
            "project_namespace": "public-demo",
            "workorder_id": "PUBLIC-owner-intent",
        },
        provenance=graph_engine.DurableProvenance(
            source_provenance="user_authoritative",
            authorized_by="user",
        ),
    )

    assert updated == [node_id]
    assert len(engine.nodes) == 1
    after = engine.route(
        graph_engine.RoutingRequest(
            task="current public module Git checkpoint",
            project_id="public-demo",
            project_namespace="public-demo",
            max_nodes=3,
        )
    )
    assert after.activated_nodes[0].id == node_id
    assert after.activated_nodes[0].content.current_state.startswith("accepted:")
    assert not any(path.name.startswith("commit-") for path in nodes_dir.iterdir())
