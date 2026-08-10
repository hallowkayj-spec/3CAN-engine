"""Token usage and cost metering for 3CAN engine.

The ledger records provider-returned usage when available, plus explicit
estimate/manual entries. It intentionally does not store prompts, completions,
API keys, cookies, or other private runtime payloads.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import uuid
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any


ENGINE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GRAPH_DIR = ENGINE_ROOT / "graph"
GRAPH_DIR = Path(os.environ.get("THREECAN_GRAPH_DIR") or DEFAULT_GRAPH_DIR)
DEFAULT_DB_PATH = GRAPH_DIR / "token_usage.sqlite3"
DEFAULT_CODEX_SESSIONS_ROOT = Path.home() / ".codex" / "sessions"
USAGE_SOURCES = {"provider_response", "runtime_status", "count_tokens", "local_estimate", "manual"}
STATUSES = {"succeeded", "failed", "cancelled", "estimated"}
TRACKED_AGENTS: dict[str, dict[str, Any]] = {
    "codex-main": {
        "label": "Codex 主脑",
        "expected_model": "GPT-5.5",
        "expected_models": ("gpt-5.5", "gpt-5.5-codex", "gpt-5.5-chat"),
    },
    "mimo": {
        "label": "MiMo 副脑",
        "expected_model": "MiMo v2.5",
        "expected_models": ("mimo-v2.5", "mimo-2.5", "xiaomi-mimo-v2.5", "mimov2.5"),
    },
}
SMOKE_MARKERS = ("mock", "smoke", "test", "demo", "dummy")
THREECAN_REQUEST_KINDS = {
    "route",
    "session-start",
    "prepare",
    "done",
    "compact",
    "ticket",
    "ticket-consume",
    "file_change",
}
THREECAN_BASELINE_PER_ROUTE_TOKENS = 80_000
CODEX_STATUS_TAIL_BYTES = 8 * 1024 * 1024
CODEX_STATUS_MAX_LINES_PER_FILE = 6000
GROUP_BY_COLUMNS = {
    "provider": "provider",
    "model": "model",
    "agent_id": "agent_id",
    "session_id": "session_id",
    "task_id": "task_id",
    "usage_source": "usage_source",
    "status": "status",
    "date": "substr(created_at, 1, 10)",
    "request_kind": "request_kind",
}
PRIVATE_METADATA_KEYS = (
    "prompt",
    "completion",
    "message",
    "messages",
    "content",
    "text",
    "secret",
    "cookie",
    "password",
    "api_key",
    "apikey",
    "authorization",
)
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")
_HTTP_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_EMBEDDED_WINDOWS_OR_UNC_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]|\\\\)[^\s\"'<>|]+"
)
_EMBEDDED_POSIX_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_/])/(?!/)[^\s\"'<>]+"
)
_PATH_METADATA_KEYS = frozenset(
    {"db_path", "session_file", "sessions_root", "file", "files"}
)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _int_value(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, parsed)


def _float_value(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, parsed)


def _looks_like_absolute_path(value: str) -> bool:
    text = str(value or "").strip()
    return bool(
        text.startswith("/")
        or text.startswith("\\\\")
        or text.startswith("//")
        or _WINDOWS_ABSOLUTE_PATH_RE.match(text)
    )


def _public_path_basename(value: Path | str) -> str:
    text = str(value or "").strip()
    if _WINDOWS_ABSOLUTE_PATH_RE.match(text) or text.startswith(("\\\\", "//")):
        name = PureWindowsPath(text).name
    else:
        name = PurePosixPath(text.replace("\\", "/")).name
    return name or "root"


def _path_sha256(value: Path | str) -> str:
    text = str(value or "").strip()
    try:
        if _WINDOWS_ABSOLUTE_PATH_RE.match(text) or text.startswith(("\\\\", "//")):
            canonical = str(PureWindowsPath(text)).casefold()
        else:
            canonical = str(Path(text).expanduser().resolve(strict=False))
            canonical = os.path.normcase(canonical)
    except (OSError, RuntimeError, ValueError):
        canonical = text.replace("\\", "/")
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _public_path_fields(field: str, value: Path | str) -> dict[str, str]:
    return {
        field: _public_path_basename(value),
        f"{field}_sha256": _path_sha256(value),
    }


def _metadata_key_is_path(key: str) -> bool:
    lowered = key.casefold()
    return bool(
        lowered in _PATH_METADATA_KEYS
        or lowered.endswith("_path")
        or lowered.endswith("_file")
        or lowered.endswith("_root")
    )


def _metadata_path_hash_key(key: str) -> str:
    return f"{key}_sha256" if key.casefold().endswith("_path") else f"{key}_path_sha256"


def _is_http_url(value: str) -> bool:
    return bool(_HTTP_URL_RE.fullmatch(str(value or "").strip()))


def _embedded_path_replacement(match: re.Match[str]) -> str:
    raw_path = match.group(0)
    return (
        f"<absolute-path:{_public_path_basename(raw_path)}:"
        f"{_path_sha256(raw_path)}>"
    )


def _sanitize_text_paths(value: str) -> str:
    """Redact absolute paths anywhere in text while preserving HTTP(S) URLs."""

    text = str(value or "")
    protected_urls: list[str] = []

    def protect_url(match: re.Match[str]) -> str:
        index = len(protected_urls)
        protected_urls.append(match.group(0))
        return f"\x00THREECAN_URL_{index}\x00"

    sanitized = _HTTP_URL_RE.sub(protect_url, text)
    sanitized = _EMBEDDED_WINDOWS_OR_UNC_PATH_RE.sub(
        _embedded_path_replacement,
        sanitized,
    )
    sanitized = _EMBEDDED_POSIX_PATH_RE.sub(
        _embedded_path_replacement,
        sanitized,
    )
    for index, url in enumerate(protected_urls):
        sanitized = sanitized.replace(f"\x00THREECAN_URL_{index}\x00", url)
    return sanitized


def sanitize_public_payload(value: Any, *, path_key: bool = False) -> Any:
    """Recursively enforce the no-absolute-path public response contract."""

    if isinstance(value, dict):
        safe: dict[str, Any] = {}
        for raw_key, nested in value.items():
            raw_key_text = str(raw_key)
            key = _sanitize_text_paths(raw_key_text)
            nested_is_path = _metadata_key_is_path(raw_key_text)
            if (
                isinstance(nested, str)
                and nested_is_path
                and not _is_http_url(nested)
                and _looks_like_absolute_path(nested)
            ):
                safe[key] = _public_path_basename(nested)
                safe[_metadata_path_hash_key(key)] = _path_sha256(nested)
            else:
                safe[key] = sanitize_public_payload(
                    nested,
                    path_key=nested_is_path,
                )
        return safe
    if isinstance(value, (list, tuple, set)):
        return [sanitize_public_payload(item, path_key=path_key) for item in value]
    if isinstance(value, Path):
        value = str(value)
    if isinstance(value, str):
        if _is_http_url(value):
            return value
        if path_key and (
            _looks_like_absolute_path(value)
            or (
                not _HTTP_URL_RE.search(value)
                and ("/" in value or "\\" in value)
            )
        ):
            return _sanitize_text_paths(_public_path_basename(value))
        return _sanitize_text_paths(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _sanitize_text_paths(str(value)[:500])


def _sanitize_nested_metadata(value: Any, *, path_key: bool = False) -> Any:
    if isinstance(value, dict):
        safe: dict[str, Any] = {}
        for raw_key, nested in value.items():
            key = str(raw_key)
            lowered = key.casefold()
            if any(private_key in lowered for private_key in PRIVATE_METADATA_KEYS):
                continue
            nested_is_path = _metadata_key_is_path(key)
            if (
                isinstance(nested, str)
                and not _is_http_url(nested)
                and (
                    _looks_like_absolute_path(nested)
                    or (nested_is_path and not _HTTP_URL_RE.search(nested))
                )
            ):
                safe[key] = _public_path_basename(nested)
                safe[_metadata_path_hash_key(key)] = _path_sha256(nested)
            else:
                safe[key] = _sanitize_nested_metadata(nested, path_key=nested_is_path)
        return safe
    if isinstance(value, list):
        return [_sanitize_nested_metadata(item, path_key=path_key) for item in value]
    if isinstance(value, str):
        if _is_http_url(value):
            return value
        if _looks_like_absolute_path(value) or (
            path_key and not _HTTP_URL_RE.search(value)
        ):
            return _public_path_basename(value)
        return _sanitize_text_paths(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _sanitize_text_paths(str(value)[:500])


def sanitize_metadata(metadata: Any) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        return {}
    safe: dict[str, Any] = {}
    for key, value in metadata.items():
        lowered = str(key).lower()
        if any(private_key in lowered for private_key in PRIVATE_METADATA_KEYS):
            continue
        path_key = _metadata_key_is_path(str(key))
        if (
            isinstance(value, str)
            and not _is_http_url(value)
            and (
                _looks_like_absolute_path(value)
                or (path_key and not _HTTP_URL_RE.search(value))
            )
        ):
            safe[str(key)] = _public_path_basename(value)
            safe[_metadata_path_hash_key(str(key))] = _path_sha256(value)
        elif isinstance(value, str):
            safe[str(key)] = _sanitize_text_paths(value)
        elif isinstance(value, (int, float, bool)) or value is None:
            safe[str(key)] = value
        elif isinstance(value, (list, dict)):
            encoded = json.dumps(
                _sanitize_nested_metadata(value, path_key=path_key),
                ensure_ascii=False,
            )
            safe[str(key)] = encoded[:500]
        else:
            safe[str(key)] = _sanitize_text_paths(str(value)[:500])
    return safe


def _json_metadata_from_row(row: dict[str, Any] | sqlite3.Row) -> dict[str, Any]:
    raw = row["metadata_json"] if "metadata_json" in row.keys() else "{}"  # type: ignore[attr-defined]
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    sanitized = sanitize_public_payload(parsed)
    return sanitized if isinstance(sanitized, dict) else {}


def _looks_like_smoke_event(row: dict[str, Any] | sqlite3.Row) -> bool:
    metadata = _json_metadata_from_row(row)
    parts = [
        row["request_id"] if "request_id" in row.keys() else "",  # type: ignore[attr-defined]
        row["provider"] if "provider" in row.keys() else "",  # type: ignore[attr-defined]
        row["model"] if "model" in row.keys() else "",  # type: ignore[attr-defined]
        row["task_id"] if "task_id" in row.keys() else "",  # type: ignore[attr-defined]
        json.dumps(metadata, ensure_ascii=False, sort_keys=True),
    ]
    blob = " ".join(str(part).lower() for part in parts)
    return any(marker in blob for marker in SMOKE_MARKERS)


def _model_matches_expected(model: str, spec: dict[str, Any]) -> bool:
    value = (model or "").lower()
    return any(str(expected).lower() in value for expected in spec.get("expected_models", ()))


def classify_usage_event(row: dict[str, Any] | sqlite3.Row, spec: dict[str, Any] | None = None) -> str:
    """Classify a ledger row for tracked-agent readiness checks.

    This is intentionally strict: smoke/mock rows never count as actual usage,
    and local estimates never count as provider-connected metering.
    """
    usage_source = str(row["usage_source"] if "usage_source" in row.keys() else "")  # type: ignore[attr-defined]
    model = str(row["model"] if "model" in row.keys() else "")  # type: ignore[attr-defined]
    if _looks_like_smoke_event(row):
        return "test"
    if usage_source in {"local_estimate", "count_tokens"}:
        return "estimate"
    if usage_source in {"provider_response", "runtime_status"}:
        if spec is None or _model_matches_expected(model, spec):
            return "actual"
        return "unmatched"
    return "unmatched"


def _safe_request_part(value: Any) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(value or "")).strip("-") or "unknown"


def _default_codex_sessions_root() -> Path:
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        sessions_root = Path(codex_home).expanduser() / "sessions"
        if sessions_root.exists():
            return sessions_root
    return DEFAULT_CODEX_SESSIONS_ROOT


def _codex_thread_id_from_path(path: Path) -> str:
    match = re.match(r"rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-(.+)$", path.stem)
    return match.group(1) if match else path.stem


def _codex_session_sort_key(path: Path) -> str:
    match = re.match(r"rollout-(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})-", path.stem)
    return match.group(1) if match else path.stem


def _int_usage_field(payload: dict[str, Any], key: str) -> int:
    return _int_value(payload.get(key))


def _date_key(created_at: Any) -> str:
    value = str(created_at or "")
    return value[:10] if len(value) >= 10 else "(no date)"


def _session_label(session_id: str, metadata: dict[str, Any]) -> str:
    if not session_id:
        return "(no session)"
    thread_id = str(metadata.get("thread_id") or "")
    if session_id.startswith("codex-"):
        return f"Codex TUI - {thread_id[:8] or session_id.removeprefix('codex-')[:8]}"
    if session_id.startswith("SES-"):
        return session_id.replace("SES-", "Session ", 1)
    return session_id


def _extract_codex_model(payload: dict[str, Any], info: dict[str, Any], current_model: str) -> tuple[str, bool]:
    direct = payload.get("model") or info.get("model")
    if direct:
        return str(direct), False
    collaboration = payload.get("collaboration_mode")
    if isinstance(collaboration, dict):
        settings = collaboration.get("settings")
        if isinstance(settings, dict) and settings.get("model"):
            return str(settings["model"]), False
    if current_model:
        return current_model, False
    return "gpt-5.5", True


def _latest_codex_session_files(root: Path, max_files: int) -> list[Path]:
    if not root.exists():
        return []
    files = sorted(root.glob("**/rollout-*.jsonl"), key=_codex_session_sort_key, reverse=True)
    if max_files > 0:
        files = files[:max_files]
    return sorted(files, key=_codex_session_sort_key)


def _recent_jsonl_lines(path: Path, *, max_lines: int) -> list[str]:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            offset = max(0, size - CODEX_STATUS_TAIL_BYTES)
            handle.seek(offset)
            data = handle.read()
    except OSError:
        return []
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if offset > 0 and lines:
        lines = lines[1:]
    return lines[-max_lines:] if max_lines > 0 else lines


def collect_codex_status_events(
    *,
    sessions_root: Path | str | None = None,
    max_files: int = 1,
    max_events: int = 5000,
    agent_id: str = "codex-main",
) -> dict[str, Any]:
    """Collect Codex slash-status token usage from local session JSONL files.

    Codex writes status-like telemetry into ``~/.codex/sessions``. The JSONL may
    repeat the same status snapshot several times, so we dedupe by cumulative
    ``total_token_usage.total_tokens`` per thread and only ingest the delta-like
    ``last_token_usage``. Prompt/message bodies are never copied.
    """
    root = Path(sessions_root).expanduser() if sessions_root else _default_codex_sessions_root()
    files = _latest_codex_session_files(root, max_files=max(0, int(max_files)))
    events: list[dict[str, Any]] = []
    latest_snapshot: dict[str, Any] | None = None

    for path in files:
        session_file_ref = _public_path_fields("session_file", path)
        thread_id = _codex_thread_id_from_path(path)
        session_id = f"codex-{thread_id}"
        current_model = ""
        seen_cumulative_totals: set[int] = set()
        line_limit = CODEX_STATUS_MAX_LINES_PER_FILE
        if max_events > 0:
            line_limit = min(CODEX_STATUS_MAX_LINES_PER_FILE, max(500, int(max_events) * 4))
        for line_no, line in enumerate(_recent_jsonl_lines(path, max_lines=line_limit), 1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            info = payload.get("info") if isinstance(payload.get("info"), dict) else payload
            if not isinstance(info, dict):
                continue

            model, model_inferred = _extract_codex_model(payload, info, current_model)
            if not model_inferred:
                current_model = model

            usage = info.get("last_token_usage")
            if not isinstance(usage, dict):
                continue
            total_usage = info.get("total_token_usage")
            total_usage = total_usage if isinstance(total_usage, dict) else {}
            cumulative_total = _int_value(total_usage.get("total_tokens"))
            if cumulative_total:
                if cumulative_total in seen_cumulative_totals:
                    continue
                seen_cumulative_totals.add(cumulative_total)

            timestamp = str(record.get("timestamp") or utc_now())
            request_key = cumulative_total or f"{timestamp}-{line_no}"
            rate_limits = info.get("rate_limits")
            rate_limits = rate_limits if isinstance(rate_limits, dict) else {}
            metadata = {
                "source": "codex_session_jsonl_status",
                **session_file_ref,
                "thread_id": thread_id,
                "line_no": line_no,
                "model_inferred": model_inferred,
                "model_context_window": _int_value(info.get("model_context_window") or payload.get("model_context_window")),
                "cumulative_total_tokens": cumulative_total,
                "cumulative_input_tokens": _int_value(total_usage.get("input_tokens")),
                "cumulative_output_tokens": _int_value(total_usage.get("output_tokens")),
                "rate_limit_name": str(rate_limits.get("limit_name") or ""),
                "rate_limit_id": str(rate_limits.get("limit_id") or ""),
                "plan_type": str(rate_limits.get("plan_type") or ""),
            }
            event = {
                "created_at": timestamp.replace("Z", "+00:00"),
                "request_id": f"codex_status_{_safe_request_part(thread_id)}_{_safe_request_part(request_key)}",
                "provider": "codex-cli",
                "model": model,
                "agent_id": agent_id,
                "session_id": session_id,
                "task_id": "codex-status",
                "request_kind": "runtime_status",
                "usage_source": "runtime_status",
                "status": "succeeded",
                "input_tokens": _int_usage_field(usage, "input_tokens"),
                "output_tokens": _int_usage_field(usage, "output_tokens"),
                "total_tokens": _int_usage_field(usage, "total_tokens"),
                "cached_tokens": _int_usage_field(usage, "cached_input_tokens"),
                "reasoning_tokens": _int_usage_field(usage, "reasoning_output_tokens"),
                "metadata": metadata,
            }
            events.append(event)
            latest_snapshot = {
                "session_id": session_id,
                "thread_id": thread_id,
                "timestamp": event["created_at"],
                "model": model,
                "last_token_usage": {
                    "input_tokens": event["input_tokens"],
                    "cached_input_tokens": event["cached_tokens"],
                    "output_tokens": event["output_tokens"],
                    "reasoning_output_tokens": event["reasoning_tokens"],
                    "total_tokens": event["total_tokens"],
                },
                "total_token_usage": total_usage,
                "rate_limits": rate_limits,
                "model_context_window": metadata["model_context_window"],
                "session_file": metadata["session_file"],
                "session_file_sha256": metadata["session_file_sha256"],
            }

    if max_events > 0 and len(events) > max_events:
        events = events[-max_events:]
    root_ref = _public_path_fields("sessions_root", root)
    file_refs = [_public_path_fields("file", path) for path in files]
    return sanitize_public_payload({
        "ok": True,
        **root_ref,
        "files": [item["file"] for item in file_refs],
        "files_sha256": [item["file_sha256"] for item in file_refs],
        "file_count": len(file_refs),
        "event_count": len(events),
        "events": events,
        "latest_snapshot": latest_snapshot,
    })


def _estimate_token_count(value: Any, *, model: str = "") -> tuple[int, int, str]:
    if value in (None, "", [], {}):
        return 0, 0, "empty"
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
    byte_count = len(encoded.encode("utf-8"))
    try:
        import tiktoken

        try:
            encoding = tiktoken.encoding_for_model(model) if model else tiktoken.get_encoding("o200k_base")
        except Exception:
            if model.lower().startswith(("gpt-5", "gpt-4o", "o1", "o3", "o4")):
                encoding = tiktoken.get_encoding("o200k_base")
            else:
                encoding = tiktoken.get_encoding("cl100k_base")
        return len(encoding.encode(encoded)), byte_count, f"tiktoken:{encoding.name}"
    except Exception:
        token_estimate = max(1, int(byte_count / 3.5)) if byte_count else 0
        return token_estimate, byte_count, "json_utf8_bytes_div_3_5"


def estimate_tokens_for_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a conservative local estimate for prompt/tool/history payloads.

    This is C-level metering: useful for guardrails and warnings only. Accurate
    accounting should come from provider response usage or official count APIs.
    """
    input_parts: list[Any] = []
    output_parts: list[Any] = []
    for key in (
        "input",
        "input_text",
        "request",
        "text",
        "prompt",
        "system",
        "user",
        "history",
        "messages",
        "tools",
        "tool_schema",
        "route_context",
    ):
        if key in payload:
            input_parts.append(payload[key])
    for key in ("output", "output_text", "response", "completion", "assistant"):
        if key in payload:
            output_parts.append(payload[key])
    if not input_parts and not output_parts and payload:
        input_parts.append(payload)

    model = str(payload.get("model") or "")
    input_tokens, input_bytes, input_method = _estimate_token_count(input_parts, model=model)
    output_tokens, output_bytes, output_method = _estimate_token_count(output_parts, model=model)
    tool_tokens, _tool_bytes, _tool_method = _estimate_token_count(payload.get("tools", []), model=model) if "tools" in payload else (0, 0, "empty")
    methods = sorted({item for item in (input_method, output_method) if item != "empty"})
    return {
        "usage_source": "local_estimate",
        "estimate_method": "+".join(methods) if methods else "empty",
        "estimated_input_tokens": input_tokens,
        "estimated_output_tokens": output_tokens,
        "estimated_tool_tokens": tool_tokens,
        "estimated_total_tokens": input_tokens + output_tokens,
        "input_byte_count": input_bytes,
        "output_byte_count": output_bytes,
        "byte_count": input_bytes + output_bytes,
    }


