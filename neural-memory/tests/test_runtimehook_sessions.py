"""Global invocation counterexamples. Native payload fixtures are not App UAT."""
import io
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_runtimehook_plugin as shared

isolated_scope_cache = shared.isolated_scope_cache
plain_repo = shared.plain_repo


@pytest.fixture
def lane(monkeypatch):
    controller = shared._scope_module()
    policy = Path(os.environ["CODEX_HOME"]) / "runtimehook/policy.json"
    policy.parent.mkdir(parents=True)
    policy.write_text(json.dumps({"schema": "3can.runtimehook-policy/v1", "jev_required": True}))
    monkeypatch.setattr(controller.writeback_adapter, "deliver", lambda *a: pytest.fail("native event went online"))
    return controller


def hook(lane, monkeypatch, capsys, root, sid, event="SessionStart", **extra):
    payload = {"cwd": str(root), "session_id": sid, "hook_event_name": event,
               "source": "startup", **extra}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert lane.hook(SimpleNamespace(root=None, session_orientation=True)) == 0
    output = capsys.readouterr().out
    return json.loads(output) if output.strip() else {}


def command(root, sid, *args):
    result = subprocess.run([sys.executable, str(shared.PLUGIN_CLI), "--root", str(root),
                             "--native-cwd", str(root), "--session-id", sid, *args],
                            capture_output=True, timeout=30)
    value = json.loads(result.stdout.decode("utf-8"))
    assert result.returncode == 0, value
    return value


def activate(root, sid, goal):
    return command(root, sid, "on", "--goal", goal, "--acceptance", "A01=Observed output matches the request.",
                   "--intensity", "light", "--reason", "Bounded independent task.")


@pytest.mark.parametrize("event", ["SessionStart", "UserPromptSubmit", "SubagentStart"])
def test_required_entry_registers_only_this_task_and_requires_real_intent(lane, monkeypatch, capsys, plain_repo, event):
    extra = {"agent_id": "child-1"} if event == "SubagentStart" else {}
    result = hook(lane, monkeypatch, capsys, plain_repo, "parent-1", event, **extra)
    sid = extra.get("agent_id", "parent-1")
    assert "INTENT_REQUIRED" in result["hookSpecificOutput"]["additionalContext"]
    assert lane._read_scope(sid)["activation_id"] is None
    assert not (lane._session_dir(sid) / "state.json").exists()
    assert not (plain_repo / ".codex/runtimehook").exists()
    if extra:
        assert not lane._scope_path("parent-1").exists()


def test_missing_initialization_requires_one_bounded_continuation(lane, monkeypatch, capsys, plain_repo):
    first = hook(lane, monkeypatch, capsys, plain_repo, "new-task", "Stop")
    second = hook(lane, monkeypatch, capsys, plain_repo, "new-task", "Stop", stop_hook_active=True)
    assert first["decision"] == "block" and "INTENT_REQUIRED" in first["reason"]
    assert "decision" not in second and "INTENT_REQUIRED" in second["systemMessage"]


def test_concurrent_sessions_in_one_directory_never_share_semantic_state(lane, plain_repo):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda sid: activate(plain_repo, sid, "Goal for " + sid), ["one", "two"]))
    assert results[0]["activation_id"] != results[1]["activation_id"]
    before = (lane._session_dir("two") / "state.json").read_bytes()
    command(plain_repo, "one", "off")
    assert (lane._session_dir("two") / "state.json").read_bytes() == before
    assert command(plain_repo, "two", "status")["run_intent"]["goal"] == "Goal for two"
    assert not (plain_repo / ".codex/runtimehook").exists()


def test_parent_and_sibling_native_events_use_agent_id(lane, monkeypatch, capsys, plain_repo):
    for sid in ["parent", "child-a", "child-b"]:
        activate(plain_repo, sid, "Goal " + sid)
    before = {sid: (lane._session_dir(sid) / "state.json").read_bytes() for sid in ["parent", "child-b"]}
    start = hook(lane, monkeypatch, capsys, plain_repo, "parent", "SubagentStart", agent_id="child-a")
    assert "Goal child-a" in start["hookSpecificOutput"]["additionalContext"]
    tool = hook(lane, monkeypatch, capsys, plain_repo, "parent", "PostToolUse", agent_id="child-a",
                tool_name="update_plan", tool_input={"plan": [{"step": "Independent stage", "status": "completed"}]})
    assert "Goal child-a" in tool["hookSpecificOutput"]["additionalContext"]
    stop = hook(lane, monkeypatch, capsys, plain_repo, "parent", "SubagentStop", agent_id="child-a")
    assert stop["decision"] == "block"
    for sid, raw in before.items():
        assert (lane._session_dir(sid) / "state.json").read_bytes() == raw


