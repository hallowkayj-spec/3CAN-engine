from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
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
_OWNER_INTENT_MODULE: Any | None = None
_OWNER_INTENT_MODULE_PATH: Path | None = None

DEFAULT_BASE_URL = (
    os.environ.get("THREECAN_URL")
    or os.environ.get("THREECAN_BASE_URL")
    or "http://127.0.0.1:9700"
)
DEFAULT_MIN_NODES = int(os.environ.get("THREECAN_MIN_NODES", "100"))
DEFAULT_MIN_TICKET_TTL_SEC = int(os.environ.get("THREECAN_MIN_TICKET_TTL_SEC", "5"))
RUNTIME_IDENTITY_SCHEMA = "3can.runtime-identity/v1"
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
        return False, {
            "status": "UNAVAILABLE",
            "kind": "threecan_runtime_unavailable",
            "reason": str(exc.reason)[:300],
        }
    except TimeoutError as exc:
        return False, {
            "status": "UNAVAILABLE",
            "kind": "threecan_runtime_unavailable",
            "reason": str(exc)[:300],
        }
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


def _git_value(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )
    if result.returncode:
        raise ValueError("git_identity_unavailable")
    return result.stdout.strip()


def _repository_key(remote: str) -> str:
    """Normalize HTTPS/SSH/scp Git remotes to one durable repository key."""

    value = str(remote or "").strip()
    normalized_match = re.fullmatch(r"([^/\s:]+)/(.+)", value)
    scp_match = re.fullmatch(r"(?:[^@]+@)?([^:]+):(.+)", value)
    if normalized_match and "://" not in value:
        host, path = normalized_match.groups()
    elif scp_match and "://" not in value:
        host, path = scp_match.groups()
    else:
        parsed = urlparse(value)
        if not parsed.scheme or not parsed.hostname:
            raise ValueError("git_remote_invalid")
        host, path = parsed.hostname, parsed.path
    path = path.strip("/")
    if path.casefold().endswith(".git"):
        path = path[:-4]
    if not host or not path:
        raise ValueError("git_remote_invalid")
    return f"{host.casefold()}/{path.casefold()}"


def _canonical_physical_path(value: str | Path) -> str:
    """Return one path spelling for the same Windows file from WSL/Windows."""

    normalized = str(value).replace("\\", "/")
    wsl_drive = re.fullmatch(r"/mnt/([A-Za-z])(?:/(.*))?", normalized)
    if wsl_drive:
        drive, tail = wsl_drive.groups()
        normalized = f"{drive}:/{tail or ''}"
    if re.match(r"^[A-Za-z]:/", normalized):
        normalized = normalized.casefold()
    return normalized.rstrip("/") or "/"


def _local_path_sha256(path: Path) -> str:
    value = _canonical_physical_path(path.resolve(strict=False))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _actual_project_root(project_root: Path | None = None) -> Path:
    requested = (project_root or PROJECT_ROOT).resolve(strict=False)
    return Path(_git_value(requested, "rev-parse", "--show-toplevel")).resolve()


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
    return Path(project_root or PROJECT_ROOT) / ".agents" / "project.json"


def _load_project_capsule(project_root: Path | None = None) -> dict[str, Any]:
    requested_root = project_root or PROJECT_ROOT
    try:
        root = _actual_project_root(requested_root)
    except (OSError, ValueError, subprocess.SubprocessError):
        root = requested_root.resolve(strict=False)
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
    required_node_ids = _list_from_value(raw.get("required_node_ids") or raw.get("graph_anchor_node_ids"))
    capsule = {
        "configured": True,
        "path": str(path),
        "raw": raw,
        "project_id": str(raw.get("project_id") or raw.get("id") or "").strip(),
        "project_namespace": str(raw.get("project_namespace") or "").strip(),
        "project_name": str(raw.get("project_name") or raw.get("name") or "").strip(),
        "project_root": _path_identity(str(project_root_value), base=root),
        "actual_project_root": str(root),
        "git_repository": str(raw.get("git_repository") or "").strip(),
        "threecan_engine_root": _path_identity(str(engine_root_value), base=root) if engine_root_value else "",
        "required_node_ids": required_node_ids,
        "frontend_ports": raw.get("frontend_ports") or [],
        "forbidden_keywords": raw.get("forbidden_keywords") or [],
    }
    return capsule


