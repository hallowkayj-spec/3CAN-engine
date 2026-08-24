"""Deterministic task-contract, candidate, and proof bindings.

This module deliberately does not classify prompts, call an LLM, or understand
task domains. The project owns candidate and evaluator commands; this code only
checks that their receipts target the active run, task revision, and candidate.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any


TASK_HOOK_SCHEMA = "3can.task-hook/v1"
TASK_REGISTRY_SCHEMA = "3can.task-hook-registry/v1"
CANDIDATE_SCHEMA = "3can.candidate/v1"
PROOF_RECEIPT_SCHEMA = "3can.proof-receipt/v1"
EXECUTABLE_STATUSES = {
    "EPHEMERAL_ACTIVE",
    "REUSABLE_CANDIDATE",
    "REUSABLE_ACTIVE",
}
TASK_HOOK_STATUSES = EXECUTABLE_STATUSES | {
    "PROPOSED_REVISION",
    "SUPERSEDED",
    "RETIRED",
}
ORACLE_KINDS = {"DETERMINISTIC", "SEMANTIC", "RELATIONAL", "HUMAN"}
ORACLE_TYPES = {"command", "artifact", "external_receipt", "owner_review"}
PROOF_STATUSES = {
    "PASS",
    "FAIL",
    "MISSING",
    "PARTIAL",
    "CONTRADICTS",
    "UNREQUESTED",
    "STALE_EVIDENCE",
    "UNBOUND",
    "IMPLICIT_MUTABLE_BINDING",
    "FALLBACK_NOT_ALLOWED",
    "UNVERIFIABLE",
    "CONFLICT",
}
ID_PATTERN = re.compile(r"[A-Za-z0-9_.-]+")
MAX_BINDINGS_BYTES = 16 * 1024
MAX_PROVIDER_OUTPUT_BYTES = 256 * 1024
MAX_EXTERNAL_PROOF_BYTES = 8 * 1024
MAX_DIRECT_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_ORACLES = 16
MAX_ACCEPTANCE = 32
MAX_PROMOTION_RECEIPTS = 16
MAX_LINEAGE_COMMITS = 64
MAX_LINEAGE_BYTES = 4 * 1024 * 1024
MAX_TASK_HOOK_BYTES = 256 * 1024
MAX_REGISTRY_FAMILIES = 128


def _known_fields(value: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise TaskOracleError(
            "INVALID_TASK_HOOK",
            f"{label} has unsupported fields: {', '.join(unknown)}",
        )


class TaskOracleError(ValueError):
    """A typed deterministic task-oracle contract failure."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def task_semantics_sha256(task_hook: dict[str, Any]) -> str:
    semantic = {
        key: value
        for key, value in task_hook.items()
        if key not in {"status", "promotion", "transition"}
    }
    return sha256_json(semantic)


def _git(
    root: Path, *args: str, input_bytes: bytes | None = None
) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise TaskOracleError(
            "UNAVAILABLE", "Git revision lineage could not be inspected"
        ) from exc


def _repo_relative(root: Path, path: Path) -> str:
    prefix = _git(root, "rev-parse", "--show-prefix")
    if prefix.returncode != 0:
        raise TaskOracleError("REVISION_PENDING", "a Git worktree is required")
    relative = path.resolve().relative_to(root.resolve()).as_posix()
    return (
        prefix.stdout.decode("utf-8", errors="surrogateescape").strip() + relative
    ).replace("\\", "/")


