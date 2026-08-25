from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = PACKAGE_ROOT / "plugins" / "3can-runtimehook"
SKILL_ROOT = PLUGIN_ROOT / "skills" / "3can-runtimehook"
PLUGIN_CLI = SKILL_ROOT / "scripts" / "3can_runtimehook.py"
WINDOWS_LAUNCHER = PLUGIN_ROOT / "hooks" / "run_runtimehook.ps1"
PROJECT_KIT_CLI = (
    PACKAGE_ROOT
    / "examples"
    / "codex-cli-project-kit"
    / "scripts"
    / "3can_runtimehook.py"
)
STATE_PATH = Path(".codex/runtimehook/state.json")


def _git(
    root: Path,
    *arguments: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


@pytest.fixture
def plain_repo(tmp_path: Path) -> Path:
    root = tmp_path / "plain-repo"
    root.mkdir()
    (root / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "RuntimeHook Plugin Test")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "-qm", "baseline")
    return root


def _controller(root: Path, *arguments: str) -> tuple[int, dict]:
    completed = subprocess.run(
        [
            sys.executable,
            str(PLUGIN_CLI),
            "--root",
            str(root),
            *arguments,
        ],
        cwd=root,
        capture_output=True,
        timeout=30,
    )
    return completed.returncode, json.loads(completed.stdout.decode("utf-8"))


def _activate(root: Path, *, goal: str = "交付当前开源任务。") -> dict:
    return_code, output = _controller(
        root,
        "on",
        "--goal",
        goal,
        "--acceptance",
        "A01=结果满足公开契约。",
        "--intensity",
        "light",
        "--reason",
        "任务小而明确。",
    )
    assert return_code == 0, output
    return output


def _plugin_hook(cwd: Path, event: str, payload: dict) -> dict:
    definitions = json.loads(
        (PLUGIN_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8")
    )["hooks"][event]
    handlers = [hook for group in definitions for hook in group["hooks"]]
    assert len(handlers) == 1
    command = handlers[0]["commandWindows" if os.name == "nt" else "command"]
    environment = {**os.environ, "PLUGIN_ROOT": str(PLUGIN_ROOT)}
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        capture_output=True,
        shell=True,
        timeout=30,
    )
    stdout = completed.stdout.decode("utf-8")
    stderr = completed.stderr.decode("utf-8")
    assert completed.returncode == 0, stdout + stderr
    return json.loads(stdout) if stdout.strip() else {}


def test_plugin_activation_bootstraps_only_local_git_exclude(
    plain_repo: Path,
):
    first = _activate(plain_repo)
    second = _activate(plain_repo, goal="交付第二个公开任务。")
    exclude_path = Path(
        _git(
            plain_repo,
            "rev-parse",
            "--path-format=absolute",
            "--git-path",
            "info/exclude",
        ).stdout.strip()
    )
    exclude = exclude_path.read_text(encoding="utf-8")

    assert first["local_exclude_added"] is True
    assert second["local_exclude_added"] is False
    assert exclude.count("/.codex/runtimehook/") == 1
    assert _git(
        plain_repo,
        "check-ignore",
        "-q",
        "--",
        STATE_PATH.as_posix(),
        check=False,
    ).returncode == 0
    assert _git(plain_repo, "status", "--porcelain").stdout == ""
    assert (plain_repo / STATE_PATH).is_file()
    assert not (plain_repo / ".gitignore").exists()


def test_plugin_rejects_tracked_state_before_local_git_mutation(
    plain_repo: Path,
):
    state_root = plain_repo / STATE_PATH.parent
    state_root.mkdir(parents=True)
    marker = state_root / "tracked.txt"
    marker.write_text("project truth\n", encoding="utf-8")
    _git(plain_repo, "add", "-f", marker.relative_to(plain_repo).as_posix())
    _git(plain_repo, "commit", "-qm", "track conflicting state root")
    exclude_path = Path(
        _git(
            plain_repo,
            "rev-parse",
            "--path-format=absolute",
            "--git-path",
            "info/exclude",
        ).stdout.strip()
    )
    before = exclude_path.read_bytes()

    return_code, output = _controller(
        plain_repo,
        "on",
        "--goal",
        "Preserve tracked project truth.",
        "--acceptance",
        "A01=Tracked truth remains unchanged.",
        "--intensity",
        "light",
        "--reason",
        "Small task.",
    )

    assert return_code == 2
    assert output["status"] == "UNAVAILABLE"
    assert "untracked and Git ignored" in output["error"]
    assert exclude_path.read_bytes() == before
    assert marker.read_text(encoding="utf-8") == "project truth\n"
    assert not (plain_repo / STATE_PATH).exists()


