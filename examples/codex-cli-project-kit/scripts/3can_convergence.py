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


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACT = Path(".codex/convergence.json")
DEFAULT_RECEIPT = Path("test-results/3can/convergence/receipt.json")
CONTRACT_SCHEMA = "3can.convergence-contract/v1"
RECEIPT_SCHEMA = "3can.convergence-receipt/v1"
REPORTABLE_OUTCOMES = {
    "CANDIDATE_READY",
    "BLOCKED",
    "UNAVAILABLE",
    "CONFLICT",
    "PARTIAL",
}
TYPED_INCOMPLETE_OUTCOMES = {
    "BLOCKED",
    "UNAVAILABLE",
    "CONFLICT",
    "PARTIAL",
}
MAX_CONTEXT_CHARS = 4_000
MAX_HASH_BYTES = 8 * 1024 * 1024


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


def _write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
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


def validate_contract(value: Any, root: Path) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError("contract must be a JSON object")
    if value.get("schema") != CONTRACT_SCHEMA:
        raise ContractError(f"contract schema must be {CONTRACT_SCHEMA}")
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
        elif stages != ["final"]:
            raise ContractError(f"owner_review check {check_id} must use only the final stage")
    if not automated_final:
        raise ContractError("at least one automated check must run at the final stage")

    acceptance = value.get("acceptance")
    if not isinstance(acceptance, list) or not acceptance:
        raise ContractError("acceptance must be a non-empty list of evidence bindings")
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

    guards = value.get("guards", [])
    if not isinstance(guards, list):
        raise ContractError("guards must be a list")
    for index, guard in enumerate(guards):
        if not isinstance(guard, dict):
            raise ContractError(f"guard {index} must be an object")
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
            raise ContractError(f"guard {index} references unknown checks: {', '.join(unknown)}")
        if not guard.get("tool_name_glob") and not guard.get("input_contains"):
            raise ContractError(f"guard {index} must declare a tool glob or input substring")
    return value


def load_contract(root: Path, contract_path: Path) -> tuple[dict[str, Any] | None, str | None]:
    path = _bounded_path(root, contract_path, label="contract path")
    if not path.is_file():
        return None, None
    raw = path.read_bytes()
    value = json.loads(raw.decode("utf-8-sig"))
    return validate_contract(value, root), _sha256_bytes(_json_bytes(value))


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=20,
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
    result: dict[str, Any] = {"id": check_id, "path": relative}
    if not path.is_file():
        result["state"] = "missing"
        return result
    stat = path.stat()
    result.update({"state": "file", "bytes": stat.st_size})
    if stat.st_size <= MAX_HASH_BYTES:
        result["sha256"] = _sha256_bytes(path.read_bytes())
    else:
        # Large artifacts should normally use a task-specific manifest or
        # candidate provider. Size + mtime keeps the generic hook bounded while
        # still making the recorded evidence re-verifiable.
        result["mtime_ns"] = stat.st_mtime_ns
    return result


def evidence_fingerprint(contract: dict[str, Any], root: Path) -> str:
    artifacts = [
        _artifact_fingerprint(root, check)
        for check in contract.get("checks", [])
        if check.get("type") == "artifact"
    ]
    return _sha256_bytes(_json_bytes(artifacts))


