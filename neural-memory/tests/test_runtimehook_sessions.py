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


@pytest.mark.parametrize("cli", [shared.PLUGIN_CLI, shared.PROJECT_KIT_CLI])
@pytest.mark.parametrize("change", ["workspace", "native_host", "state_workspace", "workspace_marker"])
def test_scope_refusal_names_its_owner_and_exact_context_without_mutation(
    lane, plain_repo, tmp_path, cli, change,
):
    sid = "scope-diagnostics"
    activate(plain_repo, sid, "Keep my verified intent")
    peer = tmp_path / "new-workspace"
    peer.mkdir()
    state_path = lane._session_dir(sid) / "state.json"
    scope_path = lane._scope_path(sid)
    if change == "state_workspace":
        scope_path.unlink()
    elif change == "workspace_marker":
        binding = lane._read_scope(sid)
        binding["git_marker"][1] += 1
        lane._save_scope(binding)
    before = state_path.read_bytes()
    scope_before = scope_path.read_bytes() if scope_path.exists() else None
    root = plain_repo if change in {"native_host", "workspace_marker"} else peer
    native = plain_repo if change == "workspace_marker" else peer
    completed = subprocess.run(
        [sys.executable, str(cli), "--root", str(root), "--native-cwd", str(native),
         "--session-id", sid, "status"], capture_output=True, timeout=30,
    )
    result = json.loads(completed.stdout.decode("utf-8"))
    assert completed.returncode == 2
    assert result["status"] == "UNAVAILABLE"
    assert result["error_code"] == "CONTEXT_MISMATCH"
    assert result["component"] == "3can-runtimehook" and result["operation"] == "status"
    assert result["local_work_blocked"] is False
    assert result["context"]["registered_workspace"] == str(plain_repo)
    assert result["context"]["requested_workspace"] == str(root)
    assert result["context"]["mismatch"] == change
    assert result["recovery"]["automatic"] is False
    assert result["recovery"]["action"] == {
        "native_host": "verify_host_binding", "workspace_marker": "verify_workspace_identity",
        "workspace": "workspace_migration_required", "state_workspace": "workspace_migration_required",
    }[change]
    assert str(plain_repo) in result["error"] and str(native) in result["error"]
    assert "Keep my verified intent" not in json.dumps(result)
    assert state_path.read_bytes() == before
    assert (scope_path.read_bytes() if scope_path.exists() else None) == scope_before


def test_native_scope_message_names_both_paths_and_never_blocks_other_work(
    lane, monkeypatch, capsys, plain_repo, tmp_path,
):
    activate(plain_repo, "native-scope-diagnostics", "Keep original intent")
    peer = tmp_path / "moved-native-host"
    peer.mkdir()
    result = hook(lane, monkeypatch, capsys, peer, "native-scope-diagnostics")
    message = result["hookSpecificOutput"]["additionalContext"]
    assert str(plain_repo) in message and str(peer) in message
    assert "当前任务的 RuntimeHook" in message and "独立安全工作" in message
    assert "decision" not in result


def test_foreign_state_identity_is_not_exposed_as_workspace_recovery(lane, plain_repo):
    sid = "foreign-state-diagnostics"
    activate(plain_repo, sid, "Own intent")
    state_path = lane._session_dir(sid) / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["session_id"] = "another-task"
    state["workspace"] = "foreign-private-workspace"
    state["run_intent"]["goal"] = "Foreign private intent"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    before = state_path.read_bytes()
    completed = subprocess.run(
        [sys.executable, str(shared.PLUGIN_CLI), "--root", str(plain_repo),
         "--native-cwd", str(plain_repo), "--session-id", sid, "status"],
        capture_output=True, timeout=30,
    )
    assert completed.returncode == 2
    result = json.loads(completed.stdout.decode("utf-8"))
    assert "CONTEXT_MISMATCH" in result["error"]
    assert "context" not in result and "recovery" not in result
    assert "foreign-private-workspace" not in json.dumps(result)
    assert "Foreign private intent" not in json.dumps(result)
    assert state_path.read_bytes() == before


