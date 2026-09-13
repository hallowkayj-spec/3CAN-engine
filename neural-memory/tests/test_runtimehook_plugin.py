from __future__ import annotations

import json
import hashlib
import importlib.util
import os
import shutil
import subprocess
import sys
import time
import statistics
from pathlib import Path

import pytest


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = PACKAGE_ROOT / "plugins" / "3can-runtimehook"
SKILL_ROOT = PLUGIN_ROOT / "skills" / "3can-runtimehook"
PLUGIN_CLI = SKILL_ROOT / "scripts" / "3can_runtimehook.py"
WINDOWS_LAUNCHER = PLUGIN_ROOT / "hooks" / "run_runtimehook.ps1"
POSIX_LAUNCHER = PLUGIN_ROOT / "hooks" / "run_runtimehook.sh"
PROJECT_KIT_CLI = (
    PACKAGE_ROOT
    / "examples"
    / "codex-cli-project-kit"
    / "scripts"
    / "3can_runtimehook.py"
)
STATE_PATH = Path(".codex/runtimehook/state.json")


@pytest.fixture(autouse=True)
def isolated_scope_cache(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)


def _session_id(root: Path) -> str:
    root = root.resolve()
    root = next((p for p in (root, *root.parents) if (p / ".git").exists()), root)
    return "test-" + hashlib.sha256(str(root).encode()).hexdigest()[:20]


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
            "--session-id", _session_id(root),
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
        "--native-cwd", str(root),
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


def _plugin_hook(
    cwd: Path,
    event: str,
    payload: dict,
    *,
    search_path: str | None = None,
) -> dict:
    definitions = json.loads(
        (PLUGIN_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8")
    )["hooks"][event]
    handlers = [hook for group in definitions for hook in group["hooks"]]
    assert len(handlers) == 1
    command = handlers[0]["commandWindows" if os.name == "nt" else "command"]
    if os.name == "nt":
        command = [
            str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"),
            "-NoProfile", "-NonInteractive", "-Command", command,
        ]
    environment = {**os.environ, "PLUGIN_ROOT": str(PLUGIN_ROOT)}
    if search_path is not None:
        environment["PATH"] = search_path
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        input=json.dumps({"session_id": _session_id(cwd), **payload}, ensure_ascii=False).encode("utf-8"),
        capture_output=True,
        shell=os.name != "nt",
        timeout=30,
    )
    stdout = completed.stdout.decode("utf-8")
    stderr = completed.stderr.decode("utf-8")
    assert completed.returncode == 0, stdout + stderr
    return json.loads(stdout) if stdout.strip() else {}


