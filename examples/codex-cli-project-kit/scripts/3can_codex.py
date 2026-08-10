from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _bundled_engine_root() -> Path:
    """Locate an engine shipped beside the reusable project kit.

    A copied kit normally relies on ``THREECAN_ENGINE_ROOT`` or its project
    capsule.  The ancestor scan keeps the source distribution and editable
    checkouts self-contained without embedding the maintainer's repository
    layout.
    """

    script_path = Path(__file__).resolve()
    for parent in script_path.parents:
        direct = parent if parent.name == "neural-memory" else parent / "neural-memory"
        if (direct / "backend" / "app.py").is_file():
            return direct
    return PROJECT_ROOT / "neural-memory"


STAGING_ENGINE_ROOT = _bundled_engine_root()
LOG_DIR = Path(
    os.environ.get("THREECAN_LOG_DIR")
    or PROJECT_ROOT / "test-results" / "3can"
).expanduser()
PENDING_WRITEBACK_DIR = Path(
    os.environ.get("THREECAN_PENDING_WRITEBACK_DIR")
    or PROJECT_ROOT / "data" / "_3can_pending_writeback"
).expanduser()
LOCAL_RUNTIME_DIR = Path(
    os.environ.get("THREECAN_LOCAL_RUNTIME_DIR")
    or PROJECT_ROOT / "data" / "_3can_runtime"
).expanduser()

DEFAULT_BASE_URL = os.environ.get("THREECAN_BASE_URL", "http://127.0.0.1:9700")
DEFAULT_MIN_NODES = int(os.environ.get("THREECAN_MIN_NODES", "100"))
DEFAULT_MIN_TICKET_TTL_SEC = int(os.environ.get("THREECAN_MIN_TICKET_TTL_SEC", "5"))
DEFAULT_SUPERVISOR_TASK = "3CAN Production Runtime Supervisor"
RUNTIME_IDENTITY_SCHEMA = "3can.runtime-identity/v1"
PROCESS_EXIT_TIMEOUT_MS = 5000
SCOPED_STATE_INDEX_LIMIT = 80
GENERIC_SCOPE_TOKENS = {
    "3can",
    "codex",
    "runtime",
    "harness",
    "wrapper",
    "task",
    "edit",
    "done",
    "change",
    "changes",
    "file",
    "files",
    "update",
    "updated",
    "fix",
    "fixed",
}
CORE_MEMORY_REGISTRY_NODE_ID = "MEM-3can-core-memory-lane-registry-20260523"
FALLBACK_CORE_MEMORY_NODE_IDS = {
    "user_preferences": [],
    "environment_constraints": [],
    "error_warnings": [],
    "project_constitution": [],
}
FALLBACK_LANE_EXECUTION_WEIGHTS = {
    "user_preferences": 100,
    "error_warnings": 100,
    "environment_constraints": 95,
    "project_constitution": 90,
}
FALLBACK_REQUIRED_MEMORY_EDGES: list[dict[str, Any]] = []
PRIORITY_EXECUTION_WEIGHTS = {"critical": 100, "high": 80, "medium": 50, "low": 25}
TYPE_EXECUTION_BONUS = {"feedback": 7, "config": 6, "knowledge": 4, "reference": 3, "decision": 3, "session": -10}
STATUS_EXECUTION_FACTOR = {"active": 1.0, "blocked": 0.2, "deprecated": 0.2, "dormant": 0.35, "archived": 0.15}
STABLE_MEMORY_PREFIXES = ("USR-", "ENV-", "PRJ-", "RUL-", "MEM-")
BASE_PREFLIGHT_LANES = ("user_preferences", "environment_constraints", "error_warnings")
LOOP_GATE_THRESHOLD = 2
LOOP_RULE_PROMOTION_THRESHOLD = 3
ERROR_CASE_BLOCK_WINDOW_HOURS = 72
GITHUB_PATTERN = re.compile(r"\b(gh|github|pull request|push|upload)\b|\bPR\b|仓库|上传|提交|拉取请求", re.IGNORECASE)
STALE_ENV_PATTERN = re.compile(r"\bWSL\b|\bmnt\b|旧挂载|挂载路径|旧环境|deprecated path", re.IGNORECASE)
TICKET_PATTERN = re.compile(r"ticket|ttl|expired|过期|失效|route ticket", re.IGNORECASE)
PRODUCT_PATTERN = re.compile(
    r"product|saas|ecommerce|commerce|merchant|store|rpa|产品|商家|店铺|运营|顾问",
    re.IGNORECASE,
)
PROJECT_ISOLATION_PATTERN = re.compile(
    r"project[-_ ]?isolation|cross[-_ ]?project|proxy|backend lane|"
    r"port isolation|frontend contamination|contamination",
    re.IGNORECASE,
)
PROJECT_FILE_SYSTEM_PATTERN = re.compile(
    r"project[-_ ]?file[-_ ]?system|desktop|generated artifact|tenant asset|tenant[-_ ]?data|"
    r"workorder|ledger|typed contract|contract schema|frontend uat|archive|quarantine|"
    r"file placement|path placement|user asset|asset vault",
    re.IGNORECASE,
)
SKILL_MCP_PATTERN = re.compile(
    r"skill|mcp|modelcontextprotocol|active tool surface|on[-_ ]?demand skill|Claude MCP|Codex MCP",
    re.IGNORECASE,
)
PROJECT_BRAIN_PATTERN = re.compile(
    r"project brain|project brief|full project understanding|active specs|project constitution|"
    r"whole project|project context|latest specs|current project",
    re.IGNORECASE,
)
TOKEN_ROUTE_PATTERN = re.compile(
    r"token|cache ratio|route budget|skeleton|slim|full|token efficiency|token operations",
    re.IGNORECASE,
)


