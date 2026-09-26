"""Bounded 3CAN semantic writeback through the canonical client, not a queue.

Only explicit connect/error and semantic review call this module. Native Hook
events remain offline. Git and canonical ErrorKnowledge retain their authority.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import quote, urlparse

import runtimehook_checkpoints as cp


class WritebackError(ValueError):
    pass


def config():
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    path = home / "runtimehook/writeback.json"
    if not path.exists():
        return None
    settings = cp.read_json(path, 4096)
    if (not isinstance(settings, dict) or set(settings) != {"schema", "client_path", "selector_path", "base_url"}
            or settings["schema"] != "3can.runtimehook-writeback/v1"
            or any(not isinstance(v, str) or not v.strip() for v in settings.values())):
        raise WritebackError("INVALID_WRITEBACK_CONFIG")
    return settings


def _client(settings, root):
    path = Path(settings["client_path"])
    if not path.is_absolute() or not path.is_file():
        raise WritebackError("CANONICAL_CLIENT_UNAVAILABLE")
    spec = importlib.util.spec_from_file_location("runtimehook_canonical_client", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.PROJECT_ROOT = root
    return module


def context(client, root):
    try:
        value = client._execution_context(root)
    except RuntimeError:
        raise WritebackError("PROJECT_CAPSULE_INVALID") from None
    if not all(value.get(key) for key in ("project_id", "project_namespace", "workspace_id")):
        raise WritebackError("PROJECT_CAPSULE_REQUIRED")
    # No inherited Workorder from another shell/task.
    return {key: value[key] for key in ("project_id", "project_namespace", "workspace_id")}


def connection(settings, root, *, agent_id, workorder_id, node_id):
    for value in (agent_id, workorder_id, node_id):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,191}", value):
            raise WritebackError("WRITEBACK_ID_REQUIRED")
    if agent_id in {"unknown", "codex-main"} or node_id.startswith(("ERR-", "FIX-", "EVD-")):
        raise WritebackError("CANONICAL_ERROR_LIFECYCLE_REQUIRED")
    client = _client(settings, root)
    return {**context(client, root), "agent_id": agent_id,
            "workorder_id": workorder_id, "node_id": node_id}


def _safe_text(client, text, limit):
    if not isinstance(text, str) or not text.strip() or len(text) > limit:
        raise WritebackError("WRITEBACK_TEXT_INVALID")
    # Reuse the client sanitizer; omit credentials and private home paths.
    text = client._redact_sensitive_text(text)
    if re.search(r"(?i)(?:sk-[A-Za-z0-9_-]{12,}|vck_[A-Za-z0-9_-]{12,}|-----BEGIN .*PRIVATE KEY)", text):
        raise WritebackError("WRITEBACK_SECRET_REJECTED")
    return text


def deliver(settings, root, binding, event):
    """Append one stable semantic delta with CAS and exact readback.

    No automatic retry or graph creation. Failed delivery retains this exact
    sanitized packet in the caller's receipt; explicit replay uses its marker.
    """
    started = time.monotonic()
    receipt = {"status": "UNAVAILABLE", "local_work_blocked": False}
    client = None
    verified_runtime = False
    base = ""
    try:
        client = _client(settings, root)
        identity = context(client, root)
        if any(binding.get(k) != v for k, v in identity.items()):
            raise WritebackError("PROJECT_WORKSPACE_CHANGED")
        if binding["node_id"].startswith(("ERR-", "FIX-", "EVD-")):
            raise WritebackError("CANONICAL_ERROR_LIFECYCLE_REQUIRED")
        base = settings["base_url"].rstrip("/")
        url = urlparse(base)
        # This adapter is a local-machine bridge, never an arbitrary uploader.
        if url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"} or url.username or url.password or url.query or url.fragment or url.path:
            raise WritebackError("LOCAL_3CAN_URL_REQUIRED")
        selector = cp.read_json(Path(settings["selector_path"]))
        engine, graph = Path(selector["engine_root"]), Path(selector["graph_root"])
        gate = client._project_identity_gate(base, {"selected": str(engine)}, require_configured=True)
        if gate["status"] != "pass":
            raise WritebackError("PROJECT_IDENTITY_MISMATCH")
        deadline = started + 10

        def request(path, payload=None):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WritebackError("WRITEBACK_DEADLINE")
            ok, result = client._try_json_request(base, path, method="POST" if payload is not None else "GET",
                                                  payload=payload, timeout=min(2, remaining))
            if not ok:
                code = result.get("http_status") if isinstance(result, dict) else None
                raise WritebackError("CONFLICT" if code == 409 else f"HTTP_{code}" if code else "RUNTIME_UNAVAILABLE")
            return result

        packet = {**binding, "schema": "3can.semantic-delta/v1", "event": event["event"],
                  "activation_id": event["activation_id"], "scope": event.get("scope", "main"),
                  "checkpoint": event.get("checkpoint"), "result": event["result"],
                  "summary": _safe_text(client, event["summary"], 2000),
                  "reference": _safe_text(client, event["reference"], 2000),
                  "next_objective": _safe_text(client, event["next_objective"], 2000) if event.get("next_objective") else None}
        if event["event"] not in {"onboarding", "milestone", "error"}:
            raise WritebackError("UNKNOWN_WRITEBACK_EVENT")
        if event["event"] == "error":
            error = event["error"]
            if error["state"] not in {"observed", "investigating", "mitigated", "resolution_claimed"}:
                raise WritebackError("VERIFIED_RESOLUTION_REQUIRES_CANONICAL_DONE")
            packet["error"] = {"id": _safe_text(client, error["id"], 191), "state": error["state"]}
        marker = "RUNTIMEHOOK_DELTA_" + cp.digest(packet)
        entry = "\n\n" + marker + "\n" + json.dumps(packet, ensure_ascii=False, sort_keys=True)
        receipt.update(event_id=marker, packet=packet)
        stats = request("/api/stats?deep=true")
        valid, _ = client._validate_stats(stats, min_nodes=0, expected_engine_root=engine, expected_graph_root=graph)
        if not valid:
            raise WritebackError("RUNTIME_IDENTITY_OR_READINESS_UNVERIFIED")
        verified_runtime = True
        node_path = "/api/nodes/" + quote(binding["node_id"], safe="")
        node = request(node_path)
        extra = (node.get("content") or {}).get("extra") or {}
        if any(extra.get(k) != identity[k] for k in ("project_id", "project_namespace")):
            raise WritebackError("NODE_PROJECT_BINDING_UNVERIFIED")
        notes = node.get("content", {}).get("notes") or ""
        if marker in notes:
            if entry.strip() not in notes:
                raise WritebackError("DELTA_MARKER_CONFLICT")
            receipt["status"] = "ALREADY_RECORDED"
            return receipt
        if len((notes + entry).encode("utf-8")) > 256 * 1024:
            raise WritebackError("KNOWLEDGE_NODE_COMPACTION_REQUIRED")
        if event["event"] == "onboarding":
            checked = request("/api/agents/checkin", {"agent_id": binding["agent_id"],
                "name": "RuntimeHook client", "role": "development", "current_task": packet["summary"][:400],
                "session_id": event["session_id"], "meta": {"project_identity": identity, "workorder_id": binding["workorder_id"]}})
            if checked.get("agent_id") != binding["agent_id"]:
                raise WritebackError("CHECKIN_NOT_CONFIRMED")
        result = request("/api/writeback", {**identity, "workorder_id": binding["workorder_id"],
            "agent_id": binding["agent_id"], "authorized_by": "user", "verification_state": "observed",
            "evidence_refs": [packet["reference"]], "changes": [{"node_id": binding["node_id"],
                "field": "notes", "action": "set", "value": notes + entry,
                "expected_updated_at": node["updated_at"]}]})
        if result.get("count") != 1 or result.get("updated") != [binding["node_id"]]:
            raise WritebackError("WRITEBACK_EFFECT_NOT_CONFIRMED")
        readback = request(node_path)
        if entry.strip() not in (readback.get("content", {}).get("notes") or ""):
            raise WritebackError("WRITEBACK_READBACK_MISMATCH")
        receipt.update(status="WRITTEN_AND_READBACK_VERIFIED", updated_at=readback.get("updated_at"))
    except (KeyError, TypeError, ValueError, OSError) as exc:
        # Do not echo provider bodies, local paths or arbitrary exception text.
        code = str(exc) if isinstance(exc, WritebackError) else "WRITEBACK_CONTEXT_UNAVAILABLE"
        receipt.update(status="CONFLICT" if code == "CONFLICT" else "UNAVAILABLE", error_code=code)
        if verified_runtime and code.startswith(("HTTP_", "CONFLICT", "WRITEBACK_")):
            # One sanitized observation; its own failure never recurses.
            ok, result = client._try_json_request(base, "/api/activity/log", method="POST", timeout=1, payload={
                "agent_id": binding["agent_id"], "action": "3can_issue_observed", "detail": code,
                "affected_nodes": ["DOC-3can-issue-intake-v1"],
                "meta": {**binding, "category": "writeback", "severity": "warning", "error_code": code,
                         "evidence_ref": receipt.get("event_id"), "operation": "runtimehook_auto_writeback"}})
            receipt["issue_intake"] = "RECORDED" if ok and isinstance(result, dict) and result.get("self_hash") else "UNAVAILABLE"
    finally:
        receipt["elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
    return receipt