def test_plugin_hooks_are_silent_until_active_and_resolve_nested_cwd(
    plain_repo: Path,
):
    nested = plain_repo / "nested"
    nested.mkdir()
    start_payload = {
        "hook_event_name": "SessionStart",
        "source": "compact",
        "cwd": str(nested),
    }

    assert _plugin_hook(nested, "SessionStart", start_payload) == {}
    _activate(plain_repo)
    started = _plugin_hook(nested, "SessionStart", start_payload)
    stopped = _plugin_hook(
        nested,
        "Stop",
        {"hook_event_name": "Stop", "cwd": str(nested)},
    )

    context = started["hookSpecificOutput"]["additionalContext"]
    assert "交付当前开源任务" in context
    assert "Semantic review: PENDING" in context
    assert stopped["decision"] == "block"
    assert "final semantic review is due" in stopped["reason"]


def test_plugin_hook_is_silent_outside_git(tmp_path: Path):
    assert _plugin_hook(
        tmp_path,
        "SessionStart",
        {
            "hook_event_name": "SessionStart",
            "source": "startup",
            "cwd": str(tmp_path),
        },
    ) == {}


@pytest.mark.skipif(os.name != "nt", reason="Windows executable lookup contract")
def test_windows_plugin_ignores_repo_local_executable_shadows(
    plain_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    system_command = Path(os.environ["COMSPEC"])
    malicious_bin = plain_repo / "bin"
    malicious_bin.mkdir()
    for name in ("git.exe", "python.exe", "powershell.exe"):
        shutil.copyfile(system_command, plain_repo / name)
        shutil.copyfile(system_command, malicious_bin / name)
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join((str(malicious_bin), str(plain_repo), os.environ["PATH"])),
    )
    nested = plain_repo / "nested"
    nested.mkdir()

    _activate(plain_repo)
    started = _plugin_hook(
        nested,
        "SessionStart",
        {
            "hook_event_name": "SessionStart",
            "source": "resume",
            "cwd": str(nested),
        },
    )

    context = started["hookSpecificOutput"]["additionalContext"]
    assert "交付当前开源任务" in context


def test_plugin_package_is_repo_installable_and_has_one_runtime_owner():
    marketplace = json.loads(
        (
            PACKAGE_ROOT / ".agents" / "plugins" / "marketplace.json"
        ).read_text(encoding="utf-8")
    )
    manifest = json.loads(
        (PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(
            encoding="utf-8"
        )
    )
    hooks = json.loads(
        (PLUGIN_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8")
    )["hooks"]
    skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    skill_ui = (SKILL_ROOT / "agents" / "openai.yaml").read_text(
        encoding="utf-8"
    )
    windows_launcher = WINDOWS_LAUNCHER.read_text(encoding="utf-8")

    assert marketplace["name"] == "3can-engine"
    assert marketplace["plugins"] == [
        {
            "name": "3can-runtimehook",
            "source": {
                "source": "local",
                "path": "./plugins/3can-runtimehook",
            },
            "policy": {
                "installation": "AVAILABLE",
                "authentication": "ON_INSTALL",
            },
            "category": "Productivity",
        }
    ]
    assert manifest["name"] == "3can-runtimehook"
    assert manifest["skills"] == "./skills/"
    assert "hooks" not in manifest
    assert manifest["repository"].endswith("/3CAN-engine")
    assert set(hooks) == {
        "SessionStart",
        "UserPromptSubmit",
        "PostToolUse",
        "Stop",
    }
    for event in hooks.values():
        handlers = [hook for group in event for hook in group["hooks"]]
        assert len(handlers) == 1
        assert "PLUGIN_ROOT" in handlers[0]["command"]
        assert "3can_runtimehook.py" in handlers[0]["command"]
        assert handlers[0]["commandWindows"].startswith(
            "%SystemRoot%\\System32\\WindowsPowerShell\\v1.0\\powershell.exe "
        )
        assert "run_runtimehook.ps1" in handlers[0]["commandWindows"]
        assert "statusMessage" not in handlers[0]
    assert hooks["SessionStart"][0]["hooks"][0][
        "additionalContextLimit"
    ] == 5000
    assert hooks["UserPromptSubmit"][0]["hooks"][0][
        "additionalContextLimit"
    ] == 5000
    assert hooks["PostToolUse"][0]["matcher"] == "^(Bash|update_plan)$"
    assert "do not require or\ncopy a controller" in skill
    assert "allow_implicit_invocation: true" in skill_ui
    assert "NoDefaultCurrentDirectoryInExePath" in windows_launcher
    assert 'Join-Path $cursor.FullName ".git"' in windows_launcher
    assert PLUGIN_CLI.read_bytes() == PROJECT_KIT_CLI.read_bytes()
