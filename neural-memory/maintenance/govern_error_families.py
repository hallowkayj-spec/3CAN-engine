#!/usr/bin/env python3
"""Plan, activate, or roll back advisory ErrorFamily route aliases.

The active family manifest is a derived, atomic sidecar. ErrorCase nodes and
edges remain untouched; exact ek2 identity stays authoritative.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


_MAINTENANCE_DIR = Path(__file__).resolve().parent
_BACKEND_DIR = _MAINTENANCE_DIR.parent / "backend"
for _path in (_MAINTENANCE_DIR, _BACKEND_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from error_family import (  # noqa: E402
    ACTIVE_SCHEMA,
    ASSIGNMENT_SCHEMA,
    CANDIDATE_SCHEMA,
    DECISION_SCHEMA,
    default_aliases,
    deterministic_family_id,
    error_case_identity,
    identity_is_complete,
    identity_sha256,
    sha256_payload,
    validate_active_manifest,
)
from migrate_legacy_errors import (  # noqa: E402
    DEFAULT_ENGINE_PROBE_TIMEOUT_SEC,
    MigrationError,
    _atomic_write_bytes,
    _atomic_write_json,
    _load_graph,
    _mutation_lock,
    _read_json,
    _require_engine_quiescence,
    _sha256_bytes,
    _sha256_file,
    _snapshot_id,
)


RECEIPT_SCHEMA = "3can.error-family-activation-receipt/v1"
ROLLBACK_RECEIPT_SCHEMA = "3can.error-family-rollback-receipt/v1"
ERROR_CLUSTER = "ErrorKnowledge"


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _manifest_hash(payload: Mapping[str, Any]) -> str:
    return sha256_payload(payload)


def build_candidate_manifest(graph_dir: Path | str) -> dict[str, Any]:
    graph = Path(graph_dir).resolve()
    nodes, _paths, edges, corrupt = _load_graph(graph)
    if corrupt:
        raise MigrationError("error-family planning requires a corruption-free graph")
    candidates = []
    deferred = []
    error_ids = sorted(
        node_id
        for node_id, node in nodes.items()
        if node_id.casefold().startswith("err-")
        and str(node.get("cluster") or "") == ERROR_CLUSTER
    )
    for node_id in error_ids:
        identity = error_case_identity(nodes[node_id])
        if not identity_is_complete(identity):
            deferred.append(
                {
                    "case_id": node_id,
                    "status": "review_required",
                    "reason": "deterministic_family_identity_incomplete",
                    "missing_fields": [
                        key
                        for key in ("project_id", "component", "error_type")
                        if not identity.get(key)
                        or identity[key].startswith("unknown")
                    ],
                }
            )
            continue
        candidates.append(
            {
                "case_id": node_id,
                "status": "decision_required",
                "confidence_basis": "deterministic_project_component_error_type",
                "family_id": deterministic_family_id(identity),
                "identity": identity,
                "identity_sha256": identity_sha256(node_id, identity),
                "proposed_aliases": default_aliases(identity),
            }
        )
    snapshot_id = _snapshot_id(graph)
    manifest = {
        "schema_version": CANDIDATE_SCHEMA,
        "manifest_id": f"EKF-CAND-{snapshot_id[:24]}",
        "graph_dir_sha256": hashlib.sha256(str(graph).encode("utf-8")).hexdigest(),
        "graph_snapshot_id": snapshot_id,
        "edges_sha256": _sha256_file(graph / "edges.json"),
        "error_case_count": len(error_ids),
        "candidate_count": len(candidates),
        "deferred_count": len(deferred),
        "family_count": len({item["family_id"] for item in candidates}),
        "candidates": candidates,
        "deferred": deferred,
        "policy": {
            "automatic_semantic_merge": False,
            "similarity_role": "review_candidate_only",
            "solution_inheritance": False,
            "blocking_identity": "canonical_ek2_only",
        },
    }
    manifest["manifest_sha256"] = _manifest_hash(
        {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    )
    return manifest


def _validate_candidate_manifest(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("schema_version") != CANDIDATE_SCHEMA:
        raise MigrationError(f"candidate manifest must use {CANDIDATE_SCHEMA}")
    declared = str(payload.get("manifest_sha256") or "")
    actual = _manifest_hash(
        {key: value for key, value in payload.items() if key != "manifest_sha256"}
    )
    if declared != actual:
        raise MigrationError("candidate manifest checksum mismatch")
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        raise MigrationError("candidate manifest has no candidate list")
    return copy.deepcopy(payload)


def _clean_aliases(value: Any, *, field: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(alias, str)
        or not alias.strip()
        or len(alias.strip()) > 120
        or any(ord(character) < 32 for character in alias)
        for alias in value
    ):
        raise MigrationError(f"{field} must contain bounded printable strings")
    return list(dict.fromkeys(alias.strip() for alias in value))[:12]


def compile_active_manifest(
    graph_dir: Path | str,
    candidate_payload: Any,
    decision_payload: Any,
) -> dict[str, Any]:
    graph = Path(graph_dir).resolve()
    candidates = _validate_candidate_manifest(candidate_payload)
    if (
        not isinstance(decision_payload, dict)
        or decision_payload.get("schema_version") != DECISION_SCHEMA
        or decision_payload.get("candidate_manifest_sha256")
        != candidates["manifest_sha256"]
        or not str(decision_payload.get("decided_by") or "").strip()
        or not str(decision_payload.get("decided_at") or "").strip()
    ):
        raise MigrationError("decision manifest is missing approval or candidate binding")
    nodes, _paths, _edges, corrupt = _load_graph(graph)
    if corrupt:
        raise MigrationError("error-family compilation requires a corruption-free graph")
    if _snapshot_id(graph) != candidates.get("graph_snapshot_id"):
        raise MigrationError("graph changed after family candidates were generated")

    candidate_by_id = {
        str(item.get("case_id") or ""): item
        for item in candidates["candidates"]
        if isinstance(item, Mapping)
    }
    decisions = decision_payload.get("decisions")
    if not isinstance(decisions, list):
        raise MigrationError("decision manifest has no decisions")
    decision_by_id: dict[str, Mapping[str, Any]] = {}
    for item in decisions:
        if not isinstance(item, Mapping):
            raise MigrationError("family decision must be an object")
        case_id = str(item.get("case_id") or "")
        if not case_id or case_id in decision_by_id or case_id not in candidate_by_id:
            raise MigrationError(f"unknown or duplicate family decision: {case_id!r}")
        if item.get("decision") not in {"accept", "defer", "reject"}:
            raise MigrationError(f"invalid family decision for {case_id}")
        decision_by_id[case_id] = item
    if set(decision_by_id) != set(candidate_by_id):
        raise MigrationError("every deterministic family candidate requires an explicit decision")

    assignments = []
    decision_counts = {"accept": 0, "defer": 0, "reject": 0}
    for case_id in sorted(candidate_by_id):
        candidate = candidate_by_id[case_id]
        decision = decision_by_id[case_id]
        decision_name = str(decision["decision"])
        decision_counts[decision_name] += 1
        if decision_name != "accept":
            continue
        identity = error_case_identity(nodes[case_id])
        if (
            identity_sha256(case_id, identity) != candidate.get("identity_sha256")
            or deterministic_family_id(identity) != candidate.get("family_id")
        ):
            raise MigrationError(f"family identity drifted for {case_id}")
        proposed = _clean_aliases(
            candidate.get("proposed_aliases"),
            field=f"candidate aliases for {case_id}",
        )
        additional = _clean_aliases(
            decision.get("additional_aliases", []),
            field=f"additional aliases for {case_id}",
        )
        assignments.append(
            {
                "schema_version": ASSIGNMENT_SCHEMA,
                "case_id": case_id,
                "family_id": candidate["family_id"],
                "identity_sha256": candidate["identity_sha256"],
                # Reviewer aliases take the bounded slots first; otherwise a
                # long proposed list could silently evict the reviewed route
                # evidence and make the compiled manifest invalid.
                "aliases": list(dict.fromkeys([*additional, *proposed]))[:12],
                # Only aliases explicitly added by the reviewer may activate
                # the narrow exact-alias route boost. Proposed identity aliases
                # remain lexical/embedding evidence and are never promoted.
                "reviewed_aliases": additional,
                "decision": "accepted",
                "reason_code": str(decision.get("reason_code") or "reviewed_identity"),
            }
        )
    decision_sha256 = sha256_payload(decision_payload)
    manifest_identity = {
        "candidate_manifest_sha256": candidates["manifest_sha256"],
        "decision_manifest_sha256": decision_sha256,
        "assignments": assignments,
    }
    active = {
        "schema_version": ACTIVE_SCHEMA,
        "manifest_id": f"EKF-ACT-{sha256_payload(manifest_identity)[:24]}",
        "candidate_manifest_sha256": candidates["manifest_sha256"],
        "decision_manifest_sha256": decision_sha256,
        "decision_counts": decision_counts,
        "assignments": assignments,
        "policy": copy.deepcopy(candidates["policy"]),
    }
    accepted, diagnostics = validate_active_manifest(active, nodes)
    if diagnostics.get("status") != "verified" or len(accepted) != len(assignments):
        raise MigrationError(
            f"compiled family manifest failed runtime validation: {diagnostics.get('reason')}"
        )
    return active


def _family_paths(graph: Path) -> dict[str, Path]:
    root = graph / "maintenance" / "error_families"
    return {
        "root": root,
        "active": root / "active.json",
        "revisions": root / "revisions",
        "receipts": root / "receipts",
        "journals": root / "journals",
    }


def _publish_revision(path: Path, payload: bytes) -> None:
    if path.exists():
        if path.read_bytes() != payload:
            raise MigrationError(f"content-addressed family revision mismatch: {path}")
        return
    _atomic_write_bytes(path, payload)


def activate(
    graph_dir: Path | str,
    candidate_payload: Any,
    decision_payload: Any,
    *,
    confirm_engine_stopped: bool = False,
    engine_endpoints: Sequence[str] | None = None,
    additional_engine_endpoints: Sequence[str] | None = None,
    engine_probe_timeout_sec: float = DEFAULT_ENGINE_PROBE_TIMEOUT_SEC,
) -> dict[str, Any]:
    graph = Path(graph_dir).resolve()
    active = compile_active_manifest(graph, candidate_payload, decision_payload)
    active_bytes = (
        json.dumps(active, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    active_sha256 = _sha256_bytes(active_bytes)
    paths = _family_paths(graph)
    quiescence = _require_engine_quiescence(
        graph_dir=graph,
        confirm_engine_stopped=confirm_engine_stopped,
        engine_endpoints=engine_endpoints,
        additional_engine_endpoints=additional_engine_endpoints,
        timeout_sec=engine_probe_timeout_sec,
    )
    with _mutation_lock(graph, operation="error-family-activate", plan_hash=active_sha256):
        quiescence = _require_engine_quiescence(
            graph_dir=graph,
            confirm_engine_stopped=confirm_engine_stopped,
            engine_endpoints=engine_endpoints,
            additional_engine_endpoints=additional_engine_endpoints,
            timeout_sec=engine_probe_timeout_sec,
        )
        quiescence["checked_immediately_before_mutation"] = True
        active_path = paths["active"]
        previous_bytes = active_path.read_bytes() if active_path.is_file() else b""
        previous_sha256 = _sha256_bytes(previous_bytes) if previous_bytes else ""
        paths["revisions"].mkdir(parents=True, exist_ok=True)
        _publish_revision(paths["revisions"] / f"{active_sha256}.json", active_bytes)
        if previous_bytes:
            _publish_revision(
                paths["revisions"] / f"{previous_sha256}.json",
                previous_bytes,
            )
        run_id = active["manifest_id"]
        journal_path = paths["journals"] / f"{run_id}.json"
        receipt_path = paths["receipts"] / f"{run_id}.json"
        journal = {
            "schema_version": RECEIPT_SCHEMA,
            "operation": "activate",
            "run_id": run_id,
            "phase": "prepared",
            "graph_dir": str(graph),
            "active_sha256": active_sha256,
            "previous_active_sha256": previous_sha256,
            "active_revision": str(paths["revisions"] / f"{active_sha256}.json"),
            "previous_revision": (
                str(paths["revisions"] / f"{previous_sha256}.json")
                if previous_sha256
                else ""
            ),
            "started_at": _utc_now(),
        }
        _atomic_write_json(journal_path, journal)
        _atomic_write_bytes(active_path, active_bytes)
        journal["phase"] = "completed"
        journal["completed_at"] = _utc_now()
        journal["engine_quiescence"] = quiescence
        _atomic_write_json(journal_path, journal)
        _atomic_write_json(receipt_path, journal)
    return {
        **journal,
        "receipt_path": str(receipt_path),
        "assignment_count": len(active["assignments"]),
        "family_count": len(
            {assignment["family_id"] for assignment in active["assignments"]}
        ),
    }


def rollback(
    graph_dir: Path | str,
    receipt_path: Path | str,
    *,
    confirm_engine_stopped: bool = False,
    engine_endpoints: Sequence[str] | None = None,
    additional_engine_endpoints: Sequence[str] | None = None,
    engine_probe_timeout_sec: float = DEFAULT_ENGINE_PROBE_TIMEOUT_SEC,
) -> dict[str, Any]:
    graph = Path(graph_dir).resolve()
    receipt_file = Path(receipt_path).resolve()
    receipt = _read_json(receipt_file)
    if (
        not isinstance(receipt, Mapping)
        or receipt.get("schema_version") != RECEIPT_SCHEMA
        or receipt.get("phase") != "completed"
        or Path(str(receipt.get("graph_dir") or "")).resolve() != graph
    ):
        raise MigrationError("invalid ErrorFamily activation receipt")
    paths = _family_paths(graph)
    active_path = paths["active"]
    expected_active_sha256 = str(receipt.get("active_sha256") or "")
    if not active_path.is_file() or _sha256_file(active_path) != expected_active_sha256:
        raise MigrationError(
            "active ErrorFamily manifest changed after activation; refusing overwrite"
        )
    quiescence = _require_engine_quiescence(
        graph_dir=graph,
        confirm_engine_stopped=confirm_engine_stopped,
        engine_endpoints=engine_endpoints,
        additional_engine_endpoints=additional_engine_endpoints,
        timeout_sec=engine_probe_timeout_sec,
    )
    plan_hash = sha256_payload(
        {
            "operation": "error-family-rollback",
            "receipt_sha256": _sha256_file(receipt_file),
        }
    )
    with _mutation_lock(graph, operation="error-family-rollback", plan_hash=plan_hash):
        quiescence = _require_engine_quiescence(
            graph_dir=graph,
            confirm_engine_stopped=confirm_engine_stopped,
            engine_endpoints=engine_endpoints,
            additional_engine_endpoints=additional_engine_endpoints,
            timeout_sec=engine_probe_timeout_sec,
        )
        quiescence["checked_immediately_before_mutation"] = True
        previous_sha256 = str(receipt.get("previous_active_sha256") or "")
        if previous_sha256:
            previous = Path(str(receipt.get("previous_revision") or "")).resolve()
            if (
                previous.parent != paths["revisions"].resolve()
                or not previous.is_file()
                or _sha256_file(previous) != previous_sha256
            ):
                raise MigrationError("previous ErrorFamily revision is missing or changed")
            _atomic_write_bytes(active_path, previous.read_bytes())
        else:
            active_path.unlink()
        rollback_receipt = {
            "schema_version": ROLLBACK_RECEIPT_SCHEMA,
            "status": "completed",
            "rolled_back_at": _utc_now(),
            "activation_receipt_sha256": _sha256_file(receipt_file),
            "rolled_back_active_sha256": expected_active_sha256,
            "restored_active_sha256": previous_sha256,
            "engine_quiescence": quiescence,
        }
        rollback_path = (
            paths["receipts"]
            / f"rollback-{receipt.get('run_id')}-{plan_hash[:12]}.json"
        )
        _atomic_write_json(rollback_path, rollback_receipt)
    return {**rollback_receipt, "receipt_path": str(rollback_path)}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph-dir", required=True, type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan-output", type=Path)
    mode.add_argument("--compile-output", type=Path)
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--rollback", type=Path)
    parser.add_argument("--candidates", type=Path)
    parser.add_argument("--decisions", type=Path)
    parser.add_argument("--confirm-engine-stopped", action="store_true")
    parser.add_argument("--engine-endpoint", action="append", default=None)
    parser.add_argument("--additional-engine-endpoint", action="append", default=None)
    parser.add_argument(
        "--engine-probe-timeout-sec",
        type=float,
        default=DEFAULT_ENGINE_PROBE_TIMEOUT_SEC,
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.plan_output:
        manifest = build_candidate_manifest(args.graph_dir)
        _atomic_write_json(args.plan_output.resolve(), manifest)
        print(
            json.dumps(
                {
                    key: manifest[key]
                    for key in (
                        "manifest_id",
                        "manifest_sha256",
                        "candidate_count",
                        "deferred_count",
                        "family_count",
                    )
                },
                indent=2,
            )
        )
        return 0
    if args.compile_output:
        if not args.candidates or not args.decisions:
            raise SystemExit("--compile-output requires --candidates and --decisions")
        active = compile_active_manifest(
            args.graph_dir,
            _read_json(args.candidates.resolve()),
            _read_json(args.decisions.resolve()),
        )
        _atomic_write_json(args.compile_output.resolve(), active)
        print(
            json.dumps(
                {
                    "manifest_id": active["manifest_id"],
                    "assignment_count": len(active["assignments"]),
                    "family_count": len(
                        {
                            assignment["family_id"]
                            for assignment in active["assignments"]
                        }
                    ),
                },
                indent=2,
            )
        )
        return 0
    if args.rollback:
        result = rollback(
            args.graph_dir,
            args.rollback,
            confirm_engine_stopped=args.confirm_engine_stopped,
            engine_endpoints=args.engine_endpoint,
            additional_engine_endpoints=args.additional_engine_endpoint,
            engine_probe_timeout_sec=args.engine_probe_timeout_sec,
        )
    else:
        if not args.candidates or not args.decisions:
            raise SystemExit("--apply requires --candidates and --decisions")
        result = activate(
            args.graph_dir,
            _read_json(args.candidates.resolve()),
            _read_json(args.decisions.resolve()),
            confirm_engine_stopped=args.confirm_engine_stopped,
            engine_endpoints=args.engine_endpoint,
            additional_engine_endpoints=args.additional_engine_endpoint,
            engine_probe_timeout_sec=args.engine_probe_timeout_sec,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