def test_missing_child_id_never_falls_back_to_parent(lane, monkeypatch, capsys, plain_repo):
    activate(plain_repo, "parent", "Private parent goal")
    before = (lane._session_dir("parent") / "state.json").read_bytes()
    result = hook(lane, monkeypatch, capsys, plain_repo, "parent", "SubagentStart")
    assert "CHILD_ID_REQUIRED" in result["systemMessage"]
    assert "Private parent goal" not in json.dumps(result)
    assert (lane._session_dir("parent") / "state.json").read_bytes() == before


def test_projectless_task_has_same_supervision_without_fake_git(lane, monkeypatch, capsys, tmp_path):
    root = tmp_path / "projectless"
    root.mkdir()
    hook(lane, monkeypatch, capsys, root, "projectless")
    activate(root, "projectless", "Review a document")
    state = command(root, "projectless", "status")
    assert state["workspace_kind"] == "directory" and state["boundary"]["observed_git_head"] is None
    result = hook(lane, monkeypatch, capsys, root, "projectless", "UserPromptSubmit")
    assert "Review a document" in result["hookSpecificOutput"]["additionalContext"]
    assert not list(root.iterdir())


def legacy_fixture(lane, root, owner="owner", wrong_owner=False):
    activate(root, owner, "Retained legacy goal")
    state = lane._load_state(root, owner)
    for key in ["session_id", "workspace", "workspace_kind"]:
        state.pop(key)
    state["schema"] = lane.STATE_SCHEMA
    state["knowledge"] = {"session_id": "foreign" if wrong_owner else owner}
    legacy = root / lane.STATE_PATH
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps(state), encoding="utf-8")
    exclude = root / ".git/info/exclude"
    with exclude.open("a", encoding="utf-8") as file:
        file.write("\n/.codex/runtimehook/\n")
    binding = lane._read_scope(owner)
    binding["schema"] = "3can.runtimehook-scope/v2"
    lane._save_scope(binding)
    (lane._session_dir(owner) / "state.json").unlink()
    return legacy


def test_legacy_import_is_exact_owner_only_and_keeps_original(lane, monkeypatch, capsys, plain_repo):
    legacy = legacy_fixture(lane, plain_repo)
    before = legacy.read_bytes()
    result = hook(lane, monkeypatch, capsys, plain_repo, "owner")
    assert "Retained legacy goal" in result["hookSpecificOutput"]["additionalContext"]
    assert lane._load_state(plain_repo, "owner")["schema"] == lane.SESSION_STATE_SCHEMA
    assert legacy.read_bytes() == before
    other = hook(lane, monkeypatch, capsys, plain_repo, "new-other")
    assert "INTENT_REQUIRED" in other["hookSpecificOutput"]["additionalContext"]
    assert "Retained legacy goal" not in json.dumps(other)


def test_wrong_legacy_owner_cannot_be_imported(lane, monkeypatch, capsys, plain_repo):
    legacy = legacy_fixture(lane, plain_repo, wrong_owner=True)
    before = legacy.read_bytes()
    result = hook(lane, monkeypatch, capsys, plain_repo, "owner")
    assert "LEGACY_OWNER_MISMATCH" in result["hookSpecificOutput"]["additionalContext"]
    assert not (lane._session_dir("owner") / "state.json").exists()
    assert legacy.read_bytes() == before


def test_real_plugin_launcher_covers_child_event(lane, plain_repo):
    result = shared._plugin_hook(plain_repo, "SubagentStart", {
        "cwd": str(plain_repo), "hook_event_name": "SubagentStart",
        "session_id": "parent-launcher", "agent_id": "child-launcher"})
    assert result["hookSpecificOutput"]["hookEventName"] == "SubagentStart"
    assert "INTENT_REQUIRED" in result["hookSpecificOutput"]["additionalContext"]
    assert lane._read_scope("child-launcher")["activation_id"] is None
    assert not lane._scope_path("parent-launcher").exists()


