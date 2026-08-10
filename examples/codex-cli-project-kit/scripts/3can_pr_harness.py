#!/usr/bin/env python3
"""3CAN PR harness for local GitHub PR creation.

Purpose:
- Prefer the documented local wincred + REST path when gh is missing or the
  GitHub connector cannot access a private repo.
- Never print or persist token values.
- Provide a Codex hook that changes the next action instead of only logging an
  error after the fact.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_API_BASE = "https://api.github.com"
ERROR_NODE_ID = "ERR-20260508-github-pr-local-rest-fallback-required"


@dataclass(frozen=True)
class RemoteRepo:
    owner: str
    repo: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"


def _print_json(data: Any) -> None:
    sys.stdout.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def run_git(args: list[str], *, cwd: Path = PROJECT_ROOT, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        check=False,
    )


def parse_github_remote(remote_url: str) -> RemoteRepo:
    """Parse common GitHub HTTPS/SSH remotes into owner/repo."""
    value = remote_url.strip()
    patterns = (
        r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/.]+)(?:\.git)?/?$",
        r"^git@github\.com:(?P<owner>[^/]+)/(?P<repo>[^/.]+)(?:\.git)?$",
    )
    for pattern in patterns:
        match = re.match(pattern, value)
        if match:
            return RemoteRepo(match.group("owner"), match.group("repo"))
    raise ValueError(f"Unsupported GitHub remote URL: {remote_url}")


def detect_repo(cwd: Path = PROJECT_ROOT, remote: str = "origin") -> RemoteRepo:
    result = run_git(["remote", "get-url", remote], cwd=cwd)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"git remote get-url {remote} failed")
    return parse_github_remote(result.stdout.strip())


def detect_branch(cwd: Path = PROJECT_ROOT) -> str:
    result = run_git(["branch", "--show-current"], cwd=cwd)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git branch --show-current failed")
    return result.stdout.strip()


def _credential_fill_token(cwd: Path = PROJECT_ROOT) -> str:
    result = run_git(
        ["credential", "fill"],
        cwd=cwd,
        stdin="protocol=https\nhost=github.com\n\n",
    )
    if result.returncode != 0:
        return ""
    for line in result.stdout.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1].strip()
    return ""


def load_token(cwd: Path = PROJECT_ROOT) -> str:
    """Load a GitHub token from env or git credential manager without printing it."""
    return (
        os.environ.get("GITHUB_TOKEN", "").strip()
        or os.environ.get("GH_TOKEN", "").strip()
        or _credential_fill_token(cwd)
    )


def request_github(
    *,
    api_base: str,
    token: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    query: dict[str, str] | None = None,
) -> tuple[int, Any]:
    url = f"{api_base.rstrip('/')}{path}"
    if query:
        url += "?" + urlencode(query)
    body = None
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {token}",
    }
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(req, timeout=20) as response:
            raw = response.read().decode("utf-8")
            return int(response.status), json.loads(raw) if raw else {}
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            data = {"raw": raw}
        return int(exc.code), data
    except URLError as exc:
        return 0, {"message": str(exc.reason)}


def find_existing_pr(
    *,
    repo: RemoteRepo,
    head_branch: str,
    base_branch: str,
    token: str,
    api_base: str = DEFAULT_API_BASE,
) -> dict[str, Any] | None:
    status, data = request_github(
        api_base=api_base,
        token=token,
        method="GET",
        path=f"/repos/{repo.full_name}/pulls",
        query={"state": "open", "head": f"{repo.owner}:{head_branch}", "base": base_branch},
    )
    if status == 200 and isinstance(data, list) and data:
        item = data[0]
        return {"number": item.get("number"), "html_url": item.get("html_url")}
    return None


def create_pr(
    *,
    repo: RemoteRepo,
    head_branch: str,
    base_branch: str,
    title: str,
    body: str,
    approval_id: str,
    cwd: Path = PROJECT_ROOT,
    api_base: str = DEFAULT_API_BASE,
) -> dict[str, Any]:
    if not approval_id:
        return {
            "ok": False,
            "status": "block",
            "reason": "approval_required",
            "message": "Creating a GitHub PR is an external publish action. Pass --approval-id from the user/task approval.",
        }

    token = load_token(cwd)
    if not token:
        return {
            "ok": False,
            "status": "block",
            "reason": "github_token_unavailable",
            "message": "No GITHUB_TOKEN/GH_TOKEN or git credential-manager token was available. Token value was not printed.",
            "fallback_url": f"https://github.com/{repo.full_name}/pull/new/{head_branch}",
        }

    existing = find_existing_pr(
        repo=repo,
        head_branch=head_branch,
        base_branch=base_branch,
        token=token,
        api_base=api_base,
    )
    if existing:
        return {"ok": True, "status": "exists", "repo": repo.full_name, **existing}

    payload = {
        "title": title,
        "head": head_branch,
        "base": base_branch,
        "body": body,
        "maintainer_can_modify": True,
    }
    status, data = request_github(
        api_base=api_base,
        token=token,
        method="POST",
        path=f"/repos/{repo.full_name}/pulls",
        payload=payload,
    )
    if status in {200, 201} and isinstance(data, dict):
        return {
            "ok": True,
            "status": "created",
            "repo": repo.full_name,
            "number": data.get("number"),
            "html_url": data.get("html_url"),
        }
    return {
        "ok": False,
        "status": "failed",
        "http_status": status,
        "repo": repo.full_name,
        "message": data.get("message") if isinstance(data, dict) else str(data),
        "fallback_url": f"https://github.com/{repo.full_name}/pull/new/{head_branch}",
    }


def build_check_report(cwd: Path = PROJECT_ROOT, *, check_token: bool = False) -> dict[str, Any]:
    repo = detect_repo(cwd)
    branch = detect_branch(cwd)
    helper = run_git(["config", "--get", "credential.helper"], cwd=cwd)
    token_present = None
    if check_token:
        token_present = bool(load_token(cwd))
    return {
        "ok": True,
        "status": "ready",
        "repo": repo.full_name,
        "branch": branch,
        "gh_available": shutil.which("gh") is not None,
        "credential_helper": helper.stdout.strip() if helper.returncode == 0 else "",
        "token_present": token_present,
        "recommended_pr_path": "local_rest_wincred" if shutil.which("gh") is None else "gh_or_local_rest",
        "create_command_shape": (
            "python scripts/3can_pr_harness.py create-pr --approval-id <approval> "
            "--title \"...\" --body-file <file-or-use-body> --base main"
        ),
        "error_node_id": ERROR_NODE_ID,
    }


def build_error_node_payload(*, agent_id: str, detail: str = "") -> dict[str, Any]:
    return {
        "id": ERROR_NODE_ID,
        "name": "GitHub PR creation must use local REST fallback when gh/connector fails",
        "cluster": "错误与教训",
        "type": "feedback",
        "status": "active",
        "content": {
            "description": (
                "GitHub PR creation repeatedly failed when Codex tried gh CLI or the GitHub connector. "
                "For this repo, use local git push plus wincred/GITHUB_TOKEN REST PR creation, then manual human merge."
            ),
            "current_state": detail or "Active guardrail: prefer scripts/3can_pr_harness.py create-pr after push.",
            "key_files": ["scripts/3can_pr_harness.py", ".codex/hooks.json", "DOC-github-pr-without-gh-cli-20260423"],
            "notes": (
                "Do not stop at a manual PR link unless local REST creation also failed. "
                "Do not print or write tokens. Do not use git add . in dirty worktrees. "
                "PR creation needs approval-id because it is an external publish action."
            ),
        },
        "activation_keywords": [
            "ERR",
            "github",
            "pull request",
            "gh cli",
            "connector 404",
            "wincred",
            "local rest",
            "manual merge",
            "pr fallback",
        ],
        "primary_author": agent_id or "codex-main",
        "priority": "high",
    }


def _hook_json(data: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    if str(data.get("hook_event_name") or "") != "PreToolUse":
        return 0, {"continue": True}

    tool_name = str(data.get("tool_name") or "")
    tool_input = data.get("tool_input") or {}
    if isinstance(tool_input, dict):
        command = str(tool_input.get("command") or tool_input)
    else:
        command = str(tool_input)

    connector_pr = tool_name.endswith("_create_pull_request") or "_create_pull_request" in tool_name
    gh_pr_create = bool(re.search(r"\bgh\s+pr\s+create\b", command))
    git_push = bool(re.search(r"\bgit\s+push\b", command))

    if connector_pr:
        return 2, {
            "decision": "block",
            "reason": (
                "3CAN PR harness: GitHub connector PR creation has repeatedly returned 404 for this private repo. "
                "Use local git push plus scripts/3can_pr_harness.py create-pr with approval-id. "
                f"Related ERR node: {ERROR_NODE_ID}."
            ),
        }

    if gh_pr_create and shutil.which("gh") is None:
        return 2, {
            "decision": "block",
            "reason": (
                "3CAN PR harness: gh CLI is not installed. Use scripts/3can_pr_harness.py create-pr, "
                "which uses GITHUB_TOKEN/GH_TOKEN or git credential-manager in memory."
            ),
        }

    if git_push:
        return 0, {
            "systemMessage": (
                "3CAN PR harness: after push, create the PR locally with "
                "scripts/3can_pr_harness.py create-pr --approval-id <approval> --title \"...\" --body-file <file>. "
                "Do not fall back to only giving a manual link unless local REST creation fails."
            )
        }

    return 0, {"continue": True}


def run_hook() -> int:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as exc:
        _print_json({"systemMessage": f"3CAN PR hook received invalid JSON: {exc}"})
        return 0
    code, payload = _hook_json(data)
    _print_json(payload)
    return code


def _read_body(args: argparse.Namespace) -> str:
    if args.body_file:
        return Path(args.body_file).read_text(encoding="utf-8")
    return args.body or ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="3CAN local GitHub PR harness.")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="Inspect local PR creation readiness.")
    check.add_argument("--cwd", default=str(PROJECT_ROOT))
    check.add_argument("--check-token", action="store_true")

    create = sub.add_parser("create-pr", help="Create a GitHub PR through local REST.")
    create.add_argument("--cwd", default=str(PROJECT_ROOT))
    create.add_argument("--repo", default="")
    create.add_argument("--head", default="")
    create.add_argument("--base", default="main")
    create.add_argument("--title", required=True)
    create.add_argument("--body", default="")
    create.add_argument("--body-file", default="")
    create.add_argument("--approval-id", default=os.environ.get("THREECAN_APPROVAL_ID", ""))
    create.add_argument("--api-base", default=DEFAULT_API_BASE)

    err = sub.add_parser("error-node", help="Print the ERR node payload for 3CAN writeback.")
    err.add_argument("--agent-id", default="codex-main")
    err.add_argument("--detail", default="")

    sub.add_parser("hook", help="Run as a Codex PreToolUse hook.")

    args = parser.parse_args(argv)
    if args.command == "check":
        _print_json(build_check_report(Path(args.cwd), check_token=args.check_token))
        return 0
    if args.command == "create-pr":
        cwd = Path(args.cwd)
        repo = parse_github_remote(f"https://github.com/{args.repo}.git") if args.repo else detect_repo(cwd)
        head = args.head or detect_branch(cwd)
        result = create_pr(
            repo=repo,
            head_branch=head,
            base_branch=args.base,
            title=args.title,
            body=_read_body(args),
            approval_id=args.approval_id,
            cwd=cwd,
            api_base=args.api_base,
        )
        _print_json(result)
        return 0 if result.get("ok") else 1
    if args.command == "error-node":
        _print_json(build_error_node_payload(agent_id=args.agent_id, detail=args.detail))
        return 0
    if args.command == "hook":
        return run_hook()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
