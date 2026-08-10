"""Fail-closed production readiness evaluation for a loaded 3CAN graph."""

from __future__ import annotations

import datetime as dt
import copy
import hashlib
import json
import math
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping


READINESS_SCHEMA = "3can.production-readiness/v1"
PROFILE_SCHEMA = "3can.graph-readiness-profile/v1"
WAIVER_SCHEMA = "3can.migration-waiver/v1"
RUNTIME_IDENTITY_SCHEMA = "3can.runtime-identity/v1"
DEFAULT_PROFILE_NAME = "readiness-profile.json"
READINESS_PROFILE_REBUILD_MARKER_NAME = "readiness-profile.rebuild_required.json"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
EMBEDDING_CONTRACT_FIELDS = frozenset({
    "requested_backend",
    "active_backend_id",
    "source_manifest_sha256",
    "cache_sha256",
    "meta_sha256",
    "row_count",
    "dimension",
})
HASHING_BACKEND_ID = "hashing-blake2b-char-ngram-v1"
EMBEDDING_DIMENSION = 1024
_BGE_M3_ACTIVE_BACKEND_RE = re.compile(
    r"sentence-transformers:BAAI/bge-m3@([0-9a-f]{7,64}):maxseq=([0-9]{2,4})"
)
READINESS_MODE_DEVELOPMENT = "development"
READINESS_MODE_PRODUCTION = "production"
DEFAULT_READINESS_CACHE_TTL_SECONDS = 30.0
MAX_READINESS_CACHE_TTL_SECONDS = 60.0