def workspace_fingerprint(root: Path) -> dict[str, Any]:
    resolved = root.resolve()
    top = _git(resolved, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        return {
            "kind": "directory",
            "workspace_sha256": _sha256_bytes(str(resolved).casefold().encode("utf-8")),
        }
    prefix = _git(resolved, "rev-parse", "--show-prefix")
    branch = _git(resolved, "branch", "--show-current")
    head = _git(resolved, "rev-parse", "HEAD")
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
    if any(item.returncode != 0 for item in (branch, head, prefix, status)):
        raise RuntimeError("Git workspace fingerprint failed")
    git_top = Path(top.stdout.decode("utf-8", errors="surrogateescape").strip())
    submodule_output = b""
    if (git_top / ".gitmodules").is_file():
        submodules = _git(resolved, "submodule", "status", "--recursive")
        if submodules.returncode != 0:
            raise RuntimeError("Git submodule fingerprint failed")
        submodule_output = submodules.stdout
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
        relative_paths.append(normalized)
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
    state = {
        "kind": "git",
        "workspace_sha256": _sha256_bytes(str(resolved).casefold().encode("utf-8")),
        "branch": branch.stdout.decode("utf-8", errors="replace").strip(),
        "head": head.stdout.decode("ascii", errors="replace").strip(),
        "porcelain_sha256": _sha256_bytes(status.stdout),
        "changed_file_count": len(files),
        "changed_files_sha256": _sha256_bytes(_json_bytes(files)),
    }
    state["fingerprint"] = _sha256_bytes(_json_bytes(state))
    return state


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
    value = _read_json(path)
    return value if isinstance(value, dict) else None


def receipt_is_current(
    receipt: dict[str, Any] | None,
    *,
    contract_sha256: str,
    workspace: dict[str, Any],
    evidence_sha256: str,
) -> bool:
    return bool(
        receipt
        and receipt.get("schema") == RECEIPT_SCHEMA
        and receipt.get("contract_sha256") == contract_sha256
        and receipt.get("evidence_sha256") == evidence_sha256
        and workspace.get("kind") == "git"
        and bool(workspace.get("fingerprint"))
        and receipt.get("workspace", {}).get("fingerprint") == workspace.get("fingerprint")
    )


def _result_digest(value: bytes) -> dict[str, Any]:
    return {"bytes": len(value), "sha256": _sha256_bytes(value)}


def _run_check(check: dict[str, Any], root: Path, stage: str) -> dict[str, Any]:
    check_id = check["id"]
    if stage not in _check_stages(check, check_id=check_id):
        return {"id": check_id, "type": check["type"], "status": "skipped"}
    if check["type"] == "owner_review":
        return {"id": check_id, "type": "owner_review", "status": "pending"}
    if check["type"] == "artifact":
        fingerprint = _artifact_fingerprint(root, check)
        size = int(fingerprint.get("bytes", 0))
        passed = fingerprint.get("state") == "file" and size >= check.get("min_bytes", 1)
        result: dict[str, Any] = {
            "id": check_id,
            "type": "artifact",
            "status": "pass" if passed else "fail",
            **{key: value for key, value in fingerprint.items() if key != "id"},
        }
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
        )
        return {
            "id": check_id,
            "type": "command",
            "status": "pass" if completed.returncode == 0 else "fail",
            "exit_code": completed.returncode,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "stdout": _result_digest(completed.stdout),
            "stderr": _result_digest(completed.stderr),
        }
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
) -> dict[str, Any]:
    open_checks = [item["id"] for item in checks if item.get("status") in {"fail", "pending"}]
    open_acceptance = [
        item["id"] for item in acceptance if item.get("status") != "pass"
    ]
    writeback = "AUTO_CLOSEOUT" if outcome == "CONVERGED" else "NONE"
    proof_eligible = outcome in {"PASS", "CANDIDATE_READY", "CONVERGED"}
    return {
        "schema": RECEIPT_SCHEMA,
        "recorded_at": _now(),
        "contract_sha256": contract_sha256,
        "evidence_sha256": evidence_sha256,
        "workspace": workspace,
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
    before_workspace = workspace_fingerprint(root)
    before_evidence = evidence_fingerprint(contract, root)
    checks = [_run_check(check, root, stage) for check in contract.get("checks", [])]
    after_contract, after_contract_sha256 = load_contract(root, contract_path)
    if after_contract is None or after_contract_sha256 is None:
        raise ContractError("convergence contract disappeared during verification")
    after_workspace = workspace_fingerprint(root)
    after_evidence = evidence_fingerprint(after_contract, root)
    verification_changed = (
        contract_sha256 != after_contract_sha256
        or before_workspace.get("fingerprint") != after_workspace.get("fingerprint")
        or before_evidence != after_evidence
    )
    if not any(
        item["type"] != "owner_review" and item["status"] != "skipped" for item in checks
    ):
        raise ContractError(f"no automated check applies to the {stage} stage")
    automated_failures = [
        item for item in checks if item["type"] != "owner_review" and item["status"] == "fail"
    ]
    acceptance = _evaluate_acceptance(contract, checks, stage)
    reason = ""
    if verification_changed:
        outcome = "CONFLICT"
        reason = "Contract, workspace, or artifact evidence changed while checks were running."
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
    )
    _write_json_atomic(_receipt_path(root, receipt_path), value)
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
    # A typed incomplete report must not launder checks that passed against an
    # older candidate into current evidence. Only verify() can create PASS.
    previous_acceptance = _evaluate_acceptance(contract, [], "current")
    value = _receipt(
        contract_sha256=contract_sha256,
        workspace=workspace_fingerprint(root),
        evidence_sha256=evidence_fingerprint(contract, root),
        stage=str(previous.get("stage") or "current"),
        outcome=outcome,
        checks=[],
        acceptance=previous_acceptance,
        next_objective=next_objective.strip(),
        reason=reason.strip(),
    )
    _write_json_atomic(_receipt_path(root, receipt_path), value)
    return value