_PROJECT_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


def _project_execution_reality(
    project_root: Path | None = None,
) -> tuple[dict[str, Any], dict[str, str], list[dict[str, Any]]]:
    """Resolve one capsule/worktree truth for diagnostics and request payloads."""
    capsule = _load_project_capsule(project_root)
    if not capsule.get("configured") or capsule.get("load_error"):
        return capsule, {}, []

    checks: list[dict[str, Any]] = []
    actual_root = Path(str(capsule.get("actual_project_root") or ""))
    expected_root = str(capsule.get("project_root") or "")
    root_matches = bool(actual_root) and _paths_match(expected_root, actual_root)
    checks.append(
        {
            "name": "project_root",
            "status": "pass" if root_matches else "block",
            "expected": expected_root,
            "actual": str(actual_root),
            "error": "project_root_mismatch",
        }
    )
    for field in ("project_id", "project_namespace"):
        value = str(capsule.get(field) or "").strip()
        checks.append(
            {
                "name": field,
                "status": (
                    "pass" if _PROJECT_IDENTIFIER_PATTERN.fullmatch(value) else "block"
                ),
                "actual": value,
                "error": f"{field}_invalid",
            }
        )

    configured_repository = str(capsule.get("git_repository") or "").strip()
    expected_repository = configured_repository
    actual_repository = ""
    try:
        expected_repository = _repository_key(expected_repository)
        actual_repository = _repository_key(
            _git_value(actual_root, "remote", "get-url", "origin")
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    checks.append(
        {
            "name": "git_repository",
            "status": (
                "pass"
                if configured_repository and configured_repository == expected_repository == actual_repository
                else "block"
            ),
            "expected": expected_repository,
            "actual": actual_repository,
            "configured": configured_repository,
            "error": (
                "git_repository_missing"
                if not configured_repository
                else "git_repository_not_normalized"
                if configured_repository != expected_repository
                else "git_repository_mismatch"
            ),
        }
    )

    context = {
        "project_id": str(capsule.get("project_id") or "").strip(),
        "project_namespace": str(capsule.get("project_namespace") or "").strip(),
    }
    if not any(check["status"] == "block" for check in checks):
        common_dir = Path(
            _git_value(
                actual_root,
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            )
        )
        if not common_dir.is_absolute():
            common_dir = actual_root / common_dir
        context["workspace_id"] = (
            f"git-{_local_path_sha256(common_dir)[:12]}-"
            f"{_local_path_sha256(actual_root)[:12]}"
        )
    return capsule, context, checks


def _current_project_metadata(*, base_url: str = "") -> dict[str, Any]:
    del base_url  # Transport endpoints are runtime configuration, not project identity.
    try:
        capsule, context, checks = _project_execution_reality()
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}
    if (
        not capsule.get("configured")
        or capsule.get("load_error")
        or any(check["status"] == "block" for check in checks)
    ):
        return {}
    return {
        "project_name": capsule.get("project_name") or "",
        "git_repository": _repository_key(str(capsule.get("git_repository") or "")),
        **context,
    }


def _execution_context(project_root: Path | None = None) -> dict[str, str]:
    """Resolve the path-free identity shared by route and mutation requests."""

    capsule, context, checks = _project_execution_reality(project_root)
    if not capsule.get("configured"):
        return {}
    if capsule.get("load_error"):
        raise RuntimeError(f"project_capsule_invalid:{capsule['load_error']}")
    blocking = [check for check in checks if check["status"] == "block"]
    if blocking:
        raise RuntimeError(str(blocking[0]["error"]))

    workorder_id = str(
        os.environ.get("THREECAN_WORKORDER_ID")
        or os.environ.get("WORKORDER_ID")
        or ""
    ).strip()
    if workorder_id:
        if not _PROJECT_IDENTIFIER_PATTERN.fullmatch(workorder_id):
            raise RuntimeError("workorder_id_invalid")
        context["workorder_id"] = workorder_id
    return context