def runtime_path_sha256(path: Path) -> str:
    canonical = os.path.normcase(
        str(Path(path).expanduser().resolve(strict=False))
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def configured_profile_path(graph_root: Path) -> Path:
    configured = os.environ.get("THREECAN_GRAPH_PROFILE", "").strip()
    if configured:
        return Path(configured).expanduser().resolve(strict=False)
    return (Path(graph_root) / DEFAULT_PROFILE_NAME).resolve(strict=False)


def configured_readiness_mode() -> str:
    return os.environ.get(
        "THREECAN_READINESS_MODE",
        READINESS_MODE_PRODUCTION,
    ).strip().lower()


def configured_profile_sha256_pin() -> str:
    return os.environ.get("THREECAN_GRAPH_PROFILE_SHA256", "").strip().lower()


def configured_cache_ttl_seconds() -> float:
    raw = os.environ.get("THREECAN_READINESS_CACHE_TTL_SECONDS", "").strip()
    try:
        value = float(raw) if raw else DEFAULT_READINESS_CACHE_TTL_SECONDS
    except ValueError:
        value = DEFAULT_READINESS_CACHE_TTL_SECONDS
    if not math.isfinite(value):
        value = DEFAULT_READINESS_CACHE_TTL_SECONDS
    return min(MAX_READINESS_CACHE_TTL_SECONDS, max(0.1, value))


def approved_migration_waiver_ids() -> set[str]:
    raw = os.environ.get("THREECAN_APPROVED_MIGRATION_WAIVERS", "")
    return {item.strip() for item in raw.split(",") if item.strip()}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _integer(value: Any, *, minimum: int = 0) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        return None
    return value


def _edge_value(edge: Any, field: str) -> str:
    if isinstance(edge, Mapping):
        value = edge.get(field)
    else:
        value = getattr(edge, field, None)
    if hasattr(value, "value"):
        value = value.value
    return str(value or "").strip()


def _normalized_json_value(value: Any) -> Any:
    """Return a stable JSON-compatible representation without leaking paths."""

    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            value = model_dump(mode="json")
        except TypeError:
            value = model_dump()
    if isinstance(value, Mapping):
        return {
            str(key): _normalized_json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_normalized_json_value(item) for item in value]
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if hasattr(value, "value"):
        return _normalized_json_value(value.value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _normalized_payload_like_loaded(raw: Any, loaded: Any) -> Any:
    """Normalize raw storage through the loaded model schema when available."""

    if isinstance(raw, Mapping) and callable(getattr(loaded, "model_dump", None)):
        model_type = type(loaded)
        try:
            validator = getattr(model_type, "model_validate", None)
            parsed = validator(raw) if callable(validator) else model_type(**raw)
            return _normalized_json_value(parsed)
        except Exception:
            return _normalized_json_value(raw)
    return _normalized_json_value(raw)


def _stat_signature(path: Path) -> tuple[Any, ...]:
    try:
        stat_result = path.stat()
    except OSError:
        return (False, 0, 0, 0, 0, 0, 0)
    return (
        True,
        int(stat_result.st_mode),
        int(stat_result.st_size),
        int(stat_result.st_mtime_ns),
        int(stat_result.st_ctime_ns),
        int(getattr(stat_result, "st_dev", 0)),
        int(getattr(stat_result, "st_ino", 0)),
    )


def _rebuild_marker_signature(graph_root: Path) -> tuple[Any, ...]:
    marker = Path(graph_root) / READINESS_PROFILE_REBUILD_MARKER_NAME
    try:
        stat_result = marker.lstat()
    except FileNotFoundError:
        return ("missing",)
    except OSError as exc:
        return ("check_error", int(exc.errno or 0))
    return (
        "present",
        int(stat_result.st_mode),
        int(stat_result.st_size),
        int(stat_result.st_mtime_ns),
    )


def _node_file_stat_signature(nodes_dir: Path) -> tuple[Any, ...]:
    """Return a deterministic, content-change-sensitive node directory key.

    ``nodes_dir.stat()`` only changes when directory entries change. Rewriting
    an existing node keeps that directory signature stable, so cache keys must
    include each JSON entry's own size and nanosecond mtime. ``os.scandir``
    keeps this O(N) pass lightweight and records failures instead of silently
    treating an unreadable entry as unchanged.
    """

    try:
        with os.scandir(nodes_dir) as entries:
            candidates = sorted(
                (entry for entry in entries if entry.name.endswith(".json")),
                key=lambda entry: entry.name,
            )
    except OSError as exc:
        return ("directory_scan_error", int(exc.errno or 0))

    signature: list[tuple[Any, ...]] = []
    for entry in candidates:
        try:
            stat_result = entry.stat(follow_symlinks=True)
        except OSError as exc:
            signature.append((entry.name, "stat_error", int(exc.errno or 0)))
            continue
        signature.append(
            (
                entry.name,
                "ok",
                int(stat_result.st_mode),
                int(stat_result.st_size),
                int(stat_result.st_mtime_ns),
            )
        )
    return ("ok", len(signature), tuple(signature))


def _embedding_evidence(status: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(status, Mapping):
        status = {}
    check = str(status.get("source_manifest_check") or "missing").strip().lower()
    if check not in {"verified", "failed", "not_requested", "missing"}:
        check = "unknown"
    source_match = status.get("source_manifest_match")
    synchronized = status.get("cache_synchronized")
    deep_check = str(status.get("deep_cache_check") or "missing").strip().lower()
    if deep_check not in {"verified", "failed", "not_requested", "missing"}:
        deep_check = "unknown"

    def public_sha(field: str) -> str:
        value = status.get(field)
        return value if isinstance(value, str) and _SHA256_RE.fullmatch(value) else ""

    def public_bool(field: str) -> bool | None:
        value = status.get(field)
        return value if isinstance(value, bool) else None

    return {
        "requested_backend": str(status.get("requested_backend") or ""),
        "active_backend_id": str(status.get("active_backend_id") or ""),
        "source_manifest_sha256": public_sha("source_manifest_sha256"),
        "cache_sha256": public_sha("cache_sha256"),
        "meta_sha256": public_sha("meta_sha256"),
        "row_count": _integer(status.get("row_count"), minimum=0),
        "dimension": _integer(status.get("dimension"), minimum=0),
        "degraded": public_bool("degraded"),
        "fallback_policy": str(status.get("fallback_policy") or ""),
        "model_revision": str(status.get("model_revision") or ""),
        "max_sequence_length": _integer(
            status.get("max_sequence_length"), minimum=0
        ),
        "cache_structurally_ready": (
            status.get("cache_structurally_ready") is True
        ),
        "deep_cache_check": deep_check,
        "all_rows_finite": public_bool("all_rows_finite"),
        "all_rows_nonzero": public_bool("all_rows_nonzero"),
        "all_rows_unit_norm": public_bool("all_rows_unit_norm"),
        "cache_ids_match": public_bool("cache_ids_match"),
        "cache_backend_match": public_bool("cache_backend_match"),
        "meta_backend_match": public_bool("meta_backend_match"),
        "source_manifest_check": check,
        "source_manifest_match": (
            source_match if isinstance(source_match, bool) else None
        ),
        "cache_synchronized": (
            synchronized if isinstance(synchronized, bool) else None
        ),
    }


def _validate_embedding_contract(
    profile: Mapping[str, Any],
    reasons: list[dict[str, Any]],
) -> dict[str, Any] | None:
    raw = profile.get("embedding_contract")
    if not isinstance(raw, Mapping):
        _reason(reasons, "embedding_contract_missing")
        return None
    actual_fields = set(raw)
    if actual_fields != EMBEDDING_CONTRACT_FIELDS:
        _reason(
            reasons,
            "embedding_contract_schema_invalid",
            missing_fields=sorted(EMBEDDING_CONTRACT_FIELDS - actual_fields),
            unexpected_fields=sorted(actual_fields - EMBEDDING_CONTRACT_FIELDS),
        )
        return None

    requested_backend = raw.get("requested_backend")
    active_backend_id = raw.get("active_backend_id")
    valid = True
    if requested_backend not in {"hashing", "bge-m3"}:
        _reason(reasons, "embedding_contract_requested_backend_invalid")
        valid = False
    if requested_backend == "hashing":
        if active_backend_id != HASHING_BACKEND_ID:
            _reason(reasons, "embedding_contract_active_backend_id_invalid")
            valid = False
    elif requested_backend == "bge-m3":
        match = (
            _BGE_M3_ACTIVE_BACKEND_RE.fullmatch(active_backend_id)
            if isinstance(active_backend_id, str)
            else None
        )
        if match is None or not 64 <= int(match.group(2)) <= 8192:
            _reason(reasons, "embedding_contract_active_backend_id_invalid")
            valid = False

    for field in (
        "source_manifest_sha256",
        "cache_sha256",
        "meta_sha256",
    ):
        if not isinstance(raw.get(field), str) or not _SHA256_RE.fullmatch(
            raw[field]
        ):
            _reason(reasons, "embedding_contract_sha256_invalid", field=field)
            valid = False
    if _integer(raw.get("row_count"), minimum=1) is None:
        _reason(reasons, "embedding_contract_row_count_invalid")
        valid = False
    if raw.get("dimension") != EMBEDDING_DIMENSION:
        _reason(reasons, "embedding_contract_dimension_invalid")
        valid = False
    return dict(raw) if valid else None


def _require_verified_embedding(
    evidence: Mapping[str, Any],
    contract: Mapping[str, Any],
    reasons: list[dict[str, Any]],
) -> None:
    if evidence.get("cache_structurally_ready") is not True:
        _reason(reasons, "embedding_cache_not_ready")

    check = evidence.get("source_manifest_check")
    if check == "failed":
        _reason(reasons, "embedding_source_manifest_check_failed")
    elif check != "verified":
        _reason(reasons, "embedding_source_manifest_unverified")

    source_match = evidence.get("source_manifest_match")
    if source_match is False:
        _reason(reasons, "embedding_source_manifest_mismatch")
    elif source_match is not True:
        _reason(reasons, "embedding_source_manifest_match_missing")

    synchronized = evidence.get("cache_synchronized")
    if synchronized is False:
        _reason(reasons, "embedding_cache_not_synchronized")
    elif synchronized is not True:
        _reason(reasons, "embedding_cache_synchronization_unverified")

    deep_check = evidence.get("deep_cache_check")
    if deep_check == "failed":
        _reason(reasons, "embedding_deep_cache_check_failed")
    elif deep_check != "verified":
        _reason(reasons, "embedding_deep_cache_unverified")

    required_boolean_evidence = {
        "all_rows_finite": "embedding_rows_not_finite",
        "all_rows_nonzero": "embedding_rows_zero_norm",
        "all_rows_unit_norm": "embedding_rows_not_unit_normalized",
        "cache_ids_match": "embedding_cache_ids_mismatch",
        "cache_backend_match": "embedding_cache_backend_mismatch",
        "meta_backend_match": "embedding_meta_backend_mismatch",
    }
    for field, false_code in required_boolean_evidence.items():
        value = evidence.get(field)
        if value is False:
            _reason(reasons, false_code)
        elif value is not True:
            _reason(reasons, f"{false_code}_unverified")

    # Snapshot hashes and row counts change after valid memory writes. Their
    # live consistency is proven above; the profile pins only model identity.
    for field in ("requested_backend", "active_backend_id", "dimension"):
        if evidence.get(field) != contract.get(field):
            _reason(reasons, "embedding_contract_mismatch", field=field)

    requested_backend = contract.get("requested_backend")
    if requested_backend == "hashing":
        if (
            evidence.get("active_backend_id") != HASHING_BACKEND_ID
            or evidence.get("degraded") is not False
            or evidence.get("fallback_policy") != "not_applicable"
        ):
            _reason(reasons, "embedding_hashing_backend_contract_violated")
    elif requested_backend == "bge-m3":
        active_backend_id = str(contract.get("active_backend_id") or "")
        match = _BGE_M3_ACTIVE_BACKEND_RE.fullmatch(active_backend_id)
        expected_revision = match.group(1) if match is not None else ""
        expected_max_sequence_length = (
            int(match.group(2)) if match is not None else 0
        )
        if (
            evidence.get("active_backend_id") != active_backend_id
            or evidence.get("degraded") is not False
            or evidence.get("fallback_policy") != "error"
            or evidence.get("model_revision") != expected_revision
            or evidence.get("max_sequence_length")
            != expected_max_sequence_length
        ):
            _reason(reasons, "embedding_backend_fallback_forbidden")


def _safe_graph_path(graph_root: Path, relative: Any) -> Path | None:
    raw = str(relative or "").strip()
    if not raw:
        return None
    candidate_path = Path(raw)
    if candidate_path.is_absolute():
        return None
    root = Path(graph_root).resolve(strict=False)
    candidate = (root / candidate_path).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def _valid_approval_time(value: Any) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _reason(reasons: list[dict[str, Any]], code: str, **details: Any) -> None:
    reasons.append({"code": code, **details})


def _validate_migration_lineage(
    profile: Mapping[str, Any],
    *,
    graph_root: Path,
    baseline: Mapping[str, int],
    approved_waivers: set[str],
    reasons: list[dict[str, Any]],
) -> list[dict[str, str]]:
    raw_lineage = profile.get("migration_lineage")
    if not isinstance(raw_lineage, list):
        _reason(reasons, "migration_lineage_invalid")
        return []

    verified: list[dict[str, str]] = []
    manifests: dict[str, tuple[dict[str, Any], str]] = {}
    for entry in raw_lineage:
        if not isinstance(entry, Mapping):
            _reason(reasons, "migration_lineage_entry_invalid")
            continue
        run_id = str(entry.get("run_id") or "").strip()
        expected_hash = str(entry.get("manifest_sha256") or "").strip().lower()
        manifest_path = _safe_graph_path(graph_root, entry.get("manifest_path"))
        if not run_id or not _SHA256_RE.fullmatch(expected_hash) or manifest_path is None:
            _reason(reasons, "migration_lineage_entry_invalid", run_id=run_id)
            continue
        try:
            actual_hash = _sha256_file(manifest_path)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            _reason(reasons, "migration_manifest_unreadable", run_id=run_id)
            continue
        if actual_hash != expected_hash:
            _reason(reasons, "migration_manifest_hash_mismatch", run_id=run_id)
            continue
        if not isinstance(manifest, dict) or str(manifest.get("run_id") or "") != run_id:
            _reason(reasons, "migration_manifest_identity_mismatch", run_id=run_id)
            continue
        if manifest.get("applied") is not True or manifest.get("journal_phase") != "completed":
            _reason(reasons, "migration_manifest_not_completed", run_id=run_id)
            continue
        manifests[run_id] = (manifest, expected_hash)
        verified.append({"run_id": run_id, "manifest_sha256": expected_hash})

    previous = profile.get("previous_baseline")
    if previous is None:
        _reason(reasons, "previous_baseline_required")
        return verified
    if not isinstance(previous, Mapping):
        _reason(reasons, "previous_baseline_invalid")
        return verified
    previous_nodes = _integer(previous.get("valid_nodes"), minimum=1)
    previous_edges = _integer(previous.get("edges"), minimum=0)
    if previous_nodes is None or previous_edges is None:
        _reason(reasons, "previous_baseline_invalid")
        return verified
    regressed = (
        baseline["min_valid_nodes"] < previous_nodes
        or baseline["min_edges"] < previous_edges
    )
    if not regressed:
        return verified

    waiver = profile.get("migration_waiver")
    if not isinstance(waiver, Mapping):
        _reason(reasons, "migration_waiver_required")
        return verified
    waiver_id = str(waiver.get("waiver_id") or "").strip()
    run_id = str(waiver.get("run_id") or "").strip()
    if waiver.get("schema_version") != WAIVER_SCHEMA or not waiver_id or not run_id:
        _reason(reasons, "migration_waiver_invalid")
        return verified
    if waiver_id not in approved_waivers:
        _reason(reasons, "migration_waiver_not_approved", waiver_id=waiver_id)
    if not str(waiver.get("approved_by") or "").strip():
        _reason(reasons, "migration_waiver_approver_missing", waiver_id=waiver_id)
    if not str(waiver.get("reason") or "").strip():
        _reason(reasons, "migration_waiver_reason_missing", waiver_id=waiver_id)
    if not _valid_approval_time(waiver.get("approved_at")):
        _reason(reasons, "migration_waiver_time_invalid", waiver_id=waiver_id)

    manifest_record = manifests.get(run_id)
    if manifest_record is None:
        _reason(reasons, "migration_waiver_manifest_unverified", waiver_id=waiver_id)
        return verified
    manifest, manifest_hash = manifest_record
    if str(waiver.get("manifest_sha256") or "").lower() != manifest_hash:
        _reason(reasons, "migration_waiver_manifest_mismatch", waiver_id=waiver_id)

    before = manifest.get("before") if isinstance(manifest.get("before"), Mapping) else {}
    after = manifest.get("after") if isinstance(manifest.get("after"), Mapping) else {}
    expected = {
        "from_valid_nodes": previous_nodes,
        "from_edges": previous_edges,
        "to_valid_nodes": baseline["min_valid_nodes"],
        "to_edges": baseline["min_edges"],
    }
    actual = {
        "from_valid_nodes": _integer(before.get("node_count"), minimum=1),
        "from_edges": _integer(before.get("edge_count"), minimum=0),
        "to_valid_nodes": _integer(after.get("node_count"), minimum=1),
        "to_edges": _integer(after.get("edge_count"), minimum=0),
    }
    if actual != expected:
        _reason(reasons, "migration_waiver_baseline_mismatch", waiver_id=waiver_id)
    return verified


def evaluate_readiness(
    engine: Any,
    *,
    engine_root: Path,
    graph_root: Path,
    runtime_identity: Mapping[str, Any],
    embedding_status: Mapping[str, Any],
    profile_path: Path | None = None,
    approved_waiver_ids: Iterable[str] | None = None,
    readiness_mode: str | None = None,
    profile_sha256_pin: str | None = None,
) -> dict[str, Any]:
    """Return evidence-backed readiness; absence or ambiguity always fails."""

    reasons: list[dict[str, Any]] = []
    graph = Path(graph_root).resolve(strict=False)
    mode = (
        str(readiness_mode).strip().lower()
        if readiness_mode is not None
        else configured_readiness_mode()
    )
    configured_pin = (
        str(profile_sha256_pin).strip().lower()
        if profile_sha256_pin is not None
        else configured_profile_sha256_pin()
    )
    profile_file = (
        Path(profile_path).resolve(strict=False)
        if profile_path is not None
        else configured_profile_path(graph)
    )
    metrics: dict[str, Any] = {
        "valid_nodes": len(getattr(engine, "nodes", {})),
        "edges": len(getattr(engine, "edges", [])),
        "node_files": 0,
        "invalid_node_files": 0,
        "node_payload_mismatches": 0,
        "duplicate_edges": 0,
        "self_edges": 0,
        "malformed_edges": 0,
        "orphan_edges": 0,
    }
    result: dict[str, Any] = {
        "schema": READINESS_SCHEMA,
        "mode": mode,
        "ready": False,
        "production_ready": False,
        "development_ready": mode == READINESS_MODE_DEVELOPMENT,
        "profile_id": "",
        "profile_sha256": "",
        "metrics": metrics,
        "embedding_contract": {},
        "embedding_evidence": _embedding_evidence(embedding_status),
        "verified_migrations": [],
        "reasons": reasons,
    }

    if mode == READINESS_MODE_DEVELOPMENT:
        _reason(reasons, "development_mode_not_production")
        return result
    if mode != READINESS_MODE_PRODUCTION:
        result["development_ready"] = False
        _reason(reasons, "readiness_mode_invalid")
        return result
    rebuild_marker_state = _rebuild_marker_signature(graph)
    if rebuild_marker_state[0] == "present":
        _reason(reasons, "readiness_profile_rebuild_required")
        return result
    if rebuild_marker_state[0] == "check_error":
        _reason(reasons, "readiness_profile_rebuild_marker_check_failed")
        return result
    try:
        profile_bytes = profile_file.read_bytes()
        profile = json.loads(profile_bytes.decode("utf-8"))
    except FileNotFoundError:
        _reason(reasons, "readiness_profile_missing")
        return result
    except (OSError, UnicodeError, json.JSONDecodeError):
        _reason(reasons, "readiness_profile_unreadable")
        return result
    if not isinstance(profile, dict) or profile.get("schema_version") != PROFILE_SCHEMA:
        _reason(reasons, "readiness_profile_schema_invalid")
        return result

    result["profile_sha256"] = hashlib.sha256(profile_bytes).hexdigest()
    if not configured_pin:
        _reason(reasons, "readiness_profile_sha256_pin_missing")
        return result
    if not _SHA256_RE.fullmatch(configured_pin):
        _reason(reasons, "readiness_profile_sha256_pin_invalid")
        return result
    if result["profile_sha256"] != configured_pin:
        _reason(reasons, "readiness_profile_sha256_pin_mismatch")
        return result
    if profile.get("readiness_mode") != READINESS_MODE_PRODUCTION:
        _reason(reasons, "readiness_profile_mode_invalid")
        return result
    result["profile_id"] = str(profile.get("profile_id") or "").strip()
    if not result["profile_id"]:
        _reason(reasons, "readiness_profile_id_missing")

    expected_identity = profile.get("runtime_identity")
    actual_identity = dict(runtime_identity) if isinstance(runtime_identity, Mapping) else {}
    computed_identity = {
        "schema": RUNTIME_IDENTITY_SCHEMA,
        "engine_root_sha256": runtime_path_sha256(engine_root),
        "graph_root_sha256": runtime_path_sha256(graph),
    }
    if any(
        actual_identity.get(key) != value
        for key, value in computed_identity.items()
    ):
        _reason(reasons, "runtime_identity_self_check_failed")
    if not isinstance(expected_identity, Mapping) or any(
        expected_identity.get(key) != value for key, value in computed_identity.items()
    ):
        _reason(reasons, "runtime_identity_profile_mismatch")

    raw_baseline = profile.get("baseline")
    if not isinstance(raw_baseline, Mapping):
        _reason(reasons, "readiness_baseline_invalid")
        return result
    min_nodes = _integer(raw_baseline.get("min_valid_nodes"), minimum=1)
    min_edges = _integer(raw_baseline.get("min_edges"), minimum=0)
    sentinels = raw_baseline.get("sentinel_node_ids")
    require_embedding = raw_baseline.get("require_embedding_cache_ready")
    if (
        min_nodes is None
        or min_edges is None
        or not isinstance(sentinels, list)
        or not all(isinstance(item, str) and item.strip() for item in sentinels)
        or not isinstance(require_embedding, bool)
    ):
        _reason(reasons, "readiness_baseline_invalid")
        return result
    baseline = {"min_valid_nodes": min_nodes, "min_edges": min_edges}

    if require_embedding is not True:
        _reason(reasons, "production_embedding_cache_required")
        return result
    embedding_contract = _validate_embedding_contract(profile, reasons)
    if embedding_contract is None:
        return result
    result["embedding_contract"] = embedding_contract
    _require_verified_embedding(
        result["embedding_evidence"],
        embedding_contract,
        reasons,
    )
    if reasons:
        return result

    loaded_nodes = getattr(engine, "nodes", {})
    if not isinstance(loaded_nodes, Mapping):
        loaded_nodes = {}
        _reason(reasons, "loaded_node_store_invalid")
    node_ids = set(loaded_nodes.keys())
    nodes_dir = graph / "nodes"
    try:
        node_files = sorted(nodes_dir.glob("*.json"), key=lambda item: item.name)
    except OSError:
        node_files = []
        _reason(reasons, "node_store_unreadable")
    metrics["node_files"] = len(node_files)
    invalid_node_files = 0
    node_payload_mismatches = 0
    disk_node_ids: list[str] = []
    disk_node_ids_exact: set[str] = set()
    disk_node_ids_casefolded: set[str] = set()
    for node_file in node_files:
        try:
            node_payload = json.loads(node_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            invalid_node_files += 1
            continue
        if not isinstance(node_payload, Mapping):
            invalid_node_files += 1
            continue
        node_id = str(node_payload.get("id") or "").strip()
        folded_id = node_id.casefold()
        if (
            not node_id
            or node_file.stem != node_id
            or node_id in disk_node_ids_exact
            or folded_id in disk_node_ids_casefolded
        ):
            invalid_node_files += 1
            continue
        disk_node_ids.append(node_id)
        disk_node_ids_exact.add(node_id)
        disk_node_ids_casefolded.add(folded_id)
        loaded_node = loaded_nodes.get(node_id)
        if loaded_node is None:
            continue
        if callable(getattr(loaded_node, "model_dump", None)):
            if (
                _normalized_payload_like_loaded(node_payload, loaded_node)
                != _normalized_json_value(loaded_node)
            ):
                node_payload_mismatches += 1
    metrics["invalid_node_files"] = invalid_node_files
    metrics["node_payload_mismatches"] = node_payload_mismatches
    if invalid_node_files:
        _reason(reasons, "node_files_invalid", count=invalid_node_files)
    if node_payload_mismatches:
        _reason(
            reasons,
            "node_payloads_differ_from_loaded_graph",
            count=node_payload_mismatches,
        )
    disk_node_set = set(disk_node_ids)
    if disk_node_set != node_ids:
        _reason(
            reasons,
            "node_file_set_not_fully_loaded",
            missing_from_disk=len(node_ids - disk_node_set),
            missing_from_loaded=len(disk_node_set - node_ids),
        )
    if len(node_ids) < min_nodes:
        _reason(reasons, "node_baseline_not_met", minimum=min_nodes)
    missing_sentinels = sorted(set(sentinels) - node_ids)
    if missing_sentinels:
        _reason(reasons, "sentinel_nodes_missing", count=len(missing_sentinels))

    edges_file = graph / "edges.json"
    try:
        raw_edges = json.loads(edges_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raw_edges = None
        _reason(reasons, "edges_file_unreadable")
    if not isinstance(raw_edges, list):
        _reason(reasons, "edges_payload_invalid")
    elif len(raw_edges) != metrics["edges"]:
        _reason(reasons, "edge_file_set_not_fully_loaded")
    if metrics["edges"] < min_edges:
        _reason(reasons, "edge_baseline_not_met", minimum=min_edges)
    loaded_edges = list(getattr(engine, "edges", []))
    raw_edge_list = raw_edges if isinstance(raw_edges, list) else []
    loaded_payloads = [_normalized_json_value(edge) for edge in loaded_edges]
    raw_payloads = [
        _normalized_payload_like_loaded(
            edge,
            loaded_edges[index] if index < len(loaded_edges) else None,
        )
        for index, edge in enumerate(raw_edge_list)
    ]
    if isinstance(raw_edges, list) and raw_payloads != loaded_payloads:
        _reason(reasons, "edge_file_set_not_fully_loaded")
    malformed_edges = 0
    orphan_edges = 0
    self_edges = 0
    duplicate_edges = 0
    seen_edge_keys: set[tuple[str, str, str]] = set()
    for edge in raw_edge_list:
        source = _edge_value(edge, "source")
        target = _edge_value(edge, "target")
        edge_type = _edge_value(edge, "type")
        if not source or not target or not edge_type:
            malformed_edges += 1
        elif source == target:
            self_edges += 1
        elif source not in node_ids or target not in node_ids:
            orphan_edges += 1
        edge_key = (source, target, edge_type)
        if all(edge_key):
            if edge_key in seen_edge_keys:
                duplicate_edges += 1
            seen_edge_keys.add(edge_key)
    metrics["malformed_edges"] = malformed_edges
    metrics["orphan_edges"] = orphan_edges
    metrics["self_edges"] = self_edges
    metrics["duplicate_edges"] = duplicate_edges
    if malformed_edges:
        _reason(reasons, "malformed_edges", count=malformed_edges)
    if orphan_edges:
        _reason(reasons, "orphan_edges", count=orphan_edges)
    if self_edges:
        _reason(reasons, "self_edges", count=self_edges)
    if duplicate_edges:
        _reason(reasons, "duplicate_edges", count=duplicate_edges)

    result["verified_migrations"] = _validate_migration_lineage(
        profile,
        graph_root=graph,
        baseline=baseline,
        approved_waivers=(
            set(approved_waiver_ids)
            if approved_waiver_ids is not None
            else approved_migration_waiver_ids()
        ),
        reasons=reasons,
    )
    result["ready"] = not reasons
    result["production_ready"] = result["ready"]
    return result


class ReadinessCache:
    """Bound deep graph verification while retaining explicit refresh semantics."""

    def __init__(
        self,
        *,
        ttl_seconds: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        configured_ttl = (
            configured_cache_ttl_seconds()
            if ttl_seconds is None
            else float(ttl_seconds)
        )
        if not math.isfinite(configured_ttl):
            configured_ttl = DEFAULT_READINESS_CACHE_TTL_SECONDS
        self.ttl_seconds = min(
            MAX_READINESS_CACHE_TTL_SECONDS,
            max(0.1, configured_ttl),
        )
        self._clock = clock
        self._lock = threading.Lock()
        self._fingerprint: tuple[Any, ...] | None = None
        self._created_at = 0.0
        self._result: dict[str, Any] | None = None

    @staticmethod
    def _has_verified_deep_evidence(result: Mapping[str, Any] | None) -> bool:
        evidence = (
            result.get("embedding_evidence", {})
            if isinstance(result, Mapping)
            else {}
        )
        return bool(
            result
            and result.get("production_ready") is True
            and evidence.get("source_manifest_check") == "verified"
            and evidence.get("deep_cache_check") == "verified"
        )

    @staticmethod
    def _fingerprint_for(
        engine: Any,
        *,
        engine_root: Path,
        graph_root: Path,
        embedding_status: Mapping[str, Any],
        profile_path: Path | None,
        approved_waiver_ids: Iterable[str] | None,
        readiness_mode: str | None,
        profile_sha256_pin: str | None,
    ) -> tuple[Any, ...]:
        graph = Path(graph_root).resolve(strict=False)
        profile = (
            Path(profile_path).resolve(strict=False)
            if profile_path is not None
            else configured_profile_path(graph)
        )
        mode = (
            str(readiness_mode).strip().lower()
            if readiness_mode is not None
            else configured_readiness_mode()
        )
        pin = (
            str(profile_sha256_pin).strip().lower()
            if profile_sha256_pin is not None
            else configured_profile_sha256_pin()
        )
        waiver_ids = tuple(sorted(
            set(approved_waiver_ids)
            if approved_waiver_ids is not None
            else approved_migration_waiver_ids()
        ))
        return (
            mode,
            pin,
            waiver_ids,
            runtime_path_sha256(engine_root),
            runtime_path_sha256(graph),
            _rebuild_marker_signature(graph),
            str(profile),
            _stat_signature(profile),
            _node_file_stat_signature(graph / "nodes"),
            _stat_signature(graph / "edges.json"),
            _stat_signature(graph / "embeddings.npz"),
            _stat_signature(graph / "embeddings.meta.json"),
            len(getattr(engine, "nodes", {})),
            len(getattr(engine, "edges", [])),
            bool(embedding_status.get("cache_structurally_ready")),
            str(embedding_status.get("requested_backend") or ""),
            str(
                embedding_status.get("active_backend_id")
                or embedding_status.get("active_backend")
                or ""
            ),
            str(embedding_status.get("cache_backend_id") or ""),
            str(embedding_status.get("cache_source_manifest") or ""),
            _integer(embedding_status.get("matrix_rows"), minimum=0) or 0,
            _integer(embedding_status.get("matrix_dimension"), minimum=0) or 0,
            str(embedding_status.get("model_revision") or ""),
            _integer(
                embedding_status.get("max_sequence_length"), minimum=0
            )
            or 0,
            str(getattr(engine, "_embedding_cache_source_manifest", "")),
        )

    def snapshot(
        self,
        engine: Any,
        *,
        engine_root: Path,
        graph_root: Path,
        runtime_identity: Mapping[str, Any],
        embedding_status: Mapping[str, Any],
        profile_path: Path | None = None,
        approved_waiver_ids: Iterable[str] | None = None,
        readiness_mode: str | None = None,
        profile_sha256_pin: str | None = None,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            now = self._clock()
            fingerprint = self._fingerprint_for(
                engine,
                engine_root=engine_root,
                graph_root=graph_root,
                embedding_status=embedding_status,
                profile_path=profile_path,
                approved_waiver_ids=approved_waiver_ids,
                readiness_mode=readiness_mode,
                profile_sha256_pin=profile_sha256_pin,
            )
            same_snapshot = bool(
                not force_refresh
                and self._result is not None
                and self._fingerprint == fingerprint
            )
            evidence_age_seconds = max(0.0, now - self._created_at)
            cache_hit = bool(
                same_snapshot and evidence_age_seconds < self.ttl_seconds
            )
            reuse_verified = bool(
                same_snapshot
                and self._has_verified_deep_evidence(self._result)
            )
            if cache_hit or reuse_verified:
                result = copy.deepcopy(self._result)
            else:
                result = evaluate_readiness(
                    engine,
                    engine_root=engine_root,
                    graph_root=graph_root,
                    runtime_identity=runtime_identity,
                    embedding_status=embedding_status,
                    profile_path=profile_path,
                    approved_waiver_ids=approved_waiver_ids,
                    readiness_mode=readiness_mode,
                    profile_sha256_pin=profile_sha256_pin,
                )
                self._fingerprint = fingerprint
                self._created_at = now
                self._result = copy.deepcopy(result)
                evidence_age_seconds = 0.0
            has_verified_evidence = self._has_verified_deep_evidence(result)
            verification_state = (
                "development"
                if (
                    result.get("mode") == READINESS_MODE_DEVELOPMENT
                    and result.get("development_ready") is True
                )
                else "verified"
                if force_refresh and has_verified_evidence
                else "cached_verified"
                if cache_hit and has_verified_evidence
                else "stale_verified"
                if reuse_verified
                else "deep_required"
            )
            result["cache"] = {
                "hit": cache_hit,
                "forced_refresh": bool(force_refresh),
                "reused_verified_deep_evidence": reuse_verified or (
                    cache_hit and has_verified_evidence
                ),
                "verification_state": verification_state,
                "deep_required": verification_state == "deep_required",
                "evidence_age_seconds": round(evidence_age_seconds, 3),
                "ttl_seconds": self.ttl_seconds,
            }
            return result

    def clear(self) -> None:
        with self._lock:
            self._fingerprint = None
            self._created_at = 0.0
            self._result = None
