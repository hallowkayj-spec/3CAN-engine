from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_KIT = PACKAGE_ROOT / "examples" / "codex-cli-project-kit"
STATE_PATH = Path(".codex/runtimehook/state.json")


@pytest.fixture
def runtimehook_project(tmp_path: Path):
    installed = tmp_path / "runtimehook-project"
    shutil.copytree(PROJECT_KIT, installed)
    shutil.rmtree(installed / "test-results", ignore_errors=True)
    shutil.copyfile(installed / ".gitignore.template", installed / ".gitignore")
    (installed / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(installed)], check=True)
    subprocess.run(
        ["git", "-C", str(installed), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(installed), "config", "user.name", "RuntimeHook Test"],
        check=True,
    )
    subprocess.run(["git", "-C", str(installed), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(installed), "commit", "-qm", "installed kit"],
        check=True,
    )
    hooks = json.loads(
        (installed / ".codex" / "hooks.json").read_text(encoding="utf-8")
    )["hooks"]

    def command(*arguments: str) -> tuple[subprocess.CompletedProcess[bytes], dict]:
        completed = subprocess.run(
            [
                sys.executable,
                str(installed / "scripts" / "3can_runtimehook.py"),
                "--root",
                str(installed),
                *arguments,
            ],
            cwd=installed,
            capture_output=True,
            timeout=30,
        )
        output = json.loads(completed.stdout.decode("utf-8"))
        return completed, output

    def native_hook(event: str, payload: dict) -> dict:
        definitions = [
            hook
            for group in hooks[event]
            for hook in group["hooks"]
            if "3can_runtimehook.py" in hook["command"]
        ]
        assert len(definitions) == 1
        definition = definitions[0]
        native_command = definition["commandWindows" if os.name == "nt" else "command"]
        completed = subprocess.run(
            native_command,
            cwd=installed / "scripts",
            input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            capture_output=True,
            shell=True,
            timeout=30,
        )
        stdout = completed.stdout.decode("utf-8")
        stderr = completed.stderr.decode("utf-8")
        assert completed.returncode == 0, stdout + stderr
        return json.loads(stdout) if stdout.strip() else {}

    return installed, hooks, command, native_hook


