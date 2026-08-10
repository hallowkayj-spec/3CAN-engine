from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RELEASE_ROOT = ROOT.parent
KIT_ROOT = RELEASE_ROOT / "examples" / "codex-cli-project-kit"


def load_module(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, KIT_ROOT / rel)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


pr = load_module("pr_harness", "scripts/3can_pr_harness.py")


def test_parse_https_and_ssh_github_remotes():
    https_repo = pr.parse_github_remote("https://github.com/example-org/example-repo.git")
    ssh_repo = pr.parse_github_remote("git@github.com:example-org/example-repo.git")

    assert https_repo.full_name == "example-org/example-repo"
    assert ssh_repo.full_name == "example-org/example-repo"


def test_create_pr_requires_approval_before_token_lookup(monkeypatch):
    called = False

    def fake_load_token(cwd):
        nonlocal called
        called = True
        return "should-not-be-read"

    monkeypatch.setattr(pr, "load_token", fake_load_token)
    result = pr.create_pr(
        repo=pr.RemoteRepo("example-org", "example-repo"),
        head_branch="feat/x",
        base_branch="main",
        title="title",
        body="body",
        approval_id="",
    )

    assert result["status"] == "block"
    assert result["reason"] == "approval_required"
    assert called is False


def test_create_pr_returns_fallback_when_no_token(monkeypatch):
    monkeypatch.setattr(pr, "load_token", lambda cwd: "")

    result = pr.create_pr(
        repo=pr.RemoteRepo("example-org", "example-repo"),
        head_branch="feat/x",
        base_branch="main",
        title="title",
        body="body",
        approval_id="APPROVED",
    )

    assert result["status"] == "block"
    assert result["reason"] == "github_token_unavailable"
    assert result["fallback_url"] == "https://github.com/example-org/example-repo/pull/new/feat/x"


def test_create_pr_reuses_existing_open_pr(monkeypatch):
    monkeypatch.setattr(pr, "load_token", lambda cwd: "token")
    monkeypatch.setattr(
        pr,
        "find_existing_pr",
        lambda **kwargs: {"number": 51, "html_url": "https://github.com/example-org/example-repo/pull/51"},
    )

    result = pr.create_pr(
        repo=pr.RemoteRepo("example-org", "example-repo"),
        head_branch="feat/x",
        base_branch="main",
        title="title",
        body="body",
        approval_id="APPROVED",
    )

    assert result["ok"] is True
    assert result["status"] == "exists"
    assert result["number"] == 51


def test_hook_blocks_github_connector_pr_creation():
    code, payload = pr._hook_json({
        "hook_event_name": "PreToolUse",
        "tool_name": "mcp__codex_apps__github._create_pull_request",
        "tool_input": {"repository_full_name": "example-org/example-repo"},
    })

    assert code == 2
    assert payload["decision"] == "block"
    assert pr.ERROR_NODE_ID in payload["reason"]


def test_hook_blocks_gh_pr_create_when_gh_missing(monkeypatch):
    monkeypatch.setattr(pr.shutil, "which", lambda name: None)

    code, payload = pr._hook_json({
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "gh pr create --title x --body y"},
    })

    assert code == 2
    assert payload["decision"] == "block"
    assert "gh CLI is not installed" in payload["reason"]


def test_hook_guides_after_git_push():
    code, payload = pr._hook_json({
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "git push -u origin feat/x"},
    })

    assert code == 0
    assert "systemMessage" in payload
    assert "3can_pr_harness.py create-pr" in payload["systemMessage"]


def test_error_node_payload_has_err_prefix_and_key_file():
    payload = pr.build_error_node_payload(agent_id="codex-main", detail="connector 404")

    assert payload["id"].startswith("ERR-")
    assert payload["priority"] == "high"
    assert "scripts/3can_pr_harness.py" in payload["content"]["key_files"]
