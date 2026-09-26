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
sys.path.insert(0, str(SKILL_ROOT / "scripts"))
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
        assert "3CAN 快速指引" in json.loads(completed.stdout)["hookSpecificOutput"]["additionalContext"]
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
    assert "3CAN 快速指引" in started["hookSpecificOutput"]["additionalContext"]


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

    assert "RuntimeHook 语义上下文不可用 UNAVAILABLE" in started["systemMessage"]


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
    assert "3CAN 快速指引" in started["hookSpecificOutput"]["additionalContext"]


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
    assert "3CAN 快速指引" in started["hookSpecificOutput"]["additionalContext"]
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
    assert "未被跟踪且已被 Git 忽略" in output["error"]
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
    assert "立即开展安全的本地工作" in inactive_context
    assert "执行前即时获取新票据" in inactive_context
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
    assert "立即开展安全的本地工作" in context
    assert "交付当前开源任务" in context
    assert "语义复核状态：PENDING" in context
    assert stopped["decision"] == "block"
    assert "阶段复核待完成" in stopped["reason"]


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
    assert "立即开展安全的本地工作" in context
    assert "当前 AgentId" in context
    assert "不激活 RuntimeHook" in context
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
        ["task", "--kind", "temporary", "--goal", "wrong task", "--acceptance", "T=wrong",
         "--reference", "wrong task", "--resume-objective", "wrong task"],
        ["task", "--kind", "cancel", "--reference", "wrong task"],
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
    assert code != 0 and "绝对路径" in output["error"]


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
    assert "输入中的 cwd" in output["systemMessage"]
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
    result = _plugin_hook(plain_repo, "SessionStart", {
        "cwd": str(plain_repo), "hook_event_name": "SessionStart", "source": "resume",
    })
    context = result["hookSpecificOutput"]["additionalContext"]
    assert "3CAN 关联记录待核对" in context and "交付当前开源任务" in context
    assert "decision" not in result and (plain_repo / STATE_PATH).read_bytes() == before


def test_scope_missing_or_corrupt_cache_never_auto_adopts(plain_repo: Path):
    _activate(plain_repo)
    module = _scope_module()
    path = module._scope_path(_session_id(plain_repo))
    path.write_text("not json", encoding="utf-8")
    before = (plain_repo / STATE_PATH).read_bytes()
    result = _plugin_hook(plain_repo, "Stop", {"cwd": str(plain_repo), "hook_event_name": "Stop"})
    assert "范围缓存不可读" in result["systemMessage"] and "decision" not in result
    path.unlink()
    result = _plugin_hook(plain_repo, "Stop", {"cwd": str(plain_repo), "hook_event_name": "Stop"})
    assert "SCOPE_UNBOUND" in result["systemMessage"] and not path.exists()
    assert (plain_repo / STATE_PATH).read_bytes() == before


def test_explicit_binding_reads_only_verified_target_not_host_peer(plain_repo: Path, tmp_path: Path, monkeypatch):
    module = _scope_module()
    peer = tmp_path / "peer"
    _git(plain_repo, "worktree", "add", "-qb", "scope-import", str(peer))
    _activate(plain_repo, goal="Host peer must remain untouched")
    target = _activate(peer, goal="Verified development target")
    before = [(p / STATE_PATH).read_bytes() for p in (plain_repo, peer)]
    load = module._load_state
    def target_only(root):
        assert root == peer
        return load(root)
    monkeypatch.setattr(module, "_load_state", target_only)
    args = module.build_parser().parse_args([
        "--root", str(peer), "--native-cwd", str(plain_repo), "--session-id", "migrating-task",
        "bind-scope", "--reference", "Owner verified the target Intent; host cwd remains unchanged",
    ])
    result = module.bind_scope(args)
    assert result["status"] == "MATCH"
    assert result["binding"]["activation_id"] == target["activation_id"]
    assert [(p / STATE_PATH).read_bytes() for p in (plain_repo, peer)] == before