def _activate(command, *, intensity: str = "medium", goal: str = "交付当前任务。"):
    completed, output = command(
        "on",
        "--goal",
        goal,
        "--acceptance",
        "A01=结果满足当前用户要求。",
        "--non-goal",
        "不得修改独立的生产门禁。",
        "--intensity",
        intensity,
        "--reason",
        "任务跨越多个有意义阶段。" if intensity != "light" else "任务小而明确。",
    )
    assert completed.returncode == 0, output
    return output


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_head(root: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_runtimehook_off_default_is_silent_and_hooks_are_independent(
    runtimehook_project,
):
    installed, hooks, command, native_hook = runtimehook_project

    completed, output = command("status")

    assert completed.returncode == 0
    assert output == {"ok": True, "status": "inactive"}
    assert (
        native_hook(
            "SessionStart", {"hook_event_name": "SessionStart", "source": "startup"}
        )
        == {}
    )
    assert native_hook("Stop", {"hook_event_name": "Stop"}) == {}
    assert not (installed / STATE_PATH).exists()
    for event in ("SessionStart", "Stop"):
        commands = [
            hook["command"] for group in hooks[event] for hook in group["hooks"]
        ]
        assert sum("3can_convergence.py" in item for item in commands) == 1
        assert sum("3can_runtimehook.py" in item for item in commands) == 1
    runtime_start = next(
        hook
        for group in hooks["SessionStart"]
        for hook in group["hooks"]
        if "3can_runtimehook.py" in hook["command"]
    )
    assert runtime_start["additionalContextLimit"] == 5000


@pytest.mark.parametrize("intensity", ["light", "medium", "max"])
def test_runtimehook_records_agent_selected_intensity_and_reinjects_utf8(
    runtimehook_project, intensity: str
):
    installed, _hooks, command, native_hook = runtimehook_project

    activation = _activate(command, intensity=intensity)
    started = native_hook(
        "SessionStart",
        {
            "hook_event_name": "SessionStart",
            "source": "clear",
            "cwd": "C:/中文项目/子目录",
        },
    )
    stopped = native_hook("Stop", {"hook_event_name": "Stop"})
    state = json.loads((installed / STATE_PATH).read_text(encoding="utf-8"))

    assert activation["internal_intensity"]["level"] == intensity
    assert state["run_intent"]["goal"] == "交付当前任务。"
    assert state["semantic_review"]["result"] == "PENDING"
    session_context = started["hookSpecificOutput"]
    assert session_context["hookEventName"] == "SessionStart"
    assert "交付当前任务" in session_context["additionalContext"]
    assert "不得修改独立的生产门禁" in session_context["additionalContext"]
    assert "hardcoded" in session_context["additionalContext"]
    assert "final semantic review remains due" in stopped["systemMessage"]
    assert "decision" not in stopped
    assert not (installed / ".codex" / "convergence.json").exists()
    assert not (
        installed / ".codex" / "task-hooks" / "runtimehook-current.json"
    ).exists()


def test_runtimehook_episode_and_final_review_record_narrow_git_anchor(
    runtimehook_project,
):
    installed, _hooks, command, native_hook = runtimehook_project
    activation = _activate(command)

    episode, episode_output = command(
        "review",
        "--stage",
        "episode",
        "--result",
        "PARTIAL",
        "--reference",
        "git:episode-review",
        "--next-objective",
        "完成剩余验收项。",
    )
    completed, state = command("status")
    final, final_output = command(
        "review",
        "--stage",
        "final",
        "--result",
        "PASS",
        "--reference",
        "pr:15-final-review",
    )
    _status, reviewed_state = command("status")
    stopped = native_hook("Stop", {"hook_event_name": "Stop"})

    assert episode.returncode == 0, episode_output
    assert completed.returncode == 0
    assert state["activation_id"] == activation["activation_id"]
    assert state["current_episode"] == "完成剩余验收项。"
    assert final.returncode == 0, final_output
    assert final_output["result"] == "PASS"
    assert reviewed_state["semantic_review"]["reviewed_git_head"] == _git_head(
        installed
    )
    assert stopped == {}


def test_runtimehook_final_pass_requires_clean_git_checkpoint(runtimehook_project):
    installed, _hooks, command, _native_hook = runtimehook_project
    _activate(command)
    (installed / "tracked.txt").write_text("changed before review\n", encoding="utf-8")

    completed, output = command(
        "review",
        "--stage",
        "final",
        "--result",
        "PASS",
        "--reference",
        "git:dirty-review",
    )
    _status, state = command("status")

    assert completed.returncode == 2
    assert output["status"] == "UNAVAILABLE"
    assert "clean Git checkpoint" in output["error"]
    assert state["semantic_review"]["result"] == "PENDING"


@pytest.mark.parametrize(
    ("change", "session_source"), [("dirty", "resume"), ("new-head", "compact")]
)
def test_runtimehook_marks_post_review_git_change_stale(
    runtimehook_project, change: str, session_source: str
):
    installed, _hooks, command, native_hook = runtimehook_project
    _activate(command)
    reviewed, output = command(
        "review",
        "--stage",
        "final",
        "--result",
        "PASS",
        "--reference",
        "git:clean-review",
    )
    assert reviewed.returncode == 0, output

    (installed / "tracked.txt").write_text("changed after review\n", encoding="utf-8")
    if change == "new-head":
        subprocess.run(
            ["git", "-C", str(installed), "add", "tracked.txt"], check=True
        )
        subprocess.run(
            ["git", "-C", str(installed), "commit", "-qm", "post-review change"],
            check=True,
        )

    started = native_hook(
        "SessionStart",
        {"hook_event_name": "SessionStart", "source": session_source},
    )
    stopped = native_hook("Stop", {"hook_event_name": "Stop"})

    context = started["hookSpecificOutput"]["additionalContext"]
    assert "Semantic review: STALE" in context
    assert "Semantic review: PASS" not in context
    assert "STALE" in stopped["systemMessage"]
    expected_reason = "worktree is dirty" if change == "dirty" else "Git HEAD changed"
    assert expected_reason in stopped["systemMessage"]
    assert "decision" not in stopped


def test_runtimehook_off_retains_state_without_touching_independent_gate(
    runtimehook_project,
):
    installed, _hooks, command, native_hook = runtimehook_project
    _activate(command, intensity="light")
    reviewed, output = command(
        "review",
        "--stage",
        "final",
        "--result",
        "PASS",
        "--reference",
        "git:reviewed-commit",
    )
    assert reviewed.returncode == 0, output
    independent = installed / ".codex" / "convergence.json"
    independent.write_text('{"independent":"gate"}\n', encoding="utf-8")
    independent_before = _sha256(independent)

    disabled, result = command("off")
    repeated, repeated_result = command("off")
    state = json.loads((installed / STATE_PATH).read_text(encoding="utf-8"))

    assert disabled.returncode == 0, result
    assert result["status"] == "disabled_by_owner"
    assert repeated.returncode == 0
    assert repeated_result["changed"] is False
    assert state["semantic_review"]["reference"] == "git:reviewed-commit"
    assert _sha256(independent) == independent_before
    assert native_hook("Stop", {"hook_event_name": "Stop"}) == {}


def test_runtimehook_new_intent_replaces_only_current_semantic_state(
    runtimehook_project,
):
    installed, _hooks, command, _native_hook = runtimehook_project
    first = _activate(command, goal="完成第一个任务。")
    second = _activate(command, intensity="light", goal="完成第二个任务。")
    state = json.loads((installed / STATE_PATH).read_text(encoding="utf-8"))

    assert first["activation_id"] != second["activation_id"]
    assert state["run_intent"]["goal"] == "完成第二个任务。"
    assert state["semantic_review"]["result"] == "PENDING"
    assert [path.name for path in (installed / STATE_PATH.parent).iterdir()] == [
        "state.json"
    ]


def test_runtimehook_rejects_tracked_state_root_before_writing(runtimehook_project):
    installed, _hooks, command, _native_hook = runtimehook_project
    state_root = installed / STATE_PATH.parent
    state_root.mkdir()
    marker = state_root / "tracked.txt"
    marker.write_text("project truth\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(installed), "add", "-f", marker.relative_to(installed)],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(installed), "commit", "-qm", "track conflicting root"],
        check=True,
    )
    before = _sha256(marker)

    completed, output = command(
        "on",
        "--goal",
        "Do not overwrite project truth.",
        "--acceptance",
        "A01=Tracked truth remains unchanged.",
        "--intensity",
        "light",
        "--reason",
        "Small task.",
    )

    assert completed.returncode == 2
    assert output["status"] == "UNAVAILABLE"
    assert "untracked and Git ignored" in output["error"]
    assert _sha256(marker) == before
    assert not (installed / STATE_PATH).exists()


def test_runtimehook_rejects_redirected_state_root(runtimehook_project, tmp_path: Path):
    installed, _hooks, command, _native_hook = runtimehook_project
    outside = tmp_path / "outside"
    outside.mkdir()
    state_root = installed / STATE_PATH.parent
    try:
        state_root.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink is unavailable: {exc}")

    completed, output = command(
        "on",
        "--goal",
        "Keep state local.",
        "--acceptance",
        "A01=No outside write occurs.",
        "--intensity",
        "light",
        "--reason",
        "Small task.",
    )

    assert completed.returncode == 2
    assert output["status"] == "UNAVAILABLE"
    assert "direct directory" in output["error"]
    assert list(outside.iterdir()) == []


def test_runtimehook_malformed_state_is_non_owning_unavailable(runtimehook_project):
    installed, _hooks, command, native_hook = runtimehook_project
    _activate(command)
    state_path = installed / STATE_PATH
    state_path.write_text("{not-json", encoding="utf-8")
    before = _sha256(state_path)

    stopped = native_hook("Stop", {"hook_event_name": "Stop"})

    assert "UNAVAILABLE" in stopped["systemMessage"]
    assert "independent project and PR15 evidence gates" in stopped["systemMessage"]
    assert "decision" not in stopped
    assert _sha256(state_path) == before


def test_runtimehook_oversized_intent_fails_before_state_write(runtimehook_project):
    installed, _hooks, command, _native_hook = runtimehook_project

    completed, output = command(
        "on",
        "--goal",
        "x" * 13_000,
        "--acceptance",
        "A01=The bounded context remains usable.",
        "--intensity",
        "light",
        "--reason",
        "Small task.",
    )

    assert completed.returncode == 2
    assert output["status"] == "UNAVAILABLE"
    assert "too large" in output["error"]
    assert not (installed / STATE_PATH).exists()


def test_runtimehook_public_surface_is_semantic_and_not_a_second_kernel():
    skill = (
        PROJECT_KIT / "installable-skills" / "3can-runtimehook" / "SKILL.md"
    ).read_text(encoding="utf-8")
    docs = (PACKAGE_ROOT / "docs" / "RUNTIMEHOOK.md").read_text(encoding="utf-8")
    script = (PROJECT_KIT / "scripts" / "3can_runtimehook.py").read_text(
        encoding="utf-8"
    )

    assert "allow_implicit_invocation: true" in (
        PROJECT_KIT
        / "installable-skills"
        / "3can-runtimehook"
        / "agents"
        / "openai.yaml"
    ).read_text(encoding="utf-8")
    assert "semantic supervisor" in skill
    assert "does not own a convergence selector" in docs
    assert "does not register" in docs and "simulate `/3CAN`" in docs
    assert '"3can_convergence.py"' not in script
    assert "task_oracle" not in script
    assert "candidate_fingerprint" not in script
    assert "receipt_sha256" not in script
