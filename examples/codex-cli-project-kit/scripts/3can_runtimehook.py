#!/usr/bin/env python3
"""Thin semantic supervisor for a project-local Codex task.

RuntimeHook remembers Owner Intent and semantic review timing. It deliberately
does not own convergence selectors, candidate freshness, proof receipts, or Stop
correctness; those remain with 3can_convergence.py and Git.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATE_ROOT = Path(".codex/runtimehook")
STATE_PATH = STATE_ROOT / "state.json"
STATE_SCHEMA = "3can.runtimehook-state/v1"
MAX_STATE_BYTES = 64 * 1024
MAX_CONTEXT_CHARS = 4_000
INTENSITIES = {"light", "medium", "max"}
REVIEW_RESULTS = {
    "PASS",
    "PARTIAL",
    "FAIL",
    "UNVERIFIABLE",
    "CONTRADICTS",
    "UNREQUESTED",
}
ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class RuntimeHookError(ValueError):
    """The local RuntimeHook state or requested transition is unavailable."""


def _configure_utf8_stdio() -> None:
    os.environ["PYTHONUTF8"] = "1"
    os.environ["PYTHONIOENCODING"] = "utf-8"
    for stream, errors in (
        (sys.stdin, "strict"),
        (sys.stdout, "strict"),
        (sys.stderr, "backslashreplace"),
    ):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors=errors)


def _is_redirect(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeHookError("Git state is unavailable") from exc
    return completed


def _git_checkpoint(root: Path) -> tuple[str, bool]:
    head = _git(root, "rev-parse", "--verify", "HEAD")
    if head.returncode != 0:
        raise RuntimeHookError("Git HEAD is unavailable")
    reviewed_head = head.stdout.strip()
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", reviewed_head):
        raise RuntimeHookError("Git HEAD is invalid")
    status = _git(
        root,
        "status",
        "--porcelain=v1",
        "--untracked-files=normal",
        "--ignore-submodules=none",
    )
    if status.returncode != 0:
        raise RuntimeHookError("Git worktree status is unavailable")
    return reviewed_head, bool(status.stdout.strip())


def _repository_root(root: Path) -> Path:
    try:
        resolved = root.resolve()
    except (OSError, RuntimeError) as exc:
        raise RuntimeHookError("project root cannot be resolved") from exc
    completed = _git(resolved, "rev-parse", "--show-toplevel")
    if completed.returncode != 0:
        raise RuntimeHookError("RuntimeHook requires a Git worktree")
    try:
        actual = Path(completed.stdout.strip()).resolve()
    except (OSError, RuntimeError) as exc:
        raise RuntimeHookError("Git worktree root cannot be resolved") from exc
    if actual != resolved:
        raise RuntimeHookError("--root must be the exact Git worktree root")
    return resolved


def _validate_directory(path: Path, expected: Path, *, label: str) -> None:
    if _is_redirect(path) or not path.is_dir():
        raise RuntimeHookError(f"{label} is not a direct directory")
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError) as exc:
        raise RuntimeHookError(f"{label} cannot be resolved") from exc
    if resolved != expected:
        raise RuntimeHookError(f"{label} redirects outside its dedicated path")


def _state_root(root: Path, *, create: bool) -> Path | None:
    codex = root / ".codex"
    state_root = root / STATE_ROOT
    if not os.path.lexists(codex):
        if not create:
            return None
        codex.mkdir()
    _validate_directory(codex, root / ".codex", label=".codex directory")
    if not os.path.lexists(state_root):
        if not create:
            return None
        state_root.mkdir()
    _validate_directory(state_root, root / STATE_ROOT, label="RuntimeHook state root")

    tracked = _git(root, "ls-files", "--", STATE_ROOT.as_posix())
    ignored = _git(root, "check-ignore", "-q", "--", STATE_PATH.as_posix())
    if tracked.returncode != 0 or tracked.stdout.strip() or ignored.returncode != 0:
        raise RuntimeHookError(
            "RuntimeHook state root must be untracked and Git ignored"
        )
    return state_root


def _text(value: Any, *, label: str) -> str:
    if not isinstance(value, str):
        raise RuntimeHookError(f"{label} must be text")
    result = value.strip()
    if not result:
        raise RuntimeHookError(f"{label} must not be empty")
    return result


def _validate_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema") != STATE_SCHEMA:
        raise RuntimeHookError(f"state schema must be {STATE_SCHEMA}")
    status = value.get("status")
    if status not in {"active", "disabled_by_owner"}:
        raise RuntimeHookError("RuntimeHook status is invalid")
    activation_id = _text(value.get("activation_id"), label="activation_id")
    if not ID_PATTERN.fullmatch(activation_id):
        raise RuntimeHookError("activation_id is invalid")

    intent = value.get("run_intent")
    if not isinstance(intent, dict):
        raise RuntimeHookError("RUN_INTENT must be an object")
    _text(intent.get("goal"), label="RUN_INTENT goal")
    acceptance = intent.get("acceptance")
    if not isinstance(acceptance, list) or not acceptance:
        raise RuntimeHookError("RUN_INTENT acceptance must be a non-empty list")
    seen: set[str] = set()
    for item in acceptance:
        if not isinstance(item, dict):
            raise RuntimeHookError("RUN_INTENT acceptance entry must be an object")
        criterion_id = _text(item.get("id"), label="Acceptance ID")
        if not ID_PATTERN.fullmatch(criterion_id) or criterion_id in seen:
            raise RuntimeHookError("Acceptance IDs must be valid and unique")
        seen.add(criterion_id)
        _text(item.get("text"), label=f"Acceptance {criterion_id}")
    non_goals = intent.get("non_goals", [])
    if not isinstance(non_goals, list):
        raise RuntimeHookError("RUN_INTENT non_goals must be a list")
    for item in non_goals:
        _text(item, label="RUN_INTENT non-goal")

    intensity = value.get("internal_intensity")
    if not isinstance(intensity, dict) or intensity.get("level") not in INTENSITIES:
        raise RuntimeHookError("internal intensity is invalid")
    _text(intensity.get("reason"), label="internal intensity reason")

    episode = value.get("current_episode")
    if episode is not None:
        _text(episode, label="episode objective")

    review = value.get("semantic_review")
    if not isinstance(review, dict):
        raise RuntimeHookError("semantic review must be an object")
    result = review.get("result")
    if result != "PENDING" and result not in REVIEW_RESULTS:
        raise RuntimeHookError("semantic review result is invalid")
    if result != "PENDING":
        if review.get("stage") not in {"episode", "final"}:
            raise RuntimeHookError("semantic review stage is invalid")
        _text(review.get("reference"), label="semantic review reference")
    reviewed_git_head = review.get("reviewed_git_head")
    if result == "PASS" and review.get("stage") == "final":
        if not isinstance(reviewed_git_head, str) or not re.fullmatch(
            r"(?:[0-9a-f]{40}|[0-9a-f]{64})", reviewed_git_head
        ):
            raise RuntimeHookError("final semantic PASS requires reviewed_git_head")
    elif reviewed_git_head is not None:
        raise RuntimeHookError("reviewed_git_head belongs only to final semantic PASS")
    return value


def _load_state(root: Path) -> dict[str, Any] | None:
    state_path = root / STATE_PATH
    if not os.path.lexists(state_path):
        return None
    state_root = _state_root(root, create=False)
    if state_root is None or _is_redirect(state_path) or not state_path.is_file():
        raise RuntimeHookError("RuntimeHook state file is not a direct file")
    if state_path.stat().st_size > MAX_STATE_BYTES:
        raise RuntimeHookError("RuntimeHook state exceeds the bounded size")
    try:
        value = json.loads(state_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeHookError("RuntimeHook state is unreadable") from exc
    return _validate_state(value)


def _context(
    state: dict[str, Any], *, review_result: str | None = None
) -> str:
    intent = state["run_intent"]
    acceptance = "; ".join(
        f"{item['id']}={item['text']}" for item in intent["acceptance"]
    )
    non_goals = "; ".join(intent["non_goals"])
    non_goal_text = f" Non-goals: {non_goals}." if non_goals else ""
    episode = state.get("current_episode")
    episode_text = f" Current episode: {episode}." if episode else ""
    review = state["semantic_review"]
    review_text = f" Semantic review: {review_result or review['result']}"
    if review["result"] != "PENDING":
        review_text += f" ({review['stage']}, {review['reference']})"
    message = (
        f"RuntimeHook semantic context [{state['activation_id']}]. "
        f"RUN_INTENT: {intent['goal']}. Acceptance: {acceptance}.{non_goal_text} "
        f"Internal intensity: {state['internal_intensity']['level']} because "
        f"{state['internal_intensity']['reason']}.{episode_text}{review_text}. "
        "At the selected review boundary, check goal/acceptance drift, decisions "
        "hardcoded without a requirement or declared contract, hidden fallback or "
        "stale state, and unrequested behavior. Use existing targeted strict "
        "evidence only for criteria needing mechanical proof. Git and the project "
        "convergence hook remain authoritative for evidence freshness and Stop."
    )
    if len(message) > MAX_CONTEXT_CHARS:
        raise RuntimeHookError("RUN_INTENT is too large for native Hook reinjection")
    return message


def _stale_review_reasons(root: Path, review: dict[str, Any]) -> list[str]:
    if review["result"] != "PASS" or review.get("stage") != "final":
        return []
    current_head, dirty = _git_checkpoint(root)
    reasons = []
    if current_head != review["reviewed_git_head"]:
        reasons.append("Git HEAD changed")
    if dirty:
        reasons.append("the worktree is dirty")
    return reasons


def _write_state(root: Path, value: dict[str, Any]) -> None:
    value = _validate_state(value)
    _context(value)
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if len(payload.encode("utf-8")) > MAX_STATE_BYTES:
        raise RuntimeHookError("RuntimeHook state exceeds the bounded size")
    state_root = _state_root(root, create=True)
    assert state_root is not None
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".state.", suffix=".tmp", dir=str(state_root)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _state_root(root, create=False)
        os.replace(temporary_name, root / STATE_PATH)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def _acceptance(values: list[str]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in values:
        criterion_id, separator, text = raw.partition("=")
        criterion_id, text = criterion_id.strip(), text.strip()
        if (
            not separator
            or not ID_PATTERN.fullmatch(criterion_id)
            or not text
            or criterion_id in seen
        ):
            raise RuntimeHookError(
                "--acceptance must use unique STABLE-ID=observable text"
            )
        seen.add(criterion_id)
        result.append({"id": criterion_id, "text": text})
    if not result:
        raise RuntimeHookError("at least one --acceptance is required")
    return result


def activate(args: argparse.Namespace) -> dict[str, Any]:
    root = _repository_root(args.root)
    activation_id = f"rh-{uuid.uuid4().hex[:16]}"
    state = {
        "schema": STATE_SCHEMA,
        "status": "active",
        "activation_id": activation_id,
        "run_intent": {
            "goal": _text(args.goal, label="goal"),
            "acceptance": _acceptance(args.acceptance),
            "non_goals": [_text(item, label="non-goal") for item in args.non_goal],
        },
        "internal_intensity": {
            "level": args.intensity,
            "reason": _text(args.reason, label="intensity reason"),
        },
        "current_episode": (
            _text(args.episode, label="episode objective")
            if args.episode.strip()
            else None
        ),
        "semantic_review": {
            "stage": None,
            "result": "PENDING",
            "reference": None,
            "reviewed_git_head": None,
        },
    }
    _write_state(root, state)
    return {
        "ok": True,
        "status": "active",
        "activation_id": activation_id,
        "internal_intensity": state["internal_intensity"],
        "state_path": STATE_PATH.as_posix(),
    }


def disable(args: argparse.Namespace) -> dict[str, Any]:
    root = _repository_root(args.root)
    state = _load_state(root)
    if state is None:
        return {"ok": True, "status": "inactive", "changed": False}
    if state["status"] == "disabled_by_owner":
        return {"ok": True, "status": "disabled_by_owner", "changed": False}
    state = {
        **state,
        "status": "disabled_by_owner",
    }
    _write_state(root, state)
    return {
        "ok": True,
        "status": "disabled_by_owner",
        "changed": True,
        "state_retained": True,
    }


def record_review(args: argparse.Namespace) -> dict[str, Any]:
    root = _repository_root(args.root)
    state = _load_state(root)
    if state is None or state["status"] != "active":
        raise RuntimeHookError("no active RuntimeHook semantic task")
    next_objective = args.next_objective.strip()
    if args.stage == "episode" and not next_objective:
        raise RuntimeHookError("episode review requires --next-objective")
    reference = _text(args.reference, label="semantic review reference")
    reviewed_git_head = None
    if args.stage == "final" and args.result == "PASS":
        reviewed_git_head, dirty = _git_checkpoint(root)
        if dirty:
            raise RuntimeHookError(
                "final semantic PASS requires a clean Git checkpoint"
            )
    state = {
        **state,
        "semantic_review": {
            "stage": args.stage,
            "result": args.result,
            "reference": reference,
            "reviewed_git_head": reviewed_git_head,
        },
        "current_episode": (
            next_objective if args.stage == "episode" else state.get("current_episode")
        ),
    }
    _write_state(root, state)
    return {
        "ok": True,
        "status": "review_recorded",
        "activation_id": state["activation_id"],
        "stage": args.stage,
        "result": args.result,
        "reference": state["semantic_review"]["reference"],
        "reviewed_git_head": reviewed_git_head,
    }


def status(args: argparse.Namespace) -> dict[str, Any]:
    root = _repository_root(args.root)
    state = _load_state(root)
    if state is None:
        return {"ok": True, "status": "inactive"}
    return {"ok": True, **state}


def _hook_error(message: str) -> int:
    print(
        json.dumps(
            {
                "systemMessage": (
                    f"RuntimeHook semantic context is UNAVAILABLE: {message}. "
                    "Safe local work may continue; independent project and PR15 "
                    "evidence gates remain authoritative."
                )
            },
            ensure_ascii=False,
        )
    )
    return 0


def hook(args: argparse.Namespace) -> int:
    try:
        try:
            root = args.root.resolve()
        except (OSError, RuntimeError) as exc:
            raise RuntimeHookError("project root cannot be resolved") from exc
        if not os.path.lexists(root / STATE_PATH):
            return 0
        try:
            payload = json.load(sys.stdin)
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeHookError("native Hook payload is unreadable") from exc
        state = _load_state(root)
        if state is None or state["status"] != "active":
            return 0
        event = payload.get("hook_event_name")
        if event == "SessionStart" and payload.get("source") in {
            "startup",
            "resume",
            "clear",
            "compact",
        }:
            stale_reasons = _stale_review_reasons(
                root, state["semantic_review"]
            )
            print(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "SessionStart",
                            "additionalContext": _context(
                                state,
                                review_result="STALE" if stale_reasons else None,
                            ),
                        }
                    },
                    ensure_ascii=False,
                )
            )
        elif event == "Stop":
            review = state["semantic_review"]
            if review["result"] == "PENDING" or review.get("stage") != "final":
                message = (
                    "RuntimeHook final semantic review remains due for activation "
                    f"{state['activation_id']}. Review goal drift, unjustified "
                    "hardcoding, hidden fallback/stale state, and unrequested behavior. "
                    "This reminder does not replace or override the project Stop gate."
                )
            elif review["result"] != "PASS":
                message = (
                    "RuntimeHook final semantic review is recorded as "
                    f"{review['result']} at {review['reference']}. Git and the project "
                    "convergence hook remain authoritative for completion."
                )
            else:
                stale_reasons = _stale_review_reasons(root, review)
                if not stale_reasons:
                    return 0
                message = (
                    "RuntimeHook final semantic review is STALE because "
                    f"{' and '.join(stale_reasons)} after {review['reference']}. "
                    "Repeat the semantic review on a clean Git checkpoint. This "
                    "reminder does not replace or override the project Stop gate."
                )
            print(json.dumps({"systemMessage": message}, ensure_ascii=False))
        return 0
    except (RuntimeHookError, OSError, subprocess.SubprocessError) as exc:
        return _hook_error(str(exc))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Control the optional 3CAN RuntimeHook semantic supervisor."
    )
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    sub = parser.add_subparsers(dest="command", required=True)

    on = sub.add_parser("on", help="Record the current semantic RUN_INTENT.")
    on.add_argument("--goal", required=True)
    on.add_argument("--acceptance", action="append", default=[])
    on.add_argument("--non-goal", action="append", default=[])
    on.add_argument("--intensity", choices=sorted(INTENSITIES), required=True)
    on.add_argument("--reason", required=True)
    on.add_argument("--episode", default="")

    sub.add_parser("off", help="Disable only RuntimeHook semantic reminders.")

    review = sub.add_parser("review", help="Record one semantic review result.")
    review.add_argument("--stage", choices=["episode", "final"], required=True)
    review.add_argument("--result", choices=sorted(REVIEW_RESULTS), required=True)
    review.add_argument("--reference", required=True)
    review.add_argument("--next-objective", default="")

    sub.add_parser("status", help="Show the current local RuntimeHook state.")
    sub.add_parser("hook", help="Run as a non-owning Codex lifecycle reminder.")
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_utf8_stdio()
    args = build_parser().parse_args(argv)
    if args.command == "hook":
        return hook(args)
    try:
        if args.command == "on":
            output = activate(args)
        elif args.command == "off":
            output = disable(args)
        elif args.command == "review":
            output = record_review(args)
        elif args.command == "status":
            output = status(args)
        else:
            raise RuntimeHookError(f"unsupported command: {args.command}")
        exit_code = 0
    except (RuntimeHookError, OSError, subprocess.SubprocessError) as exc:
        output = {"ok": False, "status": "UNAVAILABLE", "error": str(exc)}
        exit_code = 2
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