def _compact_contract_context(
    contract: dict[str, Any], receipt: dict[str, Any] | None, current: bool
) -> str:
    lines = [
        "Convergence contract is active. Preserve this accepted task boundary after compaction.",
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
            print("{}")
            return 0
        if event == "PreToolUse" and not any(
            _guard_matches(guard, payload) for guard in contract.get("guards", [])
        ):
            print("{}")
            return 0
        if event == "SessionStart" and payload.get("source") != "compact":
            print("{}")
            return 0
        if event not in {"SessionStart", "PreToolUse", "Stop"}:
            print("{}")
            return 0
        workspace = workspace_fingerprint(root)
        current_evidence = evidence_fingerprint(contract, root)
        receipt = read_receipt(root, receipt_path)
        current = receipt_is_current(
            receipt,
            contract_sha256=contract_sha256,
            workspace=workspace,
            evidence_sha256=current_evidence,
        )

        if event == "SessionStart" and payload.get("source") == "compact":
            output = {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": _compact_contract_context(contract, receipt, current),
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
                output = {}
            elif current and outcome in REPORTABLE_OUTCOMES:
                report = _stop_report(receipt or {}, outcome)
                output = (
                    {"systemMessage": report}
                    if stop_hook_active
                    else {"decision": "block", "reason": report}
                )
            elif stop_hook_active:
                output = {
                    "systemMessage": (
                        "Convergence evidence is missing or stale after one automatic continuation. "
                        "Report PARTIAL with the exact open evidence; do not claim completion."
                    )
                }
            else:
                next_objective = str((receipt or {}).get("next_objective") or "Run the declared checks and record a current receipt.")
                output = {
                    "decision": "block",
                    "reason": f"Convergence evidence is {outcome.lower()} or stale. Next objective: {next_objective}",
                }
        print(json.dumps(output, ensure_ascii=False))
        return 0
    except Exception as exc:
        unavailable = (
            "Convergence hook is UNAVAILABLE: "
            f"{type(exc).__name__}. Continue safe local work, but do not claim convergence."
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
    workspace = workspace_fingerprint(root)
    current_evidence = evidence_fingerprint(contract, root)
    receipt = read_receipt(root, receipt_path)
    return {
        "ok": True,
        "status": contract.get("status", "active"),
        "goal": contract["goal"],
        "contract_sha256": contract_sha256,
        "receipt_outcome": (receipt or {}).get("outcome", "MISSING"),
        "receipt_current": receipt_is_current(
            receipt,
            contract_sha256=contract_sha256,
            workspace=workspace,
            evidence_sha256=current_evidence,
        ),
        "threecan_writeback": (receipt or {}).get(
            "threecan_writeback", {"eligible_trigger": "NONE", "performed": False}
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify a project convergence contract.")
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--receipt", type=Path, default=DEFAULT_RECEIPT)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate", help="Validate the active contract without running checks.")
    sub.add_parser("status", help="Show compact contract and receipt status.")
    sub.add_parser("hook", help="Run as a Codex lifecycle hook; read hook JSON from stdin.")
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
            output = {"ok": True, "status": "valid", "contract_sha256": digest}
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
        return 0 if output.get("outcome") != "FAIL" else 2
    except (
        ContractError,
        json.JSONDecodeError,
        OSError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as exc:
        print(
            json.dumps(
                {"ok": False, "status": "UNAVAILABLE", "error": str(exc)},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