@pytest.mark.parametrize("event", ["SessionStart", "UserPromptSubmit", "PostToolUse", "Stop"])
def test_bound_non_git_host_runs_all_native_events_on_target(plain_repo: Path, tmp_path: Path, event: str):
    host = tmp_path / "旧宿主 folder"
    host.mkdir()
    active = _activate(plain_repo)
    code, bound = _controller(plain_repo, "--native-cwd", str(host), "bind-scope",
                              "--reference", "Host metadata and Owner-confirmed target Intent")
    assert code == 0 and bound["status"] == "MATCH", bound
    result = _plugin_hook(host, event, {
        "session_id": _session_id(plain_repo), "cwd": str(host),
        "hook_event_name": event, "source": "compact", "tool_name": "update_plan",
        "tool_input": {"plan": [{"step": "核对目标模块", "status": "completed"}]},
    })
    assert active["activation_id"] in json.dumps(result)
    assert str(plain_repo) in (result.get("reason") or result["hookSpecificOutput"]["additionalContext"])
    assert not (host / STATE_PATH).exists()
    code, status = _controller(plain_repo, "--native-cwd", str(host), "status")
    assert code == 0 and status["activation_id"] == active["activation_id"]


@pytest.mark.parametrize("change", ["unknown_session", "host", "git_marker", "activation"])
def test_cross_directory_binding_never_adopts_changed_identity(plain_repo: Path, tmp_path: Path, change: str):
    host, moved = tmp_path / "host", tmp_path / "moved"
    host.mkdir()
    moved.mkdir()
    active = _activate(plain_repo, goal="Private target Intent")
    code, _ = _controller(plain_repo, "--native-cwd", str(host), "bind-scope", "--reference", "verified")
    assert code == 0
    module = _scope_module()
    binding = module._read_scope(_session_id(plain_repo))
    if change == "git_marker":
        binding["git_marker"] = [0, 0]
        module._save_scope(binding)
    elif change == "activation":
        binding["activation_id"] = "rh-outdated"
        module._save_scope(binding)
    before = (plain_repo / STATE_PATH).read_bytes()
    result = _plugin_hook(host, "Stop", {
        "session_id": "unknown-task" if change == "unknown_session" else _session_id(plain_repo),
        "cwd": str(moved if change == "host" else host), "hook_event_name": "Stop",
    })
    assert "decision" not in result
    assert active["activation_id"] not in json.dumps(result)
    assert "Private target Intent" not in json.dumps(result)
    assert (plain_repo / STATE_PATH).read_bytes() == before


def test_legacy_scope_does_not_silently_enable_old_mismatch(plain_repo: Path, tmp_path: Path):
    _activate(plain_repo)
    module = _scope_module()
    binding = module._read_scope(_session_id(plain_repo))
    binding["schema"] = "3can.runtimehook-scope/v1"
    binding.pop("native_worktree", None)
    binding.pop("native_git_marker", None)
    module._save_scope(binding)
    assert module._check_scope({"session_id": _session_id(plain_repo), "cwd": str(plain_repo)})[0] == plain_repo
    with pytest.raises(module.RuntimeHookError, match="CONTEXT_MISMATCH"):
        module._check_scope({"session_id": _session_id(plain_repo), "cwd": str(tmp_path)})


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


def _temporary(root: Path) -> dict:
    code, output = _controller(
        root, "task", "--kind", "temporary", "--goal", "先交付相关演示片段，再返回主任务。",
        "--acceptance", "T01=实际内容满足用户确认的演示目标。",
        "--reference", "owner:先做片段然后继续开发",
        "--resume-objective", "继续主任务剩余模块的验证。",
    )
    assert code == 0, output
    return output


