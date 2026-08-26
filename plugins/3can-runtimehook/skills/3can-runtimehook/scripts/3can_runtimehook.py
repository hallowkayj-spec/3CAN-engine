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
LOCAL_EXCLUDE_RULE = "/.codex/runtimehook/"
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
BOUNDARY_KINDS = {"activation", "git", "stage", "episode"}
MAX_BOUNDARY_LABEL_CHARS = 240
ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
SESSION_FAST_PATH = (
    "3CAN fast path: start safe local work immediately. Git owns exact source "
    "truth; 3CAN supplies durable project meaning, relevant history, and typed "
    "coordination. Use route or retrieval only when that context improves the "
    "decision. Obtain a fresh ticket just in time only for an operation whose "
    "current project contract requires one; bind the current AgentId, project "
    "identity/namespace, physical workspace or worktree, and required Workorder, "
    "target, and scope, then honor its returned TTL and completion deadline. On a "
    "typed refusal, never blind-retry: refresh expired state once only for the "
    "still-pending operation, reread a version conflict, and stop on an identity "
    "or digest mismatch. Write durable meaning only at AUTO_CLOSEOUT or when the "
    "Owner requests it. This orientation does not activate RuntimeHook or replace "
    "project safety and evidence gates."
)


class RuntimeHookError(ValueError):
    """The local RuntimeHook state or requested transition is unavailable."""


def _configure_utf8_stdio() -> None:
    os.environ["PYTHONUTF8"] = "1"
    os.environ["PYTHONIOENCODING"] = "utf-8"
    if os.name == "nt":
        os.environ["NoDefaultCurrentDirectoryInExePath"] = "1"
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


def _worktree_hint(path: Path) -> Path | None:
    try:
        current = path.resolve()
    except (OSError, RuntimeError) as exc:
        raise RuntimeHookError("working directory cannot be resolved") from exc
    boundary = None
    for directory in (current, *current.parents):
        if os.path.lexists(directory / ".git"):
            boundary = directory
    return boundary


def _git_executable(root: Path) -> Path:
    try:
        resolved_root = root.resolve()
    except (OSError, RuntimeError) as exc:
        raise RuntimeHookError("working directory cannot be resolved") from exc
    untrusted_root = _worktree_hint(resolved_root) or resolved_root
    executable_name = "git.exe" if os.name == "nt" else "git"
    for raw_directory in os.environ.get("PATH", "").split(os.pathsep):
        expanded = os.path.expandvars(raw_directory.strip().strip('"'))
        if not expanded:
            continue
        directory = Path(expanded).expanduser()
        if not directory.is_absolute():
            continue
        try:
            candidate = (directory / executable_name).resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if candidate.is_relative_to(untrusted_root) or untrusted_root.is_relative_to(
            candidate.parent
        ):
            continue
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise RuntimeHookError("Git executable is unavailable from trusted PATH entries")