@pytest.fixture
def migration_lane(lane, plain_repo, tmp_path, monkeypatch, capsys):
    """Synthetic shared-repository identity; live linked-worktree CLI is checked separately."""
    source, target = plain_repo, tmp_path / "new-workspace"
    target.mkdir()
    subprocess.run(["git", "init", "-q", str(target)], check=True)
    for root in (source, target):
        (root / ".agents").mkdir()
        (root / ".agents/project.json").write_text(json.dumps({
            "schema_version": 1, "project_id": "fixture", "project_namespace": "fixture",
            "project_root": ".", "git_repository": "example.invalid/fixture/repo"}), encoding="utf-8")
        shared._git(root, "remote", "add", "origin", "https://example.invalid/fixture/repo.git")
    shared._git(target, "config", "user.email", "test@example.invalid")
    shared._git(target, "config", "user.name", "Migration Fixture")
    shared._git(target, "add", ".agents/project.json")
    shared._git(target, "commit", "-qm", "fixture")
    settings = {"client_path": str(shared.PROJECT_KIT_CLI.with_name("3can_codex.py")),
                "selector_path": str(tmp_path / "unused-selector.json"), "base_url": "http://127.0.0.1:9700"}
    monkeypatch.setattr(lane.writeback_adapter, "config", lambda: settings)
    original_common = lane._migration_common_dir
    monkeypatch.setattr(lane, "_migration_common_dir", lambda root: original_common(source))
    sid = "migration-fixture"
    activate(source, sid, "Original retained goal")
    state = lane._load_state(source, sid)
    state["temporary_task"] = {"goal": "Retain temporary work", "acceptance": [{"id": "T01", "text": "Keep evidence"}],
                               "reference": "fixture", "resume_objective": "Return to original"}
    state["knowledge"] = {**lane.writeback_adapter.connection(settings, source, agent_id="fixture-agent",
                          workorder_id="fixture-workorder", node_id="DOC-fixture"), "session_id": sid}
    state["semantic_review"] = {"stage": "episode", "result": "PASS", "reference": "old-proof",
                                "reviewed_git_head": None, "jev_request_sha256": "old-opinion"}
    lane._write_state(source, state, sid)
    history = lane._session_dir(sid) / "checkpoint.temporary.old.json"
    history.write_bytes(b'{"retained":"historical evidence"}\n')
    plan = lane._session_dir(sid) / "workspace-migration-fixture.json"

    def invoke(*arguments, expected=0):
        code = lane.main(["--root", str(target), "--native-cwd", str(target), "--session-id", sid,
                          "migrate-workspace", "--plan", str(plan), *arguments])
        result = json.loads(capsys.readouterr().out)
        assert code == expected, result
        return result
    capsys.readouterr()
    return SimpleNamespace(controller=lane, source=source, target=target, sid=sid, state=state,
                           plan=plan, history=history, invoke=invoke, original_common=original_common)


def prepare_migration(fixture):
    return fixture.invoke("--to", str(fixture.target), "--target-native-cwd", str(fixture.target),
                          "--reference", "fixture:explicit-owner-migration")


def test_workspace_migration_preserves_identity_history_and_invalidates_review(migration_lane):
    f = migration_lane
    paths = [f.controller._session_dir(f.sid) / "state.json", f.controller._scope_path(f.sid), f.history]
    before = [path.read_bytes() for path in paths]
    assert prepare_migration(f)["status"] == "MIGRATION_PREPARED"
    assert [path.read_bytes() for path in paths] == before
    result = f.invoke("--apply")
    assert result["status"] == "MIGRATION_APPLIED" and result["graph_write_performed"] is False
    moved = f.controller._load_state(f.target, f.sid)
    for name in ("activation_id", "run_intent", "temporary_task"):
        assert moved[name] == f.state[name]
    assert moved["knowledge"]["workspace_id"] != f.state["knowledge"]["workspace_id"]
    for name in ("agent_id", "workorder_id", "node_id", "session_id"):
        assert moved["knowledge"][name] == f.state["knowledge"][name]
    assert moved["semantic_review"]["result"] == "PENDING"
    assert moved["boundary"]["sequence"] == f.state["boundary"]["sequence"] + 1
    assert f.controller._check_scope({"cwd": str(f.target), "session_id": f.sid})[0] == f.target
    assert f.history.read_bytes() == before[2]
    after = [path.read_bytes() for path in paths]
    assert f.invoke("--apply")["status"] == "MIGRATION_ALREADY_APPLIED"
    assert [path.read_bytes() for path in paths] == after
    assert f.invoke("--rollback")["status"] == "MIGRATION_ROLLED_BACK"
    assert [path.read_bytes() for path in paths] == before