@pytest.mark.skipif(os.name != "nt", reason="Windows native PowerShell contract")
@pytest.mark.parametrize("event", ["SessionStart", "PostToolUse"])
def test_windows_native_argv_and_spaced_plugin_path(tmp_path: Path, event: str):
    copied = tmp_path / "plugin with spaces"
    shutil.copytree(PLUGIN_ROOT, copied)
    definitions = json.loads((copied / "hooks/hooks.json").read_text(encoding="utf-8"))
    command = definitions["hooks"][event][0]["hooks"][0]["commandWindows"]
    payload = {"cwd": str(tmp_path), "hook_event_name": event, "source": "startup"}
    # Native Codex uses the selected PowerShell, not Python shell=True's cmd.
    completed = subprocess.run(
        [str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"),
         "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=tmp_path,
        env={**os.environ, "PLUGIN_ROOT": str(copied)},
        input=json.dumps(payload).encode("utf-8"),
        capture_output=True,
        timeout=15,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    if event == "SessionStart":
        assert "3CAN fast path" in json.loads(completed.stdout)["hookSpecificOutput"]["additionalContext"]
    else:
        assert not completed.stdout.strip()


@pytest.mark.skipif(os.name != "nt", reason="Windows launcher contract")
def test_windows_plugin_uses_py_launcher_when_python_names_are_absent(
    tmp_path: Path,
):
    system_py = shutil.which("py.exe")
    if system_py is None:
        pytest.skip("Windows Python launcher is unavailable")
    cwd = tmp_path / "cwd"
    launcher_bin = tmp_path / "launcher-bin"
    cwd.mkdir()
    launcher_bin.mkdir()
    shutil.copyfile(system_py, launcher_bin / "py.exe")

    started = _plugin_hook(
        cwd,
        "SessionStart",
        {
            "hook_event_name": "SessionStart",
            "source": "startup",
            "cwd": str(cwd),
        },
        search_path=str(launcher_bin),
    )

    assert "hookSpecificOutput" in started, started
    assert "3CAN fast path" in started["hookSpecificOutput"]["additionalContext"]


@pytest.mark.skipif(os.name != "nt", reason="Windows launcher contract")
def test_windows_plugin_reports_unavailable_without_python(tmp_path: Path):
    cwd = tmp_path / "cwd"
    empty_bin = tmp_path / "empty-bin"
    cwd.mkdir()
    empty_bin.mkdir()

    started = _plugin_hook(
        cwd,
        "SessionStart",
        {
            "hook_event_name": "SessionStart",
            "source": "startup",
            "cwd": str(cwd),
        },
        search_path=str(empty_bin),
    )

    assert "RuntimeHook semantic context is UNAVAILABLE" in started["systemMessage"]


def test_inactive_non_session_events_skip_interpreter_discovery(tmp_path: Path):
    cwd = tmp_path / "cwd"
    empty_bin = tmp_path / "empty-bin"
    cwd.mkdir()
    empty_bin.mkdir()

    for event, payload in (
        ("UserPromptSubmit", {"hook_event_name": "UserPromptSubmit"}),
        (
            "PostToolUse",
            {"hook_event_name": "PostToolUse", "tool_name": "Bash"},
        ),
        ("Stop", {"hook_event_name": "Stop"}),
    ):
        assert _plugin_hook(
            cwd,
            event,
            {**payload, "cwd": str(cwd)},
            search_path=str(empty_bin),
        ) == {}


@pytest.mark.skipif(os.name != "nt", reason="Windows launcher contract")
def test_windows_plugin_skips_broken_python_before_py3(tmp_path: Path):
    system_py = shutil.which("py.exe")
    if system_py is None:
        pytest.skip("Windows Python launcher is unavailable")
    cwd = tmp_path / "cwd"
    broken_bin = tmp_path / "broken-bin"
    launcher_bin = tmp_path / "launcher-bin"
    cwd.mkdir()
    broken_bin.mkdir()
    launcher_bin.mkdir()
    shutil.copyfile(Path(os.environ["COMSPEC"]), broken_bin / "python.exe")
    shutil.copyfile(system_py, launcher_bin / "py.exe")

    started = _plugin_hook(
        cwd,
        "SessionStart",
        {
            "hook_event_name": "SessionStart",
            "source": "startup",
            "cwd": str(cwd),
        },
        search_path=os.pathsep.join((str(broken_bin), str(launcher_bin))),
    )

    assert "hookSpecificOutput" in started, started
    assert "3CAN fast path" in started["hookSpecificOutput"]["additionalContext"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX launcher contract")
def test_posix_plugin_ignores_repo_local_python_shadow(plain_repo: Path):
    system_python = shutil.which("python3")
    system_git = shutil.which("git")
    if system_python is None or system_git is None:
        pytest.skip("system Python 3 or Git is unavailable")
    malicious_bin = plain_repo / "bin"
    malicious_bin.mkdir()
    sentinel = plain_repo / "python-shadow-ran"
    shadow = malicious_bin / "python3"
    shadow.write_text(
        f"#!/bin/sh\ntouch '{sentinel}'\nexit 1\n",
        encoding="utf-8",
    )
    shadow.chmod(0o755)

    started = _plugin_hook(
        plain_repo,
        "SessionStart",
        {
            "hook_event_name": "SessionStart",
            "source": "startup",
            "cwd": str(plain_repo),
        },
        search_path=os.pathsep.join(
            (
                str(malicious_bin),
                str(Path(system_python).parent),
                str(Path(system_git).parent),
            )
        ),
    )

    assert "hookSpecificOutput" in started, started
    assert "3CAN fast path" in started["hookSpecificOutput"]["additionalContext"]
    assert not sentinel.exists()


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


def test_plugin_orients_before_activation_and_resolves_nested_cwd(
    plain_repo: Path,
):
    nested = plain_repo / "nested"
    nested.mkdir()
    start_payload = {
        "hook_event_name": "SessionStart",
        "source": "compact",
        "cwd": str(nested),
    }

    inactive = _plugin_hook(nested, "SessionStart", start_payload)
    inactive_context = inactive["hookSpecificOutput"]["additionalContext"]
    assert "start safe local work immediately" in inactive_context
    assert "fresh ticket just in time" in inactive_context
    assert "AUTO_CLOSEOUT" in inactive_context
    assert "RUN_INTENT" not in inactive_context
    assert not (plain_repo / STATE_PATH).exists()
    assert _plugin_hook(
        nested,
        "UserPromptSubmit",
        {"hook_event_name": "UserPromptSubmit", "cwd": str(nested)},
    ) == {}
    _activate(plain_repo)
    started = _plugin_hook(nested, "SessionStart", start_payload)
    stopped = _plugin_hook(
        nested,
        "Stop",
        {"hook_event_name": "Stop", "cwd": str(nested)},
    )

    context = started["hookSpecificOutput"]["additionalContext"]
    assert "start safe local work immediately" in context
    assert "交付当前开源任务" in context
    assert "Semantic review: PENDING" in context
    assert stopped["decision"] == "block"
    assert "final semantic review is due" in stopped["reason"]


def test_plugin_orients_outside_git_without_creating_state(tmp_path: Path):
    started = _plugin_hook(
        tmp_path,
        "SessionStart",
        {
            "hook_event_name": "SessionStart",
            "source": "startup",
            "cwd": str(tmp_path),
        },
    )

    context = started["hookSpecificOutput"]["additionalContext"]
    assert "start safe local work immediately" in context
    assert "current AgentId" in context
    assert "does not activate RuntimeHook" in context
    assert not (tmp_path / STATE_PATH).exists()
    assert _plugin_hook(
        tmp_path,
        "Stop",
        {"hook_event_name": "Stop", "cwd": str(tmp_path)},
    ) == {}


@pytest.mark.skipif(os.name != "nt", reason="Windows executable lookup contract")
def test_windows_plugin_ignores_repo_local_executable_shadows(
    plain_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    system_command = Path(os.environ["COMSPEC"])
    malicious_bin = plain_repo / "bin"
    malicious_bin.mkdir()
    for name in ("git.exe", "python.exe", "py.exe", "powershell.exe"):
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


def test_native_cwd_preflight_rejects_foreign_scope_before_state_access(
    plain_repo: Path, tmp_path: Path,
):
    peer = tmp_path / "peer worktree"
    _git(plain_repo, "worktree", "add", "-qb", "peer-scope", str(peer))
    _activate(plain_repo, goal="Original owner intent")
    original = (plain_repo / STATE_PATH).read_bytes()
    exclude = Path(_git(plain_repo, "rev-parse", "--git-path", "info/exclude").stdout.strip())
    if not exclude.is_absolute():
        exclude = plain_repo / exclude
    original_exclude = exclude.read_bytes()

    for command in [
        ["status"],
        ["on", "--goal", "Wrong replacement", "--acceptance", "A=wrong",
         "--intensity", "light", "--reason", "probe"],
        ["off"],
        ["checkpoint", "--kind", "stage", "--label", "wrong task"],
        ["review", "--stage", "final", "--result", "PARTIAL", "--reference", "wrong task"],
    ]:
        code, output = _controller(
            plain_repo, "--native-cwd", str(peer), *command,
        )
        assert code != 0
        assert output["status"] == "UNAVAILABLE"
        assert "CONTEXT_MISMATCH" in output["error"]
        assert "Original owner intent" not in json.dumps(output)
        assert (plain_repo / STATE_PATH).read_bytes() == original
        assert not (peer / STATE_PATH).exists()
        assert exclude.read_bytes() == original_exclude

    code, output = _controller(
        peer, "--native-cwd", str(plain_repo), "on",
        "--goal", "Do not create orphan state", "--acceptance", "A=valid",
        "--intensity", "light", "--reason", "probe",
    )
    assert code != 0 and "CONTEXT_MISMATCH" in output["error"]
    assert not (peer / STATE_PATH).exists()


def test_native_cwd_preflight_is_read_only_and_accepts_same_worktree_subdir(
    plain_repo: Path,
):
    activated = _activate(plain_repo)
    before = (plain_repo / STATE_PATH).read_bytes()
    nested = plain_repo / "nested"
    nested.mkdir()
    code, output = _controller(
        plain_repo, "--native-cwd", str(nested), "status",
    )
    assert code == 0
    assert output["native_scope"] == {"status": "MATCH", "worktree": str(plain_repo)}
    assert output["activation_id"] == activated["activation_id"]
    assert (plain_repo / STATE_PATH).read_bytes() == before
    code, output = _controller(plain_repo, "--native-cwd", ".", "status")
    assert code != 0 and "absolute" in output["error"]


@pytest.mark.parametrize("event", ["SessionStart", "UserPromptSubmit", "PostToolUse", "Stop"])
def test_native_events_keep_linked_worktree_state_isolated(
    plain_repo: Path, tmp_path: Path, event: str,
):
    peer = tmp_path / "independent peer"
    _git(plain_repo, "worktree", "add", "-qb", "isolated-peer", str(peer))
    activations = [_activate(plain_repo, goal="Task A"), _activate(peer, goal="Task B")]
    for index, (root, foreign) in enumerate([(plain_repo, peer), (peer, plain_repo)]):
        foreign_before = (foreign / STATE_PATH).read_bytes()
        payload = {
            "cwd": str(root), "session_id": _session_id(root),
            "hook_event_name": event, "source": "resume",
            "tool_name": "update_plan",
            "tool_input": {"plan": [{"step": "Review scoped module", "status": "completed"}]},
        }
        result = _plugin_hook(root, event, payload)
        serialized = json.dumps(result, ensure_ascii=False)
        assert activations[index]["activation_id"] in serialized
        assert activations[1 - index]["activation_id"] not in serialized
        message = result.get("reason") or result.get("systemMessage") or result[
            "hookSpecificOutput"
        ]["additionalContext"]
        assert str(root) in message
        assert (foreign / STATE_PATH).read_bytes() == foreign_before


@pytest.mark.parametrize("event", ["SessionStart", "UserPromptSubmit", "PostToolUse", "Stop"])
def test_explicit_hook_root_cannot_override_foreign_payload(
    plain_repo: Path, tmp_path: Path, event: str,
):
    peer = tmp_path / "foreign payload"
    _git(plain_repo, "worktree", "add", "-qb", "foreign-payload", str(peer))
    _activate(plain_repo)
    _activate(peer)
    before = [(root / STATE_PATH).read_bytes() for root in (plain_repo, peer)]
    completed = subprocess.run(
        [sys.executable, str(PLUGIN_CLI), "--root", str(plain_repo), "hook"],
        input=json.dumps({"cwd": str(peer), "hook_event_name": event, "source": "resume"}),
        cwd=plain_repo, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    output = json.loads(completed.stdout)
    assert completed.returncode == 0
    assert "CONTEXT_MISMATCH" in output["systemMessage"]
    assert "decision" not in output
    assert [(root / STATE_PATH).read_bytes() for root in (plain_repo, peer)] == before


def test_native_scope_option_cannot_substitute_hook_payload(plain_repo: Path):
    _activate(plain_repo)
    before = (plain_repo / STATE_PATH).read_bytes()
    completed = subprocess.run(
        [sys.executable, str(PLUGIN_CLI), "--root", str(plain_repo),
         "--native-cwd", str(plain_repo), "hook"],
        input=json.dumps({"cwd": str(plain_repo), "hook_event_name": "UserPromptSubmit"}),
        cwd=plain_repo, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    output = json.loads(completed.stdout)
    assert "payload cwd" in output["systemMessage"]
    assert "decision" not in output
    assert (plain_repo / STATE_PATH).read_bytes() == before


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
    posix_launcher = POSIX_LAUNCHER.read_text(encoding="utf-8")

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
        assert "run_runtimehook.sh" in handlers[0]["command"]
        command_windows = handlers[0]["commandWindows"]
        assert command_windows in {
            "& (Join-Path $env:PLUGIN_ROOT 'hooks/run_runtimehook.ps1')",
            "& (Join-Path $env:PLUGIN_ROOT 'hooks/run_runtimehook.ps1') -SessionOrientation",
        }
        assert "statusMessage" not in handlers[0]
    assert "--session-orientation" in hooks["SessionStart"][0]["hooks"][0][
        "command"
    ]
    assert hooks["SessionStart"][0]["hooks"][0][
        "additionalContextLimit"
    ] == 5000
    assert hooks["UserPromptSubmit"][0]["hooks"][0][
        "additionalContextLimit"
    ] == 5000
    assert hooks["PostToolUse"][0]["matcher"] == "^(Bash|update_plan)$"
    assert "do not require or\ncopy a controller" in skill
    assert "fresh ticket just in time" in skill
    assert "allow_implicit_invocation: true" in skill_ui
    assert "NoDefaultCurrentDirectoryInExePath" in windows_launcher
    assert 'Join-Path $cursor.FullName ".git"' in windows_launcher
    assert 'if (-not $SessionOrientation)' in windows_launcher
    assert posix_launcher.startswith("#!/bin/sh\n")
    assert "untrusted_root" in posix_launcher
    assert PLUGIN_CLI.read_bytes() == PROJECT_KIT_CLI.read_bytes()


def _scope_module():
    spec = importlib.util.spec_from_file_location("scope_probe", PLUGIN_CLI)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("event", ["SessionStart", "UserPromptSubmit", "PostToolUse", "Stop"])
def test_scope_unknown_task_never_uses_foreign_intent_or_blocks(plain_repo: Path, event: str):
    active = _activate(plain_repo, goal="Do not leak this task's intent")
    before = (plain_repo / STATE_PATH).read_bytes()
    result = _plugin_hook(plain_repo, event, {
        "session_id": "different-native-task", "cwd": str(plain_repo),
        "hook_event_name": event, "source": "resume",
        "tool_name": "update_plan", "tool_input": {"plan": [{"step": "Wrong", "status": "completed"}]},
    })
    serialized = json.dumps(result)
    assert "decision" not in result and "continue" not in result
    assert active["activation_id"] not in serialized
    assert "Do not leak this task" not in serialized
    if event != "PostToolUse":
        assert "SCOPE_UNBOUND" in serialized
    assert (plain_repo / STATE_PATH).read_bytes() == before


def test_scope_fast_lookup_uses_no_git_network_or_state_write(plain_repo: Path, monkeypatch):
    _activate(plain_repo)
    module = _scope_module()
    monkeypatch.setattr(module, "_git", lambda *_a, **_k: pytest.fail("fast lookup spawned Git"))
    monkeypatch.setattr(module, "_load_state", lambda *_a, **_k: pytest.fail("fast lookup read semantic state"))
    cache = module._scope_path(_session_id(plain_repo))
    before = cache.read_bytes()
    nested = plain_repo / "中文 nested"
    nested.mkdir()
    root, binding = module._check_scope({"session_id": _session_id(plain_repo), "cwd": str(nested)})
    assert root == plain_repo and binding["knowledge"]["status"] == "UNVERIFIED"
    assert cache.read_bytes() == before
    timings = []
    for _ in range(1000):
        started = time.perf_counter_ns()
        module._check_scope({"session_id": _session_id(plain_repo), "cwd": str(nested)})
        timings.append((time.perf_counter_ns() - started) / 1_000_000)
    print(json.dumps({"scope_lookup_ms": {
        "samples": len(timings), "p50": statistics.median(timings),
        "p95": sorted(timings)[949], "max": max(timings),
    }}))


def test_scope_cached_task_moving_to_peer_is_advisory(plain_repo: Path, tmp_path: Path):
    _activate(plain_repo, goal="Task A")
    peer = tmp_path / "peer"
    _git(plain_repo, "worktree", "add", "-qb", "scope-moved", str(peer))
    _activate(peer, goal="Task B")
    before = [(p / STATE_PATH).read_bytes() for p in (plain_repo, peer)]
    result = _plugin_hook(peer, "Stop", {
        "session_id": _session_id(plain_repo), "cwd": str(peer), "hook_event_name": "Stop",
    })
    assert "CONTEXT_MISMATCH" in result["systemMessage"]
    assert "decision" not in result
    assert [(p / STATE_PATH).read_bytes() for p in (plain_repo, peer)] == before


def test_scope_new_activation_is_not_silently_adopted(plain_repo: Path):
    _activate(plain_repo)
    module = _scope_module()
    path = module._scope_path(_session_id(plain_repo))
    before = path.read_bytes()
    _controller(plain_repo, "on", "--goal", "Different task", "--acceptance", "A=new",
                "--intensity", "light", "--reason", "new task without native observation")
    state_before = (plain_repo / STATE_PATH).read_bytes()
    result = _plugin_hook(plain_repo, "Stop", {
        "cwd": str(plain_repo), "hook_event_name": "Stop",
    })
    assert "SCOPE_STALE" in result["systemMessage"] and "decision" not in result
    assert path.read_bytes() == before and (plain_repo / STATE_PATH).read_bytes() == state_before
    code, adopted = _controller(plain_repo, "--native-cwd", str(plain_repo),
                               "bind-scope", "--reference", "Owner-authorized handoff: new current Intent")
    assert code == 0 and adopted["status"] == "MATCH"
    assert adopted["semantic_state_changed"] is False


def test_scope_3can_disagreement_is_nonblocking_and_does_not_override_git(plain_repo: Path, tmp_path: Path):
    _activate(plain_repo)
    before = (plain_repo / STATE_PATH).read_bytes()
    code, report = _controller(plain_repo, "--native-cwd", str(plain_repo), "bind-scope",
                              "--reference", "native snapshot",
                              "--knowledge-worktree", str(tmp_path / "old-location"),
                              "--knowledge-reference", "3can:fixture-contradicting-handoff")
    assert code == 0 and report["binding"]["knowledge"]["status"] == "CONTRADICTS"
    result = _plugin_hook(plain_repo, "Stop", {"cwd": str(plain_repo), "hook_event_name": "Stop"})
    assert "recorded 3CAN worktree" in result["systemMessage"]
    assert "decision" not in result and (plain_repo / STATE_PATH).read_bytes() == before


def test_scope_missing_or_corrupt_cache_never_auto_adopts(plain_repo: Path):
    _activate(plain_repo)
    module = _scope_module()
    path = module._scope_path(_session_id(plain_repo))
    path.write_text("not json", encoding="utf-8")
    before = (plain_repo / STATE_PATH).read_bytes()
    result = _plugin_hook(plain_repo, "Stop", {"cwd": str(plain_repo), "hook_event_name": "Stop"})
    assert "scope cache is unreadable" in result["systemMessage"] and "decision" not in result
    path.unlink()
    result = _plugin_hook(plain_repo, "Stop", {"cwd": str(plain_repo), "hook_event_name": "Stop"})
    assert "SCOPE_UNBOUND" in result["systemMessage"] and not path.exists()
    assert (plain_repo / STATE_PATH).read_bytes() == before


def test_scope_binding_import_can_report_migration_without_peer_state_access(plain_repo: Path, tmp_path: Path, monkeypatch):
    module = _scope_module()
    peer = tmp_path / "peer"
    _git(plain_repo, "worktree", "add", "-qb", "scope-import", str(peer))
    monkeypatch.setattr(module, "_load_state", lambda *_: pytest.fail("mismatch read another state"))
    args = module.build_parser().parse_args([
        "--root", str(peer), "--native-cwd", str(plain_repo), "--session-id", "migrating-task",
        "bind-scope", "--reference", "Owner and host metadata identify an incomplete native migration",
    ])
    result = module.bind_scope(args)
    assert result["status"] == "CONTEXT_MISMATCH"
    assert result["binding"]["activation_id"] is None
    assert not (plain_repo / STATE_PATH).exists() and not (peer / STATE_PATH).exists()


def test_scope_new_nested_git_marker_invalidates_cached_parent(plain_repo: Path, tmp_path: Path):
    _activate(plain_repo)
    nested = plain_repo / "nested"
    nested.mkdir()
    subprocess.run(["git", "init", "-q", str(nested)], check=True)
    module = _scope_module()
    with pytest.raises(module.RuntimeHookError, match="CONTEXT_MISMATCH"):
        module._check_scope({"session_id": _session_id(plain_repo), "cwd": str(nested)})


def test_scope_local_wrong_root_cannot_change_peer_state(plain_repo: Path, tmp_path: Path):
    _activate(plain_repo)
    peer = tmp_path / "peer"
    _git(plain_repo, "worktree", "add", "-qb", "scope-cli", str(peer))
    _activate(peer)
    before = [(p / STATE_PATH).read_bytes() for p in (plain_repo, peer)]
    code, result = _controller(peer, "--session-id", _session_id(plain_repo), "off")
    assert code == 2 and "CONTEXT_MISMATCH" in result["error"]
    assert [(p / STATE_PATH).read_bytes() for p in (plain_repo, peer)] == before