class TokenUsageStore:
    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS llm_usage_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    request_id TEXT NOT NULL UNIQUE,
                    trace_id TEXT NOT NULL DEFAULT '',
                    span_id TEXT NOT NULL DEFAULT '',
                    provider TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '',
                    agent_id TEXT NOT NULL DEFAULT '',
                    session_id TEXT NOT NULL DEFAULT '',
                    task_id TEXT NOT NULL DEFAULT '',
                    route_ticket_id TEXT NOT NULL DEFAULT '',
                    request_kind TEXT NOT NULL DEFAULT 'chat',
                    usage_source TEXT NOT NULL DEFAULT 'provider_response',
                    status TEXT NOT NULL DEFAULT 'succeeded',
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    cached_tokens INTEGER NOT NULL DEFAULT 0,
                    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                    image_tokens INTEGER NOT NULL DEFAULT 0,
                    audio_tokens INTEGER NOT NULL DEFAULT 0,
                    credits_used REAL NOT NULL DEFAULT 0,
                    cost_usd REAL NOT NULL DEFAULT 0,
                    latency_ms INTEGER NOT NULL DEFAULT 0,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_usage_created_at ON llm_usage_events(created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_usage_provider_model ON llm_usage_events(provider, model)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_usage_task ON llm_usage_events(task_id, session_id, agent_id)")

    def record_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        usage_source = str(payload.get("usage_source") or "provider_response")
        if usage_source not in USAGE_SOURCES:
            raise ValueError(f"invalid usage_source: {usage_source}")
        status = str(payload.get("status") or ("estimated" if usage_source == "local_estimate" else "succeeded"))
        if status not in STATUSES:
            raise ValueError(f"invalid status: {status}")

        input_tokens = _int_value(payload.get("input_tokens", payload.get("prompt_tokens")))
        output_tokens = _int_value(payload.get("output_tokens", payload.get("completion_tokens")))
        total_tokens = _int_value(payload.get("total_tokens"), input_tokens + output_tokens)
        cached_tokens = _int_value(payload.get("cached_tokens"))
        reasoning_tokens = _int_value(payload.get("reasoning_tokens"))
        image_tokens = _int_value(payload.get("image_tokens"))
        audio_tokens = _int_value(payload.get("audio_tokens"))

        event = {
            "created_at": str(payload.get("created_at") or utc_now()),
            "request_id": str(payload.get("request_id") or f"req_{uuid.uuid4().hex}"),
            "trace_id": str(payload.get("trace_id") or ""),
            "span_id": str(payload.get("span_id") or ""),
            "provider": str(payload.get("provider") or ""),
            "model": str(payload.get("model") or ""),
            "agent_id": str(payload.get("agent_id") or ""),
            "session_id": str(payload.get("session_id") or ""),
            "task_id": str(payload.get("task_id") or ""),
            "route_ticket_id": str(payload.get("route_ticket_id") or ""),
            "request_kind": str(payload.get("request_kind") or "chat"),
            "usage_source": usage_source,
            "status": status,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "cached_tokens": cached_tokens,
            "reasoning_tokens": reasoning_tokens,
            "image_tokens": image_tokens,
            "audio_tokens": audio_tokens,
            "credits_used": _float_value(payload.get("credits_used")),
            "cost_usd": _float_value(payload.get("cost_usd", payload.get("cost_estimate"))),
            "latency_ms": _int_value(payload.get("latency_ms")),
            "metadata_json": json.dumps(sanitize_metadata(payload.get("metadata")), ensure_ascii=False, sort_keys=True),
        }

        columns = ", ".join(event.keys())
        placeholders = ", ".join("?" for _ in event)
        with self._connect() as conn:
            try:
                conn.execute(
                    f"INSERT INTO llm_usage_events ({columns}) VALUES ({placeholders})",
                    list(event.values()),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"duplicate request_id: {event['request_id']}") from exc
            row = conn.execute(
                "SELECT * FROM llm_usage_events WHERE request_id = ?",
                (event["request_id"],),
            ).fetchone()
        return self._row_to_event(row)

    def summary(
        self,
        *,
        group_by: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        where: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("provider", provider),
            ("model", model),
            ("agent_id", agent_id),
            ("session_id", session_id),
            ("task_id", task_id),
        ):
            if value:
                where.append(f"{column} = ?")
                params.append(value)
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""

        aggregate_sql = f"""
            SELECT
                COUNT(*) AS event_count,
                COALESCE(SUM(input_tokens), 0) AS input_tokens,
                COALESCE(SUM(output_tokens), 0) AS output_tokens,
                COALESCE(SUM(total_tokens), 0) AS total_tokens,
                COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
                COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens,
                COALESCE(SUM(image_tokens), 0) AS image_tokens,
                COALESCE(SUM(audio_tokens), 0) AS audio_tokens,
                COALESCE(SUM(credits_used), 0) AS credits_used,
                COALESCE(SUM(cost_usd), 0) AS cost_usd,
                COALESCE(AVG(latency_ms), 0) AS avg_latency_ms,
                MAX(created_at) AS last_event_at
            FROM llm_usage_events
            {where_sql}
        """
        with self._connect() as conn:
            aggregate = dict(conn.execute(aggregate_sql, params).fetchone())
            groups: list[dict[str, Any]] = []
            if group_by:
                if group_by not in GROUP_BY_COLUMNS:
                    raise ValueError(f"invalid group_by: {group_by}")
                column = GROUP_BY_COLUMNS[group_by]
                group_sql = f"""
                    SELECT
                        {column} AS key,
                        COUNT(*) AS event_count,
                        COALESCE(SUM(input_tokens), 0) AS input_tokens,
                        COALESCE(SUM(output_tokens), 0) AS output_tokens,
                        COALESCE(SUM(total_tokens), 0) AS total_tokens,
                        COALESCE(SUM(credits_used), 0) AS credits_used,
                        COALESCE(SUM(cost_usd), 0) AS cost_usd
                    FROM llm_usage_events
                    {where_sql}
                    GROUP BY {column}
                    ORDER BY total_tokens DESC
                """
                groups = [dict(row) for row in conn.execute(group_sql, params).fetchall()]
        aggregate["avg_latency_ms"] = round(float(aggregate["avg_latency_ms"] or 0), 2)
        aggregate["cost_usd"] = round(float(aggregate["cost_usd"] or 0), 8)
        aggregate["credits_used"] = round(float(aggregate["credits_used"] or 0), 8)
        return sanitize_public_payload({
            "ok": True,
            **_public_path_fields("db_path", self.db_path),
            "filters": {
                "provider": provider,
                "model": model,
                "agent_id": agent_id,
                "session_id": session_id,
                "task_id": task_id,
                "group_by": group_by,
            },
            "totals": aggregate,
            "groups": groups,
        })

    def overview(self, *, limit: int = 12) -> dict[str, Any]:
        """Return project-oriented usage rollups for the dashboard."""
        with self._connect() as conn:
            rows = [
                dict(row)
                for row in conn.execute("SELECT * FROM llm_usage_events ORDER BY created_at ASC").fetchall()
            ]

        classified_rows: list[tuple[dict[str, Any], str]] = []
        for row in rows:
            spec = TRACKED_AGENTS.get(str(row.get("agent_id") or ""))
            classified_rows.append((row, classify_usage_event(row, spec)))

        class_rows = {
            "actual": [row for row, kind in classified_rows if kind == "actual"],
            "estimate": [row for row, kind in classified_rows if kind == "estimate"],
            "test": [row for row, kind in classified_rows if kind == "test"],
            "unmatched": [row for row, kind in classified_rows if kind == "unmatched"],
        }

        status = self.integration_status(include_events=False)
        return sanitize_public_payload({
            "ok": True,
            "generated_at": utc_now(),
            **_public_path_fields("db_path", self.db_path),
            "totals": {
                "recorded": self._totals_for_rows(rows),
                "actual": self._totals_for_rows(class_rows["actual"]),
                "estimate": self._totals_for_rows(class_rows["estimate"]),
                "test": self._totals_for_rows(class_rows["test"]),
                "unmatched": self._totals_for_rows(class_rows["unmatched"]),
            },
            "groups": {
                "dates": self._rollup_rows(
                    classified_rows,
                    lambda row, kind: (_date_key(row.get("created_at")), _date_key(row.get("created_at")), {}),
                    limit=limit,
                    sort_by="key_desc",
                ),
                "sessions": self._rollup_rows(classified_rows, self._session_group_key, limit=limit),
                "agents": self._rollup_rows(classified_rows, lambda row, kind: (str(row.get("agent_id") or "(no agent)"), str(row.get("agent_id") or "(no agent)"), {}), limit=limit),
                "models": self._rollup_rows(classified_rows, lambda row, kind: (str(row.get("model") or "(no model)"), str(row.get("model") or "(no model)"), {}), limit=limit),
                "sources": self._rollup_rows(classified_rows, lambda row, kind: (str(row.get("usage_source") or "(no source)"), str(row.get("usage_source") or "(no source)"), {}), limit=limit),
                "request_kinds": self._rollup_rows(classified_rows, lambda row, kind: (str(row.get("request_kind") or "(no kind)"), str(row.get("request_kind") or "(no kind)"), {}), limit=limit),
                "tasks": self._rollup_rows(classified_rows, lambda row, kind: (str(row.get("task_id") or "(no task)"), str(row.get("task_id") or "(no task)"), {}), limit=limit),
                "providers": self._rollup_rows(classified_rows, lambda row, kind: (str(row.get("provider") or "(no provider)"), str(row.get("provider") or "(no provider)"), {}), limit=limit),
                "classes": self._rollup_rows(classified_rows, lambda row, kind: (kind, kind, {}), limit=limit),
            },
            "classification": {
                name: self._totals_for_rows(bucket_rows)
                for name, bucket_rows in class_rows.items()
            },
            "impact": self._threecan_impact(classified_rows, limit=limit),
            "runtime": {
                "tracked_agents": status["tracked_agents"],
                "hook_status": status["hook_status"],
                "warnings": status["warnings"],
            },
        })

    def impact(self, *, limit: int = 12) -> dict[str, Any]:
        """Return 3CAN token impact metrics for routes and wrapper calls."""
        with self._connect() as conn:
            rows = [
                dict(row)
                for row in conn.execute("SELECT * FROM llm_usage_events ORDER BY created_at ASC").fetchall()
            ]
        classified_rows = [
            (row, classify_usage_event(row, TRACKED_AGENTS.get(str(row.get("agent_id") or ""))))
            for row in rows
        ]
        return sanitize_public_payload({
            "ok": True,
            "generated_at": utc_now(),
            **_public_path_fields("db_path", self.db_path),
            "impact": self._threecan_impact(classified_rows, limit=limit),
        })

    def health(self) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS event_count, MAX(created_at) AS last_event_at FROM llm_usage_events"
            ).fetchone()
        status = self.integration_status(include_events=False)
        return sanitize_public_payload({
            "ok": True,
            **_public_path_fields("db_path", self.db_path),
            "db_exists": self.db_path.exists(),
            "event_count": int(row["event_count"] or 0),
            "last_event_at": row["last_event_at"],
            "usage_sources": sorted(USAGE_SOURCES),
            "tracked_agents": status["tracked_agents"],
            "hook_status": status["hook_status"],
            "warnings": status["warnings"],
            "layers": {
                "A": "provider_response usage fields recorded after real model calls",
                "B": "runtime_status from local coding shells such as Codex slash-status JSONL",
                "C": "count_tokens or official counting APIs recorded as preflight estimates",
                "D": "local_estimate for guardrails and warnings only",
            },
        })

    def import_codex_status_events(
        self,
        *,
        sessions_root: Path | str | None = None,
        max_files: int = 1,
        max_events: int = 5000,
        agent_id: str = "codex-main",
    ) -> dict[str, Any]:
        collected = collect_codex_status_events(
            sessions_root=sessions_root,
            max_files=max_files,
            max_events=max_events,
            agent_id=agent_id,
        )
        imported = 0
        skipped_duplicates = 0
        errors: list[dict[str, str]] = []
        for event in collected["events"]:
            try:
                self.record_event(event)
                imported += 1
            except ValueError as exc:
                if "duplicate request_id" in str(exc):
                    skipped_duplicates += 1
                else:
                    errors.append({"request_id": str(event.get("request_id") or ""), "error": str(exc)})
        return sanitize_public_payload({
            "ok": not errors,
            "source": "codex_session_jsonl_status",
            "scanned_files": collected["files"],
            "scanned_files_sha256": collected["files_sha256"],
            "scanned_file_count": collected["file_count"],
            "collected_events": collected["event_count"],
            "imported_events": imported,
            "skipped_duplicates": skipped_duplicates,
            "errors": errors,
            "latest_snapshot": collected["latest_snapshot"],
        })

    @staticmethod
    def _totals_for_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
        totals = {
            "event_count": 0,
            "input_tokens": 0,
            "fresh_input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cached_tokens": 0,
            "cached_input_ratio": 0.0,
            "fresh_output_ratio": 0.0,
            "reasoning_tokens": 0,
            "image_tokens": 0,
            "audio_tokens": 0,
            "credits_used": 0.0,
            "cost_usd": 0.0,
            "first_event_at": None,
            "last_event_at": None,
        }
        for row in rows:
            totals["event_count"] += 1
            for key in ("input_tokens", "output_tokens", "total_tokens", "cached_tokens", "reasoning_tokens", "image_tokens", "audio_tokens"):
                totals[key] += int(row.get(key) or 0)
            totals["credits_used"] = round(float(totals["credits_used"]) + float(row.get("credits_used") or 0), 8)
            totals["cost_usd"] = round(float(totals["cost_usd"]) + float(row.get("cost_usd") or 0), 8)
            created_at = row.get("created_at")
            if created_at:
                if not totals["first_event_at"] or str(created_at) < str(totals["first_event_at"]):
                    totals["first_event_at"] = created_at
                if not totals["last_event_at"] or str(created_at) > str(totals["last_event_at"]):
                    totals["last_event_at"] = created_at
        totals["fresh_input_tokens"] = max(0, int(totals["input_tokens"]) - int(totals["cached_tokens"]))
        totals["cached_input_ratio"] = round((int(totals["cached_tokens"]) / int(totals["input_tokens"])) * 100, 2) if totals["input_tokens"] else 0.0
        totals["fresh_output_ratio"] = round((int(totals["fresh_input_tokens"]) / int(totals["output_tokens"])), 2) if totals["output_tokens"] else 0.0
        return totals

    @staticmethod
    def _session_group_key(row: dict[str, Any], _kind: str) -> tuple[str, str, dict[str, Any]]:
        metadata = _json_metadata_from_row(row)
        session_id = str(row.get("session_id") or "(no session)")
        session_file = str(metadata.get("session_file") or "")
        session_file_sha256 = str(
            metadata.get("session_file_sha256")
            or metadata.get("session_file_path_sha256")
            or (_path_sha256(session_file) if session_file else "")
        )
        extra = {
            "thread_id": str(metadata.get("thread_id") or ""),
            "session_file": _public_path_basename(session_file) if session_file else "",
            "session_file_sha256": session_file_sha256,
            "source": str(metadata.get("source") or ""),
            "model_context_window": _int_value(metadata.get("model_context_window")),
            "cumulative_total_tokens": _int_value(metadata.get("cumulative_total_tokens")),
        }
        return session_id, _session_label(session_id, metadata), extra

    @classmethod
    def _rollup_rows(
        cls,
        classified_rows: list[tuple[dict[str, Any], str]],
        key_func,
        *,
        limit: int = 12,
        sort_by: str = "total_tokens",
    ) -> list[dict[str, Any]]:
        groups: dict[str, dict[str, Any]] = {}
        for row, kind in classified_rows:
            key, label, extra = key_func(row, kind)
            key = str(key or "(empty)")
            label = str(label or key)
            if key not in groups:
                groups[key] = {
                    "key": key,
                    "label": label,
                    "event_count": 0,
                    "input_tokens": 0,
                    "fresh_input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "cached_tokens": 0,
                    "cached_input_ratio": 0.0,
                    "fresh_output_ratio": 0.0,
                    "reasoning_tokens": 0,
                    "cost_usd": 0.0,
                    "credits_used": 0.0,
                    "first_event_at": row.get("created_at"),
                    "last_event_at": row.get("created_at"),
                    "agents": set(),
                    "models": set(),
                    "sources": set(),
                    "request_kinds": set(),
                    "tasks": set(),
                    "classes": set(),
                    **extra,
                }
            group = groups[key]
            group["event_count"] += 1
            for field in ("input_tokens", "output_tokens", "total_tokens", "cached_tokens", "reasoning_tokens"):
                group[field] += int(row.get(field) or 0)
            group["cost_usd"] = round(float(group["cost_usd"]) + float(row.get("cost_usd") or 0), 8)
            group["credits_used"] = round(float(group["credits_used"]) + float(row.get("credits_used") or 0), 8)
            created_at = row.get("created_at")
            if created_at:
                if not group["first_event_at"] or str(created_at) < str(group["first_event_at"]):
                    group["first_event_at"] = created_at
                if not group["last_event_at"] or str(created_at) > str(group["last_event_at"]):
                    group["last_event_at"] = created_at
            for target, source in (
                ("agents", row.get("agent_id")),
                ("models", row.get("model")),
                ("sources", row.get("usage_source")),
                ("request_kinds", row.get("request_kind")),
                ("tasks", row.get("task_id")),
            ):
                if source:
                    group[target].add(str(source))
            group["classes"].add(kind)

        results: list[dict[str, Any]] = []
        for group in groups.values():
            group["fresh_input_tokens"] = max(0, int(group["input_tokens"]) - int(group["cached_tokens"]))
            group["cached_input_ratio"] = round((int(group["cached_tokens"]) / int(group["input_tokens"])) * 100, 2) if group["input_tokens"] else 0.0
            group["fresh_output_ratio"] = round((int(group["fresh_input_tokens"]) / int(group["output_tokens"])), 2) if group["output_tokens"] else 0.0
            for field in ("agents", "models", "sources", "request_kinds", "tasks", "classes"):
                group[field] = sorted(group[field])
            results.append(group)
        if sort_by == "key_desc":
            results.sort(key=lambda item: str(item.get("key") or ""), reverse=True)
        elif sort_by == "last_event_at":
            results.sort(key=lambda item: str(item.get("last_event_at") or ""), reverse=True)
        else:
            results.sort(key=lambda item: (int(item.get("total_tokens") or 0), str(item.get("last_event_at") or "")), reverse=True)
        return results[:limit] if limit > 0 else results

    @classmethod
    def _threecan_impact(
        cls,
        classified_rows: list[tuple[dict[str, Any], str]],
        *,
        limit: int = 12,
    ) -> dict[str, Any]:
        runtime_rows = [
            row
            for row, kind in classified_rows
            if kind == "actual" and str(row.get("usage_source") or "") == "runtime_status"
        ]
        threecan_rows = [
            row
            for row, kind in classified_rows
            if kind == "estimate" and cls._is_threecan_context_estimate(row)
        ]
        route_rows = [
            row
            for row in threecan_rows
            if str(row.get("request_kind") or "") in {"route", "session-start"}
        ]
        runtime = cls._totals_for_rows(runtime_rows)
        threecan = cls._totals_for_rows(threecan_rows)
        route_context = cls._totals_for_rows(route_rows)
        baseline_events = int(route_context["event_count"])
        baseline_tokens = baseline_events * THREECAN_BASELINE_PER_ROUTE_TOKENS
        measured_context_tokens = int(route_context["total_tokens"])
        avoided_tokens = max(0, baseline_tokens - measured_context_tokens)
        actual_fresh = int(runtime["fresh_input_tokens"])
        return {
            "summary": {
                "runtime_actual": runtime,
                "threecan_estimate": threecan,
                "route_context_estimate": route_context,
                "threecan_share_of_runtime_fresh_pct": round((int(threecan["total_tokens"]) / actual_fresh) * 100, 2) if actual_fresh else 0.0,
                "route_context_share_of_runtime_fresh_pct": round((measured_context_tokens / actual_fresh) * 100, 2) if actual_fresh else 0.0,
                "runtime_cache_hit_pct": runtime["cached_input_ratio"],
                "runtime_fresh_output_ratio": runtime["fresh_output_ratio"],
                "baseline_scenario": {
                    "kind": "scenario_not_provider_measured",
                    "baseline_per_route_tokens": THREECAN_BASELINE_PER_ROUTE_TOKENS,
                    "baseline_events": baseline_events,
                    "baseline_tokens": baseline_tokens,
                    "measured_route_context_tokens": measured_context_tokens,
                    "estimated_avoided_tokens": avoided_tokens,
                    "compression_ratio": round((baseline_tokens / measured_context_tokens), 2) if measured_context_tokens else 0.0,
                    "basis": "Legacy full-context load floor used for planning only; provider-measured savings require an A/B run.",
                },
            },
            "by_date": cls._rollup_rows(
                [(row, "threecan_estimate") for row in threecan_rows],
                lambda row, kind: (_date_key(row.get("created_at")), _date_key(row.get("created_at")), {}),
                limit=limit,
                sort_by="key_desc",
            ),
            "by_kind": cls._rollup_rows(
                [(row, "threecan_estimate") for row in threecan_rows],
                lambda row, kind: (str(row.get("request_kind") or "(no kind)"), str(row.get("request_kind") or "(no kind)"), {}),
                limit=limit,
            ),
            "by_session": cls._rollup_rows(
                [(row, "threecan_estimate") for row in threecan_rows],
                cls._session_group_key,
                limit=limit,
            ),
            "recent_context_events": [
                cls._row_public_event(row)
                for row in sorted(threecan_rows, key=lambda item: str(item.get("created_at") or ""), reverse=True)[:limit]
            ],
            "notes": [
                "runtime_actual uses Codex runtime_status rows imported from local session JSONL.",
                "threecan_estimate uses wrapper local_estimate rows only; it is not provider billing data.",
                "baseline_scenario estimates avoided full-context injection and must be validated by A/B sessions.",
            ],
        }

    @staticmethod
    def _is_threecan_context_estimate(row: dict[str, Any]) -> bool:
        if str(row.get("usage_source") or "") != "local_estimate":
            return False
        request_kind = str(row.get("request_kind") or "")
        if request_kind in THREECAN_REQUEST_KINDS:
            return True
        metadata = _json_metadata_from_row(row)
        return str(metadata.get("source") or "").startswith("3can_")

    @staticmethod
    def _row_public_event(row: dict[str, Any]) -> dict[str, Any]:
        metadata = _json_metadata_from_row(row)
        return {
            "created_at": row.get("created_at"),
            "request_id": row.get("request_id"),
            "agent_id": row.get("agent_id"),
            "session_id": row.get("session_id"),
            "task_id": row.get("task_id"),
            "request_kind": row.get("request_kind"),
            "usage_source": row.get("usage_source"),
            "input_tokens": int(row.get("input_tokens") or 0),
            "output_tokens": int(row.get("output_tokens") or 0),
            "total_tokens": int(row.get("total_tokens") or 0),
            "estimate_method": metadata.get("estimate_method", ""),
            "source": metadata.get("source", ""),
        }

    def integration_status(self, *, include_events: bool = True) -> dict[str, Any]:
        with self._connect() as conn:
            rows = [dict(row) for row in conn.execute("SELECT * FROM llm_usage_events ORDER BY created_at DESC").fetchall()]

        tracked_agents: list[dict[str, Any]] = []
        warnings: list[str] = []
        for agent_id, spec in TRACKED_AGENTS.items():
            agent_rows = [row for row in rows if row.get("agent_id") == agent_id]
            buckets = {
                "actual": self._empty_usage_bucket(),
                "estimate": self._empty_usage_bucket(),
                "test": self._empty_usage_bucket(),
                "unmatched": self._empty_usage_bucket(),
            }
            models_seen: set[str] = set()
            last_event_at = None
            recent_events: list[dict[str, Any]] = []
            for row in agent_rows:
                kind = classify_usage_event(row, spec)
                bucket = buckets.get(kind, buckets["unmatched"])
                self._add_row_to_bucket(bucket, row)
                if row.get("model"):
                    models_seen.add(str(row["model"]))
                if not last_event_at:
                    last_event_at = row.get("created_at")
                if include_events and len(recent_events) < 8:
                    recent_events.append({
                        "created_at": row.get("created_at"),
                        "request_id": row.get("request_id"),
                        "provider": row.get("provider"),
                        "model": row.get("model"),
                        "session_id": row.get("session_id"),
                        "task_id": row.get("task_id"),
                        "request_kind": row.get("request_kind"),
                        "usage_source": row.get("usage_source"),
                        "classification": kind,
                        "input_tokens": int(row.get("input_tokens") or 0),
                        "output_tokens": int(row.get("output_tokens") or 0),
                        "total_tokens": int(row.get("total_tokens") or 0),
                        "cost_usd": round(float(row.get("cost_usd") or 0), 8),
                    })

            agent_status = self._agent_status_from_buckets(buckets)
            if agent_status == "test_only":
                warnings.append(f"{agent_id} 只有 smoke/mock 测试写入，不能用于真实 token 成本或项目管理。")
            elif agent_status == "estimate_only":
                warnings.append(f"{agent_id} 只有本地估算，没有 provider_response 真实用量。")
            elif agent_status == "unmatched_only":
                warnings.append(f"{agent_id} 有 provider_response 事件，但模型未命中预期 {spec['expected_model']}。")
            elif agent_status == "not_connected":
                warnings.append(f"{agent_id} 尚未写入任何 token usage 事件。")

            item = {
                "agent_id": agent_id,
                "label": spec["label"],
                "expected_model": spec["expected_model"],
                "expected_models": list(spec["expected_models"]),
                "status": agent_status,
                "last_event_at": last_event_at,
                "models_seen": sorted(models_seen),
                "actual": buckets["actual"],
                "estimate": buckets["estimate"],
                "test": buckets["test"],
                "unmatched": buckets["unmatched"],
            }
            if include_events:
                item["recent_events"] = recent_events
            tracked_agents.append(item)

        hook_status = {
            "token_api": "ready",
            "ledger_db": "ready" if self.db_path.exists() else "missing",
            "litellm_callback": "ready" if (ENGINE_ROOT / "tools" / "litellm_3can_callback.py").exists() else "missing",
            "local_estimate_hook": "ready" if (ENGINE_ROOT / "tools" / "token_usage_hook.py").exists() else "missing",
            "codex_runtime_bridge": self._bridge_status_for("codex-main", tracked_agents),
            "mimo_runtime_bridge": self._bridge_status_for("mimo", tracked_agents),
        }
        return sanitize_public_payload({
            "ok": True,
            "generated_at": utc_now(),
            **_public_path_fields("db_path", self.db_path),
            "tracked_agents": tracked_agents,
            "hook_status": hook_status,
            "warnings": warnings,
        })

    @staticmethod
    def _empty_usage_bucket() -> dict[str, Any]:
        return {
            "event_count": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cached_tokens": 0,
            "reasoning_tokens": 0,
            "credits_used": 0.0,
            "cost_usd": 0.0,
        }

    @staticmethod
    def _add_row_to_bucket(bucket: dict[str, Any], row: dict[str, Any]) -> None:
        bucket["event_count"] += 1
        for key in ("input_tokens", "output_tokens", "total_tokens", "cached_tokens", "reasoning_tokens"):
            bucket[key] += int(row.get(key) or 0)
        bucket["credits_used"] = round(float(bucket["credits_used"]) + float(row.get("credits_used") or 0), 8)
        bucket["cost_usd"] = round(float(bucket["cost_usd"]) + float(row.get("cost_usd") or 0), 8)

    @staticmethod
    def _agent_status_from_buckets(buckets: dict[str, dict[str, Any]]) -> str:
        if buckets["actual"]["event_count"]:
            return "actual_connected"
        if buckets["estimate"]["event_count"]:
            return "estimate_only"
        if buckets["test"]["event_count"]:
            return "test_only"
        if buckets["unmatched"]["event_count"]:
            return "unmatched_only"
        return "not_connected"

    @staticmethod
    def _bridge_status_for(agent_id: str, tracked_agents: list[dict[str, Any]]) -> str:
        for item in tracked_agents:
            if item["agent_id"] == agent_id:
                return item["status"]
        return "not_connected"

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        try:
            data["metadata"] = json.loads(data.pop("metadata_json") or "{}")
        except json.JSONDecodeError:
            data["metadata"] = {}
        return sanitize_public_payload(data)