@pytest.mark.parametrize("change", ["state", "plan", "project", "repository", "expired"])
def test_workspace_migration_refuses_stale_or_foreign_inputs(migration_lane, monkeypatch, change):
    f = migration_lane
    prepare_migration(f)
    if change == "state":
        state = f.controller._load_state(f.source, f.sid)
        state["current_episode"] = "New actual work"
        f.controller._write_state(f.source, state, f.sid)
    elif change in {"plan", "expired"}:
        plan = json.loads(f.plan.read_text(encoding="utf-8"))
        if change == "plan":
            plan["after"]["state"] = plan["after"]["state"].replace("Original retained goal", "Replaced goal")
        else:
            plan["created_at"] = "2020-01-01T00:00:00+00:00"
        f.plan.write_text(json.dumps(plan), encoding="utf-8")
    elif change == "project":
        capsule = f.target / ".agents/project.json"
        value = json.loads(capsule.read_text())
        value["project_namespace"] = "foreign"
        capsule.write_text(json.dumps(value))
    else:
        monkeypatch.setattr(f.controller, "_migration_common_dir", f.original_common)
    paths = [f.controller._session_dir(f.sid) / "state.json", f.controller._scope_path(f.sid), f.history]
    before = [path.read_bytes() for path in paths]
    result = f.invoke("--apply", expected=2)
    assert "MIGRATION_" in result["error"]
    assert [path.read_bytes() for path in paths] == before


def test_workspace_migration_second_write_failure_restores_exact_bytes(migration_lane, monkeypatch):
    f = migration_lane
    prepare_migration(f)
    paths = [f.controller._session_dir(f.sid) / "state.json", f.controller._scope_path(f.sid)]
    before = [path.read_bytes() for path in paths]
    replace = f.controller._migration_replace
    failures = []
    def fail_once(path, raw):
        if path == paths[1] and not failures:
            failures.append(path)
            raise OSError("injected scope write failure")
        replace(path, raw)
    monkeypatch.setattr(f.controller, "_migration_replace", fail_once)
    result = f.invoke("--apply", expected=2)
    assert "MIGRATION_APPLY_FAILED_ROLLED_BACK" in result["error"]
    assert [path.read_bytes() for path in paths] == before
    assert f.plan.exists()
    receipt = json.loads(f.plan.with_name(f.plan.stem + ".receipt.json").read_text())
    assert receipt["status"] == "MIGRATION_APPLY_FAILED_ROLLED_BACK"


def test_workspace_migration_rollback_keeps_later_work_and_rejects_tampered_plan(migration_lane):
    f = migration_lane
    prepare_migration(f)
    f.invoke("--apply")
    state = f.controller._load_state(f.target, f.sid)
    state["current_episode"] = "Work after migration"
    f.controller._write_state(f.target, state, f.sid)
    path = f.controller._session_dir(f.sid) / "state.json"
    before = path.read_bytes()
    assert "MIGRATION_ROLLBACK_CONFLICT" in f.invoke("--rollback", expected=2)["error"]
    assert path.read_bytes() == before
    plan = json.loads(f.plan.read_text(encoding="utf-8"))
    plan["before"]["state"] += " "
    f.plan.write_text(json.dumps(plan), encoding="utf-8")
    assert "MIGRATION_ROLLBACK_RECEIPT_MISMATCH" in f.invoke("--rollback", expected=2)["error"]
    assert path.read_bytes() == before


def test_native_task_lock_prevents_concurrent_cli_mutation(lane, plain_repo):
    sid = "locked-task"
    activate(plain_repo, sid, "Retained intent")
    path = lane._session_dir(sid) / "state.json"
    before = path.read_bytes()
    with lane._session_lock(sid):
        result = subprocess.run([sys.executable, str(shared.PLUGIN_CLI), "--root", str(plain_repo),
                                 "--native-cwd", str(plain_repo), "--session-id", sid, "off"],
                                capture_output=True, timeout=10)
    assert result.returncode == 2 and "SESSION_BUSY" in result.stdout.decode("utf-8")
    assert path.read_bytes() == before