def _git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    try:
        executable = _git_executable(root)
        completed = subprocess.run(
            [str(executable), "-C", str(root), *arguments],
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


def _git_head(root: Path) -> str:
    head = _git(root, "rev-parse", "--verify", "HEAD")
    if head.returncode != 0:
        raise RuntimeHookError("Git HEAD is unavailable")
    current_head = head.stdout.strip()
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", current_head):
        raise RuntimeHookError("Git HEAD is invalid")
    return current_head


def _git_checkpoint(root: Path) -> tuple[str, bool]:
    current_head = _git_head(root)
    status = _git(
        root,
        "status",
        "--porcelain=v1",
        "--untracked-files=normal",
        "--ignore-submodules=none",
    )
    if status.returncode != 0:
        raise RuntimeHookError("Git worktree status is unavailable")
    return current_head, bool(status.stdout.strip())


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


def _local_exclude_path(root: Path) -> Path:
    exclude = _git(
        root,
        "rev-parse",
        "--path-format=absolute",
        "--git-path",
        "info/exclude",
    )
    common = _git(
        root,
        "rev-parse",
        "--path-format=absolute",
        "--git-common-dir",
    )
    if exclude.returncode != 0 or common.returncode != 0:
        raise RuntimeHookError("local Git exclude path is unavailable")
    exclude_path = Path(exclude.stdout.strip())
    expected = Path(common.stdout.strip()) / "info" / "exclude"
    if not exclude_path.is_absolute() or exclude_path != expected:
        raise RuntimeHookError("local Git exclude path is invalid")
    info_dir = exclude_path.parent
    if not info_dir.is_dir() or _is_redirect(info_dir):
        raise RuntimeHookError("local Git info directory is not direct")
    if os.path.lexists(exclude_path) and (
        _is_redirect(exclude_path) or not exclude_path.is_file()
    ):
        raise RuntimeHookError("local Git exclude file is not direct")
    return exclude_path


def _ensure_state_ignored(root: Path) -> bool:
    _state_root(root, create=False)
    tracked = _git(root, "ls-files", "--", STATE_ROOT.as_posix())
    if tracked.returncode != 0 or tracked.stdout.strip():
        raise RuntimeHookError(
            "RuntimeHook state root must be untracked and Git ignored"
        )
    ignored = _git(root, "check-ignore", "-q", "--", STATE_PATH.as_posix())
    if ignored.returncode == 0:
        return False
    if ignored.returncode != 1:
        raise RuntimeHookError("RuntimeHook Git ignore status is unavailable")

    exclude_path = _local_exclude_path(root)
    flags = os.O_RDWR | os.O_CREAT | os.O_APPEND
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(exclude_path, flags, 0o666)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise RuntimeHookError("local Git exclude file is not direct")
            needs_newline = False
            if info.st_size:
                os.lseek(descriptor, -1, os.SEEK_END)
                needs_newline = os.read(descriptor, 1) not in {b"\n", b"\r"}
            entry = (
                (b"\n" if needs_newline else b"")
                + b"# 3CAN RuntimeHook local state\n"
                + LOCAL_EXCLUDE_RULE.encode("ascii")
                + b"\n"
            )
            remaining = memoryview(entry)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError("local Git exclude write made no progress")
                remaining = remaining[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise RuntimeHookError("local Git exclude file is not writable") from exc

    verified = _git(root, "check-ignore", "-q", "--", STATE_PATH.as_posix())
    if verified.returncode != 0:
        raise RuntimeHookError("RuntimeHook local Git exclude did not take effect")
    return True


def _text(value: Any, *, label: str) -> str:
    if not isinstance(value, str):
        raise RuntimeHookError(f"{label} must be text")
    result = value.strip()
    if not result:
        raise RuntimeHookError(f"{label} must not be empty")
    return result


def _boundary_label(value: Any, *, label: str) -> str:
    result = _text(value, label=label)
    if len(result) > MAX_BOUNDARY_LABEL_CHARS:
        raise RuntimeHookError(
            f"{label} exceeds {MAX_BOUNDARY_LABEL_CHARS} characters"
        )
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

    boundary = value.get("boundary")
    if boundary is not None:
        if not isinstance(boundary, dict):
            raise RuntimeHookError("review boundary must be an object")
        sequence = boundary.get("sequence")
        reviewed_sequence = boundary.get("reviewed_sequence")
        if (
            not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or sequence < 1
            or not isinstance(reviewed_sequence, int)
            or isinstance(reviewed_sequence, bool)
            or reviewed_sequence < 0
            or reviewed_sequence > sequence
        ):
            raise RuntimeHookError("review boundary sequence is invalid")
        observed_git_head = boundary.get("observed_git_head")
        if not isinstance(observed_git_head, str) or not re.fullmatch(
            r"(?:[0-9a-f]{40}|[0-9a-f]{64})", observed_git_head
        ):
            raise RuntimeHookError("review boundary Git HEAD is invalid")
        if boundary.get("last_kind") not in BOUNDARY_KINDS:
            raise RuntimeHookError("review boundary kind is invalid")
        _boundary_label(boundary.get("last_label"), label="review boundary label")
    return value


def _with_boundary(root: Path, state: dict[str, Any]) -> dict[str, Any]:
    """Adopt pre-boundary v1 state without a migration subsystem."""
    if state.get("boundary") is not None:
        return state
    current_head = _git_head(root)
    review = state["semantic_review"]
    reviewed_head = review.get("reviewed_git_head")
    return {
        **state,
        "boundary": {
            "sequence": 1,
            "reviewed_sequence": 0 if review["result"] == "PENDING" else 1,
            "observed_git_head": reviewed_head or current_head,
            "last_kind": "activation",
            "last_label": "Existing RuntimeHook state adopted",
        },
    }


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
    return _validate_state(_with_boundary(root, _validate_state(value)))


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
    boundary = state["boundary"]
    effective_review = review_result or review["result"]
    if (
        effective_review == "PENDING"
        and boundary["last_kind"] == "git"
        and boundary["reviewed_sequence"] > 0
        and boundary["reviewed_sequence"] < boundary["sequence"]
    ):
        effective_review = "STALE"
    review_text = f" Semantic review: {effective_review}"
    if review["result"] != "PENDING":
        review_text += f" ({review['stage']}, {review['reference']})"
    boundary_due = boundary["reviewed_sequence"] < boundary["sequence"]
    boundary_text = (
        f" Last boundary {boundary['sequence']}: {boundary['last_kind']}="
        f"{boundary['last_label']}. Boundary review: "
        f"{'DUE' if boundary_due else 'CURRENT'}."
    )
    message = (
        f"RuntimeHook semantic context [{state['activation_id']}]. "
        f"RUN_INTENT: {intent['goal']}. Acceptance: {acceptance}.{non_goal_text} "
        f"Internal intensity: {state['internal_intensity']['level']} because "
        f"{state['internal_intensity']['reason']}.{episode_text}{review_text}."
        f"{boundary_text} "
        "At the selected review boundary, check goal/acceptance drift, decisions "
        "hardcoded without a requirement or declared contract, hidden fallback or "
        "stale state, and unrequested behavior. Use existing targeted strict "
        "evidence only for criteria needing mechanical proof. Git and the project "
        "convergence hook remain authoritative for evidence freshness and Stop. "
        "The current Owner prompt is authoritative; if it changes this task, "
        "update or replace RUN_INTENT before acting."
    )
    if len(message) > MAX_CONTEXT_CHARS:
        raise RuntimeHookError("RUN_INTENT is too large for native Hook reinjection")
    return message


def _stale_review_reasons(root: Path, state: dict[str, Any]) -> list[str]:
    review = state["semantic_review"]
    if review["result"] != "PASS" or review.get("stage") != "final":
        return []
    current_head, dirty = _git_checkpoint(root)
    reasons = []
    if current_head != review["reviewed_git_head"]:
        reasons.append("Git HEAD changed")
    if dirty:
        reasons.append("the worktree is dirty")
    boundary = state["boundary"]
    if boundary["reviewed_sequence"] < boundary["sequence"]:
        reasons.append("a newer review boundary is due")
    return reasons


def _mark_boundary(
    state: dict[str, Any],
    *,
    kind: str,
    label: str,
    observed_git_head: str,
) -> dict[str, Any]:
    if kind not in BOUNDARY_KINDS - {"activation"}:
        raise RuntimeHookError("only Git, stage, or episode boundaries may be added")
    boundary = state["boundary"]
    return {
        **state,
        "boundary": {
            **boundary,
            "sequence": boundary["sequence"] + 1,
            "observed_git_head": observed_git_head,
            "last_kind": kind,
            "last_label": _boundary_label(label, label="review boundary label"),
        },
        "semantic_review": {
            "stage": None,
            "result": "PENDING",
            "reference": None,
            "reviewed_git_head": None,
        },
    }


def _sync_git_boundary(
    root: Path, state: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    current_head = _git_head(root)
    previous_head = state["boundary"]["observed_git_head"]
    if current_head == previous_head:
        return state, False
    state = _mark_boundary(
        state,
        kind="git",
        label=f"Git HEAD {previous_head[:12]} -> {current_head[:12]}",
        observed_git_head=current_head,
    )
    _write_state(root, state)
    return state, True


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
    local_exclude_added = _ensure_state_ignored(root)
    current_head = _git_head(root)
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
        "boundary": {
            "sequence": 1,
            "reviewed_sequence": 0,
            "observed_git_head": current_head,
            "last_kind": "activation",
            "last_label": "RuntimeHook activated",
        },
    }
    _write_state(root, state)
    return {
        "ok": True,
        "status": "active",
        "activation_id": activation_id,
        "internal_intensity": state["internal_intensity"],
        "state_path": STATE_PATH.as_posix(),
        "local_exclude_added": local_exclude_added,
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
    state, _git_changed = _sync_git_boundary(root, state)
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
        "boundary": {
            **state["boundary"],
            "reviewed_sequence": state["boundary"]["sequence"],
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
        "reviewed_boundary_sequence": state["boundary"]["reviewed_sequence"],
    }


def record_checkpoint(args: argparse.Namespace) -> dict[str, Any]:
    root = _repository_root(args.root)
    state = _load_state(root)
    if state is None or state["status"] != "active":
        raise RuntimeHookError("no active RuntimeHook semantic task")
    state, _git_changed = _sync_git_boundary(root, state)
    current_head = _git_head(root)
    state = _mark_boundary(
        state,
        kind=args.kind,
        label=args.label,
        observed_git_head=current_head,
    )
    next_objective = args.next_objective.strip()
    if next_objective:
        state = {
            **state,
            "current_episode": _text(
                next_objective, label="next episode objective"
            ),
        }
    _write_state(root, state)
    return {
        "ok": True,
        "status": "review_due",
        "activation_id": state["activation_id"],
        "boundary": state["boundary"],
    }


def status(args: argparse.Namespace) -> dict[str, Any]:
    root = _repository_root(args.root)
    state = _load_state(root)
    if state is None:
        return {"ok": True, "status": "inactive"}
    return {"ok": True, **state}


def _completed_plan_label(payload: dict[str, Any]) -> str | None:
    if payload.get("tool_name") != "update_plan":
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    plan = tool_input.get("plan")
    if not isinstance(plan, list):
        return None
    completed = [
        item.get("step", "").strip()
        for item in plan
        if isinstance(item, dict)
        and item.get("status") == "completed"
        and isinstance(item.get("step"), str)
        and item.get("step", "").strip()
    ]
    if not completed:
        return None
    prefix = "Plan checkpoint: "
    return prefix + completed[-1][:(MAX_BOUNDARY_LABEL_CHARS - len(prefix))]


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


def _hook_root(
    requested_root: Path | None,
    payload: dict[str, Any],
) -> Path | None:
    if requested_root is not None:
        return _repository_root(requested_root)
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd.strip():
        raise RuntimeHookError("native Hook payload has no working directory")
    candidate = Path(cwd)
    if _worktree_hint(candidate) is None:
        return None
    completed = _git(candidate, "rev-parse", "--show-toplevel")
    if completed.returncode != 0:
        return None
    return _repository_root(Path(completed.stdout.strip()))


def hook(args: argparse.Namespace) -> int:
    try:
        try:
            payload = json.load(sys.stdin)
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeHookError("native Hook payload is unreadable") from exc
        if not isinstance(payload, dict):
            raise RuntimeHookError("native Hook payload must be an object")
        event = payload.get("hook_event_name")
        is_session_start = event == "SessionStart" and payload.get("source") in {
            "startup",
            "resume",
            "clear",
            "compact",
        }
        root = _hook_root(args.root, payload)
        state = None
        if root is not None and os.path.lexists(root / STATE_PATH):
            state = _load_state(root)
        if state is None or state["status"] != "active":
            if is_session_start and args.session_orientation:
                print(
                    json.dumps(
                        {
                            "hookSpecificOutput": {
                                "hookEventName": "SessionStart",
                                "additionalContext": SESSION_FAST_PATH,
                            }
                        },
                        ensure_ascii=False,
                    )
                )
            return 0
        state, git_changed = _sync_git_boundary(root, state)
        if is_session_start:
            stale_reasons = _stale_review_reasons(root, state)
            print(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "SessionStart",
                            "additionalContext": (
                                (f"{SESSION_FAST_PATH} " if args.session_orientation else "")
                                + _context(
                                    state,
                                    review_result=(
                                        "STALE" if stale_reasons else None
                                    ),
                                )
                            ),
                        }
                    },
                    ensure_ascii=False,
                )
            )
        elif event == "UserPromptSubmit":
            boundary = state["boundary"]
            if boundary["reviewed_sequence"] == boundary["sequence"]:
                state = _mark_boundary(
                    state,
                    kind="episode",
                    label="Owner prompt opened a new conversation episode",
                    observed_git_head=boundary["observed_git_head"],
                )
                _write_state(root, state)
            stale_reasons = _stale_review_reasons(root, state)
            print(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "UserPromptSubmit",
                            "additionalContext": _context(
                                state,
                                review_result="STALE" if stale_reasons else None,
                            ),
                        }
                    },
                    ensure_ascii=False,
                )
            )
        elif event == "PostToolUse":
            plan_label = _completed_plan_label(payload)
            if (
                plan_label
                and state["boundary"]["last_kind"] == "stage"
                and state["boundary"]["last_label"] == plan_label
            ):
                plan_label = None
            if plan_label:
                state = _mark_boundary(
                    state,
                    kind="stage",
                    label=plan_label,
                    observed_git_head=state["boundary"]["observed_git_head"],
                )
                _write_state(root, state)
            if git_changed or plan_label:
                boundary = state["boundary"]
                reason = (
                    "RuntimeHook observed review boundary "
                    f"{boundary['sequence']} ({boundary['last_kind']}: "
                    f"{boundary['last_label']}). Re-read RUN_INTENT and Acceptance, "
                    "inspect the completed result for goal drift, unjustified "
                    "hardcoding, hidden fallback/stale state, and dropped or "
                    "unrequested behavior, then record an honest semantic review "
                    "before continuing."
                )
                print(
                    json.dumps(
                        {
                            "hookSpecificOutput": {
                                "hookEventName": "PostToolUse",
                                "additionalContext": f"{reason} {_context(state)}",
                            },
                        },
                        ensure_ascii=False,
                    )
                )
        elif event == "Stop":
            review = state["semantic_review"]
            continue_for_review = False
            if review["result"] == "PENDING" or review.get("stage") != "final":
                boundary = state["boundary"]
                review_state = (
                    "STALE"
                    if boundary["last_kind"] == "git"
                    and boundary["reviewed_sequence"] > 0
                    else "due"
                )
                message = (
                    "RuntimeHook final semantic review is "
                    f"{review_state} for activation "
                    f"{state['activation_id']} after boundary "
                    f"{boundary['sequence']} ({boundary['last_kind']}: "
                    f"{boundary['last_label']}). Review goal drift, unjustified "
                    "hardcoding, hidden fallback/stale state, and unrequested behavior. "
                    "This reminder does not replace or override the project Stop gate."
                )
                continue_for_review = True
            elif review["result"] != "PASS":
                message = (
                    "RuntimeHook final semantic review is recorded as "
                    f"{review['result']} at {review['reference']}. Git and the project "
                    "convergence hook remain authoritative for completion."
                )
            else:
                stale_reasons = _stale_review_reasons(root, state)
                if not stale_reasons:
                    return 0
                message = (
                    "RuntimeHook final semantic review is STALE because "
                    f"{' and '.join(stale_reasons)} after {review['reference']}. "
                    "Repeat the semantic review on a clean Git checkpoint. This "
                    "reminder does not replace or override the project Stop gate."
                )
                continue_for_review = True
            if continue_for_review and not bool(payload.get("stop_hook_active")):
                print(
                    json.dumps(
                        {"decision": "block", "reason": message},
                        ensure_ascii=False,
                    )
                )
            else:
                print(json.dumps({"systemMessage": message}, ensure_ascii=False))
        return 0
    except (RuntimeHookError, OSError, subprocess.SubprocessError) as exc:
        return _hook_error(str(exc))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Control the optional 3CAN RuntimeHook semantic supervisor."
    )
    parser.add_argument("--root", type=Path)
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

    checkpoint = sub.add_parser(
        "checkpoint",
        help="Declare one completed semantic stage or episode boundary.",
    )
    checkpoint.add_argument("--kind", choices=["stage", "episode"], required=True)
    checkpoint.add_argument("--label", required=True)
    checkpoint.add_argument("--next-objective", default="")

    sub.add_parser("status", help="Show the current local RuntimeHook state.")
    hook_parser = sub.add_parser(
        "hook", help="Run as a non-owning Codex lifecycle reminder."
    )
    hook_parser.add_argument(
        "--session-orientation",
        action="store_true",
        help="Emit the stateless 3CAN fast path at SessionStart.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_utf8_stdio()
    args = build_parser().parse_args(argv)
    if args.command == "hook":
        return hook(args)
    if args.root is None:
        args.root = PROJECT_ROOT
    try:
        if args.command == "on":
            output = activate(args)
        elif args.command == "off":
            output = disable(args)
        elif args.command == "review":
            output = record_review(args)
        elif args.command == "checkpoint":
            output = record_checkpoint(args)
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