def test_unborn_git_task_can_initialize_without_claiming_a_git_checkpoint(lane, tmp_path):
    root = tmp_path / "unborn"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    activate(root, "unborn", "Start requested project")
    state = command(root, "unborn", "status")
    assert state["workspace_kind"] == "git" and state["boundary"]["observed_git_head"] is None
    with pytest.raises(lane.RuntimeHookError, match="干净 Git 检查点"):
        lane.record_review(SimpleNamespace(root=root, session_id="unborn", scope="main", stage="final",
                                          result="PASS", reference="not-yet-committed", next_objective=""))


def test_lost_scope_recovers_only_the_same_task_state(lane, monkeypatch, capsys, plain_repo):
    active = activate(plain_repo, "same-task", "Retain own intent")
    before = (lane._session_dir("same-task") / "state.json").read_bytes()
    lane._scope_path("same-task").unlink()
    output = hook(lane, monkeypatch, capsys, plain_repo, "same-task", source="resume")
    assert active["activation_id"] in output["hookSpecificOutput"]["additionalContext"]
    assert (lane._session_dir("same-task") / "state.json").read_bytes() == before


def test_legacy_tracked_state_is_refused_even_for_matching_owner(lane, monkeypatch, capsys, plain_repo):
    legacy = legacy_fixture(lane, plain_repo)
    subprocess.run(["git", "-C", str(plain_repo), "add", "-f", str(legacy)], check=True)
    before = legacy.read_bytes()
    output = hook(lane, monkeypatch, capsys, plain_repo, "owner")
    assert "未被跟踪且已被 Git 忽略" in output["hookSpecificOutput"]["additionalContext"]
    assert legacy.read_bytes() == before
    assert not (lane._session_dir("owner") / "state.json").exists()


def test_existing_other_workspace_state_never_gets_automatically_reanchored(lane, monkeypatch, capsys, plain_repo, tmp_path):
    activate(plain_repo, "moving-task", "Stay with verified workspace")
    state = lane._session_dir("moving-task") / "state.json"
    before = state.read_bytes()
    lane._scope_path("moving-task").unlink()
    peer = tmp_path / "different"
    peer.mkdir()
    output = hook(lane, monkeypatch, capsys, peer, "moving-task")
    assert "CONTEXT_MISMATCH" in output["systemMessage"]
    assert not lane._scope_path("moving-task").exists()
    assert state.read_bytes() == before


@pytest.mark.parametrize("projectless", [False, True])
def test_checkpoint_and_jev_receipt_belong_to_one_session(lane, monkeypatch, plain_repo, tmp_path, projectless):
    import test_runtimehook_checkpoints as examples
    root = tmp_path / "document-task" if projectless else plain_repo
    root.mkdir(exist_ok=True)
    for sid in ["review-one", "review-two"]:
        activate(root, sid, "Verify bounded output")
    spec = root / "checkpoints.json"
    spec.write_text(json.dumps({"schema": "3can.checkpoints/v1", "final_checkpoint": "final",
                               "checkpoints": [examples.point("final")]}), encoding="utf-8")
    packet = root / "evidence.json"
    packet.write_text(json.dumps({
        "latest_user_request": "Verify bounded output",
        "claims": [{"id": "C1", "criterion_id": "A01", "text": "Three checks passed", "evidence_ids": ["E1"]}],
        "evidence": [{"id": "E1", "kind": "tool_output", "excerpt": "3 passed, 0 failed"}],
        "next_step": "Report the observed result", "parameters": {"tests_passed": 3},
    }), encoding="utf-8")
    lane.record_checkpoint(SimpleNamespace(root=root, native_cwd=root, session_id="review-one", spec=spec,
        checkpoint_id="final", packet=packet, kind="stage", label="", next_objective="Review output"))
    calls = []
    monkeypatch.setattr(lane.checkpoints.jev, "request_gateway", lambda r, t: calls.append(r) or examples.response(r))
    args = SimpleNamespace(root=root, native_cwd=root, session_id="review-one", scope="main",
                           stage="final" if projectless else "episode", result="PASS",
                           reference="fixture evidence", next_objective="Report result", timeout=10)
    assert lane.record_review(args)["result"] == "PASS" and len(calls) == 1
    args.session_id = "review-two"
    args.stage = "episode"
    with pytest.raises(lane.RuntimeHookError, match="CHECKPOINT_REQUIRED"):
        lane.record_review(args)
    assert len(calls) == 1
    assert (lane._session_dir("review-one") / "jev-observation.json").exists()
    assert not (lane._session_dir("review-two") / "jev-observation.json").exists()
    if projectless:
        assert lane._load_state(root, "review-one")["semantic_review"]["reviewed_git_head"] is None