def test_temporary_completion_clears_atomically_and_does_not_certify_main(plain_repo: Path):
    activation = _activate(plain_repo)
    _controller(plain_repo, "review", "--stage", "final", "--result", "PASS", "--reference", "git:main-before")
    state_path = plain_repo / STATE_PATH
    original = json.loads(state_path.read_text(encoding="utf-8"))
    (plain_repo / "tracked.txt").write_text("unfinished main work\n", encoding="utf-8")
    _temporary(plain_repo)
    during = json.loads(state_path.read_text(encoding="utf-8"))
    assert during["schema"] == "3can.runtimehook-state/v2"
    assert during["run_intent"] == original["run_intent"]
    assert during["activation_id"] == activation["activation_id"]
    resumed = _plugin_hook(plain_repo, "SessionStart", {
        "cwd": str(plain_repo), "hook_event_name": "SessionStart", "source": "compact",
    })["hookSpecificOutput"]["additionalContext"]
    assert during["temporary_task"]["goal"] in resumed and original["run_intent"]["goal"] in resumed
    assert "临时任务进行中" in resumed and "语义复核状态：PASS" not in resumed

    args = ["review", "--scope", "temporary", "--stage", "final", "--result", "PASS",
            "--reference", "artifact:actual-output-reviewed"]
    code, result = _controller(plain_repo, *args)
    assert code == 0 and result["temporary_cleared"] is True
    restored = json.loads(state_path.read_text(encoding="utf-8"))
    assert "temporary_task" not in restored and restored["schema"].endswith("/v1")
    assert restored["run_intent"] == original["run_intent"]
    assert restored["activation_id"] == original["activation_id"]
    assert restored["current_episode"] == during["temporary_task"]["resume_objective"]
    assert restored["semantic_review"] == {
        "stage": "episode", "result": "PARTIAL", "reference": "artifact:actual-output-reviewed",
        "reviewed_git_head": None,
    }
    assert (plain_repo / "tracked.txt").read_text(encoding="utf-8") == "unfinished main work\n"
    assert sorted(p.name for p in state_path.parent.iterdir()) == ["state.json"]
    stopped = _plugin_hook(plain_repo, "Stop", {"cwd": str(plain_repo), "hook_event_name": "Stop"})
    assert "decision" not in stopped and "主任务阶段记录" in stopped["systemMessage"]
    before = state_path.read_bytes()
    code, repeated = _controller(plain_repo, *args)
    assert code == 2 and "REVIEW_SCOPE_MISMATCH" in repeated["error"]
    assert state_path.read_bytes() == before
    main_code, main = _controller(plain_repo, "review", "--stage", "final", "--result", "PASS",
                                  "--reference", "cannot-promote-dirty-main")
    assert main_code == 2 and "干净 Git 检查点" in main["error"]


@pytest.mark.parametrize("result", ["PARTIAL", "UNVERIFIABLE", "FAIL"])
def test_temporary_unfinished_review_and_plan_completion_never_clear(plain_repo: Path, result: str):
    _activate(plain_repo)
    _temporary(plain_repo)
    code, output = _controller(plain_repo, "review", "--scope", "temporary", "--stage", "final",
                               "--result", result, "--reference", "evidence:waiting-or-incomplete")
    assert code == 0, output
    before = json.loads((plain_repo / STATE_PATH).read_text(encoding="utf-8"))
    stopped = _plugin_hook(plain_repo, "Stop", {"cwd": str(plain_repo), "hook_event_name": "Stop"})
    assert "decision" not in stopped and result in stopped["systemMessage"]
    _plugin_hook(plain_repo, "PostToolUse", {
        "cwd": str(plain_repo), "hook_event_name": "PostToolUse", "tool_name": "update_plan",
        "tool_input": {"plan": [{"step": "完成了计划步骤但没有验收", "status": "completed"}]},
    })
    after = json.loads((plain_repo / STATE_PATH).read_text(encoding="utf-8"))
    assert after["temporary_task"] == before["temporary_task"]
    assert after["run_intent"] == before["run_intent"]
    assert after["semantic_review"]["result"] == "PENDING"