def test_operator_migration_requires_fresh_idle_observation_and_keeps_operator_state(migration_lane, capsys, tmp_path):
    f = migration_lane
    actor = "migration-operator"
    activate(f.source, actor, "Independent operator goal")
    own = f.controller._session_dir(actor) / "state.json"
    own_before = own.read_bytes()
    plan = own.parent / "workspace-migration-peer.json"
    base = ["--root", str(f.source), "--native-cwd", str(f.source), "--session-id", actor,
            "migrate-workspace", "--plan", str(plan), "--target-session-id", f.sid]
    assert f.controller.main(base + ["--to", str(f.target), "--target-native-cwd", str(f.target),
                                    "--reference", "fixture:Owner-authorized-operator"]) == 0
    capsys.readouterr()
    assert f.controller.main(base + ["--apply"]) == 2
    assert "MIGRATION_FRESH_IDLE_OBSERVATION_REQUIRED" in capsys.readouterr().out
    observation = tmp_path / "observation.json"
    import time
    observed = {"source": "mcp__codex_app.read_thread", "observed_at_epoch": time.time(),
                "response": {"thread": {"id": f.sid, "cwd": str(f.target), "status": {"activeFlags": []}},
                             "turns": [{"status": "inProgress"}]}}
    observation.write_text(json.dumps(observed))
    assert f.controller.main(base + ["--apply", "--observation", str(observation)]) == 2
    assert "MIGRATION_TARGET_NOT_VERIFIED_IDLE" in capsys.readouterr().out
    observed["response"]["turns"][0]["status"] = "completed"
    observation.write_text(json.dumps(observed))
    assert f.controller.main(base + ["--apply", "--observation", str(observation)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["actor_session_id"] == actor and result["target_session_id"] == f.sid
    assert own.read_bytes() == own_before
    assert f.controller._load_state(f.target, f.sid)["activation_id"] == f.state["activation_id"]


@pytest.mark.parametrize("change", ["missing_marker", "invalid_marker", "missing_identity"])
def test_workspace_migration_rejects_malformed_plan_without_traceback(migration_lane, change):
    f = migration_lane
    prepare_migration(f)
    plan = json.loads(f.plan.read_text(encoding="utf-8"))
    if change == "missing_marker":
        del plan["source_marker"]
    elif change == "invalid_marker":
        plan["target_marker"] = None
    else:
        del plan["target_identity"]
    f.plan.write_text(json.dumps(plan), encoding="utf-8")
    assert "MIGRATION_PLAN_INVALID" in f.invoke("--apply", expected=2)["error"]
    assert f.controller._load_state(f.source, f.sid)["activation_id"] == f.state["activation_id"]


def test_workspace_migration_can_rollback_from_original_native_workspace(migration_lane, capsys):
    f = migration_lane
    paths = [f.controller._session_dir(f.sid) / "state.json", f.controller._scope_path(f.sid)]
    before = [path.read_bytes() for path in paths]
    prepare_migration(f)
    f.invoke("--apply")
    assert f.controller.main(["--root", str(f.source), "--native-cwd", str(f.source),
                              "--session-id", f.sid, "migrate-workspace", "--plan", str(f.plan),
                              "--rollback"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "MIGRATION_ROLLED_BACK"
    assert [path.read_bytes() for path in paths] == before


def test_workspace_migration_failure_never_overwrites_unknown_concurrent_state(migration_lane, monkeypatch):
    f = migration_lane
    prepare_migration(f)
    state_path = f.controller._session_dir(f.sid) / "state.json"
    scope_path = f.controller._scope_path(f.sid)
    scope_before = scope_path.read_bytes()
    replace = f.controller._migration_replace
    concurrent = []

    def fail_after_external_write(path, raw):
        if path == scope_path:
            value = json.loads(state_path.read_text(encoding="utf-8"))
            value["current_episode"] = "Concurrent external work must survive"
            state_path.write_text(json.dumps(value), encoding="utf-8")
            concurrent.append(state_path.read_bytes())
            raise OSError("injected competing writer during scope failure")
        replace(path, raw)

    monkeypatch.setattr(f.controller, "_migration_replace", fail_after_external_write)
    assert "MIGRATION_RECOVERY_REQUIRED" in f.invoke("--apply", expected=2)["error"]
    assert state_path.read_bytes() == concurrent[0] and scope_path.read_bytes() == scope_before
    assert "MIGRATION_ROLLBACK_CONFLICT" in f.invoke("--rollback", expected=2)["error"]
    assert state_path.read_bytes() == concurrent[0] and scope_path.read_bytes() == scope_before
