"""Fast Codex hooks for the 3CAN ErrorKnowledge lifecycle.

PostToolUse records only explicit non-zero tool exits. The local occurrence
store and outbox are authoritative for this fast path, so a 3CAN network
failure never blocks the completed tool call. Each PostToolUse also retries at
most one older occurrence outbox with the same short timeout.

Stop blocks only when a consumed route ticket has exact unresolved ErrorCases
whose required dispositions have not been accepted by ``/api/activity/done``.
Heuristic/similar ErrorCases and pending upload outboxes never block Stop.

This hook intentionally does not implement PreToolUse. Exact unresolved blind
retry prevention remains in the existing prepare/route-ticket supervision
gate.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
import re
import sys
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
HELPER_PATH = SCRIPT_DIR / "3can_codex.py"
DEFAULT_TIMEOUT_SECONDS = 1.5


def _load_helper() -> Any:
    spec = importlib.util.spec_from_file_location(
        "threecan_codex_error_hook_helper",
        HELPER_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("3can_codex helper import unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


HELPER = _load_helper()


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def _short_timeout() -> float:
    try:
        configured = float(
            os.environ.get(
                "THREECAN_ERROR_HOOK_TIMEOUT_SECONDS",
                DEFAULT_TIMEOUT_SECONDS,
            )
        )
    except ValueError:
        configured = DEFAULT_TIMEOUT_SECONDS
    return max(0.2, min(configured, 3.0))


def _agent_id(data: dict[str, Any]) -> str:
    configured = str(os.environ.get("THREECAN_AGENT_ID") or "").strip()
    if configured:
        return configured
    session_id = re.sub(
        r"[^A-Za-z0-9_.-]+",
        "-",
        str(data.get("session_id") or "unknown"),
    ).strip("-")
    return f"codex-hook-{session_id[:32] or 'unknown'}"


def _explicit_exit_code(value: Any, *, depth: int = 0) -> int | None:
    if depth > 3:
        return None
    if isinstance(value, dict):
        for key in ("exit_code", "exitCode"):
            if key in value:
                try:
                    return int(value[key])
                except (TypeError, ValueError):
                    return None
        for key in ("result", "metadata", "details"):
            if key in value:
                nested = _explicit_exit_code(value[key], depth=depth + 1)
                if nested is not None:
                    return nested
        return None
    if isinstance(value, list):
        for item in value[:20]:
            nested = _explicit_exit_code(item, depth=depth + 1)
            if nested is not None:
                return nested
        return None
    if isinstance(value, str):
        match = re.search(
            r"\b(?:exit(?:ed)?\s+(?:code|with code)|exit_code)\s*[:=]?\s*(-?\d+)\b",
            value,
            re.IGNORECASE,
        )
        if match:
            return int(match.group(1))
    return None


def _compact_response(value: Any) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return re.sub(r"\s+", " ", text).strip()[:1800]


def _target_files(tool_input: Any) -> list[str]:
    if not isinstance(tool_input, dict):
        return []
    raw = tool_input.get("target_files")
    if isinstance(raw, list):
        return [str(item) for item in raw[:20] if str(item).strip()]
    for key in ("path", "file_path"):
        value = str(tool_input.get(key) or "").strip()
        if value:
            return [value]
    return []


def _record_failed_tool(
    data: dict[str, Any],
    *,
    exit_code: int,
    timeout_seconds: float,
) -> dict[str, Any]:
    tool_name = str(data.get("tool_name") or "unknown-tool")
    tool_input = data.get("tool_input")
    command = (
        str(tool_input.get("command") or "")
        if isinstance(tool_input, dict)
        else str(tool_input or "")
    )
    command_summary = command or f"{tool_name} failed"
    args = argparse.Namespace(
        agent_id=_agent_id(data),
        base_url=str(
            os.environ.get("THREECAN_BASE_URL")
            or HELPER.DEFAULT_BASE_URL
        ),
        command_summary=command_summary[:1000],
        error_excerpt=(
            f"{tool_name} exit_code={exit_code}; "
            f"{_compact_response(data.get('tool_response'))}"
        ),
        target_files=_target_files(tool_input),
        scope_keywords=["codex-hook", "posttooluse"],
        related_nodes=[],
        diagnosis="",
        node_id="",
        operation_class="",
        component="",
        error_type="",
        root_cause="",
        timeout_seconds=timeout_seconds,
    )
    captured = io.StringIO()
    try:
        with contextlib.redirect_stdout(captured):
            return_code = HELPER.fail(args)
        raw = captured.getvalue().strip()
        result = json.loads(raw) if raw else {}
        if not isinstance(result, dict):
            result = {"raw": raw[:1000]}
        return {
            "attempted": True,
            "return_code": return_code,
            "status": result.get("status"),
            "occurrence_id": result.get("occurrence_id"),
            "outbox_path": result.get("outbox_path"),
        }
    except Exception as exc:
        return {
            "attempted": True,
            "return_code": 1,
            "status": "PARTIAL",
            "error": f"{type(exc).__name__}: {exc}"[:500],
        }


def _hook_json(data: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    event = str(data.get("hook_event_name") or "")
    if event == "PostToolUse":
        timeout_seconds = _short_timeout()
        replay = HELPER._flush_one_error_occurrence_outbox(
            str(
                os.environ.get("THREECAN_BASE_URL")
                or HELPER.DEFAULT_BASE_URL
            ),
            timeout_seconds=timeout_seconds,
        )
        exit_code = _explicit_exit_code(data.get("tool_response"))
        if exit_code in (None, 0):
            payload: dict[str, Any] = {"continue": True}
            if replay.get("attempted"):
                payload["systemMessage"] = (
                    "3CAN ErrorKnowledge replayed one pending occurrence."
                    if replay.get("posted")
                    else "3CAN ErrorKnowledge outbox replay remains pending."
                )
            return 0, payload
        recorded = _record_failed_tool(
            data,
            exit_code=exit_code,
            timeout_seconds=timeout_seconds,
        )
        return 0, {
            "continue": True,
            "systemMessage": (
                "3CAN ErrorKnowledge recorded the non-zero tool result"
                + (
                    " locally and queued upload."
                    if recorded.get("outbox_path")
                    else "."
                )
                + " This completed tool call is not blocked."
            ),
        }

    if event == "Stop":
        session_id = str(data.get("session_id") or "").strip()
        configured_agent = str(
            os.environ.get("THREECAN_AGENT_ID") or ""
        ).strip()
        pending = HELPER._pending_error_disposition_tickets(
            session_id=session_id,
            agent_id=configured_agent,
            cwd=str(data.get("cwd") or "").strip(),
        )
        if not pending:
            return 0, {"continue": True}
        requirements = [
            {
                "ticket_id": item.get("ticket_id"),
                "error_ids": item.get(
                    "required_error_disposition_ids",
                    [],
                ),
            }
            for item in pending
        ]
        return 0, {
            "decision": "block",
            "reason": (
                "3CAN requires a disposition for each exact unresolved "
                "ErrorCase before this task can stop. Run codex-3can done "
                "for the listed ticket(s), passing one top-level "
                "--error-disposition JSON object per ErrorCase with "
                "resolved, still_open, or not_applicable. Resolved also "
                "requires signed verification evidence. Requirements: "
                + json.dumps(
                    requirements,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            ),
        }

    return 0, {"continue": True}


def main() -> int:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as exc:
        _print_json(
            {
                "continue": True,
                "systemMessage": (
                    "3CAN error lifecycle hook received invalid JSON: "
                    f"{exc.msg}"
                ),
            }
        )
        return 0
    if not isinstance(data, dict):
        data = {}
    code, payload = _hook_json(data)
    _print_json(payload)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
