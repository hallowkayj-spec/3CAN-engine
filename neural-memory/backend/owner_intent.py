"""Load one project's human-authored 3CAN.md steering defaults.

This module deliberately owns only a tiny flat front matter contract.  It does
not interpret the Markdown body, persist policy state, watch files, or decide
objective truth.  Callers receive a compact, project-bound projection.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from threading import Lock
from typing import Any


OWNER_INTENT_FILENAME = "3CAN.md"
OWNER_INTENT_SCHEMA = "3can.owner-intent/v1"
OWNER_INTENT_MAX_BYTES = 64 * 1024
OWNER_INTENT_PRECEDENCE = "current_explicit_owner_instruction_over_default"
OWNER_INTENT_DEFAULT_VALUES = {
    "caution": frozenset({"strict", "balanced", "pragmatic"}),
    "autonomy": frozenset({"guided", "bounded", "high"}),
    "external_changes": frozenset({"confirm", "reversible", "deny"}),
    "context": frozenset({"compact", "standard", "full"}),
    "history": frozenset({"minimal", "applicable", "explicit_only"}),
    "review": frozenset({"risk_based", "always", "owner_requested"}),
    "writeback": frozenset({"meaningful_only", "durable_only", "owner_confirmed"}),
}
OWNER_INTENT_PROJECTION_KEYS = frozenset(
    {
        "schema",
        "status",
        "source",
        "digest",
        "project_id",
        "project_namespace",
        "defaults",
        "precedence",
        "hard_gates_unchanged",
    }
)
_EXPECTED_KEYS = frozenset({"version", *OWNER_INTENT_DEFAULT_VALUES})
_cache: dict[Path, tuple[tuple[int, int], dict[str, Any]]] = {}
_cache_lock = Lock()


class OwnerIntentError(ValueError):
    """A present 3CAN.md or project capsule is invalid."""


def clear_owner_intent_cache() -> None:
    """Clear process-local parsed values (used by explicit reload/tests only)."""

    with _cache_lock:
        _cache.clear()


def _parse_front_matter(raw: bytes) -> dict[str, str]:
    if len(raw) > OWNER_INTENT_MAX_BYTES:
        raise OwnerIntentError("owner_intent_file_too_large")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise OwnerIntentError("owner_intent_must_be_utf8") from exc
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise OwnerIntentError("owner_intent_front_matter_required")
    try:
        end = next(
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.strip() == "---"
        )
    except StopIteration as exc:
        raise OwnerIntentError("owner_intent_front_matter_unclosed") from exc

    values: dict[str, str] = {}
    for line in lines[1:end]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if line != line.lstrip() or ":" not in line:
            raise OwnerIntentError("owner_intent_front_matter_must_be_flat")
        key, value = (part.strip() for part in line.split(":", 1))
        if key not in _EXPECTED_KEYS:
            raise OwnerIntentError(f"owner_intent_key_unsupported:{key}")
        if key in values:
            raise OwnerIntentError(f"owner_intent_key_duplicate:{key}")
        if not value:
            raise OwnerIntentError(f"owner_intent_value_missing:{key}")
        values[key] = value

    missing = sorted(_EXPECTED_KEYS - values.keys())
    if missing:
        raise OwnerIntentError(
            "owner_intent_keys_missing:" + ",".join(missing)
        )
    if values["version"] != "1":
        raise OwnerIntentError("owner_intent_version_unsupported")
    for key, allowed in OWNER_INTENT_DEFAULT_VALUES.items():
        if values[key] not in allowed:
            raise OwnerIntentError(f"owner_intent_value_unsupported:{key}")
    return {key: values[key] for key in OWNER_INTENT_DEFAULT_VALUES}


def _project_identity(project_root: Path) -> tuple[str, str]:
    capsule_path = project_root / ".agents" / "project.json"
    try:
        capsule = json.loads(capsule_path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise OwnerIntentError("owner_intent_project_capsule_missing") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OwnerIntentError("owner_intent_project_capsule_invalid") from exc
    if not isinstance(capsule, dict):
        raise OwnerIntentError("owner_intent_project_capsule_invalid")
    project_id = str(capsule.get("project_id") or "").strip()
    namespace = str(capsule.get("project_namespace") or "").strip()
    if not project_id or not namespace:
        raise OwnerIntentError("owner_intent_project_identity_incomplete")
    return project_id, namespace


def _parsed_document(path: Path) -> tuple[dict[str, str], str]:
    stat_result = path.stat()
    signature = (int(stat_result.st_mtime_ns), int(stat_result.st_size))
    with _cache_lock:
        cached = _cache.get(path)
        if cached and cached[0] == signature:
            payload = cached[1]
            return dict(payload["defaults"]), str(payload["digest"])
        raw = path.read_bytes()
        defaults = _parse_front_matter(raw)
        digest = f"sha256:{hashlib.sha256(raw).hexdigest()}"
        _cache[path] = (
            signature,
            {"defaults": dict(defaults), "digest": digest},
        )
        return defaults, digest


def load_owner_intent(
    project_root: str | Path,
    *,
    project_id: str = "",
    project_namespace: str = "",
) -> dict[str, Any] | None:
    """Return the compact projection for one exact project, or ``None``.

    A missing file preserves legacy behavior.  A present but invalid file fails
    clearly.  Explicitly requesting another project never receives this
    project's defaults.
    """

    root = Path(project_root).expanduser().resolve(strict=False)
    path = (root / OWNER_INTENT_FILENAME).resolve(strict=False)
    if not path.is_file():
        return None
    capsule_project, capsule_namespace = _project_identity(root)
    requested_project = str(project_id or "").strip()
    requested_namespace = str(project_namespace or "").strip()
    if bool(requested_project) != bool(requested_namespace):
        raise OwnerIntentError("owner_intent_project_identity_pair_required")
    if requested_project and (
        requested_project.casefold() != capsule_project.casefold()
        or requested_namespace.casefold() != capsule_namespace.casefold()
    ):
        return {
            "schema": OWNER_INTENT_SCHEMA,
            "status": "not_applicable",
            "source": OWNER_INTENT_FILENAME,
            "project_id": requested_project,
            "project_namespace": requested_namespace,
            "reason": "project_identity_mismatch",
        }

    defaults, digest = _parsed_document(path)
    return {
        "schema": OWNER_INTENT_SCHEMA,
        "status": "applied",
        "source": OWNER_INTENT_FILENAME,
        "digest": digest,
        "project_id": capsule_project,
        "project_namespace": capsule_namespace,
        "defaults": defaults,
        "precedence": OWNER_INTENT_PRECEDENCE,
        "hard_gates_unchanged": True,
    }
