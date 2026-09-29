"""Automatic semantic writeback: real client identity + bounded transport fixtures."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_runtimehook_plugin import _activate, _git, _session_id
import runtimehook_writeback as wb
import test_runtimehook_jev as hook_tests
from test_async_graph_concurrency import graph_runtime  # noqa: F401

plain_repo = hook_tests.plain_repo
isolated_scope_cache = hook_tests.isolated_scope_cache


CLIENT = Path(__file__).resolve().parents[2] / "examples/codex-cli-project-kit/scripts/3can_codex.py"


@pytest.fixture
def lane(plain_repo, monkeypatch, tmp_path):
    root = plain_repo
    _git(root, "remote", "add", "origin", "https://example.invalid/fixture/repo.git")
    (root / ".agents").mkdir()
    (root / ".agents/project.json").write_text(json.dumps({"schema_version": 1,
        "project_id": "fixture", "project_namespace": "fixture", "project_root": ".",
        "git_repository": "example.invalid/fixture/repo"}))
    settings = {"client_path": str(CLIENT), "selector_path": str(tmp_path / "selector.json"),
                "base_url": "http://127.0.0.1:9700"}
    Path(settings["selector_path"]).write_text(json.dumps({"engine_root": str(tmp_path / "engine"), "graph_root": str(tmp_path / "graph")}))
    client = wb._client(settings, root)
    binding = wb.connection(settings, root, agent_id="fixture-agent", workorder_id="fixture-workorder", node_id="DOC-fixture")
    node = {"id": "DOC-fixture", "updated_at": "2026-09-26T05:26:01.002052+00:00",
            "content": {"notes": "existing evidence", "extra": {"project_id": "fixture", "project_namespace": "fixture"}}}
    calls = []
    def request(base, path, *, payload=None, **kwargs):
        calls.append((path, copy.deepcopy(payload)))
        if path.startswith("/api/stats"):
            return True, {"healthy": True, "total_nodes": 1, "readiness": {"production_ready": True},
                "runtime_identity": client._expected_runtime_identity(tmp_path / "engine", tmp_path / "graph")}
        if path.startswith("/api/nodes/"):
            return True, copy.deepcopy(node)
        if path == "/api/agents/checkin":
            return True, {"agent_id": payload["agent_id"]}
        if path == "/api/activity/log":
            return True, {"ok": True, "self_hash": "intake-fixture"}
        if path == "/api/writeback":
            change = payload["changes"][0]
            assert change["expected_updated_at"] == node["updated_at"]
            node["content"]["notes"] = change["value"]
            node["updated_at"] = "opaque-new-version"
            return True, {"count": 1, "updated": ["DOC-fixture"]}
        pytest.fail(path)
    monkeypatch.setattr(client, "_try_json_request", request)
    monkeypatch.setattr(wb, "_client", lambda *args: client)
    event = {"event": "milestone", "activation_id": "rh-fixture", "result": "PARTIAL",
             "summary": "解析接口已实现，生产验收未运行。", "reference": "git:fixture",
             "next_objective": "运行验收", "session_id": "fixture-session"}
    return SimpleNamespace(root=root, settings=settings, client=client, binding=binding,
                           node=node, calls=calls, event=event, request=request)


def test_meaning_is_appended_once_and_exactly_read_back(lane):
    a = wb.deliver(lane.settings, lane.root, lane.binding, lane.event)
    b = wb.deliver(lane.settings, lane.root, lane.binding, lane.event)
    assert a["status"] == "WRITTEN_AND_READBACK_VERIFIED"
    assert b["status"] == "ALREADY_RECORDED" and a["event_id"] == b["event_id"]
    assert lane.node["content"]["notes"].startswith("existing evidence")
    assert [p for p, _ in lane.calls].count("/api/writeback") == 1
    assert a["packet"]["result"] == "PARTIAL"


def test_onboarding_registers_actual_agent_not_new_graph_node(lane):
    lane.event["event"] = "onboarding"
    result = wb.deliver(lane.settings, lane.root, lane.binding, lane.event)
    assert result["status"] == "WRITTEN_AND_READBACK_VERIFIED"
    checkin = next(p for path, p in lane.calls if path == "/api/agents/checkin")
    assert checkin["agent_id"] == lane.binding["agent_id"]
    assert checkin["meta"]["project_identity"]["workspace_id"] == lane.binding["workspace_id"]
    assert not any(path == "/api/nodes" for path, _ in lane.calls)


@pytest.mark.parametrize("state", ["observed", "investigating", "mitigated", "resolution_claimed"])
def test_error_progress_keeps_case_reference_without_fake_resolution(lane, state):
    lane.event.update(event="error", error={"id": "ERR-existing-case", "state": state})
    result = wb.deliver(lane.settings, lane.root, lane.binding, lane.event)
    assert result["status"] == "WRITTEN_AND_READBACK_VERIFIED"
    assert result["packet"]["error"]["state"] == state
    assert all(path != "/api/activity/done" for path, _ in lane.calls)


def test_unverified_resolution_and_foreign_nodes_never_write(lane):
    lane.event.update(event="error", error={"id": "ERR-existing-case", "state": "resolved"})
    assert wb.deliver(lane.settings, lane.root, lane.binding, lane.event)["status"] == "UNAVAILABLE"
    assert not lane.calls
    lane.event["event"] = "milestone"
    lane.node["content"]["extra"]["project_id"] = "foreign"
    assert wb.deliver(lane.settings, lane.root, lane.binding, lane.event)["error_code"] == "NODE_PROJECT_BINDING_UNVERIFIED"
    assert not any(payload for _, payload in lane.calls)


def test_scope_change_and_secrets_do_not_upload(lane):
    wrong = {**lane.binding, "workspace_id": "foreign"}
    assert wb.deliver(lane.settings, lane.root, wrong, lane.event)["error_code"] == "PROJECT_WORKSPACE_CHANGED"
    lane.event["summary"] = "sk-" + "x" * 30
    assert wb.deliver(lane.settings, lane.root, lane.binding, lane.event)["error_code"] == "WRITEBACK_SECRET_REJECTED"
    assert not lane.calls


@pytest.mark.parametrize("failure", ["conflict", "zero_effect", "readback", "offline"])
def test_failed_write_is_typed_kept_for_retry_and_not_a_local_gate(lane, monkeypatch, failure):
    def request(base, path, **kwargs):
        if failure == "offline":
            return False, {"status": "UNAVAILABLE"}
        if path == "/api/writeback":
            lane.calls.append((path, kwargs["payload"]))
            if failure == "conflict":
                return False, {"http_status": 409}
            if failure == "zero_effect":
                return True, {"count": 0, "updated": []}
            return True, {"count": 1, "updated": ["DOC-fixture"]}
        return lane.request(base, path, **kwargs)
    monkeypatch.setattr(lane.client, "_try_json_request", request)
    result = wb.deliver(lane.settings, lane.root, lane.binding, lane.event)
    assert result["status"] in {"UNAVAILABLE", "CONFLICT"}
    assert result["local_work_blocked"] is False and result["packet"]["summary"] == lane.event["summary"]
    assert [p for p, _ in lane.calls].count("/api/writeback") <= 1
    assert [p for p, _ in lane.calls].count("/api/activity/log") <= 1


def test_review_cli_automatically_delivers_without_writeback_command(lane, monkeypatch, capsys):
    controller = hook_tests.controller
    _activate(lane.root)
    state = controller._load_state(lane.root, _session_id(lane.root))
    state["knowledge"] = {**lane.binding, "session_id": _session_id(lane.root)}
    controller._write_state(lane.root, state, _session_id(lane.root))
    monkeypatch.setattr(wb, "config", lambda: lane.settings)
    code = controller.main(["--root", str(lane.root), "--native-cwd", str(lane.root),
        "--session-id", _session_id(lane.root), "review", "--stage", "episode", "--result", "PARTIAL",
        "--reference", "test:actual-result", "--summary", "核心实现已验证，验收待完成", "--next-objective", "验收"])
    result = json.loads(capsys.readouterr().out)
    assert code == 0 and result["result"] == "PARTIAL"
    assert result["writeback"]["status"] == "WRITTEN_AND_READBACK_VERIFIED"
    assert list((controller._session_dir(_session_id(lane.root))).glob("runtimehook_delta_*.json"))


def test_native_hooks_never_deliver(lane, monkeypatch, capsys):
    controller = hook_tests.controller
    _activate(lane.root)
    monkeypatch.setattr(wb, "deliver", lambda *a: pytest.fail("native event went online"))
    monkeypatch.setattr(wb, "config", lambda: lane.settings)
    for event in ["SessionStart", "UserPromptSubmit", "PostToolUse", "Stop"]:
        import io
        monkeypatch.setattr(controller.sys, "stdin", io.StringIO(json.dumps({"hook_event_name": event,
            "cwd": str(lane.root), "session_id": _session_id(lane.root), "stop_hook_active": True})))
        assert controller.main(["hook", "--session-orientation"]) == 0
        capsys.readouterr()


def test_connect_configuration_failure_is_typed(lane, monkeypatch, capsys):
    controller = hook_tests.controller
    _activate(lane.root)
    def unavailable():
        raise wb.WritebackError("INVALID_WRITEBACK_CONFIG")
    monkeypatch.setattr(wb, "config", unavailable)
    assert controller.main(["--root", str(lane.root), "--session-id", _session_id(lane.root),
        "connect", "--agent-id", "fixture-agent", "--workorder-id", "fixture-workorder",
        "--node-id", "DOC-fixture", "--reference", "test:connect"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "UNAVAILABLE" and result["error"] == "INVALID_WRITEBACK_CONFIG"
    assert not lane.calls


def test_real_graph_persistence_and_canonical_writeback(lane, graph_runtime, monkeypatch):  # noqa: F811
    module, engine, graph_dir = graph_runtime
    engine.create_node(module.NodeCreate(id="DOC-fixture", name="Fixture module", cluster="fixture",
        content=module.NodeContent(notes="previous evidence", extra={"project_id": "fixture", "project_namespace": "fixture"})))
    def request(base, path, *, payload=None, **kwargs):
        if path == "/api/nodes/DOC-fixture":
            return True, engine.nodes["DOC-fixture"].model_dump(mode="json")
        if path == "/api/writeback":
            updated = engine.session_writeback(payload["changes"], agent_id=payload["agent_id"],
                execution_context={key: payload[key] for key in ("project_id", "project_namespace", "workspace_id", "workorder_id")})
            return True, {"count": len(updated), "updated": updated}
        return lane.request(base, path, payload=payload, **kwargs)
    monkeypatch.setattr(lane.client, "_try_json_request", request)
    receipt = wb.deliver(lane.settings, lane.root, lane.binding, lane.event)
    assert receipt["status"] == "WRITTEN_AND_READBACK_VERIFIED"
    saved = json.loads((graph_dir / "nodes/DOC-fixture.json").read_text(encoding="utf-8"))
    assert receipt["event_id"] in saved["content"]["notes"]
    assert saved["content"]["notes"].startswith("previous evidence")
    assert wb.deliver(lane.settings, lane.root, lane.binding, lane.event)["status"] == "ALREADY_RECORDED"


def test_invalid_project_capsule_never_calls_runtime(lane):
    (lane.root / ".agents/project.json").write_text("{}")
    result = wb.deliver(lane.settings, lane.root, lane.binding, lane.event)
    assert result["status"] == "UNAVAILABLE" and not lane.calls


def test_stale_runtime_identity_and_empty_effect_are_not_success(lane, monkeypatch):
    def wrong(base, path, **kwargs):
        ok, result = lane.request(base, path, **kwargs)
        if path.startswith("/api/stats"):
            result["runtime_identity"]["graph_root_sha256"] = "wrong"
        return ok, result
    monkeypatch.setattr(lane.client, "_try_json_request", wrong)
    result = wb.deliver(lane.settings, lane.root, lane.binding, lane.event)
    assert result["error_code"] == "RUNTIME_IDENTITY_OR_READINESS_UNVERIFIED"
    assert not any(payload for _, payload in lane.calls)


def test_cached_readiness_does_not_force_deep_refresh(lane, monkeypatch):
    def request(base, path, **kwargs):
        assert path != "/api/stats?deep=true"
        return lane.request(base, path, **kwargs)

    monkeypatch.setattr(lane.client, "_try_json_request", request)
    result = wb.deliver(lane.settings, lane.root, lane.binding, lane.event)
    assert result["status"] == "WRITTEN_AND_READBACK_VERIFIED"
    assert [path for path, _ in lane.calls].count("/api/stats") == 1


def test_required_slow_deep_refresh_can_write_and_read_back(lane, monkeypatch):
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(wb.time, "monotonic", lambda: clock.now)
    timeouts = []

    def request(base, path, *, timeout, **kwargs):
        timeouts.append((path, timeout))
        duration = 5.9 if path == "/api/stats?deep=true" else 0.1
        clock.now += min(duration, timeout)
        if duration > timeout:
            return False, {"reason": "timed out"}
        ok, data = lane.request(base, path, **kwargs)
        if path == "/api/stats":
            data["readiness"]["production_ready"] = False
            data["healthy"] = False
        return ok, data

    monkeypatch.setattr(lane.client, "_try_json_request", request)
    result = wb.deliver(lane.settings, lane.root, lane.binding, lane.event)
    assert result["status"] == "WRITTEN_AND_READBACK_VERIFIED"
    assert result["event_id"] in lane.node["content"]["notes"]
    assert timeouts[:2] == [("/api/stats", 4), ("/api/stats?deep=true", 30)]
    assert all(timeout == 2 for _, timeout in timeouts[2:])
    assert [path for path, _ in timeouts].count("/api/writeback") == 1


@pytest.mark.parametrize("invalid", ["wrong_identity", "missing_readiness", "health_mismatch"])
def test_no_deep_refresh_after_invalid_runtime_contract(lane, monkeypatch, invalid):
    def request(base, path, **kwargs):
        assert path == "/api/stats"
        ok, data = lane.request(base, path, **kwargs)
        if invalid == "wrong_identity":
            data["runtime_identity"]["graph_root_sha256"] = "wrong"
            data["readiness"]["production_ready"] = False
        elif invalid == "missing_readiness":
            del data["readiness"]
        else:
            data["healthy"] = False
        return ok, data

    monkeypatch.setattr(lane.client, "_try_json_request", request)
    result = wb.deliver(lane.settings, lane.root, lane.binding, lane.event)
    assert result["error_code"] == "RUNTIME_IDENTITY_OR_READINESS_UNVERIFIED"
    assert len(lane.calls) == 1


@pytest.mark.parametrize("failure", ["not_ready", "wrong_identity", "timeout"])
def test_deep_refresh_failure_never_writes_or_retries(lane, monkeypatch, failure):
    attempts = []

    def request(base, path, *, timeout, **kwargs):
        attempts.append(path)
        assert path in {"/api/stats", "/api/stats?deep=true"}
        if path.endswith("deep=true") and failure == "timeout":
            assert timeout == 30
            return False, {"reason": "timed out"}
        ok, data = lane.request(base, path, **kwargs)
        if path == "/api/stats" or failure == "not_ready":
            data["readiness"]["production_ready"] = False
        elif failure == "wrong_identity":
            data["runtime_identity"]["graph_root_sha256"] = "wrong"
        return ok, data

    monkeypatch.setattr(lane.client, "_try_json_request", request)
    result = wb.deliver(lane.settings, lane.root, lane.binding, lane.event)
    assert result["status"] == "UNAVAILABLE"
    assert attempts == ["/api/stats", "/api/stats?deep=true"]
    if failure == "timeout":
        assert result["failed_request"]["phase"] == "readiness"
        assert result["failed_request"]["timeout_seconds"] == 30
    else:
        assert result["error_code"] == "RUNTIME_IDENTITY_OR_READINESS_UNVERIFIED"


@pytest.mark.parametrize("phase", ["readiness", "node_read", "agent_checkin", "write", "readback"])
@pytest.mark.parametrize("failure,cause,code", [
    ({"reason": "timed out"}, "timeout", "RUNTIME_UNAVAILABLE"),
    ({"reason": "private exception detail"}, "transport", "RUNTIME_UNAVAILABLE"),
    ({"http_status": 409, "body": "private exception detail"}, "http", "CONFLICT"),
])
def test_failed_request_diagnostic_is_phased_bounded_and_sanitized(lane, monkeypatch, phase, failure, cause, code):
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(wb.time, "monotonic", lambda: clock.now)
    lane.event["event"] = "onboarding"
    node_reads = 0
    attempts = []

    def request(base, path, *, timeout, **kwargs):
        nonlocal node_reads
        if path.startswith("/api/nodes/"):
            node_reads += 1
            current = "node_read" if node_reads == 1 else "readback"
        else:
            current = {"/api/stats": "readiness", "/api/agents/checkin": "agent_checkin",
                       "/api/writeback": "write", "/api/activity/log": "issue_intake"}[path]
        attempts.append(current)
        clock.now += 0.125
        if current == phase:
            return False, failure
        return lane.request(base, path, **kwargs)

    monkeypatch.setattr(lane.client, "_try_json_request", request)
    result = wb.deliver(lane.settings, lane.root, lane.binding, lane.event)
    assert result["status"] == ("CONFLICT" if code == "CONFLICT" else "UNAVAILABLE")
    assert result["error_code"] == code
    assert result["failed_request"] == {"phase": phase, "cause": cause,
        "timeout_seconds": 4 if phase == "readiness" else 2, "elapsed_ms": 125.0}
    assert "private exception detail" not in json.dumps(result)
    assert result["local_work_blocked"] is False
    assert attempts.count(phase) == 1
    assert attempts.count("issue_intake") <= 1
    if phase in {"readiness", "node_read", "agent_checkin"}:
        assert "write" not in attempts


def test_readback_uses_remaining_budget_after_slow_requests(lane, monkeypatch):
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(wb.time, "monotonic", lambda: clock.now)
    lane.event["event"] = "onboarding"
    timeouts = []

    def request(base, path, *, timeout, **kwargs):
        if path == "/api/activity/log":
            return lane.request(base, path, **kwargs)
        timeouts.append((path, timeout))
        duration = 30.0 if path.endswith("deep=true") else 3.9 if path == "/api/stats" else 1.8
        clock.now += min(duration, timeout)
        if duration > timeout:
            return False, {"reason": "timed out"}
        ok, data = lane.request(base, path, **kwargs)
        if path == "/api/stats":
            data["readiness"]["production_ready"] = False
        return ok, data

    monkeypatch.setattr(lane.client, "_try_json_request", request)
    result = wb.deliver(lane.settings, lane.root, lane.binding, lane.event)
    assert result["status"] == "UNAVAILABLE"
    assert result["failed_request"]["phase"] == "readback"
    assert result["failed_request"]["cause"] == "timeout"
    assert timeouts[-1][1] == pytest.approx(0.7)
    assert result["elapsed_ms"] == pytest.approx(40_000)
    assert [path for path, _ in timeouts].count("/api/writeback") == 1
    # The write took effect, but unverified readback must never become success.
    assert result["event_id"] in lane.node["content"]["notes"]


def test_deadline_diagnostic_before_request_has_no_network_attempt(lane, monkeypatch):
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(wb.time, "monotonic", lambda: clock.now)
    gate = lane.client._project_identity_gate

    def slow_gate(*args, **kwargs):
        result = gate(*args, **kwargs)
        clock.now += 40.1
        return result

    monkeypatch.setattr(lane.client, "_project_identity_gate", slow_gate)
    result = wb.deliver(lane.settings, lane.root, lane.binding, lane.event)
    assert result["error_code"] == "WRITEBACK_DEADLINE"
    assert result["failed_request"] == {"phase": "readiness", "cause": "deadline",
        "timeout_seconds": 0, "elapsed_ms": 0}
    assert not lane.calls