def test_temporary_cannot_nest_or_overwrite_main_and_owner_cancel_is_not_success(plain_repo: Path):
    _activate(plain_repo)
    _temporary(plain_repo)
    path = plain_repo / STATE_PATH
    before = path.read_bytes()
    for args in (
        ["task", "--kind", "temporary", "--goal", "第二个临时任务", "--reference", "owner:new"],
        ["on", "--goal", "覆盖主目标", "--acceptance", "A=wrong", "--intensity", "light", "--reason", "wrong"],
        ["review", "--stage", "final", "--result", "PASS", "--reference", "wrong-main-pass"],
    ):
        code, result = _controller(plain_repo, *args)
        assert code == 2 and result["status"] == "UNAVAILABLE"
        assert path.read_bytes() == before
    code, result = _controller(plain_repo, "task", "--kind", "cancel", "--reference", "owner:不做临时片段")
    assert code == 0 and result["status"] == "CANCELLED" and result["temporary_cleared"]
    state = json.loads(path.read_text(encoding="utf-8"))
    assert "temporary_task" not in state and state["semantic_review"]["result"] == "PARTIAL"
    before = path.read_bytes()
    code, result = _controller(plain_repo, "task", "--kind", "cancel", "--reference", "owner:不做临时片段")
    assert code == 0 and result["changed"] is False and path.read_bytes() == before


def test_temporary_failed_atomic_completion_preserves_recoverable_state(plain_repo: Path, monkeypatch):
    _activate(plain_repo)
    _temporary(plain_repo)
    module = _scope_module()
    path = plain_repo / STATE_PATH
    before = path.read_bytes()
    args = module.build_parser().parse_args([
        "--root", str(plain_repo), "review", "--scope", "temporary", "--stage", "final",
        "--result", "PASS", "--reference", "evidence:validated-output",
    ])
    def fail_replace(*_args):
        raise OSError("injected save failure")
    monkeypatch.setattr(module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected save failure"):
        module.record_review(args)
    assert path.read_bytes() == before
    assert sorted(p.name for p in path.parent.iterdir()) == ["state.json"]


def test_temporary_cannot_be_silently_encoded_as_legacy_state(plain_repo: Path):
    _activate(plain_repo)
    _temporary(plain_repo)
    module = _scope_module()
    state = json.loads((plain_repo / STATE_PATH).read_text(encoding="utf-8"))
    state["schema"] = module.STATE_SCHEMA
    with pytest.raises(module.RuntimeHookError, match="v2"):
        module._validate_state(state)


@pytest.mark.parametrize("kind", ["transfer", "drift"])
def test_task_transfer_advice_does_not_mutate_or_choose_a_workspace(plain_repo: Path, kind: str):
    _activate(plain_repo)
    path = plain_repo / STATE_PATH
    before = path.read_bytes()
    files_before = set(plain_repo.rglob("*"))
    code, result = _controller(plain_repo, "task", "--kind", kind, "--reference", "owner:明确的新要求")
    assert code == 0 and result["status"] == "ADVISORY"
    assert result["semantic_state_changed"] is False
    assert "decision" not in result and "continue" not in result
    assert "建议" in result["message"] and "worktree" in result["message"]
    assert path.read_bytes() == before and set(plain_repo.rglob("*")) == files_before


def test_current_episode_wait_is_not_forced_into_final_completion(plain_repo: Path):
    _activate(plain_repo)
    _controller(plain_repo, "review", "--stage", "episode", "--result", "PARTIAL",
                "--reference", "evidence:browser-security-refusal", "--next-objective", "等待必要输入后继续采集。")
    stopped = _plugin_hook(plain_repo, "Stop", {"cwd": str(plain_repo), "hook_event_name": "Stop"})
    assert "decision" not in stopped and "这不表示整个任务完成" in stopped["systemMessage"]
    assert "等待必要输入后继续采集" in stopped["systemMessage"]
    _plugin_hook(plain_repo, "UserPromptSubmit", {"cwd": str(plain_repo), "hook_event_name": "UserPromptSubmit"})
    due = _plugin_hook(plain_repo, "Stop", {"cwd": str(plain_repo), "hook_event_name": "Stop"})
    assert due["decision"] == "block" and "阶段复核待完成" in due["reason"]