def _owner_intent_module() -> Any:
    """Load the engine's single stdlib 3CAN.md parser implementation."""

    global _OWNER_INTENT_MODULE, _OWNER_INTENT_MODULE_PATH
    candidates: list[Path] = []
    configured_root = os.environ.get("THREECAN_ENGINE_ROOT", "").strip()
    if configured_root:
        candidates.append(Path(configured_root).expanduser() / "backend" / "owner_intent.py")
    candidates.append(STAGING_ENGINE_ROOT / "backend" / "owner_intent.py")
    module_path = next(
        (candidate.resolve(strict=False) for candidate in candidates if candidate.is_file()),
        None,
    )
    if module_path is None:
        raise RuntimeError("owner_intent_loader_unavailable")
    if _OWNER_INTENT_MODULE is not None and _OWNER_INTENT_MODULE_PATH == module_path:
        return _OWNER_INTENT_MODULE
    spec = importlib.util.spec_from_file_location(
        "threecan_owner_intent",
        module_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("owner_intent_loader_unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _OWNER_INTENT_MODULE = module
    _OWNER_INTENT_MODULE_PATH = module_path
    return module


def _owner_intent_projection(
    project_root: Path | None = None,
    *,
    context: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    root = _actual_project_root(project_root)
    if not (root / "3CAN.md").is_file():
        return None
    execution_context = context or _execution_context(root)
    module = _owner_intent_module()
    projection = module.load_owner_intent(
        root,
        project_id=execution_context.get("project_id", ""),
        project_namespace=execution_context.get("project_namespace", ""),
    )
    if projection and projection.get("status") != "applied":
        raise RuntimeError(str(projection.get("reason") or "owner_intent_not_applicable"))
    return projection


def _with_owner_intent(payload: dict[str, Any]) -> dict[str, Any]:
    merged = dict(payload)
    project_id = str(merged.get("project_id") or "")
    project_namespace = str(merged.get("project_namespace") or "")
    if not project_id or not project_namespace:
        return merged
    projection = _owner_intent_projection(context={
        "project_id": project_id,
        "project_namespace": project_namespace,
    })
    if projection:
        merged["owner_intent"] = projection
    return merged


def _with_execution_context(
    payload: dict[str, Any],
    *,
    allow_project_mismatch: bool = False,
) -> dict[str, Any]:
    merged = dict(payload)
    try:
        merged.update(_execution_context())
    except (RuntimeError, ValueError):
        if not allow_project_mismatch:
            raise
    return merged


def _resolved_target_files(values: list[str]) -> list[str]:
    """Bind mutation targets to the physical Git worktree before transport."""

    root = _actual_project_root()
    resolved: list[str] = []
    for value in values:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        target = candidate.resolve(strict=False)
        if target != root and root not in target.parents:
            raise ValueError("target_path_outside_project_root")
        resolved.append(_canonical_physical_path(target))
    return resolved


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
    require_configured: bool = False,
) -> dict[str, Any]:
    del base_url, agent_id
    try:
        capsule, context, checks = _project_execution_reality()
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        capsule = _load_project_capsule()
        context = {}
        checks = [
            {
                "name": "workspace",
                "status": "block",
                "error": "project_workspace_unavailable",
                "detail": str(exc),
            }
        ]
    if not capsule.get("configured"):
        return {
            "name": "project_identity",
            "status": "block" if require_configured else "pass",
            "configured": False,
            "reason": (
                "project_capsule_required_for_mutation"
                if require_configured
                else "no_project_capsule_read_only"
            ),
            "error": (
                {"kind": "project_capsule_required_for_mutation"}
                if require_configured
                else None
            ),
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

    blocking = [item for item in checks if item.get("status") == "block"]
    return {
        "name": "project_identity",
        "status": "block" if blocking else "pass",
        "configured": True,
        "project_id": capsule.get("project_id") or "",
        "project_namespace": capsule.get("project_namespace") or "",
        "project_name": capsule.get("project_name") or "",
        "capsule_path": capsule.get("path"),
        "command": command,
        "checks": checks,
        "blocking_checks": blocking,
        "metadata": {
            "project_name": capsule.get("project_name") or "",
            "git_repository": _repository_key(
                str(capsule.get("git_repository") or "")
            ),
            **context,
        }
        if not blocking
        else {},
    }


def _agent_id_from_args(args: argparse.Namespace) -> str:
    return str(getattr(args, "agent_id", "") or getattr(args, "expect_agent_id", "") or "")


def _resolve_agent_id(explicit: str = "") -> str:
    """Resolve one stable execution identity without consulting ticket state."""
    configured = explicit.strip() or (
        os.environ.get("THREECAN_AGENT_ID", "").strip()
        or os.environ.get("CODEX_AGENT_ID", "").strip()
    )
    stable_execution = (
        os.environ.get("CODEX_THREAD_ID", "").strip()
        or os.environ.get("CODEX_SESSION_ID", "").strip()
        or os.environ.get("THREECAN_SESSION_ID", "").strip()
        or os.environ.get("THREECAN_WORKORDER_ID", "").strip()
        or os.environ.get("WORKORDER_ID", "").strip()
    )
    if configured:
        agent_id = configured
    elif stable_execution:
        readable = _safe_id_part(stable_execution)[:48]
        digest = hashlib.sha256(stable_execution.encode("utf-8")).hexdigest()[:12]
        agent_id = f"codex-{readable}-{digest}"
    else:
        agent_id = ""
    if not agent_id:
        raise ValueError("agent_id_stable_execution_identity_required")
    if agent_id.casefold() == "codex-main":
        raise ValueError("generic_agent_id_forbidden")
    return agent_id


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
    explicit = (
        explicit_session_id
        or os.environ.get("THREECAN_SESSION_ID")
        or os.environ.get("CODEX_SESSION_ID")
        or os.environ.get("SESSION_ID")
    )
    if explicit:
        return explicit
    readable = _safe_id_part(agent_id)[:48]
    digest = hashlib.sha256(agent_id.encode("utf-8")).hexdigest()[:12]
    return f"SES-{readable}-{digest}"


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
    if ticket_agent_id != requested_agent_id:
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

    return {
        "online": False,
        "started": False,
        "healthy": False,
        "code": "THREECAN_RUNTIME_UNAVAILABLE",
        "error": result,
        "project_identity": identity_gate,
        "engine_root": discovery,
    }
def session_start(args: argparse.Namespace) -> int:
    status = ensure_online(
        args.base_url,
        engine_root_override=args.engine_root,
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
    owner_defaults = _owner_intent_projection(
        context={
            "project_id": str(project_meta.get("project_id") or ""),
            "project_namespace": str(project_meta.get("project_namespace") or ""),
        }
    ) if project_meta else None
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

    briefing_path = f"/api/briefing?agent_id={quote(args.agent_id)}&role={quote(args.role)}&max_nodes={args.max_nodes}"
    if project_meta:
        briefing_path += (
            f"&project_id={quote(str(project_meta['project_id']))}"
            f"&project_namespace={quote(str(project_meta['project_namespace']))}"
        )
    ok, briefing = _try_json_request(args.base_url, briefing_path, timeout=30.0)
    if not ok:
        _print_json({"status": status, "checkin": checkin, "briefing_error": briefing})
        return 1

    if isinstance(briefing, dict):
        server_owner = briefing.get("owner_defaults")
        if server_owner is not None:
            expected_project = str(project_meta.get("project_id") or "").casefold()
            expected_namespace = str(project_meta.get("project_namespace") or "").casefold()
            if (
                not isinstance(server_owner, dict)
                or server_owner.get("status") != "applied"
                or server_owner.get("assertion_origin") != "server_local_file"
            ):
                _print_json({
                    "status": status,
                    "checkin": checkin,
                    "briefing_error": {"kind": "owner_intent_invalid"},
                })
                return 1
            if (
                str(server_owner.get("project_id") or "").casefold()
                != expected_project
                or str(server_owner.get("project_namespace") or "").casefold()
                != expected_namespace
            ):
                _print_json({
                    "status": status,
                    "checkin": checkin,
                    "briefing_error": {
                        "kind": "owner_intent_project_identity_mismatch",
                    },
                })
                return 1
        elif owner_defaults:
            briefing["owner_defaults"] = {
                **owner_defaults,
                "assertion_origin": "client_asserted",
            }
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
    payload = _with_owner_intent(_with_execution_context(
        {
            "task": args.task,
            "max_nodes": args.max_nodes,
            "agent_id": args.agent_id,
            "confirm_low_confidence": args.confirm_low_confidence,
            "allow_degraded": args.allow_degraded,
        },
        allow_project_mismatch=bool(
            getattr(args, "allow_project_mismatch", False)
        ),
    ))
    if args.mode:
        payload["mode"] = args.mode
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
    _print_json(response)
    return 0 if ok else 1


def ticket(args: argparse.Namespace) -> int:
    target_files = _resolved_target_files(args.target_files)
    payload = _with_execution_context({
        "agent_id": args.agent_id,
        "task_description": args.task_description,
        "target_files": target_files,
        "scope_keywords": args.scope_keywords,
        "task_type": args.task_type,
    })
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
    target_files = _resolved_target_files(args.target_files)
    ticket_payload = _with_execution_context({
        "agent_id": args.agent_id,
        "task_description": args.task_description,
        "target_files": target_files,
        "scope_keywords": args.scope_keywords,
        "task_type": args.task_type,
    })
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
    actual_review_required = _actual_review_required_error_ids(response) if ok else []
    _record_local_token_estimate(
        args.base_url,
        agent_id=args.agent_id,
        command="done",
        request_payload=payload,
        response_payload=response,
        route_ticket_id=str(payload.get("ticket_id") or ""),
    )
    result = {
        "ok": ok,
        "status": "complete" if ok else "failed",
        "response": response,
        "requested_resolved_errors": resolved_errors,
        "resolved_errors": actual_resolved,
        "review_required_errors": actual_review_required,
        "error_dispositions": (
            response.get("error_dispositions", [])
            if ok and isinstance(response, dict)
            else []
        ),
    }
    _print_json(result)
    return 0 if ok else 1


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
    raw_payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
    if isinstance(raw_payload, list):
        payload = {"changes": raw_payload}
    elif isinstance(raw_payload, dict):
        payload = raw_payload
    else:
        raise ValueError("writeback_payload_must_be_object_or_list")
    payload = _with_execution_context(payload)
    payload["agent_id"] = args.agent_id
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


def ensure_online_command(args: argparse.Namespace) -> int:
    _print_json(
        ensure_online(
            args.base_url,
            engine_root_override=args.engine_root,
            min_nodes=args.min_nodes,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Codex-side 3CAN helper for route, checkin, ticket, and writeback.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="3CAN proxy base URL")
    parser.add_argument(
        "--workorder-id",
        default="",
        help="Explicit current Workorder identity; overrides inherited Workorder environment.",
    )
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

    ensure_parser = subparsers.add_parser(
        "ensure-online",
        help="Check canonical readiness without changing runtime lifecycle.",
    )
    ensure_parser.set_defaults(func=ensure_online_command)

    session_parser = subparsers.add_parser(
        "session-start",
        help=(
            "Optional current-client orientation: check readiness, check in, "
            "and fetch briefing; does not create a chat/task or start 3CAN."
        ),
        description=(
            "Optional current-client orientation: check readiness, check in, "
            "and fetch briefing; does not create a chat/task or start 3CAN."
        ),
    )
    session_parser.add_argument("--agent-id", nargs="?", default="", const="")
    session_parser.add_argument("--name", default="Codex CLI")
    session_parser.add_argument("--role", default="frontend")
    session_parser.add_argument("--task", required=True)
    session_parser.add_argument("--capability", dest="capabilities", action="append", default=[])
    session_parser.add_argument("--session-id")
    session_parser.add_argument("--meta", help="Optional JSON string")
    session_parser.add_argument("--max-nodes", type=int, default=6)
    session_parser.set_defaults(func=session_start)

    route_parser = subparsers.add_parser("route", help="POST /api/route")
    route_parser.add_argument("--agent-id", nargs="?", default="", const="")
    route_parser.add_argument("--task", required=True)
    route_parser.add_argument("--max-nodes", type=int, default=6)
    route_parser.add_argument("--mode", choices=["skeleton", "slim", "full"])
    route_parser.add_argument("--budget-tokens", type=int)
    route_parser.add_argument("--confirm-low-confidence", action="store_true")
    route_parser.add_argument("--allow-degraded", action="store_true")
    route_parser.add_argument("--timeout-seconds", type=float, default=12.0)
    route_parser.set_defaults(func=route)

    ticket_parser = subparsers.add_parser("ticket", help="POST /api/route/ticket")
    ticket_parser.add_argument("--agent-id", nargs="?", default="", const="")
    ticket_parser.add_argument("--task-description", required=True)
    ticket_parser.add_argument("--target-file", dest="target_files", action="append", required=True)
    ticket_parser.add_argument("--scope-keyword", dest="scope_keywords", action="append", default=[])
    ticket_parser.add_argument("--task-type", default="Edit")
    ticket_parser.set_defaults(func=ticket)

    prepare_parser = subparsers.add_parser("prepare", help="Alias: issue route ticket then consume it for a mutating tool.")
    prepare_parser.add_argument("--agent-id", nargs="?", default="", const="")
    prepare_parser.add_argument("--task-description", required=True)
    prepare_parser.add_argument("--target-file", dest="target_files", action="append", required=True)
    prepare_parser.add_argument("--scope-keyword", dest="scope_keywords", action="append", default=[])
    prepare_parser.add_argument("--task-type", default="Edit")
    prepare_parser.add_argument("--tool-name", required=True)
    prepare_parser.add_argument("--tool-input-summary", required=True)
    prepare_parser.set_defaults(func=prepare)

    activity_parser = subparsers.add_parser("activity-log", help="POST /api/activity/log")
    activity_parser.add_argument("--agent-id", nargs="?", default="", const="")
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
    done_parser.add_argument("--agent-id", nargs="?", default="", const="")
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
    ticket_status_parser.add_argument(
        "--expect-agent-id",
        nargs="?",
        const="__current_execution__",
    )
    ticket_status_parser.add_argument("--expect-scope-text")
    ticket_status_parser.add_argument("--expect-target-file", dest="expect_target_files", action="append", default=[])
    ticket_status_parser.add_argument("--min-remaining-ttl-sec", type=int, default=DEFAULT_MIN_TICKET_TTL_SEC)
    ticket_status_parser.set_defaults(func=ticket_status)

    ticket_consume_parser = subparsers.add_parser("ticket-consume", help="Record live ticket consumption before a mutating step.")
    ticket_consume_parser.add_argument("--ticket-id", required=True)
    ticket_consume_parser.add_argument("--agent-id", nargs="?", default="", const="")
    ticket_consume_parser.add_argument("--tool-name", required=True)
    ticket_consume_parser.add_argument("--tool-input-summary", required=True)
    ticket_consume_parser.set_defaults(func=ticket_consume)

    compact_parser = subparsers.add_parser("compact-note", help="Create a SES-* continuation note before compaction.")
    compact_parser.add_argument("--agent-id", nargs="?", default="", const="")
    compact_parser.add_argument("--title")
    compact_parser.add_argument("--task-summary", required=True)
    compact_parser.add_argument("--next-step", dest="next_steps", action="append", default=[])
    compact_parser.add_argument("--blocker", dest="blockers", action="append", default=[])
    compact_parser.add_argument("--file", dest="files", action="append", default=[])
    compact_parser.add_argument("--related-node", dest="related_nodes", action="append", default=[])
    compact_parser.add_argument("--node-id")
    compact_parser.set_defaults(func=compact_note)

    compact_alias_parser = subparsers.add_parser("compact", help="Alias: compact-note.")
    compact_alias_parser.add_argument("--agent-id", nargs="?", default="", const="")
    compact_alias_parser.add_argument("--title")
    compact_alias_parser.add_argument("--task-summary", required=True)
    compact_alias_parser.add_argument("--next-step", dest="next_steps", action="append", default=[])
    compact_alias_parser.add_argument("--blocker", dest="blockers", action="append", default=[])
    compact_alias_parser.add_argument("--file", dest="files", action="append", default=[])
    compact_alias_parser.add_argument("--related-node", dest="related_nodes", action="append", default=[])
    compact_alias_parser.add_argument("--node-id")
    compact_alias_parser.set_defaults(func=compact)

    writeback_parser = subparsers.add_parser("writeback", help="Replay a writeback JSON payload.")
    writeback_parser.add_argument("--agent-id", nargs="?", default="", const="")
    writeback_parser.add_argument("--file", required=True)
    writeback_parser.set_defaults(func=writeback)

    return parser


def _project_mismatch_bypass_is_read_only(args: argparse.Namespace) -> bool:
    command = str(getattr(args, "command", "") or "")
    if command in {
        "doctor",
        "route",
        "ticket-status",
    }:
        return True
    if command == "ensure-online":
        return True
    return False


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.workorder_id:
        workorder_id = str(args.workorder_id).strip()
        if not _PROJECT_IDENTIFIER_PATTERN.fullmatch(workorder_id):
            _print_json(
                {
                    "ok": False,
                    "command": args.command,
                    "error": {"kind": "workorder_id_invalid"},
                }
            )
            return 1
        os.environ["THREECAN_WORKORDER_ID"] = workorder_id
    if hasattr(args, "agent_id"):
        try:
            args.agent_id = _resolve_agent_id(args.agent_id)
        except ValueError as exc:
            _print_json(
                {
                    "ok": False,
                    "command": args.command,
                    "error": {"kind": str(exc)},
                }
            )
            return 1
    if (
        hasattr(args, "expect_agent_id")
        and args.expect_agent_id is not None
        and (
            not str(args.expect_agent_id).strip()
            or args.expect_agent_id == "__current_execution__"
        )
    ):
        try:
            args.expect_agent_id = _resolve_agent_id()
        except ValueError as exc:
            _print_json(
                {
                    "ok": False,
                    "command": args.command,
                    "error": {"kind": str(exc)},
                }
            )
            return 1
    if args.allow_project_mismatch and not _project_mismatch_bypass_is_read_only(args):
        _print_json(
            {
                "ok": False,
                "command": args.command,
                "error": {
                    "kind": "project_mismatch_bypass_not_allowed_for_mutation",
                    "message": (
                        "--allow-project-mismatch is limited to read-only diagnostics; "
                        "mutation commands always enforce project identity."
                    ),
                },
            }
        )
        return 2
    discovery = resolve_engine_root(args.engine_root)
    identity_gate = _project_identity_gate(
        args.base_url,
        discovery,
        agent_id=_agent_id_from_args(args),
        command=args.command,
        require_configured=not _project_mismatch_bypass_is_read_only(args),
    )
    args.project_identity = identity_gate
    if identity_gate.get("status") == "block" and not args.allow_project_mismatch and args.command != "doctor":
        _print_json(_project_identity_block_payload(args, identity_gate))
        return 1
    try:
        return args.func(args)
    except (RuntimeError, ValueError) as exc:
        _print_json(
            {
                "ok": False,
                "command": args.command,
                "error": {"kind": str(exc)},
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
