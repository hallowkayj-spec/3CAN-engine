"""Versioned, advisory ErrorFamily assignments for ErrorKnowledge retrieval.

Families are a derived route index. They never merge ErrorCase nodes, copy
solutions across cases, or become a blocking identity.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping


CANDIDATE_SCHEMA = "3can.error-family-candidates/v1"
DECISION_SCHEMA = "3can.error-family-decisions/v1"
ACTIVE_SCHEMA = "3can.error-family-active/v1"
ASSIGNMENT_SCHEMA = "3can.error-family-assignment/v1"
UNKNOWN_VALUES = frozenset(
    {"", "unknown", "unknown-operation", "unknown-component", "unknown-error"}
)


def canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def sha256_payload(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def normalize_identity_value(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().casefold())


def error_case_identity(node: Mapping[str, Any]) -> dict[str, str]:
    content = node.get("content")
    content = content if isinstance(content, Mapping) else {}
    extra = content.get("extra")
    extra = extra if isinstance(extra, Mapping) else {}
    canonical = extra.get("error_case")
    canonical = canonical if isinstance(canonical, Mapping) else {}
    identity = {
        key: normalize_identity_value(canonical.get(key) or extra.get(key))
        for key in ("project_id", "operation", "component", "error_type")
    }
    return identity


def identity_is_complete(identity: Mapping[str, str]) -> bool:
    return all(
        value and value not in UNKNOWN_VALUES
        for value in (
            identity.get("project_id", ""),
            identity.get("component", ""),
            identity.get("error_type", ""),
        )
    )


def identity_sha256(node_id: str, identity: Mapping[str, str]) -> str:
    return sha256_payload(
        {
            "node_id": str(node_id),
            "project_id": identity.get("project_id", ""),
            "operation": identity.get("operation", ""),
            "component": identity.get("component", ""),
            "error_type": identity.get("error_type", ""),
        }
    )


def deterministic_family_id(identity: Mapping[str, str]) -> str:
    if not identity_is_complete(identity):
        raise ValueError("error family requires project_id, component, and error_type")
    digest = sha256_payload(
        {
            "project_id": identity["project_id"],
            "component": identity["component"],
            "error_type": identity["error_type"],
        }
    )
    return f"ERF-{digest[:24]}"


def default_aliases(identity: Mapping[str, str]) -> list[str]:
    values = []
    for key in ("component", "error_type", "operation"):
        value = identity.get(key, "")
        if not value or value in UNKNOWN_VALUES:
            continue
        alias = re.sub(r"[-_:]+", " ", value)
        alias = re.sub(r"\s+", " ", alias).strip()
        if len(alias) >= 3:
            values.append(alias)
    combined = " ".join(
        value
        for value in (
            re.sub(r"[-_:]+", " ", identity.get("component", "")),
            re.sub(r"[-_:]+", " ", identity.get("error_type", "")),
        )
        if value
    ).strip()
    if combined:
        values.append(re.sub(r"\s+", " ", combined))
    return list(dict.fromkeys(values))[:8]


def _node_payload(node: Any) -> Mapping[str, Any]:
    if isinstance(node, Mapping):
        return node
    dump = getattr(node, "model_dump", None)
    if callable(dump):
        value = dump(mode="json")
        if isinstance(value, Mapping):
            return value
    return {}


def validate_active_manifest(
    payload: Any,
    nodes: Mapping[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    if not isinstance(payload, Mapping) or payload.get("schema_version") != ACTIVE_SCHEMA:
        return {}, {"status": "invalid", "reason": "active_schema_invalid"}
    assignments = payload.get("assignments")
    if not isinstance(assignments, list):
        return {}, {"status": "invalid", "reason": "assignments_invalid"}
    accepted: dict[str, dict[str, Any]] = {}
    for item in assignments:
        if (
            not isinstance(item, Mapping)
            or item.get("schema_version") != ASSIGNMENT_SCHEMA
        ):
            return {}, {"status": "invalid", "reason": "assignment_schema_invalid"}
        node_id = str(item.get("case_id") or "").strip()
        node = nodes.get(node_id)
        if not node or node_id in accepted:
            return {}, {"status": "invalid", "reason": "assignment_node_invalid"}
        identity = error_case_identity(_node_payload(node))
        try:
            expected_family_id = deterministic_family_id(identity)
        except ValueError:
            return {}, {"status": "invalid", "reason": "assignment_identity_incomplete"}
        aliases = item.get("aliases")
        reviewed_aliases = item.get("reviewed_aliases", [])
        if (
            str(item.get("family_id") or "") != expected_family_id
            or str(item.get("identity_sha256") or "")
            != identity_sha256(node_id, identity)
            or not isinstance(aliases, list)
            or not isinstance(reviewed_aliases, list)
            or any(
                not isinstance(alias, str)
                or not alias.strip()
                or len(alias.strip()) > 120
                for alias in [*aliases, *reviewed_aliases]
            )
        ):
            return {}, {"status": "invalid", "reason": "assignment_binding_invalid"}
        clean_aliases = list(dict.fromkeys(alias.strip() for alias in aliases))[:12]
        clean_reviewed_aliases = list(
            dict.fromkeys(alias.strip() for alias in reviewed_aliases)
        )[:12]
        if not set(clean_reviewed_aliases).issubset(clean_aliases):
            return {}, {"status": "invalid", "reason": "reviewed_alias_not_declared"}
        accepted[node_id] = {
            "family_id": expected_family_id,
            "aliases": clean_aliases,
            "reviewed_aliases": clean_reviewed_aliases,
        }
    return accepted, {
        "status": "verified",
        "reason": "ok",
        "manifest_id": str(payload.get("manifest_id") or ""),
        "assignment_count": len(accepted),
        "family_count": len(
            {assignment["family_id"] for assignment in accepted.values()}
        ),
        "sha256": sha256_payload(payload),
    }


def load_active_manifest(
    path: Path,
    nodes: Mapping[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    if not path.is_file():
        return {}, {"status": "not_configured", "reason": "active_manifest_absent"}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}, {"status": "invalid", "reason": "active_manifest_unreadable"}
    return validate_active_manifest(payload, nodes)


__all__ = [
    "ACTIVE_SCHEMA",
    "ASSIGNMENT_SCHEMA",
    "CANDIDATE_SCHEMA",
    "DECISION_SCHEMA",
    "canonical_json_bytes",
    "default_aliases",
    "deterministic_family_id",
    "error_case_identity",
    "identity_is_complete",
    "identity_sha256",
    "load_active_manifest",
    "sha256_payload",
    "validate_active_manifest",
]