def _head_json(root: Path, path: Path, label: str) -> Any:
    tracked = _git(root, "show", f"HEAD:{_repo_relative(root, path)}")
    if tracked.returncode != 0:
        raise TaskOracleError("REVISION_PENDING", f"{label} must exist in Git HEAD")
    if len(tracked.stdout) > MAX_TASK_HOOK_BYTES:
        raise TaskOracleError("INVALID_TASK_HOOK", f"{label} exceeds the bounded size")
    try:
        return json.loads(tracked.stdout.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TaskOracleError("REVISION_PENDING", f"{label} in Git HEAD is invalid") from exc


def _lineage_blobs(root: Path, object_names: list[str]) -> list[bytes]:
    request = "".join(f"{name}\n" for name in object_names).encode("utf-8")
    checked = _git(root, "cat-file", "--batch-check", input_bytes=request)
    if checked.returncode != 0:
        raise TaskOracleError("UNAVAILABLE", "Git revision lineage is unavailable")
    checked_lines = checked.stdout.splitlines()
    if len(checked_lines) != len(object_names):
        raise TaskOracleError("UNAVAILABLE", "Git revision lineage is truncated")
    available: list[str] = []
    total_bytes = 0
    for object_name, raw_line in zip(object_names, checked_lines, strict=True):
        if raw_line.endswith(b" missing"):
            continue
        fields = raw_line.rsplit(b" ", 2)
        if len(fields) != 3 or fields[1] != b"blob":
            raise TaskOracleError("UNAVAILABLE", "Git revision lineage is invalid")
        try:
            size = int(fields[2])
        except ValueError as exc:
            raise TaskOracleError("UNAVAILABLE", "Git revision lineage is invalid") from exc
        total_bytes += size
        if total_bytes > MAX_LINEAGE_BYTES:
            raise TaskOracleError(
                "UNAVAILABLE", "Git revision lineage exceeds the bounded read budget"
            )
        available.append(object_name)
    if not available:
        return []
    request = "".join(f"{name}\n" for name in available).encode("utf-8")
    loaded = _git(root, "cat-file", "--batch", input_bytes=request)
    if loaded.returncode != 0:
        raise TaskOracleError("UNAVAILABLE", "Git revision lineage is unavailable")
    stream = io.BytesIO(loaded.stdout)
    blobs: list[bytes] = []
    for _ in available:
        header = stream.readline().rstrip(b"\n")
        fields = header.rsplit(b" ", 2)
        if len(fields) != 3 or fields[1] != b"blob":
            raise TaskOracleError("UNAVAILABLE", "Git revision lineage is invalid")
        try:
            size = int(fields[2])
        except ValueError as exc:
            raise TaskOracleError("UNAVAILABLE", "Git revision lineage is invalid") from exc
        content = stream.read(size)
        if len(content) != size or stream.read(1) != b"\n":
            raise TaskOracleError("UNAVAILABLE", "Git revision lineage is truncated")
        blobs.append(content)
    return blobs


def validate_revision_lineage(
    root: Path, task_path: Path, task_hook: dict[str, Any]
) -> None:
    """Keep one repeatable revision semantically immutable across Git history."""
    if task_hook["lifecycle"] != "repeatable":
        return
    relative = task_path.resolve().relative_to(root.resolve()).as_posix()
    relative_path = Path(relative)
    if (
        relative_path.parent.as_posix() != ".codex/task-hooks"
        or relative_path.suffix != ".json"
    ):
        raise TaskOracleError(
            "INVALID_TASK_HOOK",
            "repeatable Task Hooks must be direct JSON files under .codex/task-hooks",
        )
    expected = task_semantics_sha256(task_hook)
    tracked_current = _head_json(root, task_path, "repeatable Task Hook")
    if (
        not isinstance(tracked_current, dict)
        or tracked_current.get("schema") != TASK_HOOK_SCHEMA
        or tracked_current.get("task_family") != task_hook["task_family"]
        or tracked_current.get("revision") != task_hook["revision"]
        or task_semantics_sha256(tracked_current) != expected
    ):
        raise TaskOracleError(
            "REVISION_PENDING",
            "current repeatable Task Hook family, revision, and meaning must exist in Git HEAD",
        )
    repo_relative = _repo_relative(root, task_path)
    repo_prefix = repo_relative[: -len(relative)] if relative else repo_relative
    pathspec = f":(top,glob){repo_prefix}.codex/task-hooks/*.json"
    history = _git(
        root,
        "log",
        "--format=commit:%H",
        "--raw",
        "--no-abbrev",
        f"--max-count={MAX_LINEAGE_COMMITS + 1}",
        "--",
        pathspec,
    )
    commits = [
        line.removeprefix(b"commit:").decode("ascii")
        for line in history.stdout.splitlines()
        if line.startswith(b"commit:")
    ]
    if len(history.stdout) > MAX_LINEAGE_BYTES:
        raise TaskOracleError(
            "UNAVAILABLE", "Git revision lineage exceeds the bounded read budget"
        )
    if history.returncode != 0 or not commits:
        raise TaskOracleError(
            "REVISION_PENDING",
            "repeatable Task Hook must be committed before activation",
        )
    if len(commits) > MAX_LINEAGE_COMMITS:
        raise TaskOracleError(
            "REVISION_PENDING",
            "repeatable Task Hook lineage exceeds the native audit window; create a successor revision after offline review",
        )
    blob_ids: list[str] = []
    for line in history.stdout.splitlines():
        if not line.startswith(b":") or b"\t" not in line:
            continue
        fields = line.split(b"\t", 1)[0].split()
        if len(fields) < 5:
            continue
        blob_id = fields[3].decode("ascii")
        if set(blob_id) != {"0"} and blob_id not in blob_ids:
            blob_ids.append(blob_id)
    if not blob_ids:
        raise TaskOracleError("UNAVAILABLE", "Git revision lineage has no readable blobs")
    for raw in _lineage_blobs(root, blob_ids):
        try:
            historical = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TaskOracleError(
                "REVISION_PENDING", "tracked Task Hook history is invalid"
            ) from exc
        if not isinstance(historical, dict):
            continue
        same_revision = (
            historical.get("schema") == TASK_HOOK_SCHEMA
            and historical.get("task_family") == task_hook["task_family"]
            and historical.get("revision") == task_hook["revision"]
        )
        historical_semantics = task_semantics_sha256(historical)
        if same_revision and historical_semantics != expected:
            raise TaskOracleError(
                "REVISION_PENDING",
                "repeatable Task Hook meaning changed under an existing revision; create a successor revision",
            )


def owner_contract_sha256(task_hook: dict[str, Any]) -> str:
    """Fingerprint the owner-controlled meaning, separate from evaluator mechanics."""
    return sha256_json(
        {
            "task_family": task_hook["task_family"],
            "lifecycle": task_hook["lifecycle"],
            "goal": task_hook["goal"],
            "applicability": task_hook["applicability"],
            "candidate": task_hook["candidate"],
            "acceptance": task_hook["acceptance"],
            "invariants": task_hook.get("invariants", []),
            "mutable_bindings": task_hook.get("mutable_bindings", []),
            "fallback_policy": task_hook["fallback_policy"],
            "allowed_fallback_ids": task_hook.get("allowed_fallback_ids", []),
        }
    )


def _id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise TaskOracleError("INVALID_TASK_HOOK", f"{label} has an invalid id")
    return value


def _strings(value: Any, label: str, *, required: bool = False) -> list[str]:
    if value is None and not required:
        return []
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise TaskOracleError(
            "INVALID_TASK_HOOK", f"{label} must be a list of non-empty strings"
        )
    if required and not value:
        raise TaskOracleError("INVALID_TASK_HOOK", f"{label} must not be empty")
    return [item.strip() for item in value]


def command_environment(
    context: dict[str, Any], consumes_bindings: list[str]
) -> dict[str, str]:
    """Give an adapter only its declared run inputs through one stable interface."""
    bindings = {
        key: context["bindings"][key]
        for key in consumes_bindings
    }
    return {
        **os.environ,
        "THREECAN_CONVERGENCE_RUN_ID": context["run_id"],
        "THREECAN_TASK_BINDINGS_JSON": canonical_json_bytes(bindings).decode("utf-8"),
        "THREECAN_ALLOWED_FALLBACKS_JSON": canonical_json_bytes(
            context["allowed_fallbacks"]
        ).decode("utf-8"),
    }


def _relative(root: Path, value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise TaskOracleError("INVALID_TASK_HOOK", f"{label} must be a relative path")
    relative = Path(value)
    if relative.is_absolute():
        raise TaskOracleError("INVALID_TASK_HOOK", f"{label} must be relative")
    resolved_root = root.resolve()
    resolved = (resolved_root / relative).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise TaskOracleError("INVALID_TASK_HOOK", f"{label} escapes project root") from exc
    return resolved


def _provider_path(
    provider: dict[str, Any], bindings: dict[str, Any], root: Path, label: str
) -> tuple[Path, dict[str, str]]:
    has_path = "path" in provider
    has_binding = "path_binding" in provider
    if has_path == has_binding:
        raise TaskOracleError(
            "INVALID_TASK_HOOK", f"{label} requires exactly one of path or path_binding"
        )
    if has_binding:
        binding = _id(provider["path_binding"], f"{label} path_binding")
        value = bindings.get(binding)
        path = _relative(root, value, f"binding {binding}")
        return path, {binding: sha256_json(value)}
    return _relative(root, provider["path"], f"{label} path"), {}


def _validate_provider(
    provider: Any,
    root: Path,
    *,
    label: str,
    lifecycle: str,
    max_timeout: float = 3_600,
) -> dict[str, Any]:
    if not isinstance(provider, dict):
        raise TaskOracleError("INVALID_TASK_HOOK", f"{label} must be an object")
    provider_type = provider.get("type")
    if provider_type not in {"workspace", "artifact", "command"}:
        raise TaskOracleError("INVALID_TASK_HOOK", f"{label} has unsupported type")
    if provider_type == "workspace":
        _known_fields(provider, {"type"}, label)
        return provider
    if provider_type == "artifact":
        _known_fields(provider, {"type", "path", "path_binding"}, label)
        has_path = "path" in provider
        has_binding = "path_binding" in provider
        if has_path == has_binding:
            raise TaskOracleError(
                "INVALID_TASK_HOOK",
                f"{label} requires exactly one of path or path_binding",
            )
        if has_path:
            _relative(root, provider["path"], f"{label} path")
            if lifecycle == "repeatable":
                raise TaskOracleError(
                    "UNBOUND",
                    f"repeatable {label} must use path_binding instead of a run path",
                )
        else:
            _id(provider["path_binding"], f"{label} path_binding")
        return provider
    _known_fields(
        provider,
        {
            "type",
            "argv",
            "timeout_seconds",
            "consumes_bindings",
            "invariant_argv",
        },
        label,
    )
    argv = provider.get("argv")
    if not isinstance(argv, list) or not argv or any(
        not isinstance(item, str) or not item for item in argv
    ):
        raise TaskOracleError("INVALID_TASK_HOOK", f"{label} command requires argv")
    timeout = provider.get("timeout_seconds", min(30, max_timeout))
    if not isinstance(timeout, (int, float)) or not 0 < timeout <= max_timeout:
        raise TaskOracleError(
            "INVALID_TASK_HOOK", f"{label} timeout must be 1..{max_timeout:g}"
        )
    invariant_argv = _strings(
        provider.get("invariant_argv", []), f"{label} invariant_argv"
    )
    if len(set(invariant_argv)) != len(invariant_argv):
        raise TaskOracleError(
            "INVALID_TASK_HOOK", f"{label} invariant_argv must be unique"
        )
    return provider


def validate_task_hook(value: Any, root: Path) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema") != TASK_HOOK_SCHEMA:
        raise TaskOracleError(
            "INVALID_TASK_HOOK", f"task hook schema must be {TASK_HOOK_SCHEMA}"
        )
    _known_fields(
        value,
        {
            "schema",
            "task_family",
            "status",
            "lifecycle",
            "revision",
            "parent_revision",
            "goal",
            "applicability",
            "candidate",
            "acceptance",
            "oracles",
            "invariants",
            "mutable_bindings",
            "fallback_policy",
            "allowed_fallback_ids",
            "promotion",
            "transition",
        },
        "task hook",
    )
    _id(value.get("task_family"), "task_family")
    _id(value.get("revision"), "revision")
    status = value.get("status")
    if status not in TASK_HOOK_STATUSES:
        raise TaskOracleError("INVALID_TASK_HOOK", "task hook status is invalid")
    lifecycle = value.get("lifecycle")
    if lifecycle not in {"one_off", "repeatable"}:
        raise TaskOracleError(
            "INVALID_TASK_HOOK", "task hook lifecycle must be one_off or repeatable"
        )
    for field in ("goal", "applicability"):
        if not isinstance(value.get(field), str) or not value[field].strip():
            raise TaskOracleError("INVALID_TASK_HOOK", f"{field} must be non-empty")
    parent = value.get("parent_revision")
    if parent is not None:
        _id(parent, "parent_revision")
    if status == "PROPOSED_REVISION" and parent is None:
        raise TaskOracleError(
            "REVISION_PENDING", "a proposed revision must name its parent revision"
        )
    _strings(value.get("invariants", []), "invariants")
    mutable_bindings = _strings(value.get("mutable_bindings", []), "mutable_bindings")
    if len(set(mutable_bindings)) != len(mutable_bindings):
        raise TaskOracleError("INVALID_TASK_HOOK", "mutable_bindings must be unique")
    if value.get("fallback_policy") != "explicit_only":
        raise TaskOracleError(
            "INVALID_TASK_HOOK", "fallback_policy must be explicit_only"
        )
    fallback_ids = _strings(value.get("allowed_fallback_ids", []), "allowed_fallback_ids")
    if len(set(fallback_ids)) != len(fallback_ids):
        raise TaskOracleError("INVALID_TASK_HOOK", "allowed_fallback_ids must be unique")
    candidate = value.get("candidate")
    if not isinstance(candidate, dict):
        raise TaskOracleError("INVALID_TASK_HOOK", "candidate must be an object")
    _known_fields(candidate, {"provider"}, "candidate")
    provider = _validate_provider(
        candidate.get("provider"),
        root,
        label="candidate provider",
        lifecycle=lifecycle,
        max_timeout=3,
    )
    if mutable_bindings and provider.get("type") == "workspace":
        raise TaskOracleError(
            "UNBOUND", "workspace candidate cannot attest mutable bindings"
        )
    if provider.get("type") == "artifact":
        path_binding = provider.get("path_binding")
        expected = [path_binding] if path_binding else []
        if mutable_bindings != expected:
            raise TaskOracleError(
                "UNBOUND",
                "artifact candidate can attest only its declared path binding",
            )
    elif provider.get("type") == "command":
        consumes = _strings(
            provider.get("consumes_bindings", []),
            "candidate provider consumes_bindings",
        )
        if len(set(consumes)) != len(consumes) or set(consumes) - set(mutable_bindings):
            raise TaskOracleError(
                "UNBOUND", "candidate provider consumes undeclared bindings"
            )
        if set(consumes) != set(mutable_bindings):
            raise TaskOracleError(
                "UNBOUND",
                "command candidate must consume every mutable binding",
            )
        invariant_argv = provider.get("invariant_argv", [])
        if lifecycle == "repeatable" and (
            len(provider["argv"]) != len(invariant_argv) + 2
            or provider["argv"][1:-1] != invariant_argv
        ):
            raise TaskOracleError(
                "IMPLICIT_MUTABLE_BINDING",
                "repeatable command candidate argv must be launcher, declared invariant_argv, and versioned adapter; pass run values through bindings",
            )

    oracles = value.get("oracles")
    if not isinstance(oracles, list) or not oracles:
        raise TaskOracleError("INVALID_TASK_HOOK", "oracles must be non-empty")
    if len(oracles) > MAX_ORACLES:
        raise TaskOracleError("INVALID_TASK_HOOK", "oracles exceed the bounded limit")
    oracle_ids: set[str] = set()
    oracle_stages: dict[str, list[str]] = {}
    automated_final = False
    for index, oracle in enumerate(oracles):
        if not isinstance(oracle, dict):
            raise TaskOracleError("INVALID_TASK_HOOK", f"oracle {index} must be an object")
        oracle_id = _id(oracle.get("id"), f"oracle {index}")
        if oracle_id in oracle_ids:
            raise TaskOracleError("INVALID_TASK_HOOK", f"duplicate oracle: {oracle_id}")
        oracle_ids.add(oracle_id)
        oracle_kind = oracle.get("kind")
        if oracle_kind not in ORACLE_KINDS:
            raise TaskOracleError("INVALID_TASK_HOOK", f"oracle {oracle_id} kind is invalid")
        oracle_type = oracle.get("type")
        if oracle_type not in ORACLE_TYPES:
            raise TaskOracleError("INVALID_TASK_HOOK", f"oracle {oracle_id} type is invalid")
        common_fields = {
            "id",
            "type",
            "kind",
            "independence",
            "version",
            "stages",
        }
        specific_fields = {
            "command": {"argv", "timeout_seconds", "consumes_bindings"},
            "artifact": {"path", "path_binding", "min_bytes", "role"},
            "external_receipt": {"receipt_path", "receipt_path_binding"},
            "owner_review": set(),
        }[oracle_type]
        _known_fields(
            oracle,
            common_fields | specific_fields,
            f"oracle {oracle_id}",
        )
        _id(oracle.get("version"), f"oracle {oracle_id} version")
        independence = oracle.get("independence")
        if oracle_type == "owner_review":
            if independence != "owner":
                raise TaskOracleError(
                    "INVALID_TASK_HOOK",
                    f"owner review {oracle_id} independence must be owner",
                )
        elif oracle_kind in {"SEMANTIC", "HUMAN"}:
            if independence not in {"independent", "same_agent"}:
                raise TaskOracleError(
                    "INVALID_TASK_HOOK",
                    f"oracle {oracle_id} must declare independent or same_agent review",
                )
        elif independence not in {None, "not_applicable"}:
            raise TaskOracleError(
                "INVALID_TASK_HOOK",
                f"oracle {oracle_id} independence must be not_applicable",
            )
        stages = _strings(oracle.get("stages", ["final"]), f"oracle {oracle_id} stages", required=True)
        if set(stages) - {"episode", "final"}:
            raise TaskOracleError("INVALID_TASK_HOOK", f"oracle {oracle_id} stages are invalid")
        oracle_stages[oracle_id] = stages
        automated_final = automated_final or (
            oracle_type != "owner_review" and "final" in stages
        )
        if oracle_type == "command":
            _validate_provider(
                {
                    key: oracle[key]
                    for key in (
                        "type",
                        "argv",
                        "timeout_seconds",
                        "consumes_bindings",
                    )
                    if key in oracle
                },
                root,
                label=f"oracle {oracle_id}",
                lifecycle=lifecycle,
            )
            consumes = _strings(
                oracle.get("consumes_bindings", []),
                f"oracle {oracle_id} consumes_bindings",
            )
            if len(set(consumes)) != len(consumes) or set(consumes) - set(
                mutable_bindings
            ):
                raise TaskOracleError(
                    "UNBOUND", f"oracle {oracle_id} consumes undeclared bindings"
                )
        elif oracle_type == "artifact":
            if "consumes_bindings" in oracle:
                raise TaskOracleError(
                    "INVALID_TASK_HOOK",
                    f"artifact oracle {oracle_id} binds through path_binding",
                )
            _validate_provider(
                {
                    key: oracle[key]
                    for key in (
                        "type",
                        "path",
                        "path_binding",
                    )
                    if key in oracle
                },
                root,
                label=f"oracle {oracle_id}",
                lifecycle=lifecycle,
            )
            path_binding = oracle.get("path_binding")
            if path_binding and path_binding not in mutable_bindings:
                raise TaskOracleError(
                    "UNBOUND",
                    f"oracle {oracle_id} path binding is not declared mutable",
                )
            if oracle.get("role", "byproduct") not in {"candidate", "byproduct"}:
                raise TaskOracleError("INVALID_TASK_HOOK", f"oracle {oracle_id} role is invalid")
            min_bytes = oracle.get("min_bytes", 1)
            if not isinstance(min_bytes, int) or min_bytes < 0:
                raise TaskOracleError(
                    "INVALID_TASK_HOOK",
                    f"oracle {oracle_id} min_bytes must be non-negative",
                )
        elif oracle_type == "external_receipt":
            path_keys = {key for key in ("receipt_path", "receipt_path_binding") if key in oracle}
            if len(path_keys) != 1:
                raise TaskOracleError(
                    "INVALID_TASK_HOOK",
                    f"oracle {oracle_id} requires one receipt path source",
                )
            if "receipt_path" in oracle:
                _relative(root, oracle["receipt_path"], f"oracle {oracle_id} receipt_path")
                if lifecycle == "repeatable":
                    raise TaskOracleError(
                        "UNBOUND",
                        f"repeatable oracle {oracle_id} must bind its receipt path",
                    )
            else:
                receipt_binding = _id(
                    oracle["receipt_path_binding"],
                    f"oracle {oracle_id} receipt binding",
                )
                if receipt_binding not in mutable_bindings:
                    raise TaskOracleError(
                        "UNBOUND",
                        f"oracle {oracle_id} receipt binding is not declared mutable",
                    )
        elif stages != ["final"]:
            raise TaskOracleError(
                "INVALID_TASK_HOOK", f"owner review {oracle_id} must be final-only"
            )
    if not automated_final:
        raise TaskOracleError("INVALID_TASK_HOOK", "an automated final oracle is required")

    acceptance = value.get("acceptance")
    if not isinstance(acceptance, list) or not acceptance:
        raise TaskOracleError("INVALID_TASK_HOOK", "acceptance must be non-empty")
    if len(acceptance) > MAX_ACCEPTANCE:
        raise TaskOracleError("INVALID_TASK_HOOK", "acceptance exceeds the bounded limit")
    acceptance_ids: set[str] = set()
    referenced: set[str] = set()
    for index, criterion in enumerate(acceptance):
        if not isinstance(criterion, dict):
            raise TaskOracleError("INVALID_TASK_HOOK", f"acceptance {index} must be an object")
        _known_fields(
            criterion,
            {"id", "text", "oracle_ids"},
            f"acceptance {index}",
        )
        criterion_id = _id(criterion.get("id"), f"acceptance {index}")
        if criterion_id in acceptance_ids:
            raise TaskOracleError("INVALID_TASK_HOOK", f"duplicate acceptance: {criterion_id}")
        acceptance_ids.add(criterion_id)
        if not isinstance(criterion.get("text"), str) or not criterion["text"].strip():
            raise TaskOracleError("INVALID_TASK_HOOK", f"acceptance {criterion_id} needs text")
        bound = _strings(
            criterion.get("oracle_ids"),
            f"acceptance {criterion_id} oracle_ids",
            required=True,
        )
        unknown = sorted(set(bound) - oracle_ids)
        if unknown:
            raise TaskOracleError(
                "INVALID_TASK_HOOK",
                f"acceptance {criterion_id} has unknown oracles: {', '.join(unknown)}",
            )
        non_final = sorted(item for item in bound if "final" not in oracle_stages[item])
        if non_final:
            raise TaskOracleError(
                "INVALID_TASK_HOOK",
                f"acceptance {criterion_id} oracles are not final: {', '.join(non_final)}",
            )
        referenced.update(bound)
    unbound = sorted(oracle_ids - referenced)
    if unbound:
        raise TaskOracleError(
            "UNBOUND", "every oracle must bind an acceptance: " + ", ".join(unbound)
        )

    if lifecycle == "one_off" and status in {
        "REUSABLE_CANDIDATE",
        "REUSABLE_ACTIVE",
    }:
        raise TaskOracleError(
            "INVALID_TASK_HOOK", "one-off task hooks cannot become reusable"
        )
    _validate_lifecycle_evidence(value, root)
    return value


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise TaskOracleError("INVALID_TASK_HOOK", f"{label} must be a SHA-256 digest")
    return value


def _validate_receipt_ref(
    value: Any,
    label: str,
    root: Path,
    task_hook: dict[str, Any],
) -> tuple[str, str, str]:
    if not isinstance(value, dict):
        raise TaskOracleError("INVALID_TASK_HOOK", f"{label} must be an object")
    _known_fields(
        value,
        {
            "run_id",
            "candidate_fingerprint",
            "bindings_sha256",
            "receipt_sha256",
            "receipt_path",
            "task_family",
            "task_hook_revision",
            "task_hook_sha256",
            "task_semantics_sha256",
            "outcome",
        },
        label,
    )
    run_id = _id(value.get("run_id"), f"{label} run_id")
    candidate = _digest(
        value.get("candidate_fingerprint"), f"{label} candidate_fingerprint"
    )
    bindings = _digest(value.get("bindings_sha256"), f"{label} bindings_sha256")
    _digest(value.get("receipt_sha256"), f"{label} receipt_sha256")
    _id(value.get("task_family"), f"{label} task_family")
    _id(value.get("task_hook_revision"), f"{label} task_hook_revision")
    _digest(value.get("task_hook_sha256"), f"{label} task_hook_sha256")
    _digest(
        value.get("task_semantics_sha256"),
        f"{label} task_semantics_sha256",
    )
    receipt_path = _relative(root, value.get("receipt_path"), f"{label} receipt_path")
    evidence_root = (root / ".codex" / "task-hooks" / "evidence").resolve()
    try:
        receipt_path.relative_to(evidence_root)
    except ValueError as exc:
        raise TaskOracleError(
            "INVALID_TASK_HOOK",
            f"{label} receipt_path must be under .codex/task-hooks/evidence",
        ) from exc
    if value.get("outcome") != "CONVERGED":
        raise TaskOracleError(
            "INVALID_TASK_HOOK", f"{label} must reference a CONVERGED receipt"
        )
    if not receipt_path.is_file() or receipt_path.stat().st_size > MAX_PROVIDER_OUTPUT_BYTES:
        raise TaskOracleError(
            "REVISION_PENDING", f"{label} retained receipt is missing or oversized"
        )
    try:
        retained = json.loads(receipt_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TaskOracleError(
            "REVISION_PENDING", f"{label} retained receipt is invalid"
        ) from exc
    if not isinstance(retained, dict):
        raise TaskOracleError(
            "REVISION_PENDING", f"{label} retained receipt must be an object"
        )
    unsigned = {
        key: item for key, item in retained.items() if key != "receipt_sha256"
    }
    retained_task = retained.get("task") or {}
    retained_candidate = retained_task.get("candidate") or {}
    if (
        retained.get("schema") != "3can.convergence-receipt/v2"
        or retained.get("receipt_sha256") != value["receipt_sha256"]
        or sha256_json(unsigned) != value["receipt_sha256"]
        or retained.get("outcome") != "CONVERGED"
        or retained.get("proof_eligible") is not True
        or retained_task.get("run_id") != run_id
        or retained_task.get("bindings_sha256") != bindings
        or retained_candidate.get("fingerprint") != candidate
        or retained_task.get("task_family") != value["task_family"]
        or retained_task.get("revision") != value["task_hook_revision"]
        or retained_task.get("task_hook_sha256") != value["task_hook_sha256"]
        or retained_task.get("task_semantics_sha256")
        != value["task_semantics_sha256"]
        or value["task_family"] != task_hook["task_family"]
        or value["task_hook_revision"] != task_hook["revision"]
        or value["task_semantics_sha256"] != task_semantics_sha256(task_hook)
    ):
        raise TaskOracleError(
            "REVISION_PENDING", f"{label} does not match its retained receipt"
        )
    return run_id, candidate, bindings


def _validate_confirmation(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TaskOracleError("REVISION_PENDING", f"{label} confirmation is required")
    confirmed_by = value.get("confirmed_by")
    confirmation_ref = value.get("confirmation_ref")
    if confirmed_by not in {"owner", "independent_reviewer"}:
        raise TaskOracleError("REVISION_PENDING", f"{label} confirmer is invalid")
    if not isinstance(confirmation_ref, str) or not confirmation_ref.strip():
        raise TaskOracleError("REVISION_PENDING", f"{label} confirmation_ref is required")
    return value


def _validate_lifecycle_evidence(task_hook: dict[str, Any], root: Path) -> None:
    status = task_hook["status"]
    promotion = task_hook.get("promotion")
    transition = task_hook.get("transition")
    if status in {"REUSABLE_CANDIDATE", "REUSABLE_ACTIVE"}:
        if task_hook["lifecycle"] != "repeatable" or not isinstance(promotion, dict):
            raise TaskOracleError(
                "REVISION_PENDING", "reusable status requires promotion evidence"
            )
        _known_fields(
            promotion,
            {
                "mode",
                "qualifying_receipts",
                "confirmed_by",
                "confirmation_ref",
            },
            "promotion",
        )
        receipts = promotion.get("qualifying_receipts")
        if not isinstance(receipts, list) or not receipts:
            raise TaskOracleError(
                "REVISION_PENDING", "promotion requires qualifying receipts"
            )
        if len(receipts) > MAX_PROMOTION_RECEIPTS:
            raise TaskOracleError(
                "REVISION_PENDING", "promotion receipts exceed the bounded limit"
            )
        identities = [
            _validate_receipt_ref(
                item,
                f"promotion receipt {index}",
                root,
                task_hook,
            )
            for index, item in enumerate(receipts)
        ]
        if len(set(identities)) != len(identities):
            raise TaskOracleError(
                "REVISION_PENDING", "duplicate receipts cannot support promotion"
            )
        mode = promotion.get("mode")
        if status == "REUSABLE_CANDIDATE":
            if mode != "candidate":
                raise TaskOracleError(
                    "REVISION_PENDING", "reusable candidate mode must be candidate"
                )
        elif mode == "reproduced":
            _validate_confirmation(promotion, "promotion")
            if len({item[0] for item in identities}) < 2 or len(
                {(item[1], item[2]) for item in identities}
            ) < 2:
                raise TaskOracleError(
                    "REVISION_PENDING",
                    "reproduced promotion requires two distinct runs and candidate/binding subjects",
                )
        elif mode == "owner_fast_track":
            _validate_confirmation(promotion, "promotion")
            if promotion.get("confirmed_by") != "owner":
                raise TaskOracleError(
                    "REVISION_PENDING", "fast-track promotion requires Owner confirmation"
                )
        else:
            raise TaskOracleError("REVISION_PENDING", "promotion mode is invalid")
    elif promotion is not None:
        raise TaskOracleError(
            "INVALID_TASK_HOOK", "promotion is only valid for reusable states"
        )

    if status in {"RETIRED", "SUPERSEDED"}:
        if not isinstance(transition, dict):
            raise TaskOracleError(
                "REVISION_PENDING", "terminal task-hook state requires a transition record"
            )
        _known_fields(
            transition,
            {
                "from_sha256",
                "confirmed_by",
                "confirmation_ref",
                "successor_revision",
            },
            "transition",
        )
        _digest(transition.get("from_sha256"), "transition from_sha256")
        _validate_confirmation(transition, "transition")
        if status == "SUPERSEDED":
            _id(transition.get("successor_revision"), "transition successor_revision")
        elif "successor_revision" in transition:
            raise TaskOracleError(
                "INVALID_TASK_HOOK", "retirement cannot name a successor revision"
            )
    elif transition is not None:
        raise TaskOracleError(
            "INVALID_TASK_HOOK", "transition is only valid for terminal states"
        )


def load_task_context(
    root: Path,
    convergence: dict[str, Any],
    *,
    require_executable: bool = True,
) -> dict[str, Any]:
    run_id = _id(convergence.get("run_id"), "run_id")
    reference = convergence.get("task_hook")
    if not isinstance(reference, dict):
        raise TaskOracleError("INVALID_TASK_HOOK", "task_hook reference is required")
    _known_fields(reference, {"path", "sha256", "revision"}, "task_hook reference")
    task_path = _relative(root, reference.get("path"), "task_hook path")
    if not task_path.is_file():
        raise TaskOracleError("UNAVAILABLE", "task hook file is missing")
    if task_path.stat().st_size > MAX_TASK_HOOK_BYTES:
        raise TaskOracleError("INVALID_TASK_HOOK", "task hook exceeds the bounded size")
    task_hook = validate_task_hook(
        json.loads(task_path.read_text(encoding="utf-8-sig")), root
    )
    validate_revision_lineage(root, task_path, task_hook)
    digest = sha256_json(task_hook)
    expected_digest = reference.get("sha256")
    expected_revision = reference.get("revision")
    if digest != expected_digest or task_hook["revision"] != expected_revision:
        raise TaskOracleError(
            "REVISION_PENDING", "task hook content does not match the active pinned revision"
        )
    activation = convergence.get("activation")
    if require_executable:
        if task_hook["status"] == "PROPOSED_REVISION":
            raise TaskOracleError("REVISION_PENDING", "proposed revision cannot be active")
        if task_hook["status"] not in EXECUTABLE_STATUSES:
            raise TaskOracleError("UNAVAILABLE", "task hook revision is not executable")
        if not isinstance(activation, dict):
            raise TaskOracleError(
                "REVISION_PENDING", "explicit revision activation is required"
            )
        _known_fields(
            activation,
            {
                "task_hook_sha256",
                "confirmed_revision",
                "confirmed_by",
                "confirmation_ref",
            },
            "activation",
        )
        if (
            activation.get("task_hook_sha256") != digest
            or activation.get("confirmed_revision") != task_hook["revision"]
            or activation.get("confirmed_by")
            not in {"owner", "independent_reviewer"}
            or not isinstance(activation.get("confirmation_ref"), str)
            or not activation["confirmation_ref"].strip()
        ):
            raise TaskOracleError(
                "REVISION_PENDING",
                "revision activation does not confirm the pinned task hook",
            )

    bindings = convergence.get("bindings", {})
    if not isinstance(bindings, dict) or any(not ID_PATTERN.fullmatch(str(key)) for key in bindings):
        raise TaskOracleError("UNBOUND", "bindings must be an id-keyed object")
    if len(canonical_json_bytes(bindings)) > MAX_BINDINGS_BYTES:
        raise TaskOracleError("UNBOUND", "bindings exceed the bounded contract size")
    expected_bindings = set(task_hook.get("mutable_bindings", []))
    actual_bindings = set(bindings)
    if expected_bindings != actual_bindings:
        missing = sorted(expected_bindings - actual_bindings)
        extra = sorted(actual_bindings - expected_bindings)
        raise TaskOracleError(
            "UNBOUND", f"binding mismatch; missing={missing}, undeclared={extra}"
        )
    fallbacks = _strings(
        convergence.get("allowed_fallbacks", []), "allowed_fallbacks"
    )
    if len(set(fallbacks)) != len(fallbacks):
        raise TaskOracleError("UNBOUND", "allowed_fallbacks must be unique")
    unknown_fallbacks = sorted(
        set(fallbacks) - set(task_hook.get("allowed_fallback_ids", []))
    )
    if unknown_fallbacks:
        raise TaskOracleError(
            "UNBOUND",
            "run enables undeclared fallbacks: " + ", ".join(unknown_fallbacks),
        )
    oracle_to_criteria: dict[str, list[str]] = {}
    for criterion in task_hook["acceptance"]:
        for oracle_id in criterion["oracle_ids"]:
            oracle_to_criteria.setdefault(oracle_id, []).append(criterion["id"])
    return {
        "run_id": run_id,
        "task_hook_path": reference["path"].replace("\\", "/"),
        "task_hook_sha256": digest,
        "task_semantics_sha256": task_semantics_sha256(task_hook),
        "owner_contract_sha256": owner_contract_sha256(task_hook),
        "task_hook": task_hook,
        "bindings": bindings,
        "binding_fingerprints": {
            key: sha256_json(value) for key, value in sorted(bindings.items())
        },
        "bindings_sha256": sha256_json(
            {"bindings": bindings, "allowed_fallbacks": fallbacks}
        ),
        "allowed_fallbacks": fallbacks,
        "oracle_to_criteria": oracle_to_criteria,
        "activation": activation,
    }


def load_task_registry(
    root: Path,
    relative: str,
    *,
    selected_family: str | None = None,
) -> dict[str, dict[str, Any]]:
    if selected_family is not None:
        selected_family = _id(selected_family, "selected task family")
    path = _relative(root, relative, "task registry path")
    if not path.is_file():
        raise TaskOracleError("UNAVAILABLE", "task registry file is missing")
    if path.stat().st_size > MAX_TASK_HOOK_BYTES:
        raise TaskOracleError("INVALID_TASK_HOOK", "task registry exceeds the bounded size")
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TaskOracleError("INVALID_TASK_HOOK", "task registry is invalid") from exc
    if not isinstance(value, dict) or value.get("schema") != TASK_REGISTRY_SCHEMA:
        raise TaskOracleError(
            "INVALID_TASK_HOOK",
            f"task registry schema must be {TASK_REGISTRY_SCHEMA}",
        )
    if sha256_json(_head_json(root, path, "task registry")) != sha256_json(value):
        raise TaskOracleError(
            "REVISION_PENDING", "task registry must match its exact Git HEAD value"
        )
    _known_fields(value, {"schema", "families"}, "task registry")
    families = value.get("families")
    if not isinstance(families, list):
        raise TaskOracleError("INVALID_TASK_HOOK", "task registry families must be a list")
    if len(families) > MAX_REGISTRY_FAMILIES:
        raise TaskOracleError("INVALID_TASK_HOOK", "task registry exceeds the family limit")
    result: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    for index, entry in enumerate(families):
        if not isinstance(entry, dict):
            raise TaskOracleError(
                "INVALID_TASK_HOOK", f"task registry family {index} must be an object"
            )
        _known_fields(
            entry,
            {"task_family", "path", "sha256", "revision"},
            f"task registry family {index}",
        )
        family = _id(entry.get("task_family"), f"task registry family {index}")
        if family in seen:
            raise TaskOracleError("INVALID_TASK_HOOK", f"duplicate task family: {family}")
        seen.add(family)
        task_path = _relative(root, entry.get("path"), f"task registry family {family} path")
        _digest(entry.get("sha256"), f"task registry family {family} sha256")
        _id(entry.get("revision"), f"task registry family {family} revision")
        if selected_family is not None and family != selected_family:
            continue
        if not task_path.is_file():
            raise TaskOracleError("UNAVAILABLE", f"registered task hook is missing: {family}")
        if task_path.stat().st_size > MAX_TASK_HOOK_BYTES:
            raise TaskOracleError(
                "INVALID_TASK_HOOK", f"registered task hook is too large: {family}"
            )
        try:
            task_hook = validate_task_hook(
                json.loads(task_path.read_text(encoding="utf-8-sig")), root
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise TaskOracleError(
                "INVALID_TASK_HOOK", f"registered task hook is invalid: {family}"
            ) from exc
        digest = sha256_json(task_hook)
        if sha256_json(_head_json(root, task_path, f"Task Hook {family}")) != digest:
            raise TaskOracleError(
                "REVISION_PENDING",
                f"registered Task Hook must match its exact Git HEAD value: {family}",
            )
        validate_revision_lineage(root, task_path, task_hook)
        if (
            task_hook["status"] != "REUSABLE_ACTIVE"
            or task_hook["task_family"] != family
            or task_hook["revision"] != entry.get("revision")
            or digest != entry.get("sha256")
        ):
            raise TaskOracleError(
                "REVISION_PENDING",
                f"registered task family is not the exact reusable active revision: {family}",
            )
        result[family] = dict(entry)
    return result


def _unverifiable(context: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "status": "UNVERIFIABLE",
        "reason": reason,
        "provider_fingerprint": "",
        "fingerprint": sha256_json(
            {
                "unverifiable": reason,
                "task_semantics_sha256": context["task_semantics_sha256"],
                "bindings_sha256": context["bindings_sha256"],
            }
        ),
        "binding_fingerprints": {},
        "fallbacks_used": [],
    }


def current_candidate(
    context: dict[str, Any], root: Path, workspace: dict[str, Any]
) -> dict[str, Any]:
    provider = context["task_hook"]["candidate"]["provider"]
    provider_type = provider["type"]
    reported_bindings: dict[str, str] = {}
    fallbacks_used: list[str] = []
    metadata_sha256 = ""
    if provider_type == "workspace":
        provider_fingerprint = str(workspace.get("fingerprint") or "")
        if not provider_fingerprint:
            return _unverifiable(context, "workspace fingerprint is unavailable")
    elif provider_type == "artifact":
        path, reported_bindings = _provider_path(
            provider, context["bindings"], root, "candidate provider"
        )
        if not path.is_file():
            return _unverifiable(context, "candidate artifact is missing")
        size = path.stat().st_size
        if size > MAX_DIRECT_ARTIFACT_BYTES:
            return _unverifiable(
                context,
                "large candidate requires a command provider backed by a stable manifest",
            )
        provider_fingerprint = hashlib.sha256(path.read_bytes()).hexdigest()
        metadata_sha256 = sha256_json({"bytes": size})
    else:
        try:
            completed = subprocess.run(
                provider["argv"],
                cwd=root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                shell=False,
                timeout=float(provider.get("timeout_seconds", 3)),
                env=command_environment(
                    context,
                    provider.get("consumes_bindings", []),
                ),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return _unverifiable(context, f"candidate provider failed: {type(exc).__name__}")
        if completed.returncode != 0 or len(completed.stdout) > MAX_PROVIDER_OUTPUT_BYTES:
            return _unverifiable(context, "candidate provider did not return bounded success")
        try:
            value = json.loads(completed.stdout.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _unverifiable(context, "candidate provider returned invalid JSON")
        if (
            not isinstance(value, dict)
            or value.get("schema") != CANDIDATE_SCHEMA
            or not isinstance(value.get("fingerprint"), str)
            or not value["fingerprint"].strip()
        ):
            return _unverifiable(context, "candidate provider returned an invalid receipt")
        try:
            _known_fields(
                value,
                {
                    "schema",
                    "fingerprint",
                    "binding_fingerprints",
                    "fallbacks_used",
                    "metadata",
                },
                "candidate receipt",
            )
        except TaskOracleError:
            return _unverifiable(context, "candidate provider returned unsupported fields")
        provider_fingerprint = value["fingerprint"].strip()
        reported_bindings = value.get("binding_fingerprints", {})
        fallbacks_used = value.get("fallbacks_used", [])
        if not isinstance(reported_bindings, dict) or not all(
            isinstance(key, str) and isinstance(item, str)
            for key, item in reported_bindings.items()
        ):
            return _unverifiable(context, "candidate binding fingerprints are invalid")
        if not isinstance(fallbacks_used, list) or any(
            not isinstance(item, str) or not item for item in fallbacks_used
        ):
            return _unverifiable(context, "candidate fallbacks_used is invalid")
        if "metadata" in value:
            metadata_sha256 = sha256_json(value["metadata"])

    if reported_bindings != context["binding_fingerprints"]:
        status = "IMPLICIT_MUTABLE_BINDING"
        reason = "candidate did not attest every current mutable binding"
    elif sorted(set(fallbacks_used) - set(context["allowed_fallbacks"])):
        status = "FALLBACK_NOT_ALLOWED"
        reason = "candidate used a fallback not allowed by the current run"
    else:
        status = "PASS"
        reason = "candidate is bound to the active task revision and run bindings"
    effective = sha256_json(
        {
            "provider_fingerprint": provider_fingerprint,
            "task_semantics_sha256": context["task_semantics_sha256"],
            "bindings_sha256": context["bindings_sha256"],
            "fallbacks_used": sorted(fallbacks_used),
            "metadata_sha256": metadata_sha256,
        }
    )
    return {
        "status": status,
        "reason": reason,
        "provider_fingerprint": provider_fingerprint,
        "fingerprint": effective,
        "binding_fingerprints": reported_bindings,
        "fallbacks_used": fallbacks_used,
        "metadata_sha256": metadata_sha256,
    }


def proof_receipt(
    oracle: dict[str, Any],
    context: dict[str, Any],
    candidate: dict[str, Any],
    *,
    status: str,
    reason: str,
    evidence_refs: list[str],
) -> dict[str, Any]:
    if status not in PROOF_STATUSES:
        raise TaskOracleError("UNVERIFIABLE", "proof status is invalid")
    criteria = context["oracle_to_criteria"][oracle["id"]]
    value: dict[str, Any] = {
        "schema": PROOF_RECEIPT_SCHEMA,
        "criterion_ids": criteria,
        "task_hook_revision": context["task_hook"]["revision"],
        "task_hook_sha256": context["task_hook_sha256"],
        "task_semantics_sha256": context["task_semantics_sha256"],
        "run_id": context["run_id"],
        "candidate_fingerprint": candidate["fingerprint"],
        "bindings_sha256": context["bindings_sha256"],
        "evaluator": {
            "id": oracle["id"],
            "version": oracle["version"],
            "kind": oracle["kind"],
            "independence": oracle.get("independence", "not_applicable"),
        },
        "status": status,
        "reason": reason,
        "evidence_refs": evidence_refs,
    }
    if len(criteria) == 1:
        value["criterion_id"] = criteria[0]
    return {**value, "receipt_sha256": sha256_json(value)}


def load_external_proof(
    oracle: dict[str, Any],
    context: dict[str, Any],
    candidate: dict[str, Any],
    root: Path,
) -> dict[str, Any]:
    if "receipt_path_binding" in oracle:
        binding = oracle["receipt_path_binding"]
        path = _relative(root, context["bindings"].get(binding), f"binding {binding}")
    else:
        path = _relative(root, oracle["receipt_path"], f"oracle {oracle['id']} receipt")
    if not path.is_file():
        return proof_receipt(
            oracle,
            context,
            candidate,
            status="MISSING",
            reason="external evaluator receipt is missing",
            evidence_refs=[],
        )
    try:
        if path.stat().st_size > MAX_EXTERNAL_PROOF_BYTES:
            return proof_receipt(
                oracle,
                context,
                candidate,
                status="UNVERIFIABLE",
                reason="external evaluator receipt exceeds the bounded size",
                evidence_refs=[],
            )
    except OSError:
        return proof_receipt(
            oracle,
            context,
            candidate,
            status="UNVERIFIABLE",
            reason="external evaluator receipt metadata is unavailable",
            evidence_refs=[],
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return proof_receipt(
            oracle,
            context,
            candidate,
            status="UNVERIFIABLE",
            reason="external evaluator receipt is invalid",
            evidence_refs=[],
        )
    if isinstance(value, dict):
        allowed_fields = {
            "schema",
            "criterion_ids",
            "criterion_id",
            "task_hook_revision",
            "task_hook_sha256",
            "task_semantics_sha256",
            "run_id",
            "candidate_fingerprint",
            "bindings_sha256",
            "evaluator",
            "status",
            "reason",
            "evidence_refs",
            "receipt_sha256",
        }
        if set(value) - allowed_fields:
            return proof_receipt(
                oracle,
                context,
                candidate,
                status="UNVERIFIABLE",
                reason="external evaluator receipt has unsupported fields",
                evidence_refs=[],
            )
    expected = proof_receipt(
        oracle,
        context,
        candidate,
        status="PASS",
        reason="placeholder",
        evidence_refs=[],
    )
    exact_fields = (
        "schema",
        "criterion_ids",
        "criterion_id",
        "task_hook_revision",
        "task_hook_sha256",
        "task_semantics_sha256",
        "run_id",
        "candidate_fingerprint",
        "bindings_sha256",
        "evaluator",
    )
    if not isinstance(value, dict) or any(
        value.get(key) != expected.get(key) for key in exact_fields
    ):
        return proof_receipt(
            oracle,
            context,
            candidate,
            status="STALE_EVIDENCE",
            reason="external evaluator receipt targets another task revision or candidate",
            evidence_refs=[],
        )
    status = value.get("status")
    reason = value.get("reason")
    evidence_refs = value.get("evidence_refs")
    if (
        status not in PROOF_STATUSES
        or not isinstance(reason, str)
        or not reason.strip()
        or not isinstance(evidence_refs, list)
        or any(not isinstance(item, str) or not item for item in evidence_refs)
        or (status == "PASS" and not evidence_refs)
        or (
            status == "PASS"
            and any(
                not re.fullmatch(r"[^\s]*sha256:[0-9a-f]{64}", item)
                for item in evidence_refs
            )
        )
    ):
        return proof_receipt(
            oracle,
            context,
            candidate,
            status="UNVERIFIABLE",
            reason="external evaluator receipt has invalid result fields",
            evidence_refs=[],
        )
    supplied_digest = value.get("receipt_sha256")
    unsigned = {key: item for key, item in value.items() if key != "receipt_sha256"}
    if supplied_digest != sha256_json(unsigned):
        return proof_receipt(
            oracle,
            context,
            candidate,
            status="UNVERIFIABLE",
            reason="external evaluator receipt digest is invalid",
            evidence_refs=[],
        )
    return value
