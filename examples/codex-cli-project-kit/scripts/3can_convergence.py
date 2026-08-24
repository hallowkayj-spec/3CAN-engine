#!/usr/bin/env python3
"""Evidence-based convergence checks for long Codex tasks.

The hook is deliberately local. Development-path failures are fail-open while
Stop failures can only converge to an explicit incomplete report. It does not
parse Codex session files, call 3CAN, manage Git, or make acceptance decisions
that belong to the owner. A project contract binds each acceptance condition to
named evidence; a compact task-local receipt records current results.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import task_oracle  # noqa: E402


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACT = Path(".codex/convergence.json")
DEFAULT_RECEIPT = Path("test-results/3can/convergence/receipt.json")
DEFAULT_TASK_REGISTRY = Path(".codex/task-hooks/registry.json")
CONTRACT_SCHEMA = "3can.convergence-contract/v1"
TASK_CONTRACT_SCHEMA = "3can.convergence-contract/v2"
RECEIPT_SCHEMA = "3can.convergence-receipt/v2"
REPORTABLE_OUTCOMES = {
    "CANDIDATE_READY",
    "BLOCKED",
    "UNAVAILABLE",
    "CONFLICT",
    "PARTIAL",
    "MISSING",
    "CONTRADICTS",
    "UNREQUESTED",
    "STALE_EVIDENCE",
    "UNBOUND",
    "IMPLICIT_MUTABLE_BINDING",
    "FALLBACK_NOT_ALLOWED",
    "UNVERIFIABLE",
    "REVISION_PENDING",
}
TYPED_INCOMPLETE_OUTCOMES = {
    "BLOCKED",
    "UNAVAILABLE",
    "CONFLICT",
    "PARTIAL",
}
MAX_CONTEXT_CHARS = 4_000
MAX_HASH_BYTES = 8 * 1024 * 1024
MAX_EVIDENCE_HASH_BYTES = 32 * 1024 * 1024
MAX_WORKSPACE_HASH_BYTES = 64 * 1024 * 1024
MAX_CHANGED_FILES = 256
MAX_GIT_METADATA_BYTES = 4 * 1024 * 1024
MAX_CONTROL_JSON_BYTES = 256 * 1024


class ContractError(ValueError):
    """The project convergence contract is invalid."""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _write_json_atomic(
    path: Path,
    value: Any,
    *,
    max_bytes: int | None = None,
    label: str = "JSON output",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if max_bytes is not None and len(payload.encode("utf-8")) > max_bytes:
        raise ContractError(f"{label} exceeds the bounded control-file size")
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def _compact_unavailable_receipt(value: dict[str, Any]) -> dict[str, Any]:
    """Keep an oversized receipt current while dropping its bulky details."""
    compact = {
        "schema": RECEIPT_SCHEMA,
        "recorded_at": _now(),
        "contract_sha256": value.get("contract_sha256"),
        "evidence_sha256": value.get("evidence_sha256"),
        "workspace": value.get("workspace"),
        "task": value.get("task"),
        "stage": value.get("stage", "current"),
        "outcome": "UNAVAILABLE",
        "proof_eligible": False,
        "checks": [],
        "acceptance": [],
        "open_check_ids": [],
        "open_acceptance_ids": [],
        "next_objective": "Reduce the receipt inputs and record the typed state again.",
        "reason": "Generated receipt exceeded the bounded control-file size.",
        "checkpoint_expectation": "none",
        "threecan_writeback": {"eligible_trigger": "NONE", "performed": False},
    }
    compact["receipt_sha256"] = _sha256_bytes(_json_bytes(compact))
    return compact


def _write_receipt_atomic(path: Path, value: dict[str, Any]) -> dict[str, Any]:
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    bounded = (
        value
        if len(payload.encode("utf-8")) <= MAX_CONTROL_JSON_BYTES
        else _compact_unavailable_receipt(value)
    )
    _write_json_atomic(
        path,
        bounded,
        max_bytes=MAX_CONTROL_JSON_BYTES,
        label="generated receipt",
    )
    return bounded


def _resolve_under(root: Path, relative: str, *, label: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute():
        raise ContractError(f"{label} must be relative to the project root")
    resolved_root = root.resolve()
    resolved = (resolved_root / candidate).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ContractError(f"{label} escapes the project root") from exc
    return resolved


def _string_list(value: Any, *, label: str, required: bool = False) -> list[str]:
    if value is None and not required:
        return []
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ContractError(f"{label} must be a list of non-empty strings")
    if required and not value:
        raise ContractError(f"{label} must not be empty")
    return [item.strip() for item in value]


def _check_stages(check: dict[str, Any], *, check_id: str) -> list[str]:
    stages = check.get("stages", ["final"])
    normalized = _string_list(stages, label=f"check {check_id} stages", required=True)
    invalid = sorted(set(normalized) - {"episode", "final"})
    if invalid:
        raise ContractError(f"check {check_id} has invalid stages: {', '.join(invalid)}")
    return normalized


def _validate_guards(
    guards: Any, check_ids: set[str]
) -> list[dict[str, Any]]:
    if not isinstance(guards, list):
        raise ContractError("guards must be a list")
    for index, guard in enumerate(guards):
        if not isinstance(guard, dict):
            raise ContractError(f"guard {index} must be an object")
        unknown = sorted(
            set(guard) - {"tool_name_glob", "input_contains", "requires_check_ids"}
        )
        if unknown:
            raise ContractError(
                f"guard {index} has unsupported fields: {', '.join(unknown)}"
            )
        for key in ("tool_name_glob", "input_contains"):
            pattern = guard.get(key)
            if pattern is not None and (not isinstance(pattern, str) or not pattern):
                raise ContractError(f"guard {index} {key} must be a non-empty string")
        required_ids = _string_list(
            guard.get("requires_check_ids", []),
            label=f"guard {index} requires_check_ids",
        )
        unknown = sorted(set(required_ids) - check_ids)
        if unknown:
            raise ContractError(
                f"guard {index} references unknown checks: {', '.join(unknown)}"
            )
        if not guard.get("tool_name_glob") and not guard.get("input_contains"):
            raise ContractError(f"guard {index} must declare a tool glob or input substring")
    return guards


def _task_checks(context: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for oracle in context["task_hook"]["oracles"]:
        check = dict(oracle)
        if check.get("type") == "artifact":
            check.setdefault("role", "byproduct")
            if "path_binding" in check:
                check["path"] = context["bindings"][check["path_binding"]]
        checks.append(check)
    return checks


def _task_acceptance(context: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": item["id"],
            "text": item["text"],
            "evidence": item["oracle_ids"],
        }
        for item in context["task_hook"]["acceptance"]
    ]


def _normalize_task_contract(value: dict[str, Any], root: Path) -> dict[str, Any]:
    allowed = {
        "schema",
        "status",
        "scope",
        "run_id",
        "task_hook",
        "activation",
        "bindings",
        "allowed_fallbacks",
        "non_goals",
        "guards",
        "closeout",
    }
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ContractError(
            "task convergence contract has unsupported fields: " + ", ".join(unknown)
        )
    if value.get("status", "active") not in {"active", "complete"}:
        raise ContractError("task convergence status must be active or complete")
    if value.get("scope") != "current_repository_only":
        raise ContractError("contract scope must be current_repository_only")
    if value.get("status", "active") != "active":
        context = task_oracle.load_task_context(
            root, value, require_executable=False
        )
        closeout = value.get("closeout")
        if not isinstance(closeout, dict):
            raise task_oracle.TaskOracleError(
                "REVISION_PENDING", "explicit closeout is required"
            )
        closeout_fields = {
            "task_hook_sha256",
            "final_receipt_sha256",
            "disposition",
            "confirmed_by",
            "confirmation_ref",
        }
        closeout_unknown = sorted(set(closeout) - closeout_fields)
        if closeout_unknown:
            raise ContractError(
                "closeout has unsupported fields: " + ", ".join(closeout_unknown)
            )
        task_hook = context["task_hook"]
        expected_disposition = {
            "RETIRED": "retired",
            "REUSABLE_CANDIDATE": "reusable_candidate",
            "REUSABLE_ACTIVE": "reusable_active",
        }.get(task_hook["status"])
        if (
            expected_disposition is None
            or closeout.get("disposition") != expected_disposition
            or closeout.get("task_hook_sha256") != context["task_hook_sha256"]
            or not re.fullmatch(
                r"[0-9a-f]{64}", str(closeout.get("final_receipt_sha256") or "")
            )
            or closeout.get("confirmed_by")
            not in {"owner", "independent_reviewer"}
            or not isinstance(closeout.get("confirmation_ref"), str)
            or not closeout["confirmation_ref"].strip()
        ):
            raise task_oracle.TaskOracleError(
                "REVISION_PENDING", "closeout does not confirm the final disposition"
            )
        checks = _task_checks(context)
        return {
            **value,
            "goal": "Inactive convergence contract.",
            "acceptance": _task_acceptance(context),
            "non_goals": [],
            "checks": checks,
            "guards": [],
            "_task_context": context,
        }
    context = task_oracle.load_task_context(root, value)
    task_hook = context["task_hook"]
    checks = _task_checks(context)
    acceptance = _task_acceptance(context)
    non_goals = _string_list(value.get("non_goals", []), label="non_goals")
    guards = _validate_guards(value.get("guards", []), {item["id"] for item in checks})
    return {
        **value,
        "goal": task_hook["goal"],
        "acceptance": acceptance,
        "non_goals": non_goals,
        "checks": checks,
        "guards": guards,
        "_task_context": context,
    }


def validate_contract(value: Any, root: Path) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError("contract must be a JSON object")
    if value.get("schema") == TASK_CONTRACT_SCHEMA:
        return _normalize_task_contract(value, root)
    if value.get("schema") != CONTRACT_SCHEMA:
        raise ContractError(
            f"contract schema must be {CONTRACT_SCHEMA} or {TASK_CONTRACT_SCHEMA}"
        )
    if value.get("status", "active") not in {"active", "paused", "complete"}:
        raise ContractError("contract status must be active, paused, or complete")
    if value.get("scope") != "current_repository_only":
        raise ContractError("contract scope must be current_repository_only")
    goal = value.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        raise ContractError("contract goal must be a non-empty string")
    _string_list(value.get("non_goals", []), label="non_goals")

    checks = value.get("checks", [])
    if not isinstance(checks, list) or not checks:
        raise ContractError("checks must be a non-empty list")
    if len(checks) > task_oracle.MAX_ORACLES:
        raise ContractError("checks exceed the bounded limit")
    check_ids: set[str] = set()
    check_stages: dict[str, list[str]] = {}
    check_types: dict[str, str] = {}
    automated_final = False
    for index, check in enumerate(checks):
        if not isinstance(check, dict):
            raise ContractError(f"check {index} must be an object")
        check_id = check.get("id")
        if not isinstance(check_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", check_id):
            raise ContractError(f"check {index} id must use letters, digits, dot, dash, or underscore")
        if check_id in check_ids:
            raise ContractError(f"duplicate check id: {check_id}")
        check_ids.add(check_id)
        check_type = check.get("type")
        if check_type not in {"command", "artifact", "owner_review"}:
            raise ContractError(f"check {check_id} has unsupported type: {check_type}")
        stages = _check_stages(check, check_id=check_id)
        check_stages[check_id] = stages
        check_types[check_id] = check_type
        if check_type == "command":
            automated_final = automated_final or "final" in stages
            argv = check.get("argv")
            if not isinstance(argv, list) or not argv or any(
                not isinstance(item, str) or not item for item in argv
            ):
                raise ContractError(f"command check {check_id} requires non-empty argv strings")
            timeout = check.get("timeout_seconds", 120)
            if not isinstance(timeout, (int, float)) or not 0 < timeout <= 3_600:
                raise ContractError(f"command check {check_id} timeout_seconds must be 1..3600")
        elif check_type == "artifact":
            automated_final = automated_final or "final" in stages
            path_value = check.get("path")
            if not isinstance(path_value, str) or not path_value.strip():
                raise ContractError(f"artifact check {check_id} requires path")
            _resolve_under(root, path_value, label=f"artifact check {check_id} path")
            min_bytes = check.get("min_bytes", 1)
            if not isinstance(min_bytes, int) or min_bytes < 0:
                raise ContractError(f"artifact check {check_id} min_bytes must be non-negative")
            if check.get("role", "candidate") not in {"candidate", "byproduct"}:
                raise ContractError(
                    f"artifact check {check_id} role must be candidate or byproduct"
                )
        elif stages != ["final"]:
            raise ContractError(f"owner_review check {check_id} must use only the final stage")
    if not automated_final:
        raise ContractError("at least one automated check must run at the final stage")

    acceptance = value.get("acceptance")
    if not isinstance(acceptance, list) or not acceptance:
        raise ContractError("acceptance must be a non-empty list of evidence bindings")
    if len(acceptance) > task_oracle.MAX_ACCEPTANCE:
        raise ContractError("acceptance exceeds the bounded limit")
    acceptance_ids: set[str] = set()
    referenced_evidence: set[str] = set()
    for index, condition in enumerate(acceptance):
        if not isinstance(condition, dict):
            raise ContractError(f"acceptance {index} must bind text to evidence ids")
        acceptance_id = condition.get("id")
        if not isinstance(acceptance_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_.-]+", acceptance_id
        ):
            raise ContractError(
                f"acceptance {index} id must use letters, digits, dot, dash, or underscore"
            )
        if acceptance_id in acceptance_ids:
            raise ContractError(f"duplicate acceptance id: {acceptance_id}")
        acceptance_ids.add(acceptance_id)
        text = condition.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ContractError(f"acceptance {acceptance_id} requires non-empty text")
        evidence = _string_list(
            condition.get("evidence"),
            label=f"acceptance {acceptance_id} evidence",
            required=True,
        )
        unknown = sorted(set(evidence) - check_ids)
        if unknown:
            raise ContractError(
                f"acceptance {acceptance_id} references unknown checks: {', '.join(unknown)}"
            )
        non_final = sorted(
            check_id for check_id in evidence if "final" not in check_stages[check_id]
        )
        if non_final:
            raise ContractError(
                f"acceptance {acceptance_id} evidence must run at final: {', '.join(non_final)}"
            )
        referenced_evidence.update(evidence)
    unbound_owner_review = sorted(
        check_id
        for check_id, check_type in check_types.items()
        if check_type == "owner_review" and check_id not in referenced_evidence
    )
    if unbound_owner_review:
        raise ContractError(
            "owner_review checks must prove an acceptance condition: "
            + ", ".join(unbound_owner_review)
        )

    _validate_guards(value.get("guards", []), check_ids)
    return value


def load_contract(root: Path, contract_path: Path) -> tuple[dict[str, Any] | None, str | None]:
    path = _bounded_path(root, contract_path, label="contract path")
    if not path.is_file():
        return None, None
    if path.stat().st_size > MAX_CONTROL_JSON_BYTES:
        raise ContractError("contract exceeds the bounded control-file size")
    raw = path.read_bytes()
    if len(raw) > MAX_CONTROL_JSON_BYTES:
        raise ContractError("contract exceeds the bounded control-file size")
    value = json.loads(raw.decode("utf-8-sig"))
    return validate_contract(value, root), _sha256_bytes(_json_bytes(value))


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=3,
    )


def _changed_paths(status: bytes) -> list[str]:
    paths: list[str] = []
    entries = status.decode("utf-8", errors="surrogateescape").split("\0")
    skip_rename_source = False
    for entry in entries:
        if not entry:
            continue
        if skip_rename_source:
            skip_rename_source = False
            paths.append(entry)
            continue
        if len(entry) < 4:
            continue
        code = entry[:2]
        paths.append(entry[3:])
        if "R" in code or "C" in code:
            skip_rename_source = True
    return sorted(set(paths))


def _submodule_paths(status: bytes) -> list[str]:
    paths: list[str] = []
    for raw_line in status.decode("utf-8", errors="surrogateescape").splitlines():
        match = re.match(r"^[ +\-U]?[0-9a-f]+\s+(.+?)(?:\s+\(|$)", raw_line)
        if match:
            paths.append(match.group(1).replace("\\", "/"))
    return paths


def _file_fingerprint(root: Path, relative: str) -> dict[str, Any]:
    try:
        path = _resolve_under(root, relative, label="changed path")
    except ContractError:
        return {"path_sha256": _sha256_bytes(relative.encode("utf-8")), "state": "outside"}
    path_hash = _sha256_bytes(relative.replace("\\", "/").encode("utf-8"))
    if not path.exists():
        return {"path_sha256": path_hash, "state": "missing"}
    stat = path.stat()
    result: dict[str, Any] = {
        "path_sha256": path_hash,
        "state": "directory" if path.is_dir() else "file",
        "size": stat.st_size,
    }
    if path.is_file() and stat.st_size <= MAX_HASH_BYTES:
        result["content_sha256"] = _sha256_bytes(path.read_bytes())
    else:
        result["mtime_ns"] = stat.st_mtime_ns
    return result


def _artifact_fingerprint(root: Path, check: dict[str, Any]) -> dict[str, Any]:
    check_id = check["id"]
    relative = check["path"].replace("\\", "/")
    path = _resolve_under(root, relative, label=f"artifact check {check_id} path")
    result: dict[str, Any] = {
        "id": check_id,
        "type": "artifact",
        "path": relative,
    }
    if not path.is_file():
        result["state"] = "missing"
        return result
    stat = path.stat()
    result.update({"state": "file", "bytes": stat.st_size})
    if stat.st_size <= MAX_HASH_BYTES:
        result["sha256"] = _sha256_bytes(path.read_bytes())
    else:
        # Size + mtime is forgeable and therefore cannot make a receipt proof
        # eligible. Large outputs use a bounded, content-addressed manifest or
        # a task-specific command provider instead of repeated full hashing.
        result["state"] = "unverifiable_large"
        result["requires"] = "content-addressed manifest or command provider"
    return result


def _external_receipt_fingerprint(
    contract: dict[str, Any], root: Path, check: dict[str, Any]
) -> dict[str, Any]:
    context = contract.get("_task_context")
    if "receipt_path_binding" in check:
        if not isinstance(context, dict):
            raise ContractError("external receipt binding requires a task context")
        binding = check["receipt_path_binding"]
        relative = context["bindings"].get(binding)
    else:
        relative = check.get("receipt_path")
    path = _resolve_under(
        root, relative, label=f"external receipt {check['id']} path"
    )
    result: dict[str, Any] = {
        "id": check["id"],
        "type": "external_receipt",
        "path_sha256": _sha256_bytes(str(relative).replace("\\", "/").encode("utf-8")),
    }
    if not path.is_file():
        return {**result, "state": "missing"}
    stat = path.stat()
    if stat.st_size > task_oracle.MAX_EXTERNAL_PROOF_BYTES:
        return {**result, "state": "unverifiable_large", "bytes": stat.st_size}
    raw = path.read_bytes()
    return {
        **result,
        "state": "file",
        "bytes": len(raw),
        "sha256": _sha256_bytes(raw),
    }


def _evidence_file_path(
    contract: dict[str, Any], root: Path, check: dict[str, Any]
) -> Path:
    if check.get("type") == "external_receipt":
        context = contract.get("_task_context")
        if "receipt_path_binding" in check:
            if not isinstance(context, dict):
                raise ContractError("external receipt binding requires a task context")
            relative = context["bindings"].get(check["receipt_path_binding"])
        else:
            relative = check.get("receipt_path")
        label = f"external receipt {check['id']} path"
    else:
        relative = check.get("path")
        label = f"artifact check {check['id']} path"
    if not isinstance(relative, str) or not relative.strip():
        raise ContractError(f"{label} must be a non-empty relative path")
    return _resolve_under(root, relative, label=label)


def _enforce_evidence_budget(
    contract: dict[str, Any], root: Path, *, role: str | None
) -> None:
    paths: set[Path] = set()
    for check in contract.get("checks", []):
        check_type = check.get("type")
        if check_type == "artifact":
            if role is not None and check.get("role", "candidate") != role:
                continue
        elif check_type != "external_receipt" or role is not None:
            continue
        paths.add(_evidence_file_path(contract, root, check))
    total = 0
    for path in paths:
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError as exc:
            raise ContractError("evidence metadata is unavailable") from exc
        if total > MAX_EVIDENCE_HASH_BYTES:
            raise ContractError("evidence exceeds the aggregate hash budget")


def evidence_snapshot(
    contract: dict[str, Any], root: Path, *, role: str | None = None
) -> list[dict[str, Any]]:
    _enforce_evidence_budget(contract, root, role=role)
    evidence = [
        _artifact_fingerprint(root, check)
        for check in contract.get("checks", [])
        if check.get("type") == "artifact"
        and (role is None or check.get("role", "candidate") == role)
    ]
    if role is None:
        evidence.extend(
            _external_receipt_fingerprint(contract, root, check)
            for check in contract.get("checks", [])
            if check.get("type") == "external_receipt"
        )
    return evidence


def evidence_fingerprint(
    contract: dict[str, Any], root: Path, *, role: str | None = None
) -> str:
    return _sha256_bytes(_json_bytes(evidence_snapshot(contract, root, role=role)))


def _path_excluded(relative: str, exclusions: tuple[str, ...]) -> bool:
    normalized = relative.replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    if os.name == "nt":
        normalized = normalized.casefold()
        exclusions = tuple(item.casefold() for item in exclusions)
    return any(
        normalized == item.rstrip("/")
        or (item.endswith("/") and normalized.startswith(item))
        for item in exclusions
    )


def workspace_fingerprint(
    root: Path, *, exclude_paths: tuple[str, ...] = ()
) -> dict[str, Any]:
    resolved = root.resolve()
    top = _git(resolved, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        return {
            "kind": "directory",
            "workspace_sha256": _sha256_bytes(str(resolved).casefold().encode("utf-8")),
        }
    prefix = _git(resolved, "rev-parse", "--show-prefix")
    branch = _git(resolved, "branch", "--show-current")
    head = None if exclude_paths else _git(resolved, "rev-parse", "HEAD")
    status = _git(
        resolved,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--ignore-submodules=none",
        "--",
        ".",
    )
    index = (
        _git(resolved, "ls-files", "--stage", "-z", "--", ".")
        if exclude_paths
        else None
    )
    commands = [branch, prefix, status, *(item for item in (head, index) if item)]
    if any(item.returncode != 0 for item in commands):
        raise RuntimeError("Git workspace fingerprint failed")
    if any(len(item.stdout) > MAX_GIT_METADATA_BYTES for item in commands):
        raise ContractError("Git workspace metadata exceeds the bounded read budget")
    git_top = Path(top.stdout.decode("utf-8", errors="surrogateescape").strip())
    submodule_output = b""
    if (git_top / ".gitmodules").is_file():
        submodules = _git(resolved, "submodule", "status", "--recursive")
        if submodules.returncode != 0:
            raise RuntimeError("Git submodule fingerprint failed")
        submodule_output = submodules.stdout
        if len(submodule_output) > MAX_GIT_METADATA_BYTES:
            raise ContractError("Git submodule metadata exceeds the bounded read budget")
    prefix_text = (
        prefix.stdout.decode("utf-8", errors="surrogateescape")
        .strip()
        .replace("\\", "/")
    )
    relative_paths: list[str] = []
    for item in _changed_paths(status.stdout):
        normalized = item.replace("\\", "/")
        if prefix_text:
            # status.relativePaths may emit either repository-root or current
            # directory paths. The explicit pathspec already limits results to
            # this project root.
            if normalized.startswith(prefix_text):
                normalized = normalized[len(prefix_text) :]
        if not _path_excluded(normalized, exclude_paths):
            relative_paths.append(normalized)
    relative_paths = sorted(set(relative_paths))
    if len(relative_paths) > MAX_CHANGED_FILES:
        raise ContractError("changed files exceed the workspace fingerprint limit")
    workspace_bytes = 0
    for relative in relative_paths:
        try:
            candidate = _resolve_under(resolved, relative, label="changed path")
        except ContractError:
            continue
        try:
            if candidate.is_file():
                workspace_bytes += candidate.stat().st_size
        except OSError as exc:
            raise ContractError("changed file metadata is unavailable") from exc
        if workspace_bytes > MAX_WORKSPACE_HASH_BYTES:
            raise ContractError("changed files exceed the aggregate fingerprint budget")
    submodule_paths: set[str] = set()
    for item in _submodule_paths(submodule_output):
        normalized = item
        if prefix_text:
            if not normalized.startswith(prefix_text):
                continue
            normalized = normalized[len(prefix_text) :]
        submodule_paths.add(normalized)
    dirty_submodules = sorted(set(relative_paths).intersection(submodule_paths))
    if dirty_submodules:
        raise ContractError(
            "dirty submodules are unsupported for scope=current_repository_only"
        )
    files = [_file_fingerprint(resolved, item) for item in relative_paths]
    state: dict[str, Any] = {
        "kind": "git",
        "workspace_sha256": _sha256_bytes(str(resolved).casefold().encode("utf-8")),
        "branch": branch.stdout.decode("utf-8", errors="replace").strip(),
        "changed_file_count": len(files),
        "changed_files_sha256": _sha256_bytes(_json_bytes(files)),
    }
    if exclude_paths:
        index_records: list[str] = []
        assert index is not None
        for raw_record in index.stdout.decode(
            "utf-8", errors="surrogateescape"
        ).split("\0"):
            if not raw_record or "\t" not in raw_record:
                continue
            metadata, path = raw_record.split("\t", 1)
            normalized = path.replace("\\", "/")
            if prefix_text:
                if not normalized.startswith(prefix_text):
                    continue
                normalized = normalized[len(prefix_text) :]
            if not _path_excluded(normalized, exclude_paths):
                index_records.append(f"{metadata}\t{normalized}")
        state.update(
            {
                "scope": "task-candidate-without-control-plane",
                "index_sha256": _sha256_bytes(
                    _json_bytes(sorted(index_records))
                ),
            }
        )
    else:
        assert head is not None
        state.update(
            {
                "head": head.stdout.decode("ascii", errors="replace").strip(),
                "porcelain_sha256": _sha256_bytes(status.stdout),
            }
        )
    state["fingerprint"] = _sha256_bytes(_json_bytes(state))
    return state


def contract_workspace_fingerprint(
    contract: dict[str, Any],
    root: Path,
    contract_path: Path,
    receipt_path: Path | None = None,
) -> dict[str, Any]:
    if contract.get("schema") != TASK_CONTRACT_SCHEMA:
        return workspace_fingerprint(root)
    contract_file = _bounded_path(root, contract_path, label="contract path")
    contract_relative = contract_file.relative_to(root.resolve()).as_posix()
    task_path = contract["_task_context"]["task_hook_path"]
    receipt_relative = (
        _bounded_path(root, receipt_path, label="receipt path")
        .relative_to(root.resolve())
        .as_posix()
        if receipt_path is not None
        else ""
    )
    return workspace_fingerprint(
        root,
        exclude_paths=(
            contract_relative,
            task_path,
            ".codex/task-hooks/evidence/",
            receipt_relative,
        ),
    )


def _bounded_path(root: Path, path: Path, *, label: str) -> Path:
    resolved_root = root.resolve()
    resolved = path.resolve() if path.is_absolute() else (resolved_root / path).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ContractError(f"{label} escapes the project root") from exc
    return resolved


def _receipt_path(root: Path, receipt_path: Path) -> Path:
    return _bounded_path(root, receipt_path, label="receipt path")


def read_receipt(root: Path, receipt_path: Path) -> dict[str, Any] | None:
    path = _receipt_path(root, receipt_path)
    if not path.is_file():
        return None
    if path.stat().st_size > MAX_CONTROL_JSON_BYTES:
        raise ContractError("receipt exceeds the bounded control-file size")
    raw = path.read_bytes()
    if len(raw) > MAX_CONTROL_JSON_BYTES:
        raise ContractError("receipt exceeds the bounded control-file size")
    value = json.loads(raw.decode("utf-8-sig"))
    return value if isinstance(value, dict) else None


def receipt_is_current(
    receipt: dict[str, Any] | None,
    *,
    contract_sha256: str,
    workspace: dict[str, Any],
    evidence_sha256: str,
    task_state: dict[str, Any] | None = None,
) -> bool:
    receipt_digest = None
    if receipt:
        unsigned = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
        receipt_digest = _sha256_bytes(_json_bytes(unsigned))
    return bool(
        receipt
        and receipt.get("schema") == RECEIPT_SCHEMA
        and receipt.get("contract_sha256") == contract_sha256
        and receipt.get("evidence_sha256") == evidence_sha256
        and workspace.get("kind") == "git"
        and bool(workspace.get("fingerprint"))
        and receipt.get("workspace", {}).get("fingerprint") == workspace.get("fingerprint")
        and receipt.get("task") == task_state
        and receipt.get("receipt_sha256") == receipt_digest
    )


def ensure_revision_boundary(
    contract: dict[str, Any], receipt: dict[str, Any] | None
) -> None:
    """Reject semantic self-replacement inside an already evidenced run."""
    context = contract.get("_task_context")
    previous = (receipt or {}).get("task")
    if not isinstance(context, dict) or not isinstance(previous, dict):
        return
    task_hook = context["task_hook"]
    if (
        previous.get("run_id") != context["run_id"]
        or previous.get("task_family") != task_hook["task_family"]
    ):
        return
    previous_revision = previous.get("revision")
    previous_semantics = previous.get("task_semantics_sha256")
    if previous_revision == task_hook["revision"]:
        if previous_semantics != context["task_semantics_sha256"]:
            raise task_oracle.TaskOracleError(
                "REVISION_PENDING",
                "Task meaning changed inside an evidenced revision; create and confirm a successor revision.",
            )
        return
    if task_hook.get("parent_revision") != previous_revision:
        raise task_oracle.TaskOracleError(
            "REVISION_PENDING",
            "A successor revision must name the evidenced revision as parent.",
        )
    owner_changed = (
        previous.get("owner_contract_sha256")
        != context["owner_contract_sha256"]
    )
    if owner_changed and context["activation"].get("confirmed_by") != "owner":
        raise task_oracle.TaskOracleError(
            "REVISION_PENDING",
            "Goal, acceptance, or candidate meaning changed without Owner confirmation.",
        )


def closeout_is_valid(
    contract: dict[str, Any],
    receipt: dict[str, Any] | None,
    root: Path,
    contract_path: Path,
    receipt_path: Path,
) -> bool:
    closeout = contract.get("closeout")
    if not isinstance(closeout, dict) or not receipt:
        return False
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    digest = _sha256_bytes(_json_bytes(unsigned))
    if (
        receipt.get("receipt_sha256") != digest
        or closeout.get("final_receipt_sha256") != digest
        or receipt.get("outcome") != "CONVERGED"
        or receipt.get("proof_eligible") is not True
        or receipt.get("schema") != RECEIPT_SCHEMA
    ):
        return False
    context = contract.get("_task_context")
    task_state = receipt.get("task")
    if not isinstance(context, dict) or not isinstance(task_state, dict):
        return False
    task_hook = context["task_hook"]
    current = _stable_current_capture(contract, root, contract_path, receipt_path)
    current_task = current["task"]
    if not current["stable"] or not isinstance(current_task, dict):
        return False
    terminal_contract, terminal_contract_sha256 = load_contract(root, contract_path)
    terminal_receipt = read_receipt(root, receipt_path)
    if terminal_contract is None or terminal_contract != contract or terminal_receipt != receipt:
        return False
    terminal = _stable_current_capture(
        terminal_contract, root, contract_path, receipt_path
    )
    closing_contract, closing_contract_sha256 = load_contract(root, contract_path)
    closing_receipt = read_receipt(root, receipt_path)
    if (
        terminal_contract_sha256 is None
        or closing_contract is None
        or closing_contract_sha256 != terminal_contract_sha256
        or closing_contract != terminal_contract
        or closing_receipt != terminal_receipt
        or not terminal["stable"]
        or terminal["workspace"] != current["workspace"]
        or terminal["task"] != current_task
        or terminal["evidence_sha256"] != current["evidence_sha256"]
    ):
        return False
    if (
        task_state.get("run_id") != context["run_id"]
        or task_state.get("task_family") != task_hook["task_family"]
        or task_state.get("revision") != task_hook["revision"]
        or task_state.get("task_semantics_sha256")
        != context["task_semantics_sha256"]
        or task_state.get("owner_contract_sha256")
        != context["owner_contract_sha256"]
        or task_state.get("bindings_sha256") != context["bindings_sha256"]
        or task_state.get("candidate") != current_task.get("candidate")
        or receipt.get("workspace", {}).get("fingerprint")
        != current["workspace"].get("fingerprint")
        or receipt.get("evidence_sha256") != current["evidence_sha256"]
    ):
        return False
    if task_hook["status"] == "RETIRED":
        transition = task_hook.get("transition", {})
        return (
            task_state.get("task_hook_sha256") == transition.get("from_sha256")
            and closeout.get("task_hook_sha256") == context["task_hook_sha256"]
        )
    if task_hook["status"] == "REUSABLE_ACTIVE":
        return (
            task_state.get("task_hook_sha256") == context["task_hook_sha256"]
            and closeout.get("task_hook_sha256") == context["task_hook_sha256"]
        )
    promotion_receipts = (task_hook.get("promotion") or {}).get(
        "qualifying_receipts", []
    )
    return any(
        item.get("receipt_sha256") == digest
        and item.get("run_id") == context["run_id"]
        and item.get("candidate_fingerprint")
        == (task_state.get("candidate") or {}).get("fingerprint")
        and item.get("bindings_sha256") == task_state.get("bindings_sha256")
        and item.get("task_hook_sha256") == task_state.get("task_hook_sha256")
        and item.get("task_semantics_sha256")
        == task_state.get("task_semantics_sha256")
        and closeout.get("task_hook_sha256") == context["task_hook_sha256"]
        for item in promotion_receipts
        if isinstance(item, dict)
    )


def task_snapshot(
    contract: dict[str, Any], root: Path, workspace: dict[str, Any]
) -> dict[str, Any] | None:
    context = contract.get("_task_context")
    if not isinstance(context, dict):
        return None
    task_hook = context["task_hook"]
    return {
        "task_family": task_hook["task_family"],
        "revision": task_hook["revision"],
        "task_hook_path": context["task_hook_path"],
        "task_hook_sha256": context["task_hook_sha256"],
        "task_semantics_sha256": context["task_semantics_sha256"],
        "owner_contract_sha256": context["owner_contract_sha256"],
        "run_id": context["run_id"],
        "bindings_sha256": context["bindings_sha256"],
        "candidate": task_oracle.current_candidate(context, root, workspace),
    }


def _stable_current_capture(
    contract: dict[str, Any],
    root: Path,
    contract_path: Path,
    receipt_path: Path,
) -> dict[str, Any]:
    """Bracket a candidate provider with data-plane fingerprints."""
    before_workspace = contract_workspace_fingerprint(
        contract, root, contract_path, receipt_path
    )
    before_evidence = evidence_fingerprint(contract, root)
    context = contract.get("_task_context")
    provider_type = (
        context["task_hook"]["candidate"]["provider"]["type"]
        if isinstance(context, dict)
        else None
    )
    before_passive_candidate = (
        task_oracle.current_candidate(context, root, before_workspace)
        if isinstance(context, dict) and provider_type != "command"
        else None
    )
    task = task_snapshot(contract, root, before_workspace)
    after_workspace = contract_workspace_fingerprint(
        contract, root, contract_path, receipt_path
    )
    after_evidence = evidence_fingerprint(contract, root)
    after_passive_candidate = (
        task_oracle.current_candidate(context, root, after_workspace)
        if isinstance(context, dict) and provider_type != "command"
        else None
    )
    return {
        "stable": (
            before_workspace == after_workspace
            and before_evidence == after_evidence
            and before_passive_candidate == after_passive_candidate
        ),
        "workspace": after_workspace,
        "evidence_sha256": after_evidence,
        "task": task,
    }


def _result_digest(value: bytes) -> dict[str, Any]:
    return {"bytes": len(value), "sha256": _sha256_bytes(value)}


def _run_check(
    check: dict[str, Any],
    root: Path,
    stage: str,
    *,
    task_context: dict[str, Any] | None = None,
    candidate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    check_id = check["id"]
    if stage not in _check_stages(check, check_id=check_id):
        return {"id": check_id, "type": check["type"], "status": "skipped"}
    if check["type"] == "owner_review":
        return {"id": check_id, "type": "owner_review", "status": "pending"}
    if check["type"] == "external_receipt":
        if task_context is None or candidate is None:
            raise ContractError("external receipts require a task-oracle contract")
        before_source = _external_receipt_fingerprint(
            {"_task_context": task_context}, root, check
        )
        proof = task_oracle.load_external_proof(check, task_context, candidate, root)
        after_source = _external_receipt_fingerprint(
            {"_task_context": task_context}, root, check
        )
        if before_source != after_source:
            proof = task_oracle.proof_receipt(
                check,
                task_context,
                candidate,
                status="CONFLICT",
                reason="external evaluator receipt changed while it was being read",
                evidence_refs=[],
            )
        return {
            "id": check_id,
            "type": "external_receipt",
            "status": "pass" if proof["status"] == "PASS" else "fail",
            "proof_status": proof["status"],
            "proof": proof,
            "source": after_source,
        }
    if check["type"] == "artifact":
        fingerprint = _artifact_fingerprint(root, check)
        size = int(fingerprint.get("bytes", 0))
        passed = fingerprint.get("state") == "file" and size >= check.get("min_bytes", 1)
        result: dict[str, Any] = {
            "id": check_id,
            "type": "artifact",
            "status": "pass" if passed else "fail",
            **{key: value for key, value in fingerprint.items() if key != "id"},
            "source": fingerprint,
        }
        if task_context is not None and candidate is not None:
            result["proof"] = task_oracle.proof_receipt(
                check,
                task_context,
                candidate,
                status="PASS" if passed else "FAIL",
                reason=(
                    "artifact exists and meets the declared minimum size"
                    if passed
                    else "artifact is missing or below the declared minimum size"
                ),
                evidence_refs=[
                    f"sha256:{fingerprint['sha256']}"
                    if fingerprint.get("sha256")
                    else f"artifact:{fingerprint.get('state', 'unknown')}:{size}"
                ],
            )
        return result

    started = time.monotonic()
    try:
        completed = subprocess.run(
            check["argv"],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=float(check.get("timeout_seconds", 120)),
            shell=False,
            env=(
                task_oracle.command_environment(
                    task_context,
                    check.get("consumes_bindings", []),
                )
                if task_context is not None
                else None
            ),
        )
        result = {
            "id": check_id,
            "type": "command",
            "status": "pass" if completed.returncode == 0 else "fail",
            "exit_code": completed.returncode,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "stdout": _result_digest(completed.stdout),
            "stderr": _result_digest(completed.stderr),
        }
        if task_context is not None and candidate is not None:
            result["proof"] = task_oracle.proof_receipt(
                check,
                task_context,
                candidate,
                status="PASS" if completed.returncode == 0 else "FAIL",
                reason=f"command exited with code {completed.returncode}",
                evidence_refs=[
                    f"stdout-sha256:{result['stdout']['sha256']}",
                    f"stderr-sha256:{result['stderr']['sha256']}",
                ],
            )
        return result
    except subprocess.TimeoutExpired as exc:
        return {
            "id": check_id,
            "type": "command",
            "status": "fail",
            "error": "timeout",
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "stdout": _result_digest(exc.stdout or b""),
            "stderr": _result_digest(exc.stderr or b""),
        }
    except OSError as exc:
        return {
            "id": check_id,
            "type": "command",
            "status": "fail",
            "error": type(exc).__name__,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
        }


def _run_checks(
    checks: list[dict[str, Any]],
    root: Path,
    stage: str,
    *,
    task_context: dict[str, Any] | None = None,
    candidate: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for check in checks:
        if check.get("type") != "artifact":
            results[check["id"]] = _run_check(
                check,
                root,
                stage,
                task_context=task_context,
                candidate=candidate,
            )
    for check in checks:
        if check.get("type") == "artifact":
            results[check["id"]] = _run_check(
                check,
                root,
                stage,
                task_context=task_context,
                candidate=candidate,
            )
    return [results[check["id"]] for check in checks]


def _evaluate_acceptance(
    contract: dict[str, Any], checks: list[dict[str, Any]], stage: str
) -> list[dict[str, Any]]:
    status_by_id = {item["id"]: item["status"] for item in checks}
    results: list[dict[str, Any]] = []
    for condition in contract["acceptance"]:
        evidence = condition["evidence"]
        statuses = [status_by_id.get(check_id, "missing") for check_id in evidence]
        if stage != "final":
            status = "not_evaluated"
        elif any(item in {"fail", "missing", "skipped"} for item in statuses):
            status = "fail"
        elif all(item == "pass" for item in statuses):
            status = "pass"
        else:
            status = "pending"
        results.append(
            {
                "id": condition["id"],
                "status": status,
                "evidence": evidence,
            }
        )
    return results


def _receipt(
    *,
    contract_sha256: str,
    workspace: dict[str, Any],
    evidence_sha256: str,
    stage: str,
    outcome: str,
    checks: list[dict[str, Any]],
    acceptance: list[dict[str, Any]],
    next_objective: str,
    reason: str = "",
    task_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    open_checks = [item["id"] for item in checks if item.get("status") in {"fail", "pending"}]
    open_acceptance = [
        item["id"] for item in acceptance if item.get("status") != "pass"
    ]
    writeback = "AUTO_CLOSEOUT" if outcome == "CONVERGED" else "NONE"
    proof_eligible = outcome in {"PASS", "CANDIDATE_READY", "CONVERGED"}
    value = {
        "schema": RECEIPT_SCHEMA,
        "recorded_at": _now(),
        "contract_sha256": contract_sha256,
        "evidence_sha256": evidence_sha256,
        "workspace": workspace,
        "task": task_state,
        "stage": stage,
        "outcome": outcome,
        "proof_eligible": proof_eligible,
        "checks": checks,
        "acceptance": acceptance,
        "open_check_ids": open_checks,
        "open_acceptance_ids": open_acceptance,
        "next_objective": next_objective,
        "reason": reason,
        "checkpoint_expectation": (
            "normal_git_checkpoint_before_next_destructive_episode"
            if outcome == "PASS"
            else "none"
        ),
        "threecan_writeback": {"eligible_trigger": writeback, "performed": False},
    }
    value["receipt_sha256"] = _sha256_bytes(_json_bytes(value))
    return value


def verify(
    root: Path,
    contract_path: Path,
    receipt_path: Path,
    *,
    stage: str,
    next_objective: str,
) -> dict[str, Any]:
    contract, contract_sha256 = load_contract(root, contract_path)
    if contract is None or contract_sha256 is None:
        raise ContractError("no convergence contract found")
    ensure_revision_boundary(contract, read_receipt(root, receipt_path))
    before = _stable_current_capture(
        contract, root, contract_path, receipt_path
    )
    before_workspace = before["workspace"]
    before_task = before["task"]
    before_evidence = evidence_fingerprint(contract, root, role="candidate")
    task_context = contract.get("_task_context")
    candidate = (before_task or {}).get("candidate")
    candidate_status = str((candidate or {}).get("status") or "PASS")
    if candidate_status == "PASS":
        checks = _run_checks(
            contract.get("checks", []),
            root,
            stage,
            task_context=task_context if isinstance(task_context, dict) else None,
            candidate=candidate if isinstance(candidate, dict) else None,
        )
    else:
        checks = [
            {
                "id": check["id"],
                "type": check["type"],
                "status": "skipped",
                "reason": "current candidate is not bound to the active task run",
            }
            for check in contract.get("checks", [])
        ]
    after_contract, after_contract_sha256 = load_contract(root, contract_path)
    if after_contract is None or after_contract_sha256 is None:
        raise ContractError("convergence contract disappeared during verification")
    after = _stable_current_capture(
        after_contract, root, contract_path, receipt_path
    )
    after_workspace = after["workspace"]
    after_task = after["task"]
    after_candidate_evidence = evidence_fingerprint(
        after_contract, root, role="candidate"
    )
    after_evidence = after["evidence_sha256"]
    after_sources = {
        item["id"]: item
        for item in evidence_snapshot(after_contract, root)
        if item.get("type") in {"artifact", "external_receipt"}
    }
    checked_sources_match = all(
        item.get("source") == after_sources.get(item["id"])
        for item in checks
        if item.get("type") in {"artifact", "external_receipt"}
        and "source" in item
    )
    verification_changed = (
        not before["stable"]
        or not after["stable"]
        or contract_sha256 != after_contract_sha256
        or before_workspace.get("fingerprint") != after_workspace.get("fingerprint")
        or before_evidence != after_candidate_evidence
        or before_task != after_task
        or not checked_sources_match
    )
    if candidate_status == "PASS" and not any(
        item["type"] != "owner_review" and item["status"] != "skipped" for item in checks
    ):
        raise ContractError(f"no automated check applies to the {stage} stage")
    automated_failures = [
        item for item in checks if item["type"] != "owner_review" and item["status"] == "fail"
    ]
    acceptance = _evaluate_acceptance(contract, checks, stage)
    reason = ""
    current_candidate_status = str(
        ((after_task or {}).get("candidate") or {}).get("status") or "PASS"
    )
    typed_proof_outcomes = [
        str(item.get("proof_status"))
        for item in checks
        if item.get("proof_status")
        not in {None, "PASS", "FAIL"}
    ]
    if verification_changed:
        outcome = "CONFLICT"
        reason = (
            "Contract, workspace, candidate, or artifact evidence changed while checks were running."
        )
    elif current_candidate_status != "PASS":
        outcome = current_candidate_status
        reason = str(((after_task or {}).get("candidate") or {}).get("reason") or "")
    elif typed_proof_outcomes:
        outcome = typed_proof_outcomes[0]
        reason = "A declared proof receipt is not valid for the current task candidate."
    elif automated_failures:
        outcome = "FAIL"
    elif stage == "episode":
        outcome = "PASS"
    elif any(item["status"] == "fail" for item in acceptance):
        outcome = "FAIL"
    elif any(item["status"] == "pending" for item in acceptance):
        outcome = "CANDIDATE_READY"
    else:
        outcome = "CONVERGED"
    if stage == "episode" and outcome == "PASS" and not next_objective.strip():
        raise ContractError("episode verification requires --next-objective")
    value = _receipt(
        contract_sha256=after_contract_sha256,
        workspace=after_workspace,
        evidence_sha256=after_evidence,
        stage=stage,
        outcome=outcome,
        checks=checks,
        acceptance=acceptance,
        next_objective=next_objective.strip(),
        reason=reason,
        task_state=after_task,
    )
    value = _write_receipt_atomic(_receipt_path(root, receipt_path), value)
    return value


def record_typed(
    root: Path,
    contract_path: Path,
    receipt_path: Path,
    *,
    outcome: str,
    reason: str,
    next_objective: str,
) -> dict[str, Any]:
    contract, contract_sha256 = load_contract(root, contract_path)
    if contract is None or contract_sha256 is None:
        raise ContractError("no convergence contract found")
    if not reason.strip():
        raise ContractError("typed incomplete state requires a non-empty reason")
    previous = read_receipt(root, receipt_path) or {}
    ensure_revision_boundary(contract, previous)
    # A typed incomplete report must not launder checks that passed against an
    # older candidate into current evidence. Only verify() can create PASS.
    previous_acceptance = _evaluate_acceptance(contract, [], "current")
    captured = _stable_current_capture(
        contract, root, contract_path, receipt_path
    )
    recorded_outcome = outcome if captured["stable"] else "CONFLICT"
    recorded_reason = (
        reason.strip()
        if captured["stable"]
        else "Workspace, evidence, or candidate changed while recording the typed state."
    )
    value = _receipt(
        contract_sha256=contract_sha256,
        workspace=captured["workspace"],
        evidence_sha256=captured["evidence_sha256"],
        stage=str(previous.get("stage") or "current"),
        outcome=recorded_outcome,
        checks=[],
        acceptance=previous_acceptance,
        next_objective=next_objective.strip(),
        reason=recorded_reason,
        task_state=captured["task"],
    )
    value = _write_receipt_atomic(_receipt_path(root, receipt_path), value)
    return value


def _session_contract_context(
    contract: dict[str, Any],
    receipt: dict[str, Any] | None,
    current: bool,
    current_task: dict[str, Any] | None = None,
) -> str:
    lines = [
        "Convergence contract is active. Preserve this accepted task boundary across the session.",
        f"Goal: {contract['goal'].strip()}",
        "Acceptance:",
    ]
    lines.extend(
        f"- {item['id']}: {item['text']} (evidence: {', '.join(item['evidence'])})"
        for item in contract["acceptance"]
    )
    non_goals = contract.get("non_goals", [])
    if non_goals:
        lines.append("Non-goals:")
        lines.extend(f"- {item}" for item in non_goals)
    if receipt:
        freshness = "current" if current else "stale"
        lines.append(f"Latest evidence receipt: {receipt.get('outcome', 'UNKNOWN')} ({freshness}).")
        if receipt.get("open_check_ids"):
            lines.append("Open checks: " + ", ".join(receipt["open_check_ids"]))
        if receipt.get("open_acceptance_ids"):
            lines.append(
                "Open acceptance: " + ", ".join(receipt["open_acceptance_ids"])
            )
        if receipt.get("next_objective"):
            objective_label = (
                "Next objective: "
                if current
                else "Previous next objective (stale; re-evaluate): "
            )
            lines.append(objective_label + str(receipt["next_objective"]))
        if receipt.get("reason"):
            lines.append("Typed reason: " + str(receipt["reason"]))
    else:
        lines.append("Latest evidence receipt: missing.")
    context = contract.get("_task_context")
    if isinstance(context, dict):
        task_hook = context["task_hook"]
        lines.extend(
            [
                f"Task family/revision: {task_hook['task_family']} / {task_hook['revision']}",
                f"Run: {context['run_id']}",
                "Revision state: " + task_hook["status"],
            ]
        )
        candidate_state = (current_task or {}).get("candidate", {})
        if candidate_state.get("fingerprint"):
            lines.append(
                "Current candidate: "
                + str(candidate_state["fingerprint"])
                + f" ({candidate_state.get('status', 'UNKNOWN')})"
            )
    lines.append(
        "Git and validation receipts prove candidate state; owner acceptance, merge, deployment, and publication remain separate decisions."
    )
    text = "\n".join(lines)
    if len(text) > MAX_CONTEXT_CHARS:
        raise ContractError("compact convergence context exceeds the configured limit")
    return text


def _hook_input_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return str(value)


def _guard_denial(
    contract: dict[str, Any],
    payload: dict[str, Any],
    receipt: dict[str, Any] | None,
    current: bool,
) -> str | None:
    tool_name = str(payload.get("tool_name") or "")
    tool_input = _hook_input_text(payload.get("tool_input", {}))
    passed = {
        item.get("id")
        for item in (receipt or {}).get("checks", [])
        if isinstance(item, dict) and item.get("status") == "pass"
    }
    proof_eligible = bool((receipt or {}).get("proof_eligible"))
    for guard in contract.get("guards", []):
        tool_glob = guard.get("tool_name_glob")
        input_contains = guard.get("input_contains")
        if tool_glob and not fnmatch.fnmatchcase(tool_name, tool_glob):
            continue
        if input_contains and input_contains not in tool_input:
            continue
        required = set(guard.get("requires_check_ids", []))
        missing = sorted(required - passed)
        if not current or not proof_eligible or missing:
            detail = (
                ", ".join(missing)
                if missing
                else "current eligible evidence receipt"
            )
            return f"Convergence guard requires {detail} before this declared high-cost operation."
    return None


def _guard_matches(guard: dict[str, Any], payload: dict[str, Any]) -> bool:
    tool_name = str(payload.get("tool_name") or "")
    tool_input = _hook_input_text(payload.get("tool_input", {}))
    return bool(
        (
            not guard.get("tool_name_glob")
            or fnmatch.fnmatchcase(tool_name, guard["tool_name_glob"])
        )
        and (
            not guard.get("input_contains")
            or guard["input_contains"] in tool_input
        )
    )


def _stop_report(receipt: dict[str, Any], outcome: str) -> str:
    parts = [
        f"Current convergence outcome is {outcome}.",
        "Do not claim completion; explicitly report the typed outcome and open evidence.",
    ]
    if receipt.get("open_acceptance_ids"):
        parts.append("Open acceptance: " + ", ".join(receipt["open_acceptance_ids"]) + ".")
    if receipt.get("open_check_ids"):
        parts.append("Open checks: " + ", ".join(receipt["open_check_ids"]) + ".")
    if receipt.get("next_objective"):
        parts.append("Next objective: " + str(receipt["next_objective"]))
    return " ".join(parts)


def run_hook(root: Path, contract_path: Path, receipt_path: Path) -> int:
    event = ""
    stop_hook_active = False
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("hook input must be a JSON object")
        event = str(payload.get("hook_event_name") or "")
        stop_hook_active = bool(payload.get("stop_hook_active"))
        contract, contract_sha256 = load_contract(root, contract_path)
        if contract is None or contract_sha256 is None:
            previous = read_receipt(root, receipt_path)
            if (
                previous is None
                and event == "SessionStart"
                and payload.get("source") in {"startup", "resume", "compact"}
                and _environment_selection(root, contract_path) is not None
            ):
                contract, contract_sha256 = load_contract(root, contract_path)
        if contract is None or contract_sha256 is None:
            previous = read_receipt(root, receipt_path)
            if previous is None:
                print("{}")
                return 0
            unavailable = (
                "Convergence contract is missing after prior activation. "
                "Current convergence is UNAVAILABLE; do not claim completion."
            )
            if event == "Stop":
                output = (
                    {"systemMessage": unavailable + " Report PARTIAL with the exact gap."}
                    if stop_hook_active
                    else {"decision": "block", "reason": unavailable}
                )
            elif event in {"SessionStart", "PreToolUse"}:
                output = {"systemMessage": unavailable + " The development path failed open."}
            else:
                output = {}
            print(json.dumps(output, ensure_ascii=False))
            return 0
        if contract.get("status", "active") != "active":
            if (
                contract.get("schema") == TASK_CONTRACT_SCHEMA
                and not closeout_is_valid(
                    contract,
                    read_receipt(root, receipt_path),
                    root,
                    contract_path,
                    receipt_path,
                )
            ):
                raise task_oracle.TaskOracleError(
                    "REVISION_PENDING",
                    "closeout does not reference the retained final CONVERGED receipt",
                )
            print("{}")
            return 0
        if event == "PreToolUse" and not any(
            _guard_matches(guard, payload) for guard in contract.get("guards", [])
        ):
            print("{}")
            return 0
        session_sources = {"startup", "resume", "compact"}
        if event == "SessionStart" and payload.get("source") not in session_sources:
            print("{}")
            return 0
        if event not in {"SessionStart", "PreToolUse", "Stop"}:
            print("{}")
            return 0
        receipt = read_receipt(root, receipt_path)
        ensure_revision_boundary(contract, receipt)
        captured = _stable_current_capture(
            contract, root, contract_path, receipt_path
        )
        workspace = captured["workspace"]
        current_evidence = captured["evidence_sha256"]
        current_task = captured["task"]
        current = bool(
            captured["stable"]
            and receipt_is_current(
                receipt,
                contract_sha256=contract_sha256,
                workspace=workspace,
                evidence_sha256=current_evidence,
                task_state=current_task,
            )
        )

        if event == "SessionStart" and payload.get("source") in session_sources:
            output = {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": _session_contract_context(
                        contract, receipt, current, current_task
                    ),
                }
            }
        elif event == "PreToolUse":
            denial = _guard_denial(contract, payload, receipt, current)
            output = (
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": denial,
                    }
                }
                if denial
                else {}
            )
        elif event == "Stop":
            outcome = str((receipt or {}).get("outcome") or "MISSING")
            if current and outcome == "CONVERGED":
                final_contract, final_contract_sha256 = load_contract(
                    root, contract_path
                )
                final_receipt = read_receipt(root, receipt_path)
                if (
                    final_contract is not None
                    and final_contract_sha256 == contract_sha256
                    and final_contract.get("status", "active") == "active"
                    and final_receipt == receipt
                ):
                    final = _stable_current_capture(
                        final_contract, root, contract_path, receipt_path
                    )
                    closing_contract, closing_contract_sha256 = load_contract(
                        root, contract_path
                    )
                    closing_receipt = read_receipt(root, receipt_path)
                    current = bool(
                        final["stable"]
                        and final["workspace"] == workspace
                        and final["evidence_sha256"] == current_evidence
                        and final["task"] == current_task
                        and closing_contract is not None
                        and closing_contract_sha256 == final_contract_sha256
                        and closing_receipt == final_receipt
                        and receipt_is_current(
                            final_receipt,
                            contract_sha256=final_contract_sha256,
                            workspace=final["workspace"],
                            evidence_sha256=final["evidence_sha256"],
                            task_state=final["task"],
                        )
                    )
                else:
                    current = False
                receipt = final_receipt
                outcome = str((receipt or {}).get("outcome") or "MISSING")
                output = {} if current and outcome == "CONVERGED" else None
            else:
                output = None
            if output is None and current and outcome in REPORTABLE_OUTCOMES:
                report = _stop_report(receipt or {}, outcome)
                output = (
                    {"systemMessage": report}
                    if stop_hook_active
                    else {"decision": "block", "reason": report}
                )
            elif output is None and stop_hook_active:
                output = {
                    "systemMessage": (
                        "Convergence evidence is missing or stale after one automatic continuation. "
                        "Report PARTIAL with the exact open evidence; do not claim completion."
                    )
                }
            elif output is None:
                next_objective = str((receipt or {}).get("next_objective") or "Run the declared checks and record a current receipt.")
                output = {
                    "decision": "block",
                    "reason": f"Convergence evidence is {outcome.lower()} or stale. Next objective: {next_objective}",
                }
        print(json.dumps(output, ensure_ascii=False))
        return 0
    except Exception as exc:
        typed_code = (
            exc.code if isinstance(exc, task_oracle.TaskOracleError) else "UNAVAILABLE"
        )
        unavailable = (
            f"Convergence hook is {typed_code}: "
            f"{exc}. Continue safe local work, but do not claim convergence."
        )
        if event == "Stop":
            output = (
                {"systemMessage": unavailable + " Report PARTIAL with the exact gap."}
                if stop_hook_active
                else {"decision": "block", "reason": unavailable}
            )
        else:
            output = {
                "systemMessage": unavailable + " The development path failed open."
            }
        print(
            json.dumps(output, ensure_ascii=False)
        )
        return 0


def _summary(
    root: Path, contract_path: Path, receipt_path: Path
) -> dict[str, Any]:
    contract, contract_sha256 = load_contract(root, contract_path)
    if contract is None or contract_sha256 is None:
        return {"ok": True, "status": "inactive", "reason": "contract_missing"}
    if contract.get("status", "active") != "active":
        receipt = read_receipt(root, receipt_path)
        return {
            "ok": True,
            "status": contract.get("status"),
            "closeout_valid": (
                closeout_is_valid(
                    contract, receipt, root, contract_path, receipt_path
                )
                if contract.get("schema") == TASK_CONTRACT_SCHEMA
                else True
            ),
            "receipt_outcome": (receipt or {}).get("outcome", "MISSING"),
            "task_family": (contract.get("_task_context") or {})
            .get("task_hook", {})
            .get("task_family"),
        }
    receipt = read_receipt(root, receipt_path)
    ensure_revision_boundary(contract, receipt)
    captured = _stable_current_capture(
        contract, root, contract_path, receipt_path
    )
    return {
        "ok": True,
        "status": contract.get("status", "active"),
        "goal": contract["goal"],
        "contract_sha256": contract_sha256,
        "receipt_outcome": (receipt or {}).get("outcome", "MISSING"),
        "receipt_current": bool(
            captured["stable"]
            and receipt_is_current(
                receipt,
                contract_sha256=contract_sha256,
                workspace=captured["workspace"],
                evidence_sha256=captured["evidence_sha256"],
                task_state=captured["task"],
            )
        ),
        "task": captured["task"],
        "threecan_writeback": (receipt or {}).get(
            "threecan_writeback", {"eligible_trigger": "NONE", "performed": False}
        ),
    }


def _parse_binding_args(values: list[str]) -> dict[str, Any]:
    bindings: dict[str, Any] = {}
    for value in values:
        name, separator, encoded = value.partition("=")
        if not separator or not task_oracle.ID_PATTERN.fullmatch(name):
            raise ContractError("--binding must use NAME=JSON with a valid name")
        if name in bindings:
            raise ContractError(f"duplicate binding: {name}")
        try:
            bindings[name] = json.loads(encoded)
        except json.JSONDecodeError as exc:
            raise ContractError(f"binding {name} value must be valid JSON") from exc
    return bindings


def select_task(
    root: Path,
    contract_path: Path,
    registry_path: Path,
    *,
    task_family: str,
    run_id: str,
    confirmed_by: str,
    confirmation_ref: str,
    bindings: dict[str, Any],
    allowed_fallbacks: list[str],
) -> dict[str, Any]:
    output = _bounded_path(root, contract_path, label="contract path")
    if output.exists():
        raise ContractError(
            "refusing to replace an existing convergence selector; close out and archive it first"
        )
    registry_file = _bounded_path(root, registry_path, label="task registry path")
    registry_relative = registry_file.relative_to(root.resolve()).as_posix()
    families = task_oracle.load_task_registry(
        root, registry_relative, selected_family=task_family
    )
    entry = families.get(task_family)
    if entry is None:
        raise task_oracle.TaskOracleError(
            "UNAVAILABLE", f"task family is not registered: {task_family}"
        )
    digest = entry["sha256"]
    contract = {
        "schema": TASK_CONTRACT_SCHEMA,
        "status": "active",
        "scope": "current_repository_only",
        "run_id": run_id,
        "task_hook": {
            "path": entry["path"],
            "sha256": digest,
            "revision": entry["revision"],
        },
        "activation": {
            "task_hook_sha256": digest,
            "confirmed_revision": entry["revision"],
            "confirmed_by": confirmed_by,
            "confirmation_ref": confirmation_ref,
        },
        "bindings": bindings,
        "allowed_fallbacks": allowed_fallbacks,
        "non_goals": [
            "Owner acceptance, merge, deployment, publication, and 3CAN writeback remain separate decisions."
        ],
        "guards": [],
    }
    validate_contract(contract, root)
    _write_json_atomic(
        output,
        contract,
        max_bytes=MAX_CONTROL_JSON_BYTES,
        label="generated contract",
    )
    return {
        "ok": True,
        "status": "selected",
        "task_family": task_family,
        "revision": entry["revision"],
        "task_hook_sha256": digest,
        "run_id": run_id,
        "contract_path": output.relative_to(root.resolve()).as_posix(),
    }


def _environment_selection(
    root: Path, contract_path: Path
) -> dict[str, Any] | None:
    task_family = os.environ.get("THREECAN_TASK_FAMILY", "").strip()
    if not task_family:
        return None
    run_id = os.environ.get("THREECAN_RUN_ID", "").strip()
    confirmed_by = os.environ.get("THREECAN_TASK_CONFIRMED_BY", "").strip()
    confirmation_ref = os.environ.get("THREECAN_TASK_CONFIRMATION_REF", "").strip()
    if not run_id or not confirmed_by or not confirmation_ref:
        raise ContractError(
            "explicit task-family startup requires run id, confirmer, and confirmation reference"
        )
    raw_bindings = os.environ.get("THREECAN_TASK_BINDINGS_JSON", "{}")
    raw_fallbacks = os.environ.get("THREECAN_ALLOWED_FALLBACKS_JSON", "[]")
    if (
        len(raw_bindings.encode("utf-8")) > task_oracle.MAX_BINDINGS_BYTES
        or len(raw_fallbacks.encode("utf-8")) > task_oracle.MAX_BINDINGS_BYTES
    ):
        raise ContractError("startup selection inputs exceed the bounded size")
    try:
        bindings = json.loads(raw_bindings)
        allowed_fallbacks = json.loads(raw_fallbacks)
    except json.JSONDecodeError as exc:
        raise ContractError("startup selection inputs must be valid JSON") from exc
    if not isinstance(bindings, dict) or not isinstance(allowed_fallbacks, list):
        raise ContractError("startup bindings must be an object and fallbacks a list")
    registry_path = Path(
        os.environ.get("THREECAN_TASK_REGISTRY", str(DEFAULT_TASK_REGISTRY))
    )
    return select_task(
        root,
        contract_path,
        registry_path,
        task_family=task_family,
        run_id=run_id,
        confirmed_by=confirmed_by,
        confirmation_ref=confirmation_ref,
        bindings=bindings,
        allowed_fallbacks=allowed_fallbacks,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify a project convergence contract.")
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--receipt", type=Path, default=DEFAULT_RECEIPT)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate", help="Validate the active contract without running checks.")
    sub.add_parser("status", help="Show compact contract and receipt status.")
    sub.add_parser("hook", help="Run as a Codex lifecycle hook; read hook JSON from stdin.")
    task_parser = sub.add_parser(
        "task-digest", help="Validate a Task Hook and print its canonical digest."
    )
    task_parser.add_argument("--task-hook", type=Path, required=True)
    registry_parser = sub.add_parser(
        "validate-registry",
        help="Run the complete reusable-family registry audit outside native hooks.",
    )
    registry_parser.add_argument(
        "--registry", type=Path, default=DEFAULT_TASK_REGISTRY
    )
    select_parser = sub.add_parser(
        "select-task",
        help="Select an exact reusable task family for a new run.",
    )
    select_parser.add_argument(
        "--registry", type=Path, default=DEFAULT_TASK_REGISTRY
    )
    select_parser.add_argument("--task-family", required=True)
    select_parser.add_argument("--run-id", required=True)
    select_parser.add_argument(
        "--confirmed-by",
        choices=["owner", "independent_reviewer"],
        required=True,
    )
    select_parser.add_argument("--confirmation-ref", required=True)
    select_parser.add_argument("--binding", action="append", default=[])
    select_parser.add_argument("--allowed-fallback", action="append", default=[])
    verify_parser = sub.add_parser("verify", help="Run checks and write an evidence receipt.")
    verify_parser.add_argument("--stage", choices=["episode", "final"], required=True)
    verify_parser.add_argument("--next-objective", default="")
    record_parser = sub.add_parser("record", help="Record an honest typed incomplete state.")
    record_parser.add_argument("--status", choices=sorted(TYPED_INCOMPLETE_OUTCOMES), required=True)
    record_parser.add_argument("--reason", required=True)
    record_parser.add_argument("--next-objective", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.root.resolve()
    try:
        if args.command == "validate":
            contract, digest = load_contract(root, args.contract)
            if contract is None:
                raise ContractError("no convergence contract found")
            if contract.get("status", "active") == "active":
                ensure_revision_boundary(
                    contract, read_receipt(root, args.receipt)
                )
            output = {"ok": True, "status": "valid", "contract_sha256": digest}
        elif args.command == "task-digest":
            task_path = _bounded_path(root, args.task_hook, label="task hook path")
            task_value = _read_json(task_path)
            validated = task_oracle.validate_task_hook(task_value, root)
            output = {
                "ok": True,
                "status": "valid",
                "task_family": validated["task_family"],
                "revision": validated["revision"],
                "task_hook_sha256": task_oracle.sha256_json(validated),
            }
        elif args.command == "validate-registry":
            registry_path = _bounded_path(
                root, args.registry, label="task registry path"
            )
            registry_relative = registry_path.relative_to(root).as_posix()
            families = task_oracle.load_task_registry(root, registry_relative)
            output = {
                "ok": True,
                "status": "valid",
                "family_count": len(families),
            }
        elif args.command == "select-task":
            output = select_task(
                root,
                args.contract,
                args.registry,
                task_family=args.task_family,
                run_id=args.run_id,
                confirmed_by=args.confirmed_by,
                confirmation_ref=args.confirmation_ref,
                bindings=_parse_binding_args(args.binding),
                allowed_fallbacks=args.allowed_fallback,
            )
        elif args.command == "status":
            output = _summary(root, args.contract, args.receipt)
        elif args.command == "verify":
            output = verify(
                root,
                args.contract,
                args.receipt,
                stage=args.stage,
                next_objective=args.next_objective,
            )
        elif args.command == "record":
            output = record_typed(
                root,
                args.contract,
                args.receipt,
                outcome=args.status,
                reason=args.reason,
                next_objective=args.next_objective,
            )
        elif args.command == "hook":
            return run_hook(root, args.contract, args.receipt)
        else:
            raise ContractError(f"unsupported command: {args.command}")
        print(json.dumps(output, ensure_ascii=False, indent=2))
        if args.command == "verify":
            expected = "PASS" if args.stage == "episode" else "CONVERGED"
            return 0 if output.get("outcome") == expected else 2
        if args.command == "record" and output.get("outcome") != args.status:
            return 2
        return 0
    except (
        ContractError,
        json.JSONDecodeError,
        OSError,
        RuntimeError,
        subprocess.SubprocessError,
        task_oracle.TaskOracleError,
    ) as exc:
        status = exc.code if isinstance(exc, task_oracle.TaskOracleError) else "UNAVAILABLE"
        print(
            json.dumps(
                {"ok": False, "status": status, "error": str(exc)},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