def _early_capsule_engine_root() -> Path | None:
    """Read only the engine-root field needed before the full capsule loader."""

    capsule_path = PROJECT_ROOT / ".agents" / "project.json"
    if not capsule_path.is_file():
        return None
    try:
        payload = json.loads(capsule_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    raw = str(payload.get("threecan_engine_root") or payload.get("engine_root") or "").strip()
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    return candidate


def _error_knowledge_candidates() -> list[Path]:
    candidates: list[Path] = []
    configured_engine_root = os.environ.get("THREECAN_ENGINE_ROOT", "").strip()
    if configured_engine_root:
        candidates.append(Path(configured_engine_root).expanduser() / "backend" / "error_knowledge.py")
    capsule_root = _early_capsule_engine_root()
    if capsule_root is not None:
        candidates.append(capsule_root / "backend" / "error_knowledge.py")
    mcp_path = os.environ.get("THREECAN_MCP", "").strip()
    if mcp_path:
        candidates.append(Path(mcp_path).expanduser().parent / "backend" / "error_knowledge.py")
    candidates.append(STAGING_ENGINE_ROOT / "backend" / "error_knowledge.py")

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            key = str(candidate.resolve(strict=False)).casefold()
        except OSError:
            key = str(candidate).casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def _load_error_knowledge_contract() -> tuple[Any | None, dict[str, Any]]:
    """Load the one canonical error contract; never synthesize a substitute."""

    candidates = _error_knowledge_candidates()
    failures: list[dict[str, str]] = []
    for candidate in candidates:
        if not candidate.is_file():
            failures.append({"path": str(candidate), "error": "not_found"})
            continue
        spec = importlib.util.spec_from_file_location("threecan_error_knowledge_contract", candidate)
        if spec is None or spec.loader is None:
            failures.append({"path": str(candidate), "error": "import_spec_unavailable"})
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
            required = ("deterministic_fingerprint", "is_error_intent")
            missing = [name for name in required if not callable(getattr(module, name, None))]
            if missing:
                raise RuntimeError(f"missing canonical exports: {', '.join(missing)}")
            schema_version = str(getattr(module, "SCHEMA_VERSION", "") or "")
            fingerprint_version = str(getattr(module, "FINGERPRINT_VERSION", "") or "")
            if (
                schema_version != "3can.error-knowledge/v2"
                or fingerprint_version != "ek2"
            ):
                raise RuntimeError(
                    "incompatible canonical contract: "
                    f"schema={schema_version or '<missing>'}; "
                    f"fingerprint={fingerprint_version or '<missing>'}"
                )
            return module, {
                "status": "ready",
                "source": str(candidate.resolve()),
                "schema_version": schema_version,
                "fingerprint_version": fingerprint_version,
                "candidates": [str(item) for item in candidates],
            }
        except Exception as exc:
            sys.modules.pop(spec.name, None)
            failures.append({"path": str(candidate), "error": f"{type(exc).__name__}: {exc}"})
            continue
    return None, {
        "status": "blocked",
        "kind": "error_knowledge_contract_unavailable",
        "candidates": [str(item) for item in candidates],
        "failures": failures,
    }


ERROR_KNOWLEDGE_CONTRACT, ERROR_KNOWLEDGE_CONTRACT_STATUS = _load_error_knowledge_contract()


class ErrorKnowledgeContractUnavailable(RuntimeError):
    """Raised when an error-lifecycle command cannot load the canonical core."""


def _require_error_knowledge_contract() -> Any:
    if ERROR_KNOWLEDGE_CONTRACT is None:
        raise ErrorKnowledgeContractUnavailable(
            json.dumps(ERROR_KNOWLEDGE_CONTRACT_STATUS, ensure_ascii=False, sort_keys=True)
        )
    return ERROR_KNOWLEDGE_CONTRACT


def _json_request(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout: float = 8.0,
) -> Any:
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(f"{base_url}{path}", data=body, headers=headers, method=method)
    with urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
    return json.loads(raw)


def _try_json_request(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout: float = 8.0,
) -> tuple[bool, Any]:
    try:
        return True, _json_request(base_url, path, method=method, payload=payload, timeout=timeout)
    except HTTPError as exc:
        try:
            body = exc.read().decode("utf-8")
            return False, {"http_status": exc.code, "body": body}
        except Exception:
            return False, {"http_status": exc.code, "body": ""}
    except URLError as exc:
        return False, {"error": str(exc.reason)}
    except Exception as exc:
        return False, {"error": str(exc)}


def _token_estimate_enabled() -> bool:
    value = os.environ.get("THREECAN_TOKEN_ESTIMATE_AUTO", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _estimate_model_for_agent(agent_id: str) -> str:
    explicit = os.environ.get("THREECAN_TOKEN_ESTIMATE_MODEL")
    if explicit:
        return explicit
    if agent_id.lower().startswith("mimo"):
        return "mimo-v2.5-estimate"
    return "gpt-5.5-estimate"


def _safe_id_part(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-") or "agent"


def _path_identity(value: str | Path, *, base: Path | None = None) -> str:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = (base or PROJECT_ROOT) / path
    try:
        return str(path.resolve(strict=False))
    except TypeError:
        return str(path.resolve())
    except Exception:
        return str(path.absolute())


def _paths_match(left: str | Path, right: str | Path) -> bool:
    return os.path.normcase(_path_identity(left)) == os.path.normcase(_path_identity(right))


def _normalize_base_url(base_url: str) -> str:
    return str(base_url or "").rstrip("/")


def _list_from_value(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, tuple):
        return [str(item) for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def _project_capsule_path(project_root: Path | None = None) -> Path:
    return (project_root or PROJECT_ROOT) / ".agents" / "project.json"


def _load_project_capsule(project_root: Path | None = None) -> dict[str, Any]:
    root = project_root or PROJECT_ROOT
    path = _project_capsule_path(root)
    if not path.exists():
        return {"configured": False, "path": str(path)}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "configured": True,
            "path": str(path),
            "load_error": str(exc),
        }
    if not isinstance(raw, dict):
        return {
            "configured": True,
            "path": str(path),
            "load_error": "project capsule must be a JSON object",
        }

    project_root_value = raw.get("project_root") or str(root)
    engine_root_value = raw.get("threecan_engine_root") or raw.get("engine_root") or ""
    base_url = raw.get("threecan_base_url") or raw.get("base_url") or DEFAULT_BASE_URL
    prefixes = _list_from_value(raw.get("agent_id_prefixes") or raw.get("agent_id_prefix"))
    required_node_ids = _list_from_value(raw.get("required_node_ids") or raw.get("graph_anchor_node_ids"))
    capsule = {
        "configured": True,
        "path": str(path),
        "raw": raw,
        "project_id": str(raw.get("project_id") or raw.get("id") or "").strip(),
        "project_name": str(raw.get("project_name") or raw.get("name") or "").strip(),
        "project_root": _path_identity(str(project_root_value), base=root),
        "threecan_base_url": _normalize_base_url(str(base_url)),
        "threecan_engine_root": _path_identity(str(engine_root_value), base=root) if engine_root_value else "",
        "agent_id_prefixes": prefixes,
        "required_node_ids": required_node_ids,
        "frontend_ports": raw.get("frontend_ports") or [],
        "backend_lanes": raw.get("backend_lanes") or {},
        "forbidden_keywords": raw.get("forbidden_keywords") or [],
    }
    return capsule


def _current_project_metadata(*, base_url: str = "") -> dict[str, Any]:
    capsule = _load_project_capsule()
    if not capsule.get("configured") or capsule.get("load_error"):
        return {}
    return {
        "project_id": capsule.get("project_id") or "",
        "project_name": capsule.get("project_name") or "",
        "project_root": capsule.get("project_root") or "",
        "threecan_base_url": _normalize_base_url(base_url or str(capsule.get("threecan_base_url") or "")),
        "threecan_engine_root": capsule.get("threecan_engine_root") or "",
        "capsule_path": capsule.get("path") or "",
    }


def _engine_root_has_node(engine_root: str | Path, node_id: str) -> bool:
    if not node_id:
        return True
    return (Path(engine_root) / "graph" / "nodes" / f"{node_id}.json").exists()


def _project_identity_gate(
    base_url: str,
    discovery: dict[str, Any],
    *,
    agent_id: str = "",
    command: str = "",
) -> dict[str, Any]:
    capsule = _load_project_capsule()
    checks: list[dict[str, Any]] = []
    if not capsule.get("configured"):
        return {
            "name": "project_identity",
            "status": "pass",
            "configured": False,
            "reason": "no_project_capsule",
            "capsule_path": capsule.get("path"),
            "command": command,
        }
    if capsule.get("load_error"):
        return {
            "name": "project_identity",
            "status": "block",
            "configured": True,
            "capsule_path": capsule.get("path"),
            "command": command,
            "error": {"kind": "project_capsule_load_error", "detail": capsule.get("load_error")},
            "checks": checks,
        }

    expected_project_root = str(capsule.get("project_root") or "")
    actual_project_root = _path_identity(PROJECT_ROOT)
    checks.append(
        {
            "name": "project_root",
            "status": "pass" if _paths_match(expected_project_root, actual_project_root) else "block",
            "expected": expected_project_root,
            "actual": actual_project_root,
        }
    )

    expected_base_url = _normalize_base_url(str(capsule.get("threecan_base_url") or ""))
    actual_base_url = _normalize_base_url(base_url)
    if expected_base_url:
        checks.append(
            {
                "name": "base_url",
                "status": "pass" if expected_base_url == actual_base_url else "block",
                "expected": expected_base_url,
                "actual": actual_base_url,
            }
        )

    expected_engine_root = str(capsule.get("threecan_engine_root") or "")
    actual_engine_root = str(discovery.get("selected") or "")
    if expected_engine_root:
        checks.append(
            {
                "name": "engine_root",
                "status": "pass" if actual_engine_root and _paths_match(expected_engine_root, actual_engine_root) else "block",
                "expected": expected_engine_root,
                "actual": actual_engine_root,
                "source": discovery.get("source"),
            }
        )

    for node_id in capsule.get("required_node_ids") or []:
        checks.append(
            {
                "name": "graph_anchor_node",
                "status": "pass" if actual_engine_root and _engine_root_has_node(actual_engine_root, str(node_id)) else "block",
                "node_id": str(node_id),
                "engine_root": actual_engine_root,
            }
        )

    prefixes = [item for item in capsule.get("agent_id_prefixes") or [] if item]
    if agent_id and prefixes:
        checks.append(
            {
                "name": "agent_id",
                "status": "pass" if any(agent_id.startswith(prefix) for prefix in prefixes) else "block",
                "expected_prefixes": prefixes,
                "actual": agent_id,
            }
        )

    expected_lanes = capsule.get("backend_lanes") if isinstance(capsule.get("backend_lanes"), dict) else {}
    derived_lanes = _backend_ports_for_base_url(actual_base_url or expected_base_url or DEFAULT_BASE_URL)
    for slot in ("green", "blue"):
        expected_port = expected_lanes.get(slot)
        if expected_port is None:
            continue
        checks.append(
            {
                "name": f"backend_lane_{slot}",
                "status": "pass" if int(expected_port) == int(derived_lanes[slot]) else "block",
                "expected": int(expected_port),
                "actual": int(derived_lanes[slot]),
            }
        )

    blocking = [item for item in checks if item.get("status") == "block"]
    return {
        "name": "project_identity",
        "status": "block" if blocking else "pass",
        "configured": True,
        "project_id": capsule.get("project_id") or "",
        "project_name": capsule.get("project_name") or "",
        "capsule_path": capsule.get("path"),
        "command": command,
        "checks": checks,
        "blocking_checks": blocking,
        "metadata": _current_project_metadata(base_url=actual_base_url),
    }


def _agent_id_from_args(args: argparse.Namespace) -> str:
    return str(getattr(args, "agent_id", "") or getattr(args, "expect_agent_id", "") or "")


def _project_identity_block_payload(args: argparse.Namespace, gate: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": False,
        "command": getattr(args, "command", ""),
        "error": {
            "kind": "project_identity_gate_blocked",
            "message": "3CAN project capsule does not match the current command context.",
        },
        "project_identity": gate,
    }


def _session_id_for_agent(agent_id: str, explicit_session_id: str = "") -> str:
    explicit = explicit_session_id or os.environ.get("THREECAN_SESSION_ID") or os.environ.get("CODEX_SESSION_ID") or os.environ.get("SESSION_ID")
    if explicit:
        return explicit

    now = datetime.now(timezone.utc)
    project_meta = _current_project_metadata()
    current_project_id = str(project_meta.get("project_id") or "")
    state_path = LOCAL_RUNTIME_DIR / f"session_{_safe_id_part(agent_id)}.json"
    try:
        if state_path.exists():
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state_project_id = str(state.get("project_id") or "")
            project_matches = not current_project_id or not state_project_id or state_project_id == current_project_id
            if state.get("date") == now.strftime("%Y%m%d") and state.get("session_id") and project_matches:
                return str(state["session_id"])
    except Exception:
        pass

    session_id = f"SES-{now.strftime('%Y%m%d')}-{_safe_id_part(agent_id)}-{now.strftime('%H%M%S')}"
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_payload = {
            "session_id": session_id,
            "agent_id": agent_id,
            "date": now.strftime("%Y%m%d"),
            **project_meta,
        }
        state_path.write_text(
            json.dumps(state_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass
    return session_id


def _agent_runtime_path(prefix: str, agent_id: str) -> Path:
    return LOCAL_RUNTIME_DIR / f"{prefix}_{_safe_id_part(agent_id)}.json"


def _error_disposition_ticket_path(ticket_id: str) -> Path:
    return (
        LOCAL_RUNTIME_DIR
        / "error_disposition_tickets"
        / f"{_safe_id_part(ticket_id)}.json"
    )


def _explicit_runtime_session_id() -> str:
    return str(
        os.environ.get("CODEX_SESSION_ID")
        or os.environ.get("THREECAN_SESSION_ID")
        or ""
    ).strip()


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _record_error_disposition_ticket(
    ticket: Any,
    *,
    agent_id: str,
    base_url: str,
) -> str | None:
    if not isinstance(ticket, dict):
        return None
    required = list(dict.fromkeys(
        str(item).strip()
        for item in ticket.get("required_error_disposition_ids") or []
        if str(item).strip()
    ))
    if not required:
        return None
    ticket_id = str(ticket.get("ticket_id") or "").strip()
    if not ticket_id:
        return None
    path = _error_disposition_ticket_path(ticket_id)
    payload = {
        "schema_version": "3can.codex-error-disposition-state/v1",
        "ticket_id": ticket_id,
        "agent_id": agent_id,
        "session_id": _explicit_runtime_session_id(),
        "cwd": _path_identity(Path.cwd()),
        "base_url": base_url,
        "state": "pending",
        "required_error_disposition_ids": required,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        _write_json_atomic(path, payload)
    except OSError:
        return None
    return str(path)


def _complete_error_disposition_ticket(
    ticket_id: str,
    *,
    response: Any,
) -> str | None:
    path = _error_disposition_ticket_path(ticket_id)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        payload["state"] = "completed"
        payload["completed_at"] = datetime.now(timezone.utc).isoformat()
        if isinstance(response, dict):
            payload["error_dispositions"] = response.get(
                "error_dispositions",
                [],
            )
            payload["completion_request_hash"] = response.get(
                "completion_request_hash"
            )
        _write_json_atomic(path, payload)
    except (OSError, ValueError):
        return None
    return str(path)


def _pending_error_disposition_tickets(
    *,
    session_id: str = "",
    agent_id: str = "",
    cwd: str = "",
) -> list[dict[str, Any]]:
    directory = LOCAL_RUNTIME_DIR / "error_disposition_tickets"
    if not directory.is_dir():
        return []
    pending: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict) or payload.get("state") != "pending":
            continue
        state_session = str(payload.get("session_id") or "").strip()
        state_agent = str(payload.get("agent_id") or "").strip()
        state_cwd = str(payload.get("cwd") or "").strip()
        if session_id and state_session and session_id != state_session:
            continue
        if agent_id and state_agent and agent_id != state_agent:
            continue
        cwd_matches = bool(
            cwd
            and state_cwd
            and _paths_match(cwd, state_cwd)
        )
        if not (
            (session_id and state_session == session_id)
            or (agent_id and state_agent == agent_id)
            or cwd_matches
        ):
            continue
        item = dict(payload)
        item["state_path"] = str(path)
        pending.append(item)
    return pending


def _agent_runtime_index_path(prefix: str, agent_id: str) -> Path:
    return LOCAL_RUNTIME_DIR / f"{prefix}_index_{_safe_id_part(agent_id)}.json"


def _agent_scoped_runtime_path(prefix: str, agent_id: str, scope_key: str) -> Path:
    return LOCAL_RUNTIME_DIR / f"{prefix}_states" / f"{prefix}_{_safe_id_part(agent_id)}_{scope_key}.json"


def _scope_state_key(
    *,
    agent_id: str,
    base_url: str = "",
    task_description: str = "",
    target_files: list[str] | None = None,
    scope_keywords: list[str] | None = None,
) -> str:
    material = {
        "agent_id": agent_id,
        "base_url": base_url,
        "task_tokens": sorted(_significant_scope_tokens(task_description))[:16],
        "target_files": sorted({_normalize_scope_path(str(item)) for item in target_files or [] if item}),
        "scope_keywords": sorted({str(item).strip().lower() for item in scope_keywords or [] if str(item).strip()}),
    }
    raw = json.dumps(material, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _load_runtime_index(prefix: str, agent_id: str) -> dict[str, Any]:
    path = _agent_runtime_index_path(prefix, agent_id)
    if not path.exists():
        return {"version": 1, "entries": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 1, "entries": []}
    if not isinstance(payload, dict):
        return {"version": 1, "entries": []}
    if not isinstance(payload.get("entries"), list):
        payload["entries"] = []
    payload.setdefault("version", 1)
    return payload


def _save_runtime_index(prefix: str, agent_id: str, payload: dict[str, Any]) -> None:
    path = _agent_runtime_index_path(prefix, agent_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _record_scoped_runtime_state(prefix: str, agent_id: str, state: dict[str, Any]) -> str:
    scope_key = str(state.get("scope_key") or "")
    if not scope_key:
        scope_key = _scope_state_key(
            agent_id=agent_id,
            base_url=str(state.get("base_url") or ""),
            task_description=str(state.get("task_description") or state.get("task") or ""),
            target_files=[str(item) for item in state.get("target_files") or []],
            scope_keywords=[str(item) for item in state.get("scope_keywords") or []],
        )
        state["scope_key"] = scope_key
    path = _agent_scoped_runtime_path(prefix, agent_id, scope_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    state["state_path"] = str(path)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    index = _load_runtime_index(prefix, agent_id)
    entries = [
        item for item in index.get("entries", [])
        if isinstance(item, dict) and item.get("scope_key") != scope_key
    ]
    entries.insert(
        0,
        {
            "scope_key": scope_key,
            "state_path": str(path),
            "agent_id": agent_id,
            "base_url": state.get("base_url"),
            "recorded_at": state.get("recorded_at"),
            "task_description": state.get("task_description") or state.get("task") or "",
            "target_files": state.get("target_files") or [],
            "scope_keywords": state.get("scope_keywords") or [],
            "status": state.get("supervision_status") or state.get("status") or "",
        },
    )
    index["entries"] = entries[:SCOPED_STATE_INDEX_LIMIT]
    _save_runtime_index(prefix, agent_id, index)
    return str(path)


def _state_match_score(
    state: dict[str, Any],
    *,
    base_url: str = "",
    expected_scope_text: str = "",
    expected_target_files: list[str] | None = None,
) -> int:
    if base_url and state.get("base_url") and state.get("base_url") != base_url:
        return -1
    score = 0
    expected_paths = {_normalize_scope_path(str(item)) for item in expected_target_files or [] if item}
    state_paths = {_normalize_scope_path(str(item)) for item in state.get("target_files") or [] if item}
    if expected_paths:
        if expected_paths.issubset(state_paths):
            score += 100 + len(expected_paths)
        elif expected_paths.intersection(state_paths):
            score += 25 + len(expected_paths.intersection(state_paths))
        else:
            return -1
    expected_tokens = _significant_scope_tokens(expected_scope_text or "")
    state_text = " ".join(
        [
            str(state.get("task_description") or state.get("task") or ""),
            " ".join(str(item) for item in state.get("scope_keywords") or []),
            " ".join(str(item) for item in state.get("target_files") or []),
        ]
    )
    state_tokens = _significant_scope_tokens(state_text)
    if expected_tokens:
        overlap = expected_tokens & state_tokens
        if overlap:
            score += min(len(overlap), 20)
        elif not expected_paths:
            return -1
    return score


def _load_scoped_runtime_state(
    prefix: str,
    agent_id: str,
    *,
    base_url: str = "",
    expected_scope_text: str = "",
    expected_target_files: list[str] | None = None,
) -> dict[str, Any] | None:
    index = _load_runtime_index(prefix, agent_id)
    candidates: list[tuple[int, str, dict[str, Any]]] = []
    for entry in index.get("entries", []):
        if not isinstance(entry, dict):
            continue
        path_text = str(entry.get("state_path") or "")
        if not path_text:
            continue
        path = Path(path_text)
        if not path.exists():
            continue
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(state, dict):
            continue
        score = _state_match_score(
            state,
            base_url=base_url,
            expected_scope_text=expected_scope_text,
            expected_target_files=expected_target_files,
        )
        if score < 0:
            continue
        candidates.append((score, str(state.get("recorded_at") or ""), state))
    if candidates:
        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        selected = dict(candidates[0][2])
        selected["_selection"] = {
            "kind": "scoped_index",
            "scope_key": selected.get("scope_key"),
            "state_path": selected.get("state_path"),
            "candidate_count": len(candidates),
        }
        return selected

    for legacy_path in (_agent_runtime_path(prefix, agent_id), _agent_runtime_path(f"last_{prefix}", agent_id)):
        try:
            if legacy_path.exists():
                state = json.loads(legacy_path.read_text(encoding="utf-8"))
                if isinstance(state, dict):
                    state["_selection"] = {
                        "kind": "legacy_latest",
                        "state_path": str(legacy_path),
                        "candidate_count": 0,
                    }
                    return state
        except Exception:
            continue
    return None


def _extract_route_node_ids(response_payload: Any) -> list[str]:
    if not isinstance(response_payload, dict):
        return []
    nodes = response_payload.get("nodes")
    if nodes is None:
        nodes = response_payload.get("activated_nodes")
    result: list[str] = []
    if isinstance(nodes, list):
        for node in nodes:
            if isinstance(node, dict) and node.get("id"):
                result.append(str(node["id"]))
    return result


def _record_route_state(base_url: str, *, agent_id: str, task: str, response_payload: Any) -> None:
    if not agent_id:
        return
    project_meta = _current_project_metadata(base_url=base_url)
    state = {
        "agent_id": agent_id,
        "task": task,
        "base_url": base_url,
        **project_meta,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "top_node_ids": _extract_route_node_ids(response_payload),
        "confidence": response_payload.get("confidence") if isinstance(response_payload, dict) else None,
    }
    try:
        LOCAL_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        _agent_runtime_path("last_route", agent_id).write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except Exception:
        pass


def _load_route_state(agent_id: str) -> dict[str, Any] | None:
    try:
        path = _agent_runtime_path("last_route", agent_id)
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return None


def _record_supervise_state(
    base_url: str,
    *,
    agent_id: str,
    result_payload: dict[str, Any],
    ticket_mode: str,
) -> str | None:
    if not agent_id:
        return None
    route_payload = result_payload.get("route") if isinstance(result_payload.get("route"), dict) else {}
    gates = result_payload.get("gates") if isinstance(result_payload.get("gates"), list) else []
    gate_statuses = {
        str(gate.get("name")): str(gate.get("status"))
        for gate in gates
        if isinstance(gate, dict) and gate.get("name")
    }
    project_meta = _current_project_metadata(base_url=base_url)
    state = {
        "agent_id": agent_id,
        "base_url": base_url,
        **project_meta,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "task_description": result_payload.get("task_description") or "",
        "target_files": result_payload.get("target_files") or [],
        "scope_keywords": result_payload.get("scope_keywords") or [],
        "supervision_status": result_payload.get("supervision_status") or "",
        "gate_statuses": gate_statuses,
        "ticket_id": result_payload.get("ticket_id"),
        "ticket_mode": ticket_mode,
        "top_node_ids": _extract_route_node_ids(route_payload),
        "memory_quality": (result_payload.get("memory_preflight") or {}).get("memory_quality")
        if isinstance(result_payload.get("memory_preflight"), dict)
        else None,
    }
    state["scope_key"] = _scope_state_key(
        agent_id=agent_id,
        base_url=base_url,
        task_description=str(state.get("task_description") or ""),
        target_files=[str(item) for item in state.get("target_files") or []],
        scope_keywords=[str(item) for item in state.get("scope_keywords") or []],
    )
    try:
        LOCAL_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        scoped_path = _record_scoped_runtime_state("supervise", agent_id, state)
        legacy_path = _agent_runtime_path("last_supervise", agent_id)
        legacy_state = dict(state)
        legacy_state["scoped_state_path"] = scoped_path
        legacy_path.write_text(json.dumps(legacy_state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return scoped_path
    except Exception:
        return None


def _load_supervise_state(
    agent_id: str,
    *,
    base_url: str = "",
    expected_scope_text: str = "",
    expected_target_files: list[str] | None = None,
) -> dict[str, Any] | None:
    return _load_scoped_runtime_state(
        "supervise",
        agent_id,
        base_url=base_url,
        expected_scope_text=expected_scope_text,
        expected_target_files=expected_target_files,
    )


def _supervise_status_next_actions(
    error: dict[str, Any],
    *,
    agent_id: str,
    expected_scope_text: str = "",
    expected_target_files: list[str] | None = None,
) -> list[str]:
    kind = str(error.get("kind") or "")
    target_files = [str(item) for item in expected_target_files or [] if str(item).strip()]
    actions = [
        "Do not blind-retry the previous done/after-edit call.",
    ]
    if kind in {"missing_supervise_state", "supervise_state_stale"}:
        actions.append(
            "Refresh supervision with scripts\\codex-3can.cmd prepare for the same task and target files before running done again."
        )
    elif kind in {"target_file_mismatch", "scope_text_mismatch"}:
        actions.append(
            "Run done with the exact TargetFiles from the matching prepare scope, or split the completion into one done call per prepared scope."
        )
    elif kind == "base_url_mismatch":
        actions.append("Run scripts\\codex-3can.cmd doctor and bootstrap the correct project-local 3CAN endpoint before continuing.")
    elif kind == "supervision_blocked":
        actions.append("Resolve the blocked supervisor gates, then rerun prepare for the same target files.")
    elif kind == "supervision_not_pass":
        actions.append("Acknowledge warning gates in the work evidence or rerun prepare until supervisor status is pass.")
    else:
        actions.append("Inspect the supervisor error payload, then rerun prepare for the intended scope.")
    if target_files:
        actions.append("Expected TargetFiles: " + ", ".join(target_files))
    if expected_scope_text:
        actions.append("Expected scope text: " + expected_scope_text[:180])
    actions.append(f"AgentId: {agent_id}")
    return actions


def _significant_scope_tokens(text: str) -> set[str]:
    tokens = {
        item.lower()
        for item in re.findall(r"[A-Za-z0-9][A-Za-z0-9_.-]{2,}|[\u4e00-\u9fff]{2,}", text)
    }
    return {item for item in tokens if item not in GENERIC_SCOPE_TOKENS}


def _normalize_scope_path(path: str) -> str:
    return path.strip().replace("\\", "/").lower()


def _ticket_scope_validation(
    ticket_payload: dict[str, Any],
    *,
    expected_scope_text: str = "",
    expected_target_files: list[str] | None = None,
) -> dict[str, Any] | None:
    scope = ticket_payload.get("scope") if isinstance(ticket_payload.get("scope"), dict) else {}
    ticket_text = " ".join(
        [
            str(ticket_payload.get("task_description") or ""),
            " ".join(str(item) for item in scope.get("scope_keywords") or []),
            " ".join(str(item) for item in scope.get("target_files") or []),
        ]
    )

    if expected_scope_text:
        expected_tokens = _significant_scope_tokens(expected_scope_text)
        ticket_tokens = _significant_scope_tokens(ticket_text)
        overlap = sorted(expected_tokens & ticket_tokens)
        if expected_tokens and not overlap:
            return {
                "kind": "scope_text_mismatch",
                "expected_scope_text": expected_scope_text[:240],
                "ticket_task_description": ticket_payload.get("task_description", ""),
                "ticket_scope_keywords": scope.get("scope_keywords") or [],
            }

    if expected_target_files:
        ticket_paths = {_normalize_scope_path(str(item)) for item in scope.get("target_files") or []}
        missing = []
        for item in expected_target_files:
            normalized = _normalize_scope_path(item)
            if normalized and normalized not in ticket_paths:
                missing.append(item)
        if missing:
            return {
                "kind": "target_file_mismatch",
                "missing_target_files": missing,
                "ticket_target_files": sorted(ticket_paths),
            }

    return None


def _ticket_consume_payload(
    ticket_payload: dict[str, Any],
    *,
    agent_id: str,
    tool_name: str,
    tool_input_summary: str,
) -> dict[str, str]:
    """Bind a consume receipt to the exact server-issued ticket snapshot."""

    if not isinstance(ticket_payload, dict):
        raise ValueError("ticket_payload_invalid")
    ticket_agent_id = str(ticket_payload.get("agent_id") or "").strip()
    requested_agent_id = str(agent_id or "").strip()
    if not ticket_agent_id:
        raise ValueError("ticket_agent_id_missing")
    if requested_agent_id and ticket_agent_id != requested_agent_id:
        raise ValueError("ticket_agent_id_mismatch")
    target_digest = str(ticket_payload.get("target_digest") or "").strip()
    scope_digest = str(ticket_payload.get("scope_digest") or "").strip()
    if not target_digest:
        raise ValueError("ticket_target_digest_missing")
    if not scope_digest:
        raise ValueError("ticket_scope_digest_missing")
    return {
        "agent_id": ticket_agent_id,
        "tool_name": str(tool_name or "").strip(),
        "tool_input_summary": str(tool_input_summary or "").strip(),
        "target_digest": target_digest,
        "scope_digest": scope_digest,
    }


def _short_node(node: dict[str, Any]) -> dict[str, Any]:
    content = node.get("content") if isinstance(node.get("content"), dict) else {}
    summary = str(
        content.get("current_state")
        or content.get("summary")
        or content.get("description")
        or node.get("summary")
        or ""
    )
    return {
        "id": node.get("id"),
        "name": node.get("name"),
        "cluster": node.get("cluster"),
        "type": node.get("type"),
        "status": node.get("status"),
        "priority": node.get("priority"),
        "activation_count": node.get("activation_count", 0),
        "summary": summary[:220],
    }


def _fetch_node_brief(base_url: str, node_id: str) -> tuple[bool, dict[str, Any]]:
    ok, response = _try_json_request(base_url, f"/api/nodes/{node_id}", timeout=8.0)
    if ok and isinstance(response, dict):
        return True, _short_node(response)
    return False, {"id": node_id, "missing": True}


def _normalize_memory_registry(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, list[str]] = {}
    for lane_name, node_ids in value.items():
        if not isinstance(lane_name, str):
            continue
        if not isinstance(node_ids, list):
            continue
        clean_ids = [str(item) for item in node_ids if str(item).strip()]
        if clean_ids:
            normalized[lane_name] = list(dict.fromkeys(clean_ids))
    return normalized


def _normalize_lane_weights(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    weights: dict[str, float] = {}
    for lane_name, raw_weight in value.items():
        if not isinstance(lane_name, str):
            continue
        try:
            weights[lane_name] = max(0.0, min(100.0, float(raw_weight)))
        except (TypeError, ValueError):
            continue
    return weights


def _normalize_required_edges(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    edges: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source") or "").strip()
        target = str(item.get("target") or "").strip()
        edge_type = str(item.get("type") or "requires").strip()
        if not source or not target or not edge_type:
            continue
        try:
            weight = float(item.get("weight", 1.0))
        except (TypeError, ValueError):
            weight = 1.0
        edges.append(
            {
                "source": source,
                "target": target,
                "type": edge_type,
                "weight": max(0.0, min(10.0, weight)),
                "description": str(item.get("description") or ""),
            }
        )
    return edges


def _load_core_memory_node_ids(base_url: str) -> tuple[dict[str, list[str]], str, list[str], dict[str, Any]]:
    ok, response = _try_json_request(base_url, f"/api/nodes/{CORE_MEMORY_REGISTRY_NODE_ID}", timeout=8.0)
    if ok and isinstance(response, dict):
        content = response.get("content") if isinstance(response.get("content"), dict) else {}
        extra = content.get("extra") if isinstance(content.get("extra"), dict) else {}
        registry = (
            _normalize_memory_registry(extra.get("memory_lanes"))
            or _normalize_memory_registry(extra.get("lanes"))
            or _normalize_memory_registry(content.get("memory_lanes"))
        )
        if registry:
            lane_weights = (
                _normalize_lane_weights(extra.get("lane_weights"))
                or _normalize_lane_weights(extra.get("memory_lane_weights"))
                or FALLBACK_LANE_EXECUTION_WEIGHTS
            )
            required_edges = (
                _normalize_required_edges(extra.get("required_edges"))
                or _normalize_required_edges(extra.get("memory_lane_required_edges"))
                or FALLBACK_REQUIRED_MEMORY_EDGES
            )
            return registry, "3can-manifest", [], {
                "lane_weights": lane_weights,
                "required_edges": required_edges,
            }
    return FALLBACK_CORE_MEMORY_NODE_IDS, "helper-fallback", [CORE_MEMORY_REGISTRY_NODE_ID], {
        "lane_weights": FALLBACK_LANE_EXECUTION_WEIGHTS,
        "required_edges": FALLBACK_REQUIRED_MEMORY_EDGES,
    }


def _edge_signature(edge: dict[str, Any]) -> tuple[str, str, str]:
    return (str(edge.get("source") or ""), str(edge.get("target") or ""), str(edge.get("type") or ""))


def _edge_brief(edge: dict[str, Any]) -> dict[str, Any]:
    try:
        weight = float(edge.get("weight", 0.0))
    except (TypeError, ValueError):
        weight = 0.0
    return {
        "source": edge.get("source"),
        "target": edge.get("target"),
        "type": edge.get("type"),
        "weight": weight,
        "description": str(edge.get("description") or "")[:160],
    }


def _fetch_edges_for_node(base_url: str, node_id: str) -> list[dict[str, Any]]:
    ok, response = _try_json_request(base_url, f"/api/edges?node_id={quote(node_id, safe='')}", timeout=8.0)
    if not ok or not isinstance(response, list):
        return []
    return [_edge_brief(edge) for edge in response if isinstance(edge, dict)]


def _parse_node_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _node_freshness_bonus(node: dict[str, Any], *, now: datetime | None = None) -> float:
    node_id = str(node.get("id") or "")
    updated = _parse_node_timestamp(node.get("updated_at") or node.get("created_at"))
    if not updated:
        return 0.0
    now = now or datetime.now(timezone.utc)
    age_days = max(0.0, (now - updated.astimezone(timezone.utc)).total_seconds() / 86400)
    if age_days <= 7:
        return 6.0
    if age_days <= 30:
        return 4.0
    if age_days <= 90:
        return 2.0
    if node_id.startswith(STABLE_MEMORY_PREFIXES):
        return 0.0
    if age_days >= 365:
        return -6.0
    return -2.0


def _node_execution_weight(node: dict[str, Any], lanes: list[str], lane_weights: dict[str, float], edge_count: int) -> float:
    lane_base = max((lane_weights.get(lane, 50.0) for lane in lanes), default=50.0)
    priority = str(node.get("priority") or "medium")
    status = str(node.get("status") or "active")
    node_type = str(node.get("type") or "")
    try:
        activation_count = int(node.get("activation_count") or 0)
    except (TypeError, ValueError):
        activation_count = 0
    priority_weight = PRIORITY_EXECUTION_WEIGHTS.get(priority, 50)
    type_bonus = TYPE_EXECUTION_BONUS.get(node_type, 0)
    heat = min(activation_count * 1.5, 8.0)
    edge_bonus = min(edge_count * 2.0, 8.0)
    freshness = _node_freshness_bonus(node)
    raw = lane_base * 0.55 + priority_weight * 0.35 + type_bonus + heat + edge_bonus + freshness
    return round(max(0.0, min(100.0, raw * STATUS_EXECUTION_FACTOR.get(status, 0.5))), 2)


def _build_coordination_graph(base_url: str, lanes: dict[str, Any], registry_meta: dict[str, Any]) -> dict[str, Any]:
    lane_weights = dict(FALLBACK_LANE_EXECUTION_WEIGHTS)
    lane_weights.update(_normalize_lane_weights(registry_meta.get("lane_weights")))
    expected_edges = _normalize_required_edges(registry_meta.get("required_edges"))

    node_to_lanes: dict[str, list[str]] = {}
    node_lookup: dict[str, dict[str, Any]] = {}
    for lane_name, payload in lanes.items():
        if not isinstance(payload, dict):
            continue
        for node in payload.get("nodes") or []:
            node_id = str(node.get("id") or "")
            if not node_id:
                continue
            node_to_lanes.setdefault(node_id, [])
            if lane_name not in node_to_lanes[node_id]:
                node_to_lanes[node_id].append(lane_name)
            node_lookup[node_id] = node

    edge_by_sig: dict[tuple[str, str, str], dict[str, Any]] = {}
    edge_counts: dict[str, int] = {}
    for node_id in sorted(node_to_lanes):
        edges = _fetch_edges_for_node(base_url, node_id)
        edge_counts[node_id] = len(edges)
        for edge in edges:
            edge_by_sig.setdefault(_edge_signature(edge), edge)

    missing_edges = [
        edge for edge in expected_edges
        if _edge_signature(edge) not in edge_by_sig
    ]
    critical_missing_edges = [edge for edge in missing_edges if float(edge.get("weight") or 0) >= 0.9]

    node_weights = []
    for node_id, node in node_lookup.items():
        weight = _node_execution_weight(node, node_to_lanes[node_id], lane_weights, edge_counts.get(node_id, 0))
        node_weights.append(
            {
                "id": node_id,
                "lanes": node_to_lanes[node_id],
                "execution_weight": weight,
                "priority": node.get("priority"),
                "status": node.get("status"),
                "type": node.get("type"),
                "activation_count": node.get("activation_count", 0),
                "edge_count": edge_counts.get(node_id, 0),
            }
        )

    node_weights.sort(key=lambda item: (-float(item["execution_weight"]), str(item["id"])))
    return {
        "status": "pass" if not critical_missing_edges else "warn",
        "lane_weights": lane_weights,
        "node_weights": node_weights,
        "must_consume_node_ids": [item["id"] for item in node_weights if float(item["execution_weight"]) >= 80.0],
        "edge_count": len(edge_by_sig),
        "edges": list(edge_by_sig.values())[:40],
        "required_edges": expected_edges,
        "missing_required_edges": missing_edges,
        "critical_missing_edges": critical_missing_edges,
    }


def _loop_signatures_path() -> Path:
    return LOG_DIR / "loop_signatures.json"


def _loop_signatures_checksum_path() -> Path:
    return LOG_DIR / "loop_signatures.sha256"


def _loop_signatures_last_good_path() -> Path:
    return LOG_DIR / "loop_signatures.last-good.json"


def _loop_signatures_last_good_checksum_path() -> Path:
    return LOG_DIR / "loop_signatures.last-good.sha256"


def _loop_signatures_lock_path() -> Path:
    return LOG_DIR / "loop_signatures.lock"


class LoopSignatureStoreError(RuntimeError):
    """The local occurrence ledger cannot be trusted."""


@contextmanager
def _loop_signature_store_lock(*, timeout_sec: float = 10.0) -> Iterator[None]:
    """Hold an OS-backed, cross-process lock around ledger transactions."""

    path = _loop_signatures_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
            os.fsync(descriptor)
        deadline = time.monotonic() + max(0.1, float(timeout_sec))
        while True:
            try:
                os.lseek(descriptor, 0, os.SEEK_SET)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    raise LoopSignatureStoreError(
                        f"BLOCKED: timed out acquiring occurrence-store lock {path}"
                    )
                time.sleep(0.025)
        try:
            yield
        finally:
            os.lseek(descriptor, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _atomic_replace_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _store_checksum(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _decode_loop_signature_store(data: bytes, *, source: Path) -> dict[str, Any]:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise LoopSignatureStoreError(f"invalid occurrence store {source}: {exc}") from exc
    if not isinstance(payload, dict):
        raise LoopSignatureStoreError(f"invalid occurrence store {source}: expected JSON object")
    signatures = payload.get("signatures")
    if not isinstance(signatures, dict):
        raise LoopSignatureStoreError(f"invalid occurrence store {source}: signatures must be an object")
    payload.setdefault("version", 2)
    return payload


def _read_loop_signature_pair(
    data_path: Path,
    checksum_path: Path,
) -> tuple[dict[str, Any], bool]:
    data = data_path.read_bytes()
    checksum_present = checksum_path.is_file()
    if checksum_present:
        expected = checksum_path.read_text(encoding="ascii").strip().casefold()
        actual = _store_checksum(data)
        if not re.fullmatch(r"[0-9a-f]{64}", expected) or expected != actual:
            raise LoopSignatureStoreError(
                f"occurrence-store checksum mismatch for {data_path}"
            )
    return _decode_loop_signature_store(data, source=data_path), checksum_present


def _load_loop_signatures_unlocked() -> dict[str, Any]:
    path = _loop_signatures_path()
    if not path.exists():
        last_good = _loop_signatures_last_good_path()
        if not last_good.exists():
            return {"version": 2, "signatures": {}, "_store_status": "READY"}
        try:
            payload, _checksum_present = _read_loop_signature_pair(
                last_good,
                _loop_signatures_last_good_checksum_path(),
            )
        except (OSError, LoopSignatureStoreError) as exc:
            raise LoopSignatureStoreError(
                f"BLOCKED: occurrence store and last-good copy are unavailable: {exc}"
            ) from exc
        payload["_store_status"] = "PARTIAL"
        payload["_store_warning"] = "primary occurrence store missing; recovered last-good copy"
        payload["_store_recovered_from"] = str(last_good)
        return payload
    try:
        payload, checksum_present = _read_loop_signature_pair(
            path,
            _loop_signatures_checksum_path(),
        )
        payload["_store_status"] = "READY" if checksum_present else "PARTIAL"
        if not checksum_present:
            payload["_store_warning"] = (
                "legacy occurrence store has no checksum; next successful write will seal it"
            )
        return payload
    except (OSError, LoopSignatureStoreError) as primary_error:
        last_good = _loop_signatures_last_good_path()
        try:
            payload, _checksum_present = _read_loop_signature_pair(
                last_good,
                _loop_signatures_last_good_checksum_path(),
            )
        except (OSError, LoopSignatureStoreError) as recovery_error:
            raise LoopSignatureStoreError(
                "BLOCKED: occurrence store is corrupt and no valid last-good copy exists; "
                f"primary={primary_error}; recovery={recovery_error}"
            ) from recovery_error
        payload["_store_status"] = "PARTIAL"
        payload["_store_warning"] = f"primary occurrence store rejected: {primary_error}"
        payload["_store_recovered_from"] = str(last_good)
        return payload


def _save_loop_signatures_unlocked(payload: dict[str, Any]) -> Path:
    path = _loop_signatures_path()
    persisted = {
        key: value
        for key, value in payload.items()
        if not str(key).startswith("_store_")
    }
    persisted.setdefault("version", 2)
    if not isinstance(persisted.get("signatures"), dict):
        raise LoopSignatureStoreError("BLOCKED: refusing to persist a non-object signatures map")
    data = (
        json.dumps(persisted, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    digest = (_store_checksum(data) + "\n").encode("ascii")

    previous_data: bytes | None = None
    if path.is_file():
        try:
            previous_data = path.read_bytes()
            _decode_loop_signature_store(previous_data, source=path)
            checksum_path = _loop_signatures_checksum_path()
            if checksum_path.is_file():
                expected = checksum_path.read_text(encoding="ascii").strip().casefold()
                if expected != _store_checksum(previous_data):
                    previous_data = None
        except (OSError, LoopSignatureStoreError):
            previous_data = None

    if previous_data is not None:
        _atomic_replace_bytes(_loop_signatures_last_good_path(), previous_data)
        _atomic_replace_bytes(
            _loop_signatures_last_good_checksum_path(),
            (_store_checksum(previous_data) + "\n").encode("ascii"),
        )

    _atomic_replace_bytes(path, data)
    _atomic_replace_bytes(_loop_signatures_checksum_path(), digest)
    if not _loop_signatures_last_good_path().exists():
        _atomic_replace_bytes(_loop_signatures_last_good_path(), data)
        _atomic_replace_bytes(_loop_signatures_last_good_checksum_path(), digest)
    return path


def _load_loop_signatures() -> dict[str, Any]:
    with _loop_signature_store_lock():
        return _load_loop_signatures_unlocked()


def _save_loop_signatures(payload: dict[str, Any]) -> Path:
    with _loop_signature_store_lock():
        return _save_loop_signatures_unlocked(payload)


@contextmanager
def _loop_signature_transaction() -> Iterator[dict[str, Any]]:
    """Read/modify/write one occurrence ledger revision under one OS lock."""

    with _loop_signature_store_lock():
        payload = _load_loop_signatures_unlocked()
        yield payload
        _save_loop_signatures_unlocked(payload)


def _normalize_signature_text(text: str) -> str:
    value = (text or "").strip().lower()
    value = re.sub(r"rt_[a-z0-9]+", "rt_*", value)
    value = re.sub(r"\b[0-9a-f]{16,}\b", "<hex>", value)
    value = re.sub(r"\b\d{4}-\d{2}-\d{2}t[0-9:.+-]+", "<timestamp>", value)
    value = re.sub(r"\s+", " ", value)
    return value[:900]


def _normalize_target_path(path: str) -> str:
    return (path or "").strip().replace("\\", "/").lower()


def _failure_operation_class(command_summary: str, explicit: str = "") -> str:
    if explicit.strip():
        return _normalize_signature_text(explicit)[:80]
    normalized = _normalize_signature_text(command_summary)
    match = re.search(
        r"\b(build|compile|test|pytest|route|ticket|prepare|writeback|deploy|start|stop|"
        r"read|write|patch|delete|move|copy|git|http|api|import|export)\b",
        normalized,
    )
    return match.group(1) if match else "unknown-operation"


def _failure_component(
    command_summary: str,
    target_files: list[str],
    scope_keywords: list[str],
    explicit: str = "",
) -> str:
    if explicit.strip():
        return _normalize_signature_text(_public_target_ref(explicit))[:120]
    for keyword in scope_keywords:
        normalized = _normalize_signature_text(_public_target_ref(keyword))
        if normalized and normalized not in GENERIC_SCOPE_TOKENS:
            return normalized[:120]
    for target in target_files:
        normalized = _normalize_target_path(_public_target_ref(target))
        if not normalized:
            continue
        path = Path(normalized)
        parent = path.parent.name
        return (parent or path.stem or "unknown-component")[:120]
    parts = _slugify(command_summary, max_parts=2)
    return "-".join(parts) if parts else "unknown-component"


def _failure_error_type(error_excerpt: str, explicit: str = "") -> str:
    if explicit.strip():
        return _normalize_signature_text(explicit)[:120]
    normalized = _normalize_signature_text(error_excerpt)
    http_status = re.search(
        r"(?:http(?:[_ ]status)?|status(?:[_ ]code)?)\D{0,8}([45]\d{2})"
        r"|\b([45]\d{2})\b",
        normalized,
    )
    if http_status:
        return f"http-{http_status.group(1) or http_status.group(2)}"
    patterns = (
        (r"unicode(decode|encode)error", "unicode-error"),
        (r"jsondecodeerror|json decode", "json-decode-error"),
        (r"permissionerror|access.+denied", "permission-denied"),
        (r"filenotfounderror|cannot find|not found", "not-found"),
        (r"timeout|timed out", "timeout"),
        (r"connection(refused|error)|actively refused", "connection-error"),
        (r"syntaxerror|parsererror", "syntax-error"),
        (r"assertionerror|assert.+failed", "assertion-failed"),
    )
    for pattern, label in patterns:
        if re.search(pattern, normalized):
            return label
    tokens = _loop_text_tokens(normalized)
    return "-".join(tokens[:3]) if tokens else "unknown-error"


def _loop_signature_key(
    command_summary: str,
    error_excerpt: str,
    target_files: list[str],
    *,
    scope_keywords: list[str] | None = None,
    operation_class: str = "",
    component: str = "",
    error_type: str = "",
    project_identity: str = "",
    root_cause: str = "",
) -> str:
    """Return a stable ErrorCase fingerprint, excluding volatile command/path details."""
    contract = _require_error_knowledge_contract()
    resolved_scope_keywords = scope_keywords or []
    resolved_operation = _failure_operation_class(command_summary, operation_class)
    resolved_component = _failure_component(
        command_summary,
        target_files,
        resolved_scope_keywords,
        component,
    )
    resolved_error_type = _failure_error_type(error_excerpt, error_type)
    return contract.deterministic_fingerprint(
        project_id=project_identity or "local-project",
        operation=resolved_operation,
        component=resolved_component,
        error_type=resolved_error_type,
        root_cause=root_cause or "unclassified-root-cause",
    )


def _loop_text_tokens(text: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9][a-z0-9_.:-]{2,}|[\u4e00-\u9fff]{2,}", text.lower())
    ignored = {"error", "failed", "failure", "command", "timeout", "traceback"}
    return [token for token in tokens if token not in ignored]


def _failure_gate_status(count: int) -> str:
    if count >= LOOP_RULE_PROMOTION_THRESHOLD:
        return "requires_err_update_or_rule_promotion"
    if count >= LOOP_GATE_THRESHOLD:
        return "block_blind_retry"
    return "recorded"


def _error_case_status(entry: dict[str, Any]) -> str:
    explicit = str(entry.get("case_status") or "").strip().lower()
    if explicit:
        return explicit
    if entry.get("resolved_at"):
        return "resolved"
    if entry.get("last_diagnosis"):
        return "diagnosed"
    return "open" if int(entry.get("count") or 0) >= LOOP_GATE_THRESHOLD else "observed"


def _error_case_is_recent(entry: dict[str, Any], *, now: datetime | None = None) -> bool:
    observed_at = _entry_datetime(entry)
    if observed_at is None:
        return False
    current = now or datetime.now(timezone.utc)
    return observed_at >= current - timedelta(hours=ERROR_CASE_BLOCK_WINDOW_HOURS)


def _infer_failure_related_nodes(text: str) -> list[str]:
    related: list[str] = []
    for hit in _memory_policy_hits(text):
        related.extend(str(item) for item in hit.get("required_node_ids") or [])
    return list(dict.fromkeys(related))


_CANONICAL_ERROR_IDENTITY_FIELD = re.compile(
    r"\[\s*(project(?:_id|_identity)?|operation(?:_class)?|component|error(?:_type)?)"
    r"\s*=\s*([^\]]+?)\s*\]",
    re.IGNORECASE,
)


def _canonical_error_identity_from_text(text: str) -> dict[str, str]:
    aliases = {
        "project": "project_id",
        "project_id": "project_id",
        "project_identity": "project_id",
        "operation": "operation",
        "operation_class": "operation",
        "component": "component",
        "error": "error_type",
        "error_type": "error_type",
    }
    fields: dict[str, str] = {}
    for raw_key, raw_value in _CANONICAL_ERROR_IDENTITY_FIELD.findall(text or ""):
        key = aliases.get(raw_key.strip().lower())
        value = _normalize_signature_text(raw_value)
        if key and value:
            fields[key] = value
    return fields


def _entry_project_identity(entry: dict[str, Any]) -> str:
    raw = entry.get("project_identity") or ""
    if isinstance(raw, dict):
        raw = raw.get("project_id") or raw.get("project_name") or ""
    return _normalize_signature_text(str(raw)) or "local-project"


def _loop_signature_matches(entry: dict[str, Any], text: str, target_files: list[str]) -> bool:
    """Return true only for an explicit reference or full canonical identity."""

    del target_files  # Paths are advisory only; they never authorize a block.
    raw_text = str(text or "").casefold()
    signature = str(entry.get("signature") or "")
    if signature and signature.casefold() in raw_text:
        return True
    node_id = str(entry.get("node_id") or "").lower()
    if node_id and node_id in raw_text:
        return True

    requested = _canonical_error_identity_from_text(text)
    if set(requested) != {"project_id", "operation", "component", "error_type"}:
        return False
    expected = {
        "project_id": _entry_project_identity(entry),
        "operation": _normalize_signature_text(str(entry.get("operation_class") or "")),
        "component": _normalize_signature_text(str(entry.get("component") or "")),
        "error_type": _normalize_signature_text(str(entry.get("error_type") or "")),
    }
    return all(expected.get(key) and expected[key] == value for key, value in requested.items())


def _loop_signature_heuristic_warning(
    entry: dict[str, Any],
    text: str,
    target_files: list[str],
) -> bool:
    """Return a non-blocking hint for same-basename or partial tuple overlap."""

    entry_targets = {
        Path(_normalize_target_path(item)).name
        for item in entry.get("target_files") or []
        if item
    }
    current_targets = {
        Path(_normalize_target_path(item)).name
        for item in target_files
        if item
    }
    if entry_targets and current_targets and entry_targets.intersection(current_targets):
        return True
    current_text = _normalize_signature_text(text)
    fields = [
        _normalize_signature_text(str(entry.get(key) or ""))
        for key in ("operation_class", "component", "error_type")
    ]
    return sum(bool(value and value in current_text) for value in fields) >= 2


def _repeated_error_policy_hits(text: str, target_files: list[str]) -> list[dict[str, Any]]:
    try:
        _require_error_knowledge_contract()
        payload = _load_loop_signatures()
    except (ErrorKnowledgeContractUnavailable, LoopSignatureStoreError) as exc:
        return [
            {
                "policy_id": "error_lifecycle_state_unavailable",
                "status": "UNAVAILABLE",
                "lanes": ["error_warnings"],
                "required_node_ids": [],
                "must_do": [
                    "Repair or restore the canonical error-lifecycle state independently; the current task may continue unless an exact server ErrorCase blocks it."
                ],
                "must_not_do": [],
                "block": False,
                "error": str(exc),
            }
        ]
    signatures = payload.get("signatures") or {}
    hits: list[dict[str, Any]] = []
    for signature, entry in sorted(signatures.items()):
        if not isinstance(entry, dict):
            continue
        try:
            count = int(entry.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        case_status = _error_case_status(entry)
        exact_match = _loop_signature_matches(entry, text, target_files)
        heuristic_match = (
            not exact_match
            and _loop_signature_heuristic_warning(entry, text, target_files)
        )
        if (
            count < LOOP_GATE_THRESHOLD
            or case_status in {"resolved", "superseded", "archived"}
            or not _error_case_is_recent(entry)
            or not (exact_match or heuristic_match)
        ):
            continue
        node_id = str(entry.get("node_id") or f"ERR-repeated-loop-{signature}")
        related_node_ids = [str(item) for item in entry.get("related_node_ids") or [] if item]
        required_nodes = list(dict.fromkeys([node_id, *related_node_ids]))
        block = exact_match and case_status in {"open", "regressed"}
        must_do = [
            (
                "Read the exact ErrorCase before retrying this canonical identity."
                if exact_match
                else "Review the similar ErrorCase; this basename/partial match is advisory only."
            )
        ]
        if block:
            must_do.append("Record a diagnosis before retrying the same unresolved failure.")
        if count >= LOOP_RULE_PROMOTION_THRESHOLD:
            must_do.append("Resolve, supersede, or promote the verified lesson after remediation.")
        hits.append(
            {
                "policy_id": "repeated_error_loop_gate",
                "signature": signature,
                "occurrence_count": count,
                "case_status": case_status,
                "gate_status": _failure_gate_status(count),
                "match_kind": "exact_identity" if exact_match else "heuristic_warning",
                "status": "BLOCKED" if block else "manual_review",
                "lanes": ["error_warnings"],
                "required_node_ids": required_nodes,
                "related_node_ids": related_node_ids,
                "must_do": must_do,
                "must_not_do": ["Do not rerun the same command/path without diagnosis after a repeated failure signature."],
                "block": block,
            }
        )
    return hits


def _memory_policy_hits(text: str) -> list[dict[str, Any]]:
    """Return public-safe static policies.

    Project-specific policies, user preferences, ports, and node IDs belong in
    the graph registry or project capsule. The reusable kit intentionally ships
    no machine- or tenant-specific defaults.
    """

    return []


def build_memory_preflight(
    base_url: str,
    *,
    agent_id: str,
    task_description: str,
    target_files: list[str],
    scope_keywords: list[str],
    tool_name: str = "",
    tool_input_summary: str = "",
) -> dict[str, Any]:
    text = " ".join(
        [
            task_description,
            " ".join(target_files),
            " ".join(scope_keywords),
            tool_name,
            tool_input_summary,
        ]
    )
    policy_hits = _memory_policy_hits(text)
    policy_hits.extend(_repeated_error_policy_hits(text, target_files))
    core_memory_node_ids, registry_source, registry_missing_node_ids, registry_meta = _load_core_memory_node_ids(base_url)
    required_lanes = set(BASE_PREFLIGHT_LANES)
    required_node_ids: set[str] = set()
    must_do: list[str] = []
    must_not_do: list[str] = []
    for lane in required_lanes:
        required_node_ids.update(core_memory_node_ids.get(lane, []))
    for hit in policy_hits:
        required_lanes.update(hit.get("lanes") or [])
        required_node_ids.update(str(item) for item in hit.get("required_node_ids") or [])
        must_do.extend(str(item) for item in hit.get("must_do") or [])
        must_not_do.extend(str(item) for item in hit.get("must_not_do") or [])

    lanes: dict[str, Any] = {}
    missing_required_node_ids: list[str] = []
    for lane_name in ("user_preferences", "environment_constraints", "error_warnings", "project_constitution"):
        lane_ids = []
        lane_ids.extend(core_memory_node_ids.get(lane_name, []))
        for hit in policy_hits:
            if lane_name in (hit.get("lanes") or []):
                lane_ids.extend(str(item) for item in hit.get("required_node_ids") or [])
        deduped_ids = list(dict.fromkeys(lane_ids))
        nodes: list[dict[str, Any]] = []
        missing: list[str] = []
        for node_id in deduped_ids:
            ok, brief = _fetch_node_brief(base_url, node_id)
            if ok:
                nodes.append(brief)
            else:
                missing.append(node_id)
                if node_id in required_node_ids:
                    missing_required_node_ids.append(node_id)
        lanes[lane_name] = {
            "required": lane_name in required_lanes,
            "node_ids": deduped_ids,
            "nodes": nodes,
            "missing_node_ids": missing,
            "status": "hit" if nodes else ("missing" if lane_name in required_lanes else "optional"),
        }

    coordination_graph = _build_coordination_graph(base_url, lanes, registry_meta)
    stale_context_warnings = []
    if STALE_ENV_PATTERN.search(text):
        stale_context_warnings.append("Legacy environment wording detected; suppress old environment route recommendations unless explicitly doing history/recovery.")

    hit_required_lanes = [
        lane
        for lane, payload in lanes.items()
        if payload["required"] and payload["nodes"]
    ]
    missing_required_lanes = [
        lane
        for lane, payload in lanes.items()
        if payload["required"] and not payload["nodes"]
    ]
    memory_quality = {
        "required_lanes": sorted(required_lanes),
        "hit_required_lanes": hit_required_lanes,
        "missing_required_lanes": missing_required_lanes,
        "missing_required_node_ids": sorted(set(missing_required_node_ids)),
        "registry_source": registry_source,
        "registry_missing_node_ids": registry_missing_node_ids,
        "coordination_graph_status": coordination_graph["status"],
        "critical_missing_edges": coordination_graph["critical_missing_edges"],
        "blocking_policy_ids": [
            str(hit.get("policy_id"))
            for hit in policy_hits
            if hit.get("block")
        ],
        "score": (
            len(hit_required_lanes)
            - len(missing_required_lanes)
            - len(stale_context_warnings)
            - len(coordination_graph["critical_missing_edges"])
            - len([hit for hit in policy_hits if hit.get("block")])
        ),
    }
    has_blocking_policy = any(hit.get("block") for hit in policy_hits)
    if has_blocking_policy:
        status = "block"
    elif not missing_required_lanes and coordination_graph["status"] == "pass":
        status = "pass"
    else:
        status = "warn"
    return {
        "ok": True,
        "agent_id": agent_id,
        "status": status,
        "source": "codex-helper-existing-wrapper",
        "registry_source": registry_source,
        "policy_hits": policy_hits,
        "lanes": lanes,
        "must_do_next": list(dict.fromkeys(must_do)),
        "must_not_do": list(dict.fromkeys(must_not_do)),
        "stale_context_warnings": stale_context_warnings,
        "coordination_graph": coordination_graph,
        "memory_quality": memory_quality,
    }


def memory_preflight(args: argparse.Namespace) -> int:
    result = build_memory_preflight(
        args.base_url,
        agent_id=args.agent_id,
        task_description=args.task_description,
        target_files=args.target_files,
        scope_keywords=args.scope_keywords,
        tool_name=args.tool_name or "",
        tool_input_summary=args.tool_input_summary or "",
    )
    _print_json(result)
    return 0


def _record_local_token_estimate(
    base_url: str,
    *,
    agent_id: str,
    command: str,
    request_payload: dict[str, Any],
    response_payload: Any = None,
    route_ticket_id: str = "",
    session_id: str = "",
) -> dict[str, Any] | None:
    if not _token_estimate_enabled() or not agent_id:
        return None
    resolved_session_id = _session_id_for_agent(agent_id, session_id)
    estimate_payload = {
        "model": _estimate_model_for_agent(agent_id),
        "input": {
            "command": command,
            "request": request_payload,
        },
        "output": response_payload if response_payload is not None else {},
    }
    ok, estimate = _try_json_request(
        base_url,
        "/api/token-usage/estimate",
        method="POST",
        payload=estimate_payload,
        timeout=8.0,
    )
    if not ok or not isinstance(estimate, dict):
        return None
    event = {
        "request_id": f"codex_estimate_{command}_{uuid.uuid4().hex}",
        "provider": "codex-cli",
        "model": _estimate_model_for_agent(agent_id),
        "agent_id": agent_id,
        "session_id": resolved_session_id,
        "task_id": command,
        "route_ticket_id": route_ticket_id,
        "request_kind": command,
        "usage_source": "local_estimate",
        "status": "estimated",
        "input_tokens": int(estimate.get("estimated_input_tokens") or 0),
        "output_tokens": int(estimate.get("estimated_output_tokens") or 0),
        "total_tokens": int(estimate.get("estimated_total_tokens") or 0),
        "metadata": {
            "source": "3can_codex_helper_auto_estimate",
            "estimate_method": estimate.get("estimate_method", ""),
            "byte_count": estimate.get("byte_count", 0),
            "session_source": "env_or_local_runtime",
            **_current_project_metadata(base_url=base_url),
        },
    }
    ok, result = _try_json_request(
        base_url,
        "/api/token-usage/events",
        method="POST",
        payload=event,
        timeout=8.0,
    )
    return result if ok and isinstance(result, dict) else None


def _print_json(data: Any) -> None:
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    try:
        sys.stdout.write(payload)
    except UnicodeEncodeError:
        sys.stdout.buffer.write(payload.encode("utf-8"))


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    unique: list[Path] = []
    for path in paths:
        try:
            resolved = str(path.resolve())
        except FileNotFoundError:
            resolved = str(path)
        lowered = resolved.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        unique.append(Path(resolved))
    return unique


def _graph_nodes_dir(engine_root: Path) -> Path:
    return engine_root / "graph" / "nodes"


def _selected_graph_root(engine_root: Path) -> Path:
    configured = os.environ.get("THREECAN_GRAPH_DIR", "").strip()
    candidate = Path(configured).expanduser() if configured else engine_root / "graph"
    return Path(_path_identity(candidate))


def _runtime_path_sha256(path: Path) -> str:
    canonical = os.path.normcase(_path_identity(path))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _expected_runtime_identity(
    engine_root: Path,
    graph_root: Path,
    *,
    startup_nonce: str = "",
) -> dict[str, str]:
    identity = {
        "schema": RUNTIME_IDENTITY_SCHEMA,
        "engine_root_sha256": _runtime_path_sha256(engine_root),
        "graph_root_sha256": _runtime_path_sha256(graph_root),
    }
    if startup_nonce:
        identity["startup_nonce_sha256"] = hashlib.sha256(
            startup_nonce.encode("utf-8")
        ).hexdigest()
    return identity


def _graph_root_node_count(graph_root: Path) -> int:
    nodes_dir = graph_root / "nodes"
    if not nodes_dir.exists():
        return 0
    return sum(1 for child in nodes_dir.glob("*.json"))


def _is_engine_root(path: Path) -> bool:
    return (path / "backend" / "app.py").exists() and (path / "proxy" / "server.py").exists()


def _graph_node_count(engine_root: Path) -> int:
    nodes_dir = _graph_nodes_dir(engine_root)
    if not nodes_dir.exists():
        return 0
    return sum(1 for child in nodes_dir.glob("*.json"))


def _project_sibling_engine_roots() -> list[Path]:
    roots: list[Path] = []
    for parent in (PROJECT_ROOT, *PROJECT_ROOT.parents):
        candidate = parent / "neural-memory"
        if _is_engine_root(candidate):
            roots.append(candidate)
    return _dedupe_paths(roots)


def _desktop_engine_roots() -> list[Path]:
    roots: list[Path] = []
    userprofile = os.environ.get("USERPROFILE")
    desktop_roots: list[Path] = []
    if userprofile:
        desktop_roots.append(Path(userprofile) / "Desktop")
    wsl_users_raw = os.environ.get("THREECAN_WSL_USERS_ROOT", "")
    wsl_users = Path(wsl_users_raw).expanduser() if wsl_users_raw else None
    if wsl_users and wsl_users.exists():
        try:
            desktop_roots.extend(path / "Desktop" for path in wsl_users.iterdir() if path.is_dir())
        except OSError:
            pass

    for desktop in desktop_roots:
        try:
            desktop_exists = desktop.exists()
        except OSError:
            continue
        if not desktop_exists:
            continue
        try:
            children = list(desktop.iterdir())
        except OSError:
            continue
        for child in children:
            candidate = child / "neural-memory"
            if _is_engine_root(candidate):
                roots.append(candidate)
        direct_candidate = desktop / "neural-memory"
        if _is_engine_root(direct_candidate):
            roots.append(direct_candidate)
    return _dedupe_paths(roots)


def _candidate_engine_roots() -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    explicit_root = os.environ.get("THREECAN_ENGINE_ROOT")
    if explicit_root:
        candidates.append({"path": Path(explicit_root).expanduser(), "source": "env:THREECAN_ENGINE_ROOT", "explicit": True})

    mcp_path = os.environ.get("THREECAN_MCP")
    if mcp_path:
        candidates.append({"path": Path(mcp_path).expanduser().parent, "source": "env:THREECAN_MCP", "explicit": False})

    for root in _project_sibling_engine_roots():
        candidates.append({"path": root, "source": "project-sibling:auto-scan", "explicit": False})

    for root in _desktop_engine_roots():
        candidates.append({"path": root, "source": "desktop:auto-scan", "explicit": False})

    candidates.append({"path": STAGING_ENGINE_ROOT, "source": "repo:staging-fallback", "explicit": False})

    unique_candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in candidates:
        path = item["path"]
        key = str(path.resolve() if path.exists() else path).lower()
        if key in seen:
            continue
        seen.add(key)
        item["exists"] = path.exists()
        item["valid_engine_root"] = _is_engine_root(path) if path.exists() else False
        item["node_count"] = _graph_node_count(path) if item["valid_engine_root"] else 0
        unique_candidates.append(item)
    return unique_candidates


def resolve_engine_root(override: str | None = None) -> dict[str, Any]:
    if override:
        root = Path(override).expanduser()
        return {
            "selected": str(root.resolve() if root.exists() else root),
            "source": "cli:--engine-root",
            "node_count": _graph_node_count(root) if _is_engine_root(root) else 0,
            "valid_engine_root": _is_engine_root(root),
            "candidates": [],
        }

    candidates = _candidate_engine_roots()
    explicit = next((item for item in candidates if item.get("explicit") and item.get("valid_engine_root")), None)
    if explicit:
        return {
            "selected": str(explicit["path"].resolve()),
            "source": explicit["source"],
            "node_count": explicit["node_count"],
            "valid_engine_root": True,
            "candidates": [
                {
                    "path": str(item["path"].resolve() if item["path"].exists() else item["path"]),
                    "source": item["source"],
                    "node_count": item["node_count"],
                    "valid_engine_root": item["valid_engine_root"],
                }
                for item in candidates
            ],
        }

    source_priority = {
        "env:THREECAN_ENGINE_ROOT": 5,
        "project-sibling:auto-scan": 4,
        "env:THREECAN_MCP": 3,
        "desktop:auto-scan": 2,
        "repo:staging-fallback": 1,
    }
    valid = [item for item in candidates if item.get("valid_engine_root")]
    valid.sort(key=lambda item: (source_priority.get(item["source"], 0), item["node_count"]), reverse=True)
    selected = valid[0] if valid else {"path": STAGING_ENGINE_ROOT, "source": "repo:staging-fallback", "node_count": 0, "valid_engine_root": False}
    payload = {
        "selected": str(selected["path"].resolve() if selected["path"].exists() else selected["path"]),
        "source": selected["source"],
        "node_count": selected["node_count"],
        "valid_engine_root": selected["valid_engine_root"],
        "candidates": [
            {
                "path": str(item["path"].resolve() if item["path"].exists() else item["path"]),
                "source": item["source"],
                "node_count": item["node_count"],
                "valid_engine_root": item["valid_engine_root"],
            }
            for item in candidates
        ],
    }
    return payload


def _proxy_state(engine_root: Path) -> dict[str, Any] | None:
    state_path = engine_root / "proxy" / "proxy_state.json"
    if not state_path.exists():
        return None
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _proxy_port_from_base_url(base_url: str, default: int = 9700) -> int:
    match = re.search(r":(\d+)(?:/.*)?$", base_url.rstrip("/"))
    return int(match.group(1)) if match else default


def _backend_ports_for_base_url(base_url: str) -> dict[str, int]:
    proxy_port = _proxy_port_from_base_url(base_url)
    if proxy_port == 9700:
        return {"green": 9701, "blue": 9702}
    return {"green": proxy_port + 1, "blue": proxy_port + 2}




def _observe_proxy_slots(
    engine_root: Path,
    *,
    base_url: str = DEFAULT_BASE_URL,
) -> dict[str, Any] | None:
    """Return persisted state plus identity-checked probes without rewriting it.

    Process ownership belongs to the proxy.  The helper must never synthesize a
    slot from a bare PID because such a slot cannot pass the proxy's verified
    retire gate.
    """

    if not (engine_root / "proxy").exists():
        return None
    defaults = _backend_ports_for_base_url(base_url)
    identity_gate = _project_identity_gate(
        base_url,
        {
            "selected": _path_identity(engine_root),
            "source": "proxy-state-refresh",
            "node_count": _graph_node_count(engine_root) if _is_engine_root(engine_root) else 0,
            "valid_engine_root": _is_engine_root(engine_root),
            "candidates": [],
        },
        command="proxy-state-refresh",
    )
    persisted = _proxy_state(engine_root) or {
        "active": "green",
        "green": {"port": defaults["green"]},
        "blue": {"port": defaults["blue"]},
    }
    if identity_gate.get("status") == "block":
        return {
            "persisted": persisted,
            "observed": {},
            "project_identity": identity_gate,
        }
    graph_root = _selected_graph_root(engine_root)
    observed: dict[str, Any] = {}
    for slot, default_port in defaults.items():
        persisted_slot = persisted.get(slot)
        if not isinstance(persisted_slot, dict):
            persisted_slot = {}
        port = int(persisted_slot.get("port") or default_port)
        ok, stats = _try_json_request(
            f"http://127.0.0.1:{port}",
            "/api/stats",
            timeout=2.0,
        )
        if ok:
            healthy, warning = _validate_stats(
                stats,
                min_nodes=0,
                expected_engine_root=engine_root,
                expected_graph_root=graph_root,
            )
            observed[slot] = {
                "port": port,
                "status": "healthy" if healthy else "unhealthy",
                "nodes": stats.get("total_nodes"),
                "edges": stats.get("total_edges"),
                "warning": warning,
            }
        else:
            observed[slot] = {
                "port": port,
                "status": "offline",
                "error": stats,
            }
    return {
        "persisted": persisted,
        "observed": observed,
        "project_identity": identity_gate,
        "writeback": {
            "performed": False,
            "reason": "proxy_owns_managed_process_identity",
        }
    }


def _validate_stats(
    stats: dict[str, Any],
    *,
    min_nodes: int,
    expected_engine_root: Path,
    expected_graph_root: Path,
    expected_startup_nonce: str = "",
) -> tuple[bool, dict[str, Any] | None]:
    if not isinstance(stats, dict):
        return False, {
            "kind": "invalid_stats_payload",
            "reason": "3CAN stats response must be a JSON object",
        }
    expected_identity = _expected_runtime_identity(
        expected_engine_root,
        expected_graph_root,
        startup_nonce=expected_startup_nonce,
    )
    actual_identity = stats.get("runtime_identity")
    if not isinstance(actual_identity, dict):
        return False, {
            "kind": "runtime_identity_missing",
            "reason": "3CAN stats omitted the public-safe runtime identity",
            "expected": expected_identity,
        }
    compared_fields = [
        "schema",
        "engine_root_sha256",
        "graph_root_sha256",
    ]
    if expected_startup_nonce:
        compared_fields.append("startup_nonce_sha256")
    mismatches = {
        field: {
            "expected": expected_identity.get(field),
            "actual": actual_identity.get(field),
        }
        for field in compared_fields
        if actual_identity.get(field) != expected_identity.get(field)
    }
    if mismatches:
        return False, {
            "kind": "runtime_identity_mismatch",
            "reason": "3CAN runtime identity does not match the selected engine/graph",
            "mismatches": mismatches,
        }

    try:
        total_nodes = int(stats.get("total_nodes") or 0)
    except (TypeError, ValueError):
        return False, {
            "kind": "invalid_stats_payload",
            "reason": "3CAN total_nodes must be an integer",
        }
    if total_nodes < min_nodes:
        return False, {
            "kind": "wrong_graph_suspected",
            "reason": f"3CAN is online but total_nodes={total_nodes} < required minimum {min_nodes}",
            "stats": stats,
        }
    readiness = stats.get("readiness")
    if not isinstance(readiness, dict):
        return False, {
            "kind": "canonical_readiness_missing",
            "reason": "3CAN stats omitted the canonical readiness contract",
        }
    if readiness.get("production_ready") is not True:
        return False, {
            "kind": "production_not_ready",
            "reason": "3CAN production readiness is not verified",
            "verification_state": (readiness.get("cache") or {}).get(
                "verification_state"
            ),
            "reasons": readiness.get("reasons") or [],
        }
    if stats.get("healthy") is not True:
        return False, {
            "kind": "compatibility_health_mismatch",
            "reason": "canonical readiness passed but healthy was not true",
        }
    return True, None


def _probe_stats(
    base_url: str,
    *,
    min_nodes: int,
    expected_engine_root: Path,
    expected_graph_root: Path,
    refresh_readiness: bool,
) -> tuple[bool, Any, bool, dict[str, Any] | None]:
    ok, stats = _try_json_request(base_url, "/api/stats", timeout=4.0)
    if not ok:
        return False, stats, False, {
            "kind": "offline",
            "reason": "stats endpoint unreachable",
            "stats_error": stats,
        }
    healthy, warning = _validate_stats(
        stats,
        min_nodes=min_nodes,
        expected_engine_root=expected_engine_root,
        expected_graph_root=expected_graph_root,
    )
    if (
        healthy
        or not refresh_readiness
        or not isinstance(warning, dict)
        or warning.get("kind") != "production_not_ready"
    ):
        return True, stats, healthy, warning

    deep_ok, deep_result = _try_json_request(
        base_url,
        "/api/health/ready?deep=true",
        timeout=30.0,
    )
    if not deep_ok:
        return True, stats, False, {
            "kind": "deep_readiness_failed",
            "reason": "canonical deep readiness did not pass",
            "shallow_warning": warning,
            "deep_error": deep_result,
        }
    ok, stats = _try_json_request(base_url, "/api/stats", timeout=4.0)
    if not ok:
        return False, stats, False, {
            "kind": "offline_after_deep_readiness",
            "reason": "stats endpoint became unreachable after deep readiness",
        }
    healthy, warning = _validate_stats(
        stats,
        min_nodes=min_nodes,
        expected_engine_root=expected_engine_root,
        expected_graph_root=expected_graph_root,
    )
    return True, stats, healthy, warning


def _request_runtime_supervisor() -> tuple[bool, dict[str, Any]]:
    task_name = os.environ.get(
        "THREECAN_SUPERVISOR_TASK_NAME",
        DEFAULT_SUPERVISOR_TASK,
    ).strip()
    if os.name != "nt":
        return False, {
            "kind": "supervisor_unavailable",
            "reason": "automatic recovery requires an external service manager",
        }
    try:
        completed = subprocess.run(
            ["schtasks.exe", "/Run", "/TN", task_name],
            check=False,
            capture_output=True,
            text=True,
            timeout=10.0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, {
            "kind": "supervisor_request_failed",
            "reason": f"{type(exc).__name__}: {exc}"[:300],
            "task_name": task_name,
        }
    if completed.returncode != 0:
        return False, {
            "kind": "supervisor_request_failed",
            "returncode": completed.returncode,
            "task_name": task_name,
        }
    return True, {
        "kind": "supervisor_requested",
        "task_name": task_name,
    }




def doctor(args: argparse.Namespace) -> int:
    discovery = resolve_engine_root(args.engine_root)
    selected_engine_root = Path(discovery["selected"])
    selected_graph_root = _selected_graph_root(selected_engine_root)
    identity_gate = getattr(args, "project_identity", None) or _project_identity_gate(
        args.base_url,
        discovery,
        agent_id=_agent_id_from_args(args),
        command="doctor",
    )
    ok, stats, healthy, validation = _probe_stats(
        args.base_url,
        min_nodes=args.min_nodes,
        expected_engine_root=selected_engine_root,
        expected_graph_root=selected_graph_root,
        refresh_readiness=True,
    )
    if identity_gate.get("status") == "block":
        healthy = False
        validation = {"kind": "project_identity_gate_blocked", "gate": identity_gate}
    proxy_state = (
        _observe_proxy_slots(Path(discovery["selected"]), base_url=args.base_url)
        if discovery["valid_engine_root"]
        else None
    )
    report = {
        "base_url": args.base_url,
        "min_nodes": args.min_nodes,
        "selected_engine_root": discovery["selected"],
        "selected_source": discovery["source"],
        "selected_node_count": discovery["node_count"],
        "selected_valid_engine_root": discovery["valid_engine_root"],
        "proxy_state": proxy_state,
        "stats_online": ok,
        "stats": stats if ok else None,
        "healthy": healthy,
        "warning": validation,
        "project_identity": identity_gate,
        "candidates": discovery["candidates"],
    }
    _print_json(report)
    return 0 if healthy else 1


def ensure_online(
    base_url: str,
    *,
    engine_root_override: str | None,
    start_if_offline: bool,
    wait_seconds: float,
    min_nodes: int,
) -> dict[str, Any]:
    discovery = resolve_engine_root(engine_root_override)
    selected_engine_root = Path(discovery["selected"])
    selected_graph_root = _selected_graph_root(selected_engine_root)
    identity_gate = _project_identity_gate(
        base_url,
        discovery,
        command="ensure-online",
    )
    if identity_gate.get("status") == "block":
        return {
            "online": False,
            "started": False,
            "healthy": False,
            "code": "THREECAN_PROJECT_IDENTITY_BLOCKED",
            "error": {"kind": "project_identity_gate_blocked"},
            "project_identity": identity_gate,
            "engine_root": discovery,
        }

    ok, result, healthy, warning = _probe_stats(
        base_url,
        min_nodes=min_nodes,
        expected_engine_root=selected_engine_root,
        expected_graph_root=selected_graph_root,
        refresh_readiness=True,
    )
    if ok:
        return {
            "online": True,
            "started": False,
            "healthy": healthy,
            "code": (
                "THREECAN_ALREADY_HEALTHY"
                if healthy
                else "THREECAN_ONLINE_NOT_READY"
            ),
            "stats": result,
            "warning": warning,
            "proxy_state": _proxy_state(selected_engine_root),
            "project_identity": identity_gate,
            "engine_root": discovery,
        }

    if not start_if_offline:
        return {
            "online": False,
            "started": False,
            "healthy": False,
            "code": "THREECAN_OFFLINE",
            "error": result,
            "project_identity": identity_gate,
            "engine_root": discovery,
        }

    if not discovery["valid_engine_root"]:
        return {
            "online": False,
            "started": False,
            "healthy": False,
            "code": "THREECAN_ENGINE_ROOT_INVALID",
            "error": {"kind": "invalid_engine_root"},
            "project_identity": identity_gate,
            "engine_root": discovery,
        }

    requested, supervisor = _request_runtime_supervisor()
    if not requested:
        return {
            "online": False,
            "started": False,
            "healthy": False,
            "code": "THREECAN_SUPERVISOR_UNAVAILABLE",
            "error": result,
            "supervisor": supervisor,
            "project_identity": identity_gate,
            "engine_root": discovery,
        }

    deadline = time.monotonic() + max(0.2, wait_seconds)
    last_error: Any = result
    while time.monotonic() < deadline:
        ok, result, healthy, warning = _probe_stats(
            base_url,
            min_nodes=min_nodes,
            expected_engine_root=selected_engine_root,
            expected_graph_root=selected_graph_root,
            refresh_readiness=True,
        )
        if ok:
            return {
                "online": True,
                "started": True,
                "healthy": healthy,
                "code": (
                    "THREECAN_SUPERVISOR_RECOVERED"
                    if healthy
                    else "THREECAN_ONLINE_NOT_READY"
                ),
                "stats": result,
                "warning": warning,
                "supervisor": supervisor,
                "proxy_state": _proxy_state(selected_engine_root),
                "project_identity": identity_gate,
                "engine_root": discovery,
            }
        last_error = result
        time.sleep(0.5)

    return {
        "online": False,
        "started": True,
        "healthy": False,
        "code": "THREECAN_SUPERVISOR_TIMEOUT",
        "error": last_error,
        "supervisor": supervisor,
        "project_identity": identity_gate,
        "engine_root": discovery,
    }
def session_start(args: argparse.Namespace) -> int:
    status = ensure_online(
        args.base_url,
        engine_root_override=args.engine_root,
        start_if_offline=args.start_if_offline,
        wait_seconds=args.wait_seconds,
        min_nodes=args.min_nodes,
    )
    if not status["online"] or not status["healthy"]:
        _print_json(status)
        return 1

    session_id = _session_id_for_agent(args.agent_id, args.session_id or "")
    checkin_payload = {
        "agent_id": args.agent_id,
        "name": args.name,
        "role": args.role,
        "current_task": args.task,
        "capabilities": args.capabilities,
        "session_id": session_id,
    }
    project_meta = _current_project_metadata(base_url=args.base_url)
    if args.meta:
        checkin_payload["meta"] = json.loads(args.meta)
    if project_meta:
        meta = checkin_payload.setdefault("meta", {})
        if isinstance(meta, dict):
            meta.setdefault("project_identity", project_meta)

    ok, checkin = _try_json_request(
        args.base_url,
        "/api/agents/checkin",
        method="POST",
        payload=checkin_payload,
        timeout=60.0,
    )
    if not ok:
        _print_json({"status": status, "checkin_error": checkin})
        return 1

    briefing_path = f"/api/briefing?agent_id={args.agent_id}&role={args.role}&max_nodes={args.max_nodes}"
    ok, briefing = _try_json_request(args.base_url, briefing_path, timeout=30.0)
    if not ok:
        _print_json({"status": status, "checkin": checkin, "briefing_error": briefing})
        return 1

    result = {"status": status, "checkin": checkin, "briefing": briefing}
    _record_local_token_estimate(
        args.base_url,
        agent_id=args.agent_id,
        command="session-start",
        request_payload={
            "task": args.task,
            "checkin": checkin_payload,
            "briefing_path": briefing_path,
        },
        response_payload=result,
        session_id=session_id,
    )
    _print_json(result)
    return 0


def route(args: argparse.Namespace) -> int:
    payload = {
        "task": args.task,
        "max_nodes": args.max_nodes,
        "agent_id": args.agent_id,
        "mode": args.mode,
        "confirm_low_confidence": args.confirm_low_confidence,
        "allow_degraded": args.allow_degraded,
    }
    if args.budget_tokens is not None:
        payload["budget_tokens"] = args.budget_tokens
    ok, response = _try_json_request(
        args.base_url,
        "/api/route",
        method="POST",
        payload=payload,
        timeout=args.timeout_seconds,
    )
    _record_local_token_estimate(
        args.base_url,
        agent_id=args.agent_id,
        command="route",
        request_payload=payload,
        response_payload=response,
        session_id=getattr(args, "session_id", ""),
    )
    if ok:
        _record_route_state(args.base_url, agent_id=args.agent_id, task=args.task, response_payload=response)
    _print_json(response)
    return 0 if ok else 1


def ticket(args: argparse.Namespace) -> int:
    payload = {
        "agent_id": args.agent_id,
        "task_description": args.task_description,
        "target_files": args.target_files,
        "scope_keywords": args.scope_keywords,
        "task_type": args.task_type,
    }
    ok, response = _try_json_request(
        args.base_url,
        "/api/route/ticket",
        method="POST",
        payload=payload,
        timeout=60.0,
    )
    route_ticket_id = response.get("ticket_id", "") if isinstance(response, dict) else ""
    _record_local_token_estimate(
        args.base_url,
        agent_id=args.agent_id,
        command="ticket",
        request_payload=payload,
        response_payload=response,
        route_ticket_id=route_ticket_id,
    )
    _print_json(response)
    return 0 if ok else 1


def prepare(args: argparse.Namespace) -> int:
    """Compatibility alias for AGENTS.md pseudo-hook examples.

    Maps to ticket + ticket-consume so older prepare guidance still produces a
    live route ticket and records the intended mutating tool before edits.
    """
    ticket_payload = {
        "agent_id": args.agent_id,
        "task_description": args.task_description,
        "target_files": args.target_files,
        "scope_keywords": args.scope_keywords,
        "task_type": args.task_type,
    }
    preflight = build_memory_preflight(
        args.base_url,
        agent_id=args.agent_id,
        task_description=args.task_description,
        target_files=args.target_files,
        scope_keywords=args.scope_keywords,
        tool_name=args.tool_name,
        tool_input_summary=args.tool_input_summary,
    )
    ticket_ok, ticket_response = _try_json_request(
        args.base_url,
        "/api/route/ticket",
        method="POST",
        payload=ticket_payload,
        timeout=60.0,
    )
    result: dict[str, Any] = {
        "alias": "prepare",
        "maps_to": ["ticket", "ticket-consume"],
        "ticket": ticket_response,
        "memory_preflight": preflight,
    }
    if not ticket_ok:
        _print_json(result)
        return 1

    ticket_id = ticket_response.get("ticket_id") if isinstance(ticket_response, dict) else None
    if not ticket_id:
        result["error"] = "route ticket response did not include ticket_id"
        _print_json(result)
        return 1

    try:
        consume_payload = _ticket_consume_payload(
            ticket_response,
            agent_id=args.agent_id,
            tool_name=args.tool_name,
            tool_input_summary=args.tool_input_summary,
        )
    except ValueError as exc:
        result["error"] = {
            "kind": str(exc),
            "message": "Route ticket is missing the immutable consume binding.",
        }
        _print_json(result)
        return 1
    consume_ok, consume_response = _try_json_request(
        args.base_url,
        f"/api/route/ticket/{ticket_id}/consume",
        method="POST",
        payload=consume_payload,
        timeout=30.0,
    )
    result["consume"] = consume_response
    if consume_ok:
        lifecycle_path = _record_error_disposition_ticket(
            ticket_response,
            agent_id=args.agent_id,
            base_url=args.base_url,
        )
        if lifecycle_path:
            result["error_disposition_state_path"] = lifecycle_path
    _record_local_token_estimate(
        args.base_url,
        agent_id=args.agent_id,
        command="prepare",
        request_payload={
            "ticket": ticket_payload,
            "consume": consume_payload,
        },
        response_payload=result,
        route_ticket_id=str(ticket_id),
    )
    _print_json(result)
    return 0 if consume_ok else 1


def supervise(args: argparse.Namespace) -> int:
    """Unified Codex pre-hook supervisor.

    This does not replace prepare for mutations; it makes the route, memory,
    runtime, ticket, and token gates visible in one machine-readable payload.
    """
    gates: list[dict[str, Any]] = []
    discovery = resolve_engine_root(args.engine_root)
    selected_engine_root = Path(discovery["selected"])
    selected_graph_root = _selected_graph_root(selected_engine_root)

    ok_stats, stats = _try_json_request(args.base_url, "/api/stats", timeout=12.0)
    runtime_gate: dict[str, Any] = {
        "name": "runtime",
        "status": "pass" if ok_stats else "block",
        "base_url": args.base_url,
        "stats": stats if ok_stats else None,
    }
    if ok_stats:
        healthy, warning = _validate_stats(
            stats,
            min_nodes=args.min_nodes,
            expected_engine_root=selected_engine_root,
            expected_graph_root=selected_graph_root,
        )
        runtime_gate["status"] = "pass" if healthy else "block"
        runtime_gate["warning"] = warning
    else:
        runtime_gate["error"] = stats
    gates.append(runtime_gate)

    route_payload = {
        "task": args.task_description,
        "max_nodes": args.max_nodes,
        "include_edges": True,
        "agent_id": args.agent_id,
        "mode": args.mode,
        "budget_tokens": args.budget_tokens,
        "confirm_low_confidence": True,
        "allow_degraded": True,
    }
    route_ok, route_response = _try_json_request(
        args.base_url,
        "/api/route",
        method="POST",
        payload=route_payload,
        timeout=args.timeout_seconds,
    )
    route_gate: dict[str, Any] = {
        "name": "route",
        "status": "pass" if route_ok else "block",
    }
    if route_ok and isinstance(route_response, dict):
        confidence = route_response.get("confidence")
        route_gate["confidence"] = confidence
        route_gate["node_ids"] = [
            str(item.get("id"))
            for item in route_response.get("nodes", [])
            if isinstance(item, dict) and item.get("id")
        ]
        route_gate["route_meta"] = route_response.get("route_meta", {})
        if confidence == "low":
            route_gate["status"] = "warn"
            route_gate["warning"] = "low_confidence_route"
        if runtime_gate.get("status") == "block":
            try:
                route_total_nodes = int(route_response.get("total_nodes") or 0)
            except (TypeError, ValueError):
                route_total_nodes = 0
            if route_total_nodes >= args.min_nodes:
                runtime_gate["status"] = "warn"
                runtime_gate["warning"] = "stats_unavailable_but_route_verified_graph"
                runtime_gate["route_total_nodes"] = route_total_nodes
        _record_route_state(args.base_url, agent_id=args.agent_id, task=args.task_description, response_payload=route_response)
    else:
        route_gate["error"] = route_response
    gates.append(route_gate)

    memory_preflight = build_memory_preflight(
        args.base_url,
        agent_id=args.agent_id,
        task_description=args.task_description,
        target_files=args.target_files,
        scope_keywords=args.scope_keywords,
        tool_name=args.tool_name or "",
        tool_input_summary=args.tool_input_summary or "",
    )
    raw_memory_status = str(memory_preflight.get("status") or "").strip().casefold()
    memory_gate = {
        "name": "memory_preflight",
        # Preserve advisory warnings. Only an explicit block (or malformed
        # unknown status) may stop a supervised mutation.
        "status": (
            raw_memory_status
            if raw_memory_status in {"pass", "warn", "block"}
            else "block"
        ),
        "memory_quality": memory_preflight.get("memory_quality"),
        "policy_hits": memory_preflight.get("policy_hits", []),
        "must_do_next": memory_preflight.get("must_do_next", []),
        "must_not_do": memory_preflight.get("must_not_do", []),
    }
    gates.append(memory_gate)

    ticket_gate: dict[str, Any] = {"name": "ticket", "status": "not_required"}
    ticket_response: Any = None
    consume_response: Any = None
    ticket_id = args.ticket_id or ""
    requires_ticket = bool(args.target_files or args.tool_input_summary)
    ticket_mode = "not_required"
    if requires_ticket and not args.tool_input_summary:
        ticket_gate = {
            "name": "ticket",
            "status": "block",
            "error": "tool_input_summary_required_for_mutation_supervision",
        }
        ticket_mode = "blocked_missing_tool_summary"
    elif requires_ticket and args.skip_ticket:
        ticket_gate = {
            "name": "ticket",
            "status": "skipped",
            "reason": "ticket_deferred_to_prepare",
        }
        ticket_mode = "skipped"
    elif requires_ticket:
        if ticket_id:
            ticket_ok, ticket_response = _try_json_request(
                args.base_url,
                f"/api/route/ticket/{ticket_id}",
                timeout=60.0,
            )
        else:
            ticket_payload = {
                "agent_id": args.agent_id,
                "task_description": args.task_description,
                "target_files": args.target_files,
                "scope_keywords": args.scope_keywords,
                "task_type": args.task_type,
            }
            ticket_ok, ticket_response = _try_json_request(
                args.base_url,
                "/api/route/ticket",
                method="POST",
                payload=ticket_payload,
                timeout=60.0,
            )
            ticket_id = ticket_response.get("ticket_id", "") if isinstance(ticket_response, dict) else ""
        if not ticket_ok or not ticket_id:
            ticket_gate = {
                "name": "ticket",
                "status": "block",
                "error": ticket_response,
            }
            ticket_mode = "blocked_issue_or_fetch"
        else:
            scope_error = _ticket_scope_validation(
                ticket_response,
                expected_scope_text=args.task_description,
                expected_target_files=args.target_files,
            ) if isinstance(ticket_response, dict) else {"kind": "ticket_payload_invalid"}
            ticket_gate = {
                "name": "ticket",
                "status": "pass" if not scope_error else "block",
                "ticket_id": ticket_id,
                "ticket": ticket_response,
            }
            if scope_error:
                ticket_gate["error"] = scope_error
                ticket_mode = "blocked_scope"
            elif not args.no_consume_ticket:
                try:
                    consume_payload = _ticket_consume_payload(
                        ticket_response,
                        agent_id=args.agent_id,
                        tool_name=args.tool_name,
                        tool_input_summary=args.tool_input_summary,
                    )
                except ValueError as exc:
                    ticket_gate["status"] = "block"
                    ticket_gate["error"] = {
                        "kind": str(exc),
                        "message": "Route ticket is missing the immutable consume binding.",
                    }
                    ticket_mode = "blocked_consume_binding"
                    consume_payload = None
                if consume_payload is None:
                    consume_ok = False
                    consume_response = ticket_gate["error"]
                else:
                    consume_ok, consume_response = _try_json_request(
                        args.base_url,
                        f"/api/route/ticket/{ticket_id}/consume",
                        method="POST",
                        payload=consume_payload,
                        timeout=30.0,
                    )
                ticket_gate["consume"] = consume_response
                if not consume_ok:
                    ticket_gate["status"] = "block"
                    ticket_gate["error"] = consume_response
                    ticket_mode = "blocked_consume"
                else:
                    ticket_mode = "consumed"
                    lifecycle_path = _record_error_disposition_ticket(
                        ticket_response,
                        agent_id=args.agent_id,
                        base_url=args.base_url,
                    )
                    if lifecycle_path:
                        ticket_gate[
                            "error_disposition_state_path"
                        ] = lifecycle_path
            else:
                ticket_mode = "issued_unconsumed"
    gates.append(ticket_gate)

    token_gate: dict[str, Any] = {"name": "token_policy", "status": "pass"}
    if route_ok and isinstance(route_response, dict):
        estimate = route_response.get("route_token_estimate") or {}
        token_gate["route_token_estimate"] = estimate
        if route_response.get("budget_truncated"):
            token_gate["status"] = "warn"
            token_gate["warning"] = "route_pack_budget_truncated"
    gates.append(token_gate)

    blocked = [gate for gate in gates if gate.get("status") == "block"]
    warned = [gate for gate in gates if gate.get("status") == "warn"]
    result = {
        "ok": not blocked,
        "command": "supervise",
        "agent_id": args.agent_id,
        "task_description": args.task_description,
        "target_files": args.target_files,
        "scope_keywords": args.scope_keywords,
        "supervision_status": "block" if blocked else ("warn" if warned else "pass"),
        "gates": gates,
        "route": route_response if route_ok else None,
        "memory_preflight": memory_preflight,
        "ticket_id": ticket_id or None,
        "ticket_mode": ticket_mode,
        "next_required_actions": [
            "Resolve blocked gates before mutation.",
        ] if blocked else [
            "Proceed only within the supervised scope.",
            "Run codex-3can done after the mutation with concrete evidence.",
        ],
    }
    state_path = _record_supervise_state(
        args.base_url,
        agent_id=args.agent_id,
        result_payload=result,
        ticket_mode=ticket_mode,
    )
    if state_path:
        result["supervision_state_path"] = state_path
    _record_local_token_estimate(
        args.base_url,
        agent_id=args.agent_id,
        command="supervise",
        request_payload={
            "route": route_payload,
            "target_files": args.target_files,
            "scope_keywords": args.scope_keywords,
            "tool_name": args.tool_name,
            "tool_input_summary": args.tool_input_summary,
        },
        response_payload=result,
        route_ticket_id=ticket_id,
    )
    _print_json(result)
    return 0 if not blocked else 1


def supervise_status(args: argparse.Namespace) -> int:
    state = _load_supervise_state(
        args.agent_id,
        base_url=args.base_url,
        expected_scope_text=args.expect_scope_text or "",
        expected_target_files=args.expect_target_files or [],
    )
    result: dict[str, Any] = {
        "agent_id": args.agent_id,
        "valid": False,
        "ttl_sec": args.ttl_sec,
        "state": state,
        "selection": state.get("_selection") if isinstance(state, dict) else None,
    }
    if not state:
        result["error"] = {"kind": "missing_supervise_state"}
        result["next_required_actions"] = _supervise_status_next_actions(
            result["error"],
            agent_id=args.agent_id,
            expected_scope_text=args.expect_scope_text or "",
            expected_target_files=args.expect_target_files or [],
        )
        _print_json(result)
        return 1

    try:
        recorded_at = datetime.fromisoformat(str(state.get("recorded_at") or ""))
        age_sec = max(0, int((datetime.now(timezone.utc) - recorded_at).total_seconds()))
    except Exception:
        result["error"] = {"kind": "invalid_recorded_at", "recorded_at": state.get("recorded_at")}
        result["next_required_actions"] = _supervise_status_next_actions(
            result["error"],
            agent_id=args.agent_id,
            expected_scope_text=args.expect_scope_text or "",
            expected_target_files=args.expect_target_files or [],
        )
        _print_json(result)
        return 1
    result["age_sec"] = age_sec

    if state.get("base_url") and state.get("base_url") != args.base_url:
        result["error"] = {
            "kind": "base_url_mismatch",
            "expected_base_url": args.base_url,
            "actual_base_url": state.get("base_url"),
        }
        result["next_required_actions"] = _supervise_status_next_actions(
            result["error"],
            agent_id=args.agent_id,
            expected_scope_text=args.expect_scope_text or "",
            expected_target_files=args.expect_target_files or [],
        )
        _print_json(result)
        return 1
    if age_sec > args.ttl_sec:
        result["error"] = {"kind": "supervise_state_stale", "age_sec": age_sec, "ttl_sec": args.ttl_sec}
        result["next_required_actions"] = _supervise_status_next_actions(
            result["error"],
            agent_id=args.agent_id,
            expected_scope_text=args.expect_scope_text or "",
            expected_target_files=args.expect_target_files or [],
        )
        _print_json(result)
        return 1

    supervision_status = str(state.get("supervision_status") or "")
    if supervision_status == "block":
        result["error"] = {"kind": "supervision_blocked", "gate_statuses": state.get("gate_statuses") or {}}
        result["next_required_actions"] = _supervise_status_next_actions(
            result["error"],
            agent_id=args.agent_id,
            expected_scope_text=args.expect_scope_text or "",
            expected_target_files=args.expect_target_files or [],
        )
        _print_json(result)
        return 1
    if args.require_pass and supervision_status != "pass":
        result["error"] = {"kind": "supervision_not_pass", "supervision_status": supervision_status}
        result["next_required_actions"] = _supervise_status_next_actions(
            result["error"],
            agent_id=args.agent_id,
            expected_scope_text=args.expect_scope_text or "",
            expected_target_files=args.expect_target_files or [],
        )
        _print_json(result)
        return 1

    scope_error = _ticket_scope_validation(
        {
            "task_description": state.get("task_description") or "",
            "scope": {
                "target_files": state.get("target_files") or [],
                "scope_keywords": state.get("scope_keywords") or [],
            },
        },
        expected_scope_text=args.expect_scope_text or "",
        expected_target_files=args.expect_target_files or [],
    )
    if scope_error:
        result["error"] = scope_error
        result["next_required_actions"] = _supervise_status_next_actions(
            result["error"],
            agent_id=args.agent_id,
            expected_scope_text=args.expect_scope_text or "",
            expected_target_files=args.expect_target_files or [],
        )
        _print_json(result)
        return 1

    result["valid"] = True
    _print_json(result)
    return 0


def activity_log(args: argparse.Namespace) -> int:
    payload: dict[str, Any] = {
        "agent_id": args.agent_id,
        "action": args.action,
        "detail": args.detail,
        "affected_nodes": args.affected_nodes,
        "meta": json.loads(args.meta) if args.meta else {},
    }
    payload["meta"].setdefault("project_identity", _current_project_metadata(base_url=args.base_url))
    if args.ticket_id:
        payload["ticket_id"] = args.ticket_id
    ok, response = _try_json_request(
        args.base_url,
        "/api/activity/log",
        method="POST",
        payload=payload,
        timeout=12.0,
    )
    _record_local_token_estimate(
        args.base_url,
        agent_id=args.agent_id,
        command=payload["action"] or "activity-log",
        request_payload=payload,
        response_payload=response,
        route_ticket_id=str(payload.get("ticket_id") or ""),
    )
    _print_json(response)
    return 0 if ok else 1


def _mark_local_error_cases_resolved(
    node_ids: list[str],
    *,
    root_cause: str,
    solution_summary: str,
    verification_evidence: list[dict[str, Any]],
    fixed_in: str,
    resolved_by: str,
) -> list[str]:
    if not node_ids:
        return []
    selected = {str(item).strip() for item in node_ids if str(item).strip()}
    updated: list[str] = []
    with _loop_signature_transaction() as state:
        signatures = state.get("signatures")
        if not isinstance(signatures, dict):
            raise LoopSignatureStoreError("BLOCKED: signatures map is not an object")
        now = datetime.now(timezone.utc).isoformat()
        for entry in signatures.values():
            if not isinstance(entry, dict) or str(entry.get("node_id") or "") not in selected:
                continue
            entry["case_status"] = "resolved"
            entry["resolved_at"] = now
            entry["resolved_by"] = resolved_by
            entry["root_cause"] = root_cause
            entry["solution_summary"] = solution_summary
            entry["verification_evidence"] = list(verification_evidence)
            entry["fixed_in"] = fixed_in
            updated.append(str(entry.get("node_id")))
    return updated


def _parse_verification_evidence(values: list[Any] | None) -> list[dict[str, Any]]:
    """Parse repeatable CLI JSON objects without corrupting commas in receipts."""

    parsed_receipts: list[dict[str, Any]] = []
    for index, raw in enumerate(values or []):
        if isinstance(raw, dict):
            parsed: Any = raw
        elif isinstance(raw, list):
            parsed = raw
        else:
            text = str(raw or "").strip()
            if not text:
                continue
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"verification_evidence_json_invalid_at_{index}: {exc.msg}"
                ) from exc
        pending = [parsed]
        while pending:
            candidate = pending.pop(0)
            if isinstance(candidate, list):
                pending[0:0] = list(candidate)
                continue
            if not isinstance(candidate, dict) or not candidate:
                raise ValueError(
                    f"verification_evidence_object_required_at_{index}"
                )
            parsed_receipts.append(dict(candidate))
    return parsed_receipts


def _parse_error_dispositions(
    values: list[Any] | None,
) -> list[dict[str, str]]:
    parsed_dispositions: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, raw in enumerate(values or []):
        if isinstance(raw, dict):
            parsed: Any = raw
        else:
            text = str(raw or "").strip()
            if not text:
                continue
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"error_disposition_json_invalid_at_{index}: {exc.msg}"
                ) from exc
        candidates = parsed if isinstance(parsed, list) else [parsed]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                raise ValueError(
                    f"error_disposition_object_required_at_{index}"
                )
            error_id = str(candidate.get("error_id") or "").strip()
            disposition = str(
                candidate.get("disposition") or ""
            ).strip().casefold()
            reason = str(candidate.get("reason") or "").strip()
            if not error_id or not disposition:
                raise ValueError(
                    f"error_disposition_fields_required_at_{index}"
                )
            if error_id in seen:
                raise ValueError(
                    f"error_disposition_duplicate_for_{error_id}"
                )
            parsed_dispositions.append(
                {
                    "error_id": error_id,
                    "disposition": disposition,
                    "reason": reason,
                }
            )
            seen.add(error_id)
    return parsed_dispositions


def _actual_resolved_error_ids(response: Any) -> list[str]:
    if not isinstance(response, dict):
        return []
    if str(response.get("resolution_outcome") or "").casefold() != "resolved":
        return []
    values = response.get("resolved_errors")
    if not isinstance(values, list):
        return []
    resolved: list[str] = []
    for item in values:
        if not isinstance(item, dict):
            continue
        if (
            str(item.get("case_status") or "").casefold() != "resolved"
            or not str(item.get("resolution_id") or "").strip()
        ):
            continue
        node_id = str(item.get("error_id") or "").strip()
        if node_id and node_id not in resolved:
            resolved.append(node_id)
    return resolved


def _actual_review_required_error_ids(response: Any) -> list[str]:
    if not isinstance(response, dict):
        return []
    values = response.get("resolved_errors")
    if not isinstance(values, list):
        return []
    return list(dict.fromkeys(
        str(item.get("error_id") or "").strip()
        for item in values
        if isinstance(item, dict)
        and str(item.get("case_status") or "").casefold() == "review_required"
        and str(item.get("error_id") or "").strip()
    ))


def _mark_local_error_cases_review_required(
    node_ids: list[str],
    *,
    reviewed_by: str,
) -> list[str]:
    selected = {str(item).strip() for item in node_ids if str(item).strip()}
    if not selected:
        return []
    updated: list[str] = []
    with _loop_signature_transaction() as state:
        signatures = state.get("signatures")
        if not isinstance(signatures, dict):
            raise LoopSignatureStoreError("signatures map is not an object")
        now = datetime.now(timezone.utc).isoformat()
        for entry in signatures.values():
            if not isinstance(entry, dict):
                continue
            node_id = str(entry.get("node_id") or "")
            if node_id not in selected:
                continue
            entry["case_status"] = "review_required"
            entry["review_required_at"] = now
            entry["reviewed_by"] = reviewed_by
            updated.append(node_id)
    return updated


def done(args: argparse.Namespace) -> int:
    """Complete a consumed ticket and optionally close verified ErrorCases."""
    ticket_id = str(getattr(args, "ticket_id", "") or "").strip()
    if not ticket_id:
        _print_json(
            {
                "ok": False,
                "error": {
                    "kind": "ticket_id_required",
                    "message": (
                        "done requires explicit --ticket-id; shared wrapper "
                        "state is never inferred."
                    ),
                },
            }
        )
        return 2
    resolved_errors = [
        str(item).strip()
        for item in getattr(args, "resolved_errors", []) or []
        if str(item).strip()
    ]
    try:
        verification_evidence = _parse_verification_evidence(
            getattr(args, "verification_evidence", []) or []
        )
        error_dispositions = _parse_error_dispositions(
            getattr(args, "error_dispositions", []) or []
        )
    except ValueError as exc:
        _print_json(
            {
                "ok": False,
                "error": {
                    "kind": "completion_evidence_or_disposition_invalid",
                    "message": str(exc),
                },
            }
        )
        return 2
    solution_summary = str(getattr(args, "solution_summary", "") or "").strip()
    root_cause = str(getattr(args, "root_cause", "") or "").strip()
    fixed_in = str(getattr(args, "fixed_in", "") or "").strip()
    if resolved_errors and (not solution_summary or not verification_evidence):
        _print_json(
            {
                "ok": False,
                "error": {
                    "kind": "resolution_evidence_required",
                    "message": (
                        "--resolved-error requires --solution-summary and at least one "
                        "--verification-evidence receipt."
                    ),
                },
            }
        )
        return 2

    meta = json.loads(args.meta) if args.meta else {}
    meta.setdefault("project_identity", _current_project_metadata(base_url=args.base_url))
    payload: dict[str, Any] = {
        "agent_id": args.agent_id,
        "action": "done",
        "detail": args.detail,
        "affected_nodes": args.affected_nodes,
        "meta": meta,
        "resolved_errors": resolved_errors,
        "error_dispositions": error_dispositions,
        "root_cause": root_cause,
        "solution_summary": solution_summary,
        "verification_evidence": verification_evidence,
        "fixed_in": fixed_in,
    }
    payload["ticket_id"] = ticket_id
    ok, response = _try_json_request(
        args.base_url,
        "/api/activity/done",
        method="POST",
        payload=payload,
        timeout=20.0,
    )
    actual_resolved = _actual_resolved_error_ids(response) if ok else []
    actual_review_required = (
        _actual_review_required_error_ids(response) if ok else []
    )
    local_resolved: list[str] = []
    local_review_required: list[str] = []
    local_state_error = ""
    if actual_resolved or actual_review_required:
        try:
            if actual_resolved:
                local_resolved = _mark_local_error_cases_resolved(
                    actual_resolved,
                    root_cause=root_cause,
                    solution_summary=solution_summary,
                    verification_evidence=verification_evidence,
                    fixed_in=fixed_in,
                    resolved_by=args.agent_id,
                )
            if actual_review_required:
                local_review_required = _mark_local_error_cases_review_required(
                    actual_review_required,
                    reviewed_by=args.agent_id,
                )
        except LoopSignatureStoreError as exc:
            local_state_error = str(exc)
    lifecycle_state_path = (
        _complete_error_disposition_ticket(
            ticket_id,
            response=response,
        )
        if ok
        else None
    )
    _record_local_token_estimate(
        args.base_url,
        agent_id=args.agent_id,
        command="done",
        request_payload=payload,
        response_payload=response,
        route_ticket_id=str(payload.get("ticket_id") or ""),
    )
    result = {
        "ok": ok and not local_state_error,
        "status": "PARTIAL" if ok and local_state_error else ("complete" if ok else "failed"),
        "response": response,
        "requested_resolved_errors": resolved_errors,
        "resolved_errors": actual_resolved,
        "review_required_errors": actual_review_required,
        "error_dispositions": (
            response.get("error_dispositions", [])
            if ok and isinstance(response, dict)
            else []
        ),
        "local_resolution_state_updated": local_resolved,
        "local_review_required_state_updated": local_review_required,
    }
    if lifecycle_state_path:
        result["error_disposition_state_path"] = lifecycle_state_path
    if local_state_error:
        result["local_resolution_state_error"] = local_state_error
    _print_json(result)
    return 0 if ok and not local_state_error else 1


def ticket_status(args: argparse.Namespace) -> int:
    ok, response = _try_json_request(
        args.base_url,
        f"/api/route/ticket/{args.ticket_id}",
        timeout=60.0,
    )
    result: dict[str, Any] = {
        "ticket_id": args.ticket_id,
        "valid": False,
    }
    if ok:
        issued_at_raw = str(response.get("issued_at") or "")
        ttl_sec = int(response.get("ttl_sec") or 0)
        expires_at = None
        remaining_ttl_sec = None
        try:
            issued_at = datetime.fromisoformat(issued_at_raw)
            expires_at_dt = issued_at + timedelta(seconds=ttl_sec)
            now = datetime.now(timezone.utc)
            remaining_ttl_sec = max(0, int((expires_at_dt - now).total_seconds()))
            expires_at = expires_at_dt.isoformat()
        except Exception:
            pass
        result["valid"] = True
        result["ticket"] = response
        result["expires_at"] = expires_at
        result["remaining_ttl_sec"] = remaining_ttl_sec
        if remaining_ttl_sec is not None and remaining_ttl_sec < args.min_remaining_ttl_sec:
            result["valid"] = False
            result["error"] = {
                "kind": "ticket_ttl_too_low",
                "remaining_ttl_sec": remaining_ttl_sec,
                "min_remaining_ttl_sec": args.min_remaining_ttl_sec,
            }
        if args.expect_agent_id and response.get("agent_id") != args.expect_agent_id:
            result["valid"] = False
            result["error"] = {
                "kind": "agent_mismatch",
                "expected_agent_id": args.expect_agent_id,
                "actual_agent_id": response.get("agent_id"),
            }
        scope_error = _ticket_scope_validation(
            response,
            expected_scope_text=args.expect_scope_text or "",
            expected_target_files=args.expect_target_files or [],
        )
        if scope_error:
            result["valid"] = False
            result["error"] = scope_error
    else:
        result["error"] = response
    _print_json(result)
    return 0 if result["valid"] else 1


def route_freshness(args: argparse.Namespace) -> int:
    state = _load_route_state(args.agent_id)
    result: dict[str, Any] = {
        "agent_id": args.agent_id,
        "valid": False,
        "ttl_sec": args.ttl_sec,
        "state": state,
    }
    if not state:
        result["error"] = {"kind": "missing_route_state"}
        _print_json(result)
        return 1
    try:
        recorded_at = datetime.fromisoformat(str(state.get("recorded_at") or ""))
        age_sec = max(0, int((datetime.now(timezone.utc) - recorded_at).total_seconds()))
    except Exception:
        result["error"] = {"kind": "invalid_recorded_at", "recorded_at": state.get("recorded_at")}
        _print_json(result)
        return 1
    result["age_sec"] = age_sec
    if state.get("base_url") and state.get("base_url") != args.base_url:
        result["error"] = {
            "kind": "base_url_mismatch",
            "expected_base_url": args.base_url,
            "actual_base_url": state.get("base_url"),
        }
        _print_json(result)
        return 1
    if age_sec > args.ttl_sec:
        result["error"] = {"kind": "route_state_stale", "age_sec": age_sec, "ttl_sec": args.ttl_sec}
        _print_json(result)
        return 1
    if args.expect_scope_text:
        expected = _significant_scope_tokens(args.expect_scope_text)
        actual = _significant_scope_tokens(str(state.get("task") or ""))
        if expected and actual and not (expected & actual):
            result["error"] = {
                "kind": "route_scope_mismatch",
                "expected_scope_text": args.expect_scope_text[:240],
                "route_task": state.get("task"),
            }
            _print_json(result)
            return 1
    result["valid"] = True
    _print_json(result)
    return 0


def ticket_consume(args: argparse.Namespace) -> int:
    ticket_ok, ticket_response = _try_json_request(
        args.base_url,
        f"/api/route/ticket/{args.ticket_id}",
        timeout=30.0,
    )
    if not ticket_ok:
        _print_json(
            {
                "ticket_id": args.ticket_id,
                "ok": False,
                "error": ticket_response,
            }
        )
        return 1
    try:
        payload = _ticket_consume_payload(
            ticket_response,
            agent_id=args.agent_id,
            tool_name=args.tool_name,
            tool_input_summary=args.tool_input_summary,
        )
    except ValueError as exc:
        _print_json(
            {
                "ticket_id": args.ticket_id,
                "ok": False,
                "error": {"kind": str(exc)},
            }
        )
        return 1
    ok, response = _try_json_request(
        args.base_url,
        f"/api/route/ticket/{args.ticket_id}/consume",
        method="POST",
        payload=payload,
        timeout=30.0,
    )
    result = {
        "ticket_id": args.ticket_id,
        "tool_name": args.tool_name,
        "tool_input_summary": args.tool_input_summary,
        "response": response,
    }
    agent_id = (
        response.get("agent_id", "")
        if isinstance(response, dict)
        else ""
    ) or args.agent_id
    _record_local_token_estimate(
        args.base_url,
        agent_id=agent_id,
        command="ticket-consume",
        request_payload={"ticket_id": args.ticket_id, **payload},
        response_payload=response,
        route_ticket_id=args.ticket_id,
    )
    if ok:
        lifecycle_path = _record_error_disposition_ticket(
            ticket_response,
            agent_id=agent_id,
            base_url=args.base_url,
        )
        if lifecycle_path:
            result["error_disposition_state_path"] = lifecycle_path
    _print_json(result)
    return 0 if ok else 1


def _slugify(text: str, max_parts: int = 6) -> list[str]:
    parts = re.findall(r"[a-zA-Z0-9]+", text.lower())
    if parts:
        return parts[:max_parts]
    fallback = re.findall(r"[\u4e00-\u9fff]+", text)
    return [item[:12] for item in fallback[:max_parts]]


_SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|passwd|secret|authorization|cookie)"
    r"\s*[:=]\s*(?:bearer\s+)?[^\s,;]+"
)
_BEARER_SECRET_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")
_WINDOWS_USER_HOME_RE = re.compile(
    r"(?i)\b[A-Z]:[\\/](?:Users|Documents and Settings)[\\/][^\\/]+"
)
_UNC_USER_HOME_RE = re.compile(
    r"\\\\[^\\/\r\n]+[\\/][^\\/\r\n]+[\\/][^\\/\r\n]+"
)
_POSIX_USER_HOME_RE = re.compile(r"(?i)(?:^|[\s\"'])(?:/home|/users)/[^/\s\"']+")


def _redact_sensitive_text(text: str) -> str:
    value = str(text or "")
    value = _SENSITIVE_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=<redacted>", value)
    value = _BEARER_SECRET_RE.sub("Bearer <redacted>", value)
    value = _WINDOWS_USER_HOME_RE.sub("<user-home>", value)
    value = _UNC_USER_HOME_RE.sub("<unc-user-home>", value)
    value = _POSIX_USER_HOME_RE.sub(" <user-home>", value)
    return value


def _compact_current_state(text: str, max_chars: int = 360) -> str:
    compacted = re.sub(r"\s+", " ", _redact_sensitive_text(text)).strip()
    if len(compacted) <= max_chars:
        return compacted
    return compacted[: max_chars - 3].rstrip() + "..."


def _compact_note_payload(args: argparse.Namespace) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    date_part = now.strftime("%Y%m%d")
    time_part = now.strftime("%H%M%S")
    title = args.title or args.task_summary or "Codex continuation"
    slug_parts = _slugify(title, max_parts=4) or ["continuation"]
    node_id = args.node_id or f"SES-{date_part}-{args.agent_id}-{time_part}"
    current_state = _compact_current_state(args.task_summary or title)

    note_lines: list[str] = [f"Task: {title}"]
    if args.task_summary:
        note_lines.append(f"Summary: {args.task_summary}")
    if args.next_steps:
        note_lines.append("Next Steps:")
        note_lines.extend([f"- {item}" for item in args.next_steps])
    if args.blockers:
        note_lines.append("Blockers:")
        note_lines.extend([f"- {item}" for item in args.blockers])
    if args.files:
        note_lines.append("Files:")
        note_lines.extend([f"- {item}" for item in args.files])
    if args.related_nodes:
        note_lines.append("Related Nodes:")
        note_lines.extend([f"- {item}" for item in args.related_nodes])

    activation_keywords = [
        "codex-cli",
        "compact",
        "continuation",
        args.agent_id,
        *slug_parts,
    ]
    seen: set[str] = set()
    deduped_keywords: list[str] = []
    for item in activation_keywords:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped_keywords.append(item)

    payload = {
        "id": node_id,
        "name": title,
        "cluster": "\u4f1a\u8bdd\u8bb0\u5f55",
        "type": "session",
        "status": "active",
        "content": {
            "description": f"Codex compact continuation note for: {title}",
            "current_state": current_state,
            "key_files": args.files,
            "notes": "\n".join(note_lines),
            "extra": {
                "project_identity": _current_project_metadata(base_url=getattr(args, "base_url", "")),
            },
        },
        "activation_keywords": deduped_keywords,
        "primary_author": args.agent_id,
        "priority": "high",
    }
    return payload


def _public_target_ref(value: str) -> str:
    """Return a useful component-local path without disclosing a host path."""

    normalized = str(value or "").strip().replace("\\", "/")
    normalized = re.sub(r"/+", "/", normalized)
    is_unc = str(value or "").strip().startswith(("\\\\", "//"))
    parts = [
        part
        for part in normalized.split("/")
        if part and not re.fullmatch(r"[A-Za-z]:", part)
    ]
    lowered = [part.casefold() for part in parts]
    for marker in ("users", "home", "documents and settings"):
        if marker in lowered:
            index = lowered.index(marker)
            parts = parts[index + 2:]
            break
    else:
        if is_unc:
            # UNC host/share/user are deployment-private. Retain only the
            # component-local tail.
            parts = parts[3:]
    normalized = re.sub(r"^~/?", "", normalized)
    return "/".join(parts[-2:]) if parts else ""


def _occurrence_outbox_path(occurrence_id: str) -> Path:
    return PENDING_WRITEBACK_DIR / f"error-occurrence-{_safe_id_part(occurrence_id)}.json"


def _write_occurrence_outbox(
    *,
    occurrence_payload: dict[str, Any],
    response: Any,
    reason: str,
) -> Path:
    """Persist the exact idempotent occurrence request for a later replay."""

    occurrence_id = str(occurrence_payload.get("occurrence_id") or "").strip()
    if not occurrence_id:
        raise ValueError("occurrence_id is required for an error occurrence outbox")
    path = _occurrence_outbox_path(occurrence_id)
    envelope = {
        "schema_version": "3can.error-occurrence-outbox/v1",
        "kind": "error_occurrence",
        "endpoint": "/api/errors/occurrences",
        "method": "POST",
        "occurrence_id": occurrence_id,
        "fingerprint": occurrence_payload.get("fingerprint"),
        "payload": occurrence_payload,
        "queued_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        "last_response": response,
    }
    _atomic_replace_bytes(
        path,
        (json.dumps(envelope, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    return path


def _remove_occurrence_outbox(occurrence_id: str) -> None:
    _occurrence_outbox_path(occurrence_id).unlink(missing_ok=True)


def _server_error_case(response: Any) -> dict[str, Any]:
    if not isinstance(response, dict):
        return {}
    case = response.get("case")
    return case if isinstance(case, dict) else {}


def _server_occurrence_status(response: Any) -> str:
    if not isinstance(response, dict):
        return ""
    return str(response.get("status") or "").strip().upper()


def _occurrence_response_contract_valid(
    occurrence_payload: dict[str, Any],
    *,
    request_ok: bool,
    response: Any,
) -> bool:
    if not request_ok:
        return False
    status = _server_occurrence_status(response)
    case = _server_error_case(response)
    expected_fingerprint = str(occurrence_payload.get("fingerprint") or "").strip()
    case_id = str(case.get("case_id") or "").strip()
    return bool(
        status in {"RECORDED", "PROMOTED", "PARTIAL"}
        and expected_fingerprint
        and str(case.get("fingerprint") or "").strip().casefold()
        == expected_fingerprint.casefold()
        and (
            status == "RECORDED"
            or bool(re.fullmatch(r"ERR-case-[0-9a-f]{24}", case_id))
        )
    )


def _record_local_occurrence(
    args: argparse.Namespace,
    *,
    signature: str,
    occurrence_id: str,
    project_identity: str,
    operation_class: str,
    component: str,
    error_type: str,
    root_cause: str,
    occurred_at: str,
    related_node_ids: list[str],
) -> tuple[dict[str, Any], str]:
    """Append one local/outbox occurrence under a single cross-process lock."""

    entry: dict[str, Any] = {}
    with _loop_signature_transaction() as state:
        signatures = state.setdefault("signatures", {})
        if not isinstance(signatures, dict):
            raise LoopSignatureStoreError(
                "BLOCKED: occurrence store signatures must be an object"
            )
        previous = signatures.get(signature) if isinstance(signatures.get(signature), dict) else {}
        count = int(previous.get("count") or 0) + 1
        previous_case_status = _error_case_status(previous) if previous else ""
        if args.diagnosis:
            case_status = "diagnosed"
        elif count < LOOP_GATE_THRESHOLD:
            case_status = "observed"
        elif previous_case_status == "resolved":
            case_status = "regressed"
        else:
            case_status = "open"
        agents = list(
            dict.fromkeys(
                [*[str(item) for item in previous.get("agents", []) if item], args.agent_id]
            )
        )
        occurrence_ids = [
            str(item) for item in previous.get("occurrence_ids", []) if str(item).strip()
        ]
        occurrence_ids.append(occurrence_id)
        entry = {
            "signature": signature,
            "fingerprint_version": "ek2",
            "count": count,
            "case_status": case_status,
            "operation_class": operation_class,
            "component": component,
            "error_type": error_type,
            "root_cause": _compact_current_state(root_cause, max_chars=500),
            "project_identity": project_identity,
            "command_summary": _compact_current_state(args.command_summary, max_chars=500),
            "normalized_command": _normalize_signature_text(
                _redact_sensitive_text(args.command_summary)
            ),
            "error_excerpt": _compact_current_state(args.error_excerpt, max_chars=1000),
            "normalized_error": _normalize_signature_text(
                _redact_sensitive_text(args.error_excerpt)
            ),
            "target_files": [
                item
                for item in (
                    _public_target_ref(value)
                    for value in args.target_files
                )
                if item
            ],
            "scope_keywords": [
                _compact_current_state(str(item), max_chars=120)
                for item in args.scope_keywords[:20]
            ],
            "related_node_ids": related_node_ids,
            # A case ID is server-authoritative. Preserve a prior server value,
            # but never invent one client-side.
            "node_id": str(previous.get("case_id") or previous.get("node_id") or ""),
            "case_id": str(previous.get("case_id") or ""),
            "occurrence_ids": occurrence_ids[-64:],
            "agents": agents,
            "first_seen_at": previous.get("first_seen_at") or occurred_at,
            "last_seen_at": occurred_at,
            "promoted_at": previous.get("promoted_at"),
            "resolved_at": previous.get("resolved_at"),
            "solution_summary": previous.get("solution_summary") or "",
            "verification_evidence": previous.get("verification_evidence") or [],
            "last_diagnosis": (
                _compact_current_state(args.diagnosis, max_chars=500)
                if args.diagnosis
                else previous.get("last_diagnosis", "")
            ),
        }
        signatures[signature] = entry
    # The transaction exit durably rewrites both the primary payload and its
    # checksum. A legacy checksum-less or last-good-recovered store is therefore
    # READY after this write; carrying the pre-write PARTIAL status outward made
    # a successfully recorded first occurrence stop the caller.
    return entry, "READY"


def _merge_server_case_into_local(
    fingerprint: str,
    response: Any,
) -> None:
    case = _server_error_case(response)
    if not case:
        return
    with _loop_signature_transaction() as state:
        signatures = state.get("signatures")
        if not isinstance(signatures, dict):
            raise LoopSignatureStoreError(
                "BLOCKED: occurrence store signatures must be an object"
            )
        entry = signatures.get(fingerprint)
        if not isinstance(entry, dict):
            return
        case_id = str(case.get("case_id") or "").strip()
        if case_id:
            entry["case_id"] = case_id
            entry["node_id"] = case_id
        try:
            server_count = int(case.get("occurrence_count") or 0)
        except (TypeError, ValueError):
            server_count = 0
        if server_count:
            entry["server_occurrence_count"] = server_count
        server_state = str(case.get("state") or "").strip().lower()
        blocking = bool(case.get("blocking")) or bool(
            case_id
            and server_count >= 2
            and server_state == "observed"
        )
        entry["blocking"] = blocking
        if blocking and server_state == "observed":
            server_state = "open"
        if server_state:
            entry["case_status"] = server_state
        for key in ("first_seen_at", "last_seen_at", "promoted_at", "resolved_at"):
            if case.get(key):
                entry[key] = case[key]


def fail(args: argparse.Namespace) -> int:
    """Record through the authoritative occurrence API, with a local outbox."""
    project_metadata = _current_project_metadata(base_url=args.base_url)
    project_identity = str(
        project_metadata.get("project_id")
        or project_metadata.get("project_name")
        or "local-project"
    )
    operation_class = _failure_operation_class(
        args.command_summary,
        getattr(args, "operation_class", ""),
    )
    component = _failure_component(
        args.command_summary,
        args.target_files,
        args.scope_keywords,
        getattr(args, "component", ""),
    )
    error_type = _failure_error_type(
        args.error_excerpt,
        getattr(args, "error_type", ""),
    )
    root_cause = _normalize_signature_text(getattr(args, "root_cause", "")) or "unclassified-root-cause"
    try:
        signature = _loop_signature_key(
            args.command_summary,
            args.error_excerpt,
            args.target_files,
            scope_keywords=args.scope_keywords,
            operation_class=operation_class,
            component=component,
            error_type=error_type,
            project_identity=project_identity,
            root_cause=root_cause,
        )
    except ErrorKnowledgeContractUnavailable as exc:
        _print_json(
            {
                "ok": False,
                "status": "BLOCKED",
                "command": "fail",
                "error": {
                    "kind": "error_knowledge_contract_unavailable",
                    "detail": str(exc),
                },
                "client_graph_projection_attempted": False,
            }
        )
        return 1
    now = datetime.now(timezone.utc).isoformat()
    text = f"{args.command_summary} {args.error_excerpt} {' '.join(args.scope_keywords)}"
    related_node_ids = list(
        dict.fromkeys(
            [
                *args.related_nodes,
                *_infer_failure_related_nodes(text),
            ]
        )
    )
    occurrence_id = (
        f"OCC-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-"
        f"{uuid.uuid4().hex[:12]}"
    )
    occurrence_payload = {
        "occurrence_id": occurrence_id,
        "fingerprint": signature,
        "project_id": project_identity,
        "operation": operation_class,
        "component": component,
        "error_type": error_type,
        "error": _compact_current_state(args.error_excerpt, max_chars=1000),
        "root_cause": root_cause,
        "occurred_at": now,
        "agent_id": args.agent_id,
        "context": {
            "command_summary": _compact_current_state(args.command_summary, max_chars=500),
            "target_files": [
                item for item in (_public_target_ref(value) for value in args.target_files)
                if item
            ],
            "scope_keywords": [
                _compact_current_state(str(item), max_chars=120)
                for item in args.scope_keywords[:20]
            ],
            "diagnosis": _compact_current_state(args.diagnosis, max_chars=500),
            "related_node_ids": related_node_ids[:20],
        },
    }

    local_error = ""
    store_status = "READY"
    entry: dict[str, Any] = {}
    try:
        entry, store_status = _record_local_occurrence(
            args,
            signature=signature,
            occurrence_id=occurrence_id,
            project_identity=project_identity,
            operation_class=operation_class,
            component=component,
            error_type=error_type,
            root_cause=root_cause,
            occurred_at=now,
            related_node_ids=related_node_ids,
        )
    except (ErrorKnowledgeContractUnavailable, LoopSignatureStoreError, OSError, ValueError) as exc:
        local_error = str(exc)
        store_status = "BLOCKED"

    server_ok, server_response = _try_json_request(
        args.base_url,
        "/api/errors/occurrences",
        method="POST",
        payload=occurrence_payload,
        timeout=float(getattr(args, "timeout_seconds", 15.0)),
    )
    server_status = _server_occurrence_status(server_response)
    case = _server_error_case(server_response)
    response_contract_valid = _occurrence_response_contract_valid(
        occurrence_payload,
        request_ok=server_ok,
        response=server_response,
    )
    if response_contract_valid:
        try:
            _merge_server_case_into_local(signature, server_response)
        except (LoopSignatureStoreError, OSError, ValueError) as exc:
            local_error = str(exc)
            store_status = "BLOCKED"

    outbox_path: Path | None = None
    needs_outbox = not response_contract_valid or server_status == "PARTIAL"
    if needs_outbox:
        if response_contract_valid and server_status == "PARTIAL":
            reason = "server_projection_partial"
        elif server_ok:
            reason = "invalid_occurrence_response_contract"
        else:
            reason = "occurrence_endpoint_unavailable_or_rejected"
        try:
            outbox_path = _write_occurrence_outbox(
                occurrence_payload=occurrence_payload,
                response=server_response,
                reason=reason,
            )
        except (OSError, ValueError) as exc:
            local_error = "; ".join(item for item in (local_error, f"outbox: {exc}") if item)
            store_status = "BLOCKED"
    else:
        _remove_occurrence_outbox(occurrence_id)

    try:
        server_count = int(case.get("occurrence_count") or 0)
    except (TypeError, ValueError):
        server_count = 0
    local_count = int(entry.get("count") or 0) if entry else 0
    count = server_count or local_count
    case_id = str(case.get("case_id") or "").strip() or None
    blocking = bool(case.get("blocking"))
    promoted = (
        bool(case.get("promoted"))
        or server_status in {"PROMOTED", "PARTIAL"}
        or blocking
    )
    case_status = str(case.get("state") or entry.get("case_status") or "observed").lower()
    if (
        case_id
        and count >= 2
        and case_status == "observed"
    ):
        blocking = True
        promoted = True
        case_status = "open"
    gate_status = _failure_gate_status(count)
    required_next_actions = ["Read the related ERR/USR/ENV nodes before retrying."]
    if promoted:
        required_next_actions.append("Write or pass a diagnosis note before rerunning the same path.")
    if count >= LOOP_RULE_PROMOTION_THRESHOLD:
        required_next_actions.append("Promote the lesson into an ERR update or RUL policy before another retry.")

    overall_status = "OK"
    server_http_status = (
        int(server_response.get("http_status") or 0)
        if isinstance(server_response, dict)
        else 0
    )
    hard_server_rejection = server_http_status in {400, 409, 422}
    if (
        not response_contract_valid
        or server_status == "PARTIAL"
        or store_status in {"PARTIAL", "BLOCKED"}
    ):
        overall_status = "BLOCKED" if store_status == "BLOCKED" else "PARTIAL"
    if hard_server_rejection:
        overall_status = "BLOCKED"
    result = {
        "ok": overall_status == "OK",
        "status": overall_status,
        "command": "fail",
        "agent_id": args.agent_id,
        "occurrence_id": occurrence_id,
        "signature": signature,
        "count": count,
        "case_status": case_status,
        "gate_status": gate_status,
        "block_next_blind_retry": blocking
        or (promoted and case_status in {"open", "regressed"}),
        "promoted": promoted,
        "case_id": case_id,
        "node_id": case_id,
        "related_node_ids": related_node_ids,
        "state_path": str(_loop_signatures_path()),
        "local_store_status": store_status,
        "local_store_error": local_error or None,
        "server_status": server_status or None,
        "server_response": server_response,
        "outbox_path": str(outbox_path) if outbox_path else None,
        "client_graph_projection_attempted": False,
        "required_next_actions": required_next_actions,
    }
    _print_json(result)
    return 0 if overall_status == "OK" else 1


def _entry_datetime(entry: dict[str, Any]) -> datetime | None:
    for key in ("last_seen_at", "updated_at", "created_at", "first_seen_at"):
        raw = str(entry.get(key) or "").strip()
        if not raw:
            continue
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


def _failure_entry_selected(entry: dict[str, Any], args: argparse.Namespace, *, now: datetime) -> bool:
    signatures = {str(item) for item in args.signatures or [] if str(item).strip()}
    node_ids = {str(item) for item in args.node_ids or [] if str(item).strip()}
    signature = str(entry.get("signature") or "")
    node_id = str(entry.get("node_id") or "")
    if signatures and signature not in signatures:
        return False
    if node_ids and node_id not in node_ids:
        return False
    if signatures or node_ids:
        return True
    since_hours = float(args.since_hours)
    if since_hours <= 0:
        return True
    observed_at = _entry_datetime(entry)
    return bool(observed_at and observed_at >= now - timedelta(hours=since_hours))


def failure_gate_sync(args: argparse.Namespace) -> int:
    """Inspect/replay idempotent occurrence outboxes; never project ERR nodes."""
    try:
        state = _load_loop_signatures()
    except LoopSignatureStoreError as exc:
        _print_json(
            {
                "ok": False,
                "status": "BLOCKED",
                "command": "failure-gate-sync",
                "error": str(exc),
                "state_path": str(_loop_signatures_path()),
            }
        )
        return 1
    signatures = state.get("signatures") if isinstance(state.get("signatures"), dict) else {}
    now = datetime.now(timezone.utc)
    selected = [
        entry for entry in signatures.values()
        if isinstance(entry, dict) and _failure_entry_selected(entry, args, now=now)
    ]
    selected.sort(key=lambda entry: _entry_datetime(entry) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    outboxes: dict[str, list[tuple[Path, dict[str, Any]]]] = {}
    for path in sorted(PENDING_WRITEBACK_DIR.glob("error-occurrence-*.json")):
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(envelope, dict) or envelope.get("kind") != "error_occurrence":
            continue
        fingerprint = str(envelope.get("fingerprint") or "").strip()
        if fingerprint:
            outboxes.setdefault(fingerprint, []).append((path, envelope))

    rows: list[dict[str, Any]] = []
    for entry in selected:
        signature = str(entry.get("signature") or "").strip()
        pending = outboxes.get(signature, [])
        row: dict[str, Any] = {
            "signature": signature,
            "case_id": entry.get("case_id") or entry.get("node_id") or None,
            "count": int(entry.get("count") or 0),
            "command_summary": entry.get("command_summary"),
            "last_seen_at": entry.get("last_seen_at"),
            "pending_occurrence_count": len(pending),
            "pending_files": [path.name for path, _ in pending],
        }
        if pending and not args.apply:
            row["status"] = "pending_replay_planned"
            rows.append(row)
            continue

        if pending:
            replay_results: list[dict[str, Any]] = []
            for path, envelope in pending:
                payload = envelope.get("payload")
                if not isinstance(payload, dict):
                    replay_results.append(
                        {
                            "file": path.name,
                            "ok": False,
                            "status": "BLOCKED",
                            "error": "outbox payload must be an object",
                        }
                    )
                    continue
                ok, response = _try_json_request(
                    args.base_url,
                    "/api/errors/occurrences",
                    method="POST",
                    payload=payload,
                    timeout=15.0,
                )
                response_status = _server_occurrence_status(response)
                complete = (
                    _occurrence_response_contract_valid(
                        payload,
                        request_ok=ok,
                        response=response,
                    )
                    and response_status != "PARTIAL"
                )
                if complete:
                    path.unlink(missing_ok=True)
                    try:
                        _merge_server_case_into_local(signature, response)
                    except (LoopSignatureStoreError, OSError, ValueError) as exc:
                        complete = False
                        response = {
                            "server_response": response,
                            "local_store_error": str(exc),
                        }
                replay_results.append(
                    {
                        "file": path.name,
                        "ok": complete,
                        "server_status": response_status or None,
                        "response": response,
                    }
                )
            row["replay_results"] = replay_results
            row["status"] = (
                "replayed"
                if all(bool(item.get("ok")) for item in replay_results)
                else "replay_failed"
            )
            rows.append(row)
            continue

        case_id = str(entry.get("case_id") or entry.get("node_id") or "").strip()
        if case_id:
            query_path = f"/api/errors/cases?case_id={quote(case_id)}"
        else:
            query_path = f"/api/errors/cases?fingerprint={quote(signature)}"
        case_ok, case_response = _try_json_request(args.base_url, query_path, timeout=8.0)
        if case_ok:
            row["status"] = "server_case_confirmed"
            row["server_case"] = case_response
        else:
            occurrence_ids = [
                str(item) for item in entry.get("occurrence_ids") or [] if str(item).strip()
            ]
            if occurrence_ids:
                occurrence_ok, occurrence_response = _try_json_request(
                    args.base_url,
                    f"/api/errors/occurrences/{quote(occurrence_ids[-1])}",
                    timeout=8.0,
                )
                row["status"] = (
                    "server_occurrence_confirmed"
                    if occurrence_ok
                    else "server_state_unavailable"
                )
                row["server_occurrence"] = occurrence_response
            else:
                row["status"] = "server_state_unavailable"
            row["server_case_response"] = case_response
        rows.append(row)

    planned = [row for row in rows if row.get("status") == "pending_replay_planned"]
    replayed = [row for row in rows if row.get("status") == "replayed"]
    failed = [
        row for row in rows
        if row.get("status") in {"replay_failed", "server_state_unavailable"}
    ]
    result = {
        "ok": not failed or not args.apply,
        "status": "PARTIAL" if failed and args.apply else "OK",
        "command": "failure-gate-sync",
        "agent_id": args.agent_id,
        "apply": bool(args.apply),
        "since_hours": args.since_hours,
        "selected_count": len(selected),
        "planned_replay_count": len(planned),
        "replayed_count": len(replayed),
        "failed_count": len(failed),
        "rows": rows[: args.sample_limit],
        "state_path": str(_loop_signatures_path()),
        "count_incremented": False,
        "client_graph_projection_attempted": False,
    }
    _print_json(result)
    return 0 if result["ok"] else 1


def compact_note(args: argparse.Namespace) -> int:
    payload = _compact_note_payload(args)
    ok, response = _try_json_request(
        args.base_url,
        "/api/nodes?force=true",
        method="POST",
        payload=payload,
        timeout=15.0,
    )
    result = {
        "node_payload": payload,
        "response": response,
    }
    _record_local_token_estimate(
        args.base_url,
        agent_id=args.agent_id,
        command="compact-note",
        request_payload=payload,
        response_payload=response,
    )
    _print_json(result)
    return 0 if ok else 1


def compact(args: argparse.Namespace) -> int:
    """Compatibility alias for compact-note."""
    return compact_note(args)


def writeback(args: argparse.Namespace) -> int:
    payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
    ok, response = _try_json_request(
        args.base_url,
        "/api/writeback",
        method="POST",
        payload=payload,
        timeout=12.0,
    )
    _record_local_token_estimate(
        args.base_url,
        agent_id=str(payload.get("agent_id") or ""),
        command="writeback",
        request_payload=payload,
        response_payload=response,
    )
    _print_json(response)
    return 0 if ok else 1


def flush_pending(args: argparse.Namespace) -> int:
    files = sorted(PENDING_WRITEBACK_DIR.glob("*.json"))
    report: list[dict[str, Any]] = []
    failures = 0
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            failures += 1
            report.append(
                {
                    "file": path.name,
                    "kind": "invalid",
                    "posted": False,
                    "status": "BLOCKED",
                    "error": str(exc),
                }
            )
            continue
        if isinstance(payload, dict) and payload.get("kind") == "error_occurrence":
            occurrence_payload = payload.get("payload")
            item: dict[str, Any] = {
                "file": path.name,
                "kind": "error_occurrence",
                "occurrence_id": payload.get("occurrence_id"),
                "fingerprint": payload.get("fingerprint"),
            }
            if not isinstance(occurrence_payload, dict):
                item.update(
                    {
                        "posted": False,
                        "status": "BLOCKED",
                        "error": "outbox payload must be an object",
                    }
                )
                failures += 1
                report.append(item)
                continue
            if not args.dry_run:
                ok, response = _try_json_request(
                    args.base_url,
                    "/api/errors/occurrences",
                    method="POST",
                    payload=occurrence_payload,
                    timeout=15.0,
                )
                response_status = _server_occurrence_status(response)
                complete = (
                    _occurrence_response_contract_valid(
                        occurrence_payload,
                        request_ok=ok,
                        response=response,
                    )
                    and response_status != "PARTIAL"
                )
                item["posted"] = complete
                item["server_status"] = response_status or None
                item["response"] = response
                if complete:
                    try:
                        _merge_server_case_into_local(
                            str(payload.get("fingerprint") or ""),
                            response,
                        )
                        path.unlink(missing_ok=True)
                        item["removed"] = True
                    except (LoopSignatureStoreError, OSError, ValueError) as exc:
                        item["posted"] = False
                        item["status"] = "PARTIAL"
                        item["local_store_error"] = str(exc)
                        failures += 1
                else:
                    failures += 1
            report.append(item)
            if not args.no_sleep and not args.dry_run:
                time.sleep(1.1)
            continue

        if not isinstance(payload, dict):
            failures += 1
            report.append(
                {
                    "file": path.name,
                    "kind": "invalid",
                    "posted": False,
                    "status": "BLOCKED",
                    "error": "pending writeback must be a JSON object",
                }
            )
            continue
        changes = payload.get("changes", [])
        existing_ids: list[str] = []
        missing_ids: list[str] = []
        for change in changes:
            node_id = change.get("node_id")
            if not node_id:
                continue
            ok, _ = _try_json_request(args.base_url, f"/api/nodes/{node_id}", timeout=4.0)
            if ok:
                existing_ids.append(node_id)
            else:
                missing_ids.append(node_id)
        item = {
            "file": path.name,
            "agent_id": payload.get("agent_id", "unknown"),
            "change_count": len(changes),
            "existing_node_ids": existing_ids,
            "missing_node_ids": missing_ids,
        }
        if not args.dry_run:
            ok, response = _try_json_request(
                args.base_url,
                "/api/writeback",
                method="POST",
                payload=payload,
                timeout=12.0,
            )
            item["posted"] = ok
            item["response"] = response
            if not ok:
                failures += 1
        report.append(item)
        if not args.no_sleep and not args.dry_run:
            time.sleep(1.1)
    _print_json(
        {
            "ok": failures == 0,
            "status": "OK" if failures == 0 else "PARTIAL",
            "pending_dir": str(PENDING_WRITEBACK_DIR),
            "files": report,
        }
    )
    return 0 if failures == 0 else 1


def _flush_one_error_occurrence_outbox(
    base_url: str,
    *,
    timeout_seconds: float = 1.5,
) -> dict[str, Any]:
    """Replay at most one occurrence outbox without blocking hook progress."""

    paths = sorted(PENDING_WRITEBACK_DIR.glob("error-occurrence-*.json"))
    if not paths:
        return {"attempted": False, "posted": False}
    path = paths[0]
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {
            "attempted": True,
            "posted": False,
            "file": path.name,
            "error": f"{type(exc).__name__}: {exc}"[:500],
        }
    occurrence_payload = (
        envelope.get("payload")
        if isinstance(envelope, dict)
        and envelope.get("kind") == "error_occurrence"
        else None
    )
    if not isinstance(occurrence_payload, dict):
        return {
            "attempted": True,
            "posted": False,
            "file": path.name,
            "error": "invalid_error_occurrence_outbox",
        }
    ok, response = _try_json_request(
        base_url,
        "/api/errors/occurrences",
        method="POST",
        payload=occurrence_payload,
        timeout=max(0.2, min(float(timeout_seconds), 3.0)),
    )
    response_status = _server_occurrence_status(response)
    complete = (
        _occurrence_response_contract_valid(
            occurrence_payload,
            request_ok=ok,
            response=response,
        )
        and response_status != "PARTIAL"
    )
    if complete:
        try:
            _merge_server_case_into_local(
                str(envelope.get("fingerprint") or ""),
                response,
            )
            path.unlink(missing_ok=True)
        except (LoopSignatureStoreError, OSError, ValueError) as exc:
            return {
                "attempted": True,
                "posted": False,
                "file": path.name,
                "server_status": response_status or None,
                "error": f"{type(exc).__name__}: {exc}"[:500],
            }
    return {
        "attempted": True,
        "posted": complete,
        "file": path.name,
        "server_status": response_status or None,
    }


def ensure_online_command(args: argparse.Namespace) -> int:
    _print_json(
        ensure_online(
            args.base_url,
            engine_root_override=args.engine_root,
            start_if_offline=args.start_if_offline,
            wait_seconds=args.wait_seconds,
            min_nodes=args.min_nodes,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Codex-side 3CAN helper for route, checkin, ticket, and writeback.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="3CAN proxy base URL")
    parser.add_argument("--engine-root", help="Optional explicit neural-memory root override")
    parser.add_argument("--min-nodes", type=int, default=DEFAULT_MIN_NODES, help="Minimum acceptable node count")
    parser.add_argument(
        "--allow-project-mismatch",
        action="store_true",
        help="Bypass local project capsule identity gate for an explicit cross-project diagnostic.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor_parser = subparsers.add_parser("doctor", help="Inspect current 3CAN target and candidate engine roots.")
    doctor_parser.set_defaults(func=doctor)

    ensure_parser = subparsers.add_parser("ensure-online", help="Check canonical readiness and optionally request the runtime supervisor.")
    ensure_parser.add_argument("--start-if-offline", action="store_true")
    ensure_parser.add_argument("--wait-seconds", type=float, default=12.0)
    ensure_parser.set_defaults(func=ensure_online_command)

    session_parser = subparsers.add_parser("session-start", help="Ensure 3CAN online, checkin, then fetch briefing.")
    session_parser.add_argument("--start-if-offline", action="store_true")
    session_parser.add_argument("--wait-seconds", type=float, default=12.0)
    session_parser.add_argument("--agent-id", required=True)
    session_parser.add_argument("--name", default="Codex CLI")
    session_parser.add_argument("--role", default="frontend")
    session_parser.add_argument("--task", required=True)
    session_parser.add_argument("--capability", dest="capabilities", action="append", default=[])
    session_parser.add_argument("--session-id")
    session_parser.add_argument("--meta", help="Optional JSON string")
    session_parser.add_argument("--max-nodes", type=int, default=6)
    session_parser.set_defaults(func=session_start)

    route_parser = subparsers.add_parser("route", help="POST /api/route")
    route_parser.add_argument("--agent-id", required=True)
    route_parser.add_argument("--task", required=True)
    route_parser.add_argument("--max-nodes", type=int, default=6)
    route_parser.add_argument("--mode", default="slim", choices=["skeleton", "slim", "full"])
    route_parser.add_argument("--budget-tokens", type=int)
    route_parser.add_argument("--confirm-low-confidence", action="store_true")
    route_parser.add_argument("--allow-degraded", action="store_true")
    route_parser.add_argument("--timeout-seconds", type=float, default=12.0)
    route_parser.set_defaults(func=route)

    ticket_parser = subparsers.add_parser("ticket", help="POST /api/route/ticket")
    ticket_parser.add_argument("--agent-id", required=True)
    ticket_parser.add_argument("--task-description", required=True)
    ticket_parser.add_argument("--target-file", dest="target_files", action="append", required=True)
    ticket_parser.add_argument("--scope-keyword", dest="scope_keywords", action="append", default=[])
    ticket_parser.add_argument("--task-type", default="Edit")
    ticket_parser.set_defaults(func=ticket)

    prepare_parser = subparsers.add_parser("prepare", help="Alias: issue route ticket then consume it for a mutating tool.")
    prepare_parser.add_argument("--agent-id", required=True)
    prepare_parser.add_argument("--task-description", required=True)
    prepare_parser.add_argument("--target-file", dest="target_files", action="append", required=True)
    prepare_parser.add_argument("--scope-keyword", dest="scope_keywords", action="append", default=[])
    prepare_parser.add_argument("--task-type", default="Edit")
    prepare_parser.add_argument("--tool-name", required=True)
    prepare_parser.add_argument("--tool-input-summary", required=True)
    prepare_parser.set_defaults(func=prepare)

    supervise_parser = subparsers.add_parser("supervise", help="Unified pre-hook supervisor: runtime, route, memory, ticket, and token gates.")
    supervise_parser.add_argument("--agent-id", required=True)
    supervise_parser.add_argument("--task-description", required=True)
    supervise_parser.add_argument("--target-file", dest="target_files", action="append", default=[])
    supervise_parser.add_argument("--scope-keyword", dest="scope_keywords", action="append", default=[])
    supervise_parser.add_argument("--task-type", default="Edit")
    supervise_parser.add_argument("--tool-name", default="codex-mutate")
    supervise_parser.add_argument("--tool-input-summary", default="")
    supervise_parser.add_argument("--ticket-id", default="")
    supervise_parser.add_argument("--mode", default="slim", choices=["skeleton", "slim", "full"])
    supervise_parser.add_argument("--max-nodes", type=int, default=8)
    supervise_parser.add_argument("--budget-tokens", type=int, default=1400)
    supervise_parser.add_argument("--timeout-seconds", type=float, default=90.0)
    supervise_parser.add_argument("--min-nodes", type=int, default=DEFAULT_MIN_NODES)
    supervise_parser.add_argument("--no-consume-ticket", action="store_true")
    supervise_parser.add_argument("--skip-ticket", action="store_true", help="Record supervision but defer mutation ticket issue/consume to prepare.")
    supervise_parser.set_defaults(func=supervise)

    supervise_status_parser = subparsers.add_parser("supervise-status", help="Validate local last-supervise state for an agent.")
    supervise_status_parser.add_argument("--agent-id", required=True)
    supervise_status_parser.add_argument("--ttl-sec", type=int, default=900)
    supervise_status_parser.add_argument("--expect-scope-text")
    supervise_status_parser.add_argument("--expect-target-file", dest="expect_target_files", action="append", default=[])
    supervise_status_parser.add_argument("--require-pass", action="store_true")
    supervise_status_parser.set_defaults(func=supervise_status)

    memory_preflight_parser = subparsers.add_parser("memory-preflight", help="Return deterministic 3CAN memory lanes for a task.")
    memory_preflight_parser.add_argument("--agent-id", required=True)
    memory_preflight_parser.add_argument("--task-description", required=True)
    memory_preflight_parser.add_argument("--target-file", dest="target_files", action="append", default=[])
    memory_preflight_parser.add_argument("--scope-keyword", dest="scope_keywords", action="append", default=[])
    memory_preflight_parser.add_argument("--tool-name", default="")
    memory_preflight_parser.add_argument("--tool-input-summary", default="")
    memory_preflight_parser.set_defaults(func=memory_preflight)

    fail_parser = subparsers.add_parser("fail", help="Record a failed command as a repeated-error loop signature and ERR gate.")
    fail_parser.add_argument("--agent-id", required=True)
    fail_parser.add_argument("--command-summary", required=True)
    fail_parser.add_argument("--error-excerpt", required=True)
    fail_parser.add_argument("--target-file", dest="target_files", action="append", default=[])
    fail_parser.add_argument("--scope-keyword", dest="scope_keywords", action="append", default=[])
    fail_parser.add_argument("--related-node", dest="related_nodes", action="append", default=[])
    fail_parser.add_argument("--diagnosis", default="")
    fail_parser.add_argument("--node-id", default="")
    fail_parser.add_argument("--operation-class", default="")
    fail_parser.add_argument("--component", default="")
    fail_parser.add_argument("--error-type", default="")
    fail_parser.add_argument("--root-cause", default="")
    fail_parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=15.0,
        help="Occurrence upload timeout; local recording/outbox still applies.",
    )
    fail_parser.set_defaults(func=fail)

    failure_gate_sync_parser = subparsers.add_parser(
        "failure-gate-sync",
        help="Reconcile loop_signatures entries into live ERR nodes without incrementing failure counts.",
    )
    failure_gate_sync_parser.add_argument("--agent-id", required=True)
    failure_gate_sync_parser.add_argument("--since-hours", type=float, default=72.0)
    failure_gate_sync_parser.add_argument("--signature", dest="signatures", action="append", default=[])
    failure_gate_sync_parser.add_argument("--node-id", dest="node_ids", action="append", default=[])
    failure_gate_sync_parser.add_argument("--sample-limit", type=int, default=120)
    failure_gate_sync_parser.add_argument("--apply", action="store_true")
    failure_gate_sync_parser.add_argument("--ensure-existing-edges", action="store_true")
    failure_gate_sync_parser.set_defaults(func=failure_gate_sync)

    activity_parser = subparsers.add_parser("activity-log", help="POST /api/activity/log")
    activity_parser.add_argument("--agent-id", required=True)
    activity_parser.add_argument("--action", required=True)
    activity_parser.add_argument("--detail", required=True)
    activity_parser.add_argument("--affected-node", dest="affected_nodes", action="append", default=[])
    activity_parser.add_argument("--ticket-id")
    activity_parser.add_argument("--meta", help="Optional JSON string")
    activity_parser.set_defaults(func=activity_log)

    done_parser = subparsers.add_parser(
        "done",
        help="Complete a consumed ticket and optionally resolve verified ErrorCases.",
    )
    done_parser.add_argument("--agent-id", required=True)
    done_parser.add_argument("--detail", required=True)
    done_parser.add_argument("--affected-node", dest="affected_nodes", action="append", default=[])
    done_parser.add_argument("--ticket-id", required=True)
    done_parser.add_argument("--meta", help="Optional JSON string")
    done_parser.add_argument("--resolved-error", dest="resolved_errors", action="append", default=[])
    done_parser.add_argument(
        "--error-disposition",
        dest="error_dispositions",
        action="append",
        default=[],
        help=(
            "JSON object with error_id, disposition "
            "(resolved|still_open|not_applicable), and optional reason; "
            "repeatable."
        ),
    )
    done_parser.add_argument("--solution-summary", default="")
    done_parser.add_argument("--root-cause", default="")
    done_parser.add_argument(
        "--verification-evidence",
        dest="verification_evidence",
        action="append",
        default=[],
        help="JSON object or JSON array of evidence receipts; repeatable.",
    )
    done_parser.add_argument("--fixed-in", default="")
    done_parser.set_defaults(func=done)

    ticket_status_parser = subparsers.add_parser("ticket-status", help="Validate a route ticket against live 3CAN.")
    ticket_status_parser.add_argument("--ticket-id", required=True)
    ticket_status_parser.add_argument("--expect-agent-id")
    ticket_status_parser.add_argument("--expect-scope-text")
    ticket_status_parser.add_argument("--expect-target-file", dest="expect_target_files", action="append", default=[])
    ticket_status_parser.add_argument("--min-remaining-ttl-sec", type=int, default=DEFAULT_MIN_TICKET_TTL_SEC)
    ticket_status_parser.set_defaults(func=ticket_status)

    route_freshness_parser = subparsers.add_parser("route-freshness", help="Validate local last-route state for an agent.")
    route_freshness_parser.add_argument("--agent-id", required=True)
    route_freshness_parser.add_argument("--ttl-sec", type=int, default=900)
    route_freshness_parser.add_argument("--expect-scope-text")
    route_freshness_parser.set_defaults(func=route_freshness)

    ticket_consume_parser = subparsers.add_parser("ticket-consume", help="Record live ticket consumption before a mutating step.")
    ticket_consume_parser.add_argument("--ticket-id", required=True)
    ticket_consume_parser.add_argument("--agent-id", default="")
    ticket_consume_parser.add_argument("--tool-name", required=True)
    ticket_consume_parser.add_argument("--tool-input-summary", required=True)
    ticket_consume_parser.set_defaults(func=ticket_consume)

    compact_parser = subparsers.add_parser("compact-note", help="Create a SES-* continuation note before compaction.")
    compact_parser.add_argument("--agent-id", required=True)
    compact_parser.add_argument("--title")
    compact_parser.add_argument("--task-summary", required=True)
    compact_parser.add_argument("--next-step", dest="next_steps", action="append", default=[])
    compact_parser.add_argument("--blocker", dest="blockers", action="append", default=[])
    compact_parser.add_argument("--file", dest="files", action="append", default=[])
    compact_parser.add_argument("--related-node", dest="related_nodes", action="append", default=[])
    compact_parser.add_argument("--node-id")
    compact_parser.set_defaults(func=compact_note)

    compact_alias_parser = subparsers.add_parser("compact", help="Alias: compact-note.")
    compact_alias_parser.add_argument("--agent-id", required=True)
    compact_alias_parser.add_argument("--title")
    compact_alias_parser.add_argument("--task-summary", required=True)
    compact_alias_parser.add_argument("--next-step", dest="next_steps", action="append", default=[])
    compact_alias_parser.add_argument("--blocker", dest="blockers", action="append", default=[])
    compact_alias_parser.add_argument("--file", dest="files", action="append", default=[])
    compact_alias_parser.add_argument("--related-node", dest="related_nodes", action="append", default=[])
    compact_alias_parser.add_argument("--node-id")
    compact_alias_parser.set_defaults(func=compact)

    writeback_parser = subparsers.add_parser("writeback", help="Replay a writeback JSON payload.")
    writeback_parser.add_argument("--file", required=True)
    writeback_parser.set_defaults(func=writeback)

    flush_parser = subparsers.add_parser("flush-pending", help="Inspect or replay queued writeback payloads.")
    flush_parser.add_argument("--dry-run", action="store_true")
    flush_parser.add_argument("--no-sleep", action="store_true")
    flush_parser.set_defaults(func=flush_pending)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    discovery = resolve_engine_root(args.engine_root)
    identity_gate = _project_identity_gate(
        args.base_url,
        discovery,
        agent_id=_agent_id_from_args(args),
        command=args.command,
    )
    args.project_identity = identity_gate
    if identity_gate.get("status") == "block" and not args.allow_project_mismatch and args.command != "doctor":
        _print_json(_project_identity_block_payload(args, identity_gate))
        return 1
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
