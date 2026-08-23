"""Safely migrate legacy ``ERR-repeated-*`` graph pollution.

The command is a dry-run unless both ``--apply`` and
``--confirm-engine-stopped`` are supplied.  Before any apply or rollback it
also probes every configured engine endpoint and refuses mutation if any
endpoint responds over HTTP or accepts a TCP connection.  An apply run:

1. snapshots the complete node store, edges file, and embedding-cache state;
2. archives every removable node with all of its connected edges as JSONL;
3. removes core-memory-registry ``requires`` fan-out to repeated errors;
4. archives unpromoted one-off legacy gates without a reusable solution;
5. normalizes retained reusable ErrorCases; and
6. invalidates embeddings so the engine rebuilds them on reload.

Only Python's standard library is used so the maintenance path remains
available even when the normal 3CAN backend environment is not installed.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import math
import os
import re
import shutil
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

_BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from graph_runtime_lock import (  # noqa: E402
    GraphRuntimeLockError,
    acquire_graph_runtime_lock,
)
from error_knowledge import ErrorCase, deterministic_fingerprint  # noqa: E402


MIGRATION_VERSION = "3can.legacy-error-migration/v2"
ARCHIVE_VERSION = "3can.legacy-error-archive/v1"
BACKUP_VERSION = "3can.graph-rollback-backup/v1"
ERROR_KNOWLEDGE_VERSION = "3can.error-knowledge/v1"
LOCK_VERSION = "3can.legacy-error-migration-lock/v1"
JOURNAL_VERSION = "3can.legacy-error-migration-journal/v1"
JOURNAL_CHECKPOINT_BATCH_SIZE = 100
ATOMIC_REPLACE_RETRY_DELAYS_SEC = (0.05, 0.1, 0.2, 0.4, 0.8)
TRANSIENT_WINDOWS_REPLACE_ERRORS = frozenset({5, 32})

REPEATED_ERROR_PREFIX = "ERR-repeated-"
LEGACY_ERROR_PREFIX = "ERR-"
CANONICAL_ERROR_CASE_PREFIX = "ERR-case-"
CANONICAL_ERROR_CLUSTER = "ErrorKnowledge"
KNOWN_CORE_REGISTRY_IDS = frozenset(
    {
        "MEM-3can-core-memory-lane-registry-20260523",
        "CORE-codex-error-registry-20260426",
        "CORE-codex-error-registry",
        "CORE-3can-error-registry",
        "CORE-error-registry",
    }
)
RESOLUTION_TARGET_ERROR_EDGE_TYPES = frozenset(
    {"resolves", "fixes", "solution_for", "mitigates"}
)
RESOLUTION_SOURCE_ERROR_EDGE_TYPES = frozenset(
    {"resolved_by", "fixed_by", "has_solution"}
)
RESOLVED_STATES = frozenset({"resolved", "mitigated", "superseded"})
DIAGNOSED_STATES = frozenset({"diagnosed", "mitigated", "resolved", "regressed", "superseded"})
OBSERVED_STATES = frozenset({"", "active", "known", "observed", "open", "recorded"})
LEGACY_BLOCKER_PATTERNS = (
    "matching second failure blocks blind retry",
    "exact retry is blocked until a diagnosis",
)
DEFAULT_ENGINE_ENDPOINTS = (
    "http://127.0.0.1:9700",
    "http://127.0.0.1:9701",
    "http://127.0.0.1:9702",
    "http://127.0.0.1:9711",
)
DEFAULT_ENGINE_PROBE_TIMEOUT_SEC = 0.25
ENGINE_ENDPOINT_ENV_KEYS = (
    "THREECAN_URL",
    "THREECAN_ENGINE_URL",
    "THREECAN_ENDPOINT",
)
ENGINE_PORT_ENV_KEYS = (
    "THREECAN_PORT",
    "THREECAN_ENGINE_PORT",
)
ENGINE_CONFIG_RELATIVE_PATHS = (
    "engine_endpoints.json",
    ".engine-endpoints.json",
    ".3can.json",
    ".3can_env",
    "3can.json",
    "config/3can.json",
    ".env",
    "backend/.env",
)
TERMINAL_JOURNAL_PHASES = frozenset({"completed", "rolled_back"})
PUBLIC_MANIFEST_LIST_LIMIT = 50


class MigrationError(RuntimeError):
    """Raised when a migration cannot safely continue."""


def _normalize_engine_endpoint(value: str) -> str:
    endpoint = str(value or "").strip()
    try:
        parsed = urllib.parse.urlsplit(endpoint)
        port = parsed.port
    except ValueError as exc:
        raise MigrationError(f"invalid engine endpoint {endpoint!r}: {exc}") from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise MigrationError(
            f"engine endpoint must be a complete http(s) URL: {endpoint!r}"
        )
    if not parsed.hostname or port is None:
        raise MigrationError(
            f"engine endpoint must include an explicit host and port: {endpoint!r}"
        )
    if parsed.username or parsed.password:
        raise MigrationError("engine endpoint must not include credentials")
    if parsed.fragment:
        raise MigrationError("engine endpoint must not include a URL fragment")
    path = parsed.path or "/"
    return urllib.parse.urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc,
            path,
            parsed.query,
            "",
        )
    )


def _endpoint_values_from_json(value: Any, *, key: str = "") -> list[str]:
    """Extract only explicitly named engine URL/port values from bounded config."""

    found: list[str] = []
    normalized_key = key.casefold().replace("-", "_")
    if isinstance(value, Mapping):
        for child_key, child_value in value.items():
            found.extend(
                _endpoint_values_from_json(
                    child_value,
                    key=str(child_key),
                )
            )
        return found
    if isinstance(value, list):
        for child in value:
            found.extend(_endpoint_values_from_json(child, key=key))
        return found
    if (
        isinstance(value, str)
        and (
            "endpoint" in normalized_key
            or normalized_key == "url"
            or normalized_key.endswith("_url")
        )
        and value.strip()
    ):
        found.append(value.strip())
    elif (
        isinstance(value, int)
        and not isinstance(value, bool)
        and (
            normalized_key == "port"
            or normalized_key.endswith("_port")
        )
        and 1 <= value <= 65535
    ):
        found.append(f"http://127.0.0.1:{value}")
    return found


def _endpoint_values_from_text(text: str) -> list[str]:
    found: list[str] = []
    url_names = "|".join(re.escape(item) for item in ENGINE_ENDPOINT_ENV_KEYS)
    port_names = "|".join(re.escape(item) for item in ENGINE_PORT_ENV_KEYS)
    for match in re.finditer(
        rf"(?im)^\s*(?:export\s+)?(?:{url_names})\s*=\s*['\"]?([^'\"\s#]+)",
        text,
    ):
        found.append(match.group(1))
    for match in re.finditer(
        rf"(?im)^\s*(?:export\s+)?(?:{port_names})\s*=\s*['\"]?(\d{{1,5}})",
        text,
    ):
        port = int(match.group(1))
        if 1 <= port <= 65535:
            found.append(f"http://127.0.0.1:{port}")
    return found


def _discover_engine_endpoints(graph_dir: Path | None) -> dict[str, list[str]]:
    """Infer serving addresses from explicit env/config without scanning source."""

    discovered: dict[str, list[str]] = {}

    def record(raw: Any, source: str) -> None:
        try:
            endpoint = _normalize_engine_endpoint(str(raw))
        except MigrationError:
            return
        discovered.setdefault(endpoint, []).append(source)

    for name in ENGINE_ENDPOINT_ENV_KEYS:
        if os.environ.get(name):
            record(os.environ[name], f"env:{name}")
    for name in ENGINE_PORT_ENV_KEYS:
        raw = str(os.environ.get(name) or "").strip()
        if raw.isdigit() and 1 <= int(raw) <= 65535:
            record(f"http://127.0.0.1:{raw}", f"env:{name}")

    if graph_dir is None:
        return discovered
    graph = graph_dir.resolve()
    candidates = [
        *(graph / relative for relative in ENGINE_CONFIG_RELATIVE_PATHS),
        *(graph.parent / relative for relative in ENGINE_CONFIG_RELATIVE_PATHS),
    ]
    for path in dict.fromkeys(candidates):
        try:
            if not path.is_file() or path.stat().st_size > 1024 * 1024:
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        values: list[str]
        if path.suffix.casefold() == ".json":
            try:
                values = _endpoint_values_from_json(json.loads(text))
            except json.JSONDecodeError:
                continue
        else:
            values = _endpoint_values_from_text(text)
        for value in values:
            record(value, f"config:{path}")
    return discovered


def _effective_engine_endpoints(
    engine_endpoints: Sequence[str] | None,
    additional_engine_endpoints: Sequence[str] | None = None,
    *,
    graph_dir: Path | None = None,
    discovered_endpoints: Sequence[str] | None = None,
) -> tuple[str, ...]:
    """Resolve defaults plus caller/config additions.

    The publicly supported local ports can never be suppressed by an empty or
    explicit caller list.  This prevents a stale wrapper from probing only one
    port while another 3CAN OS profile is still serving the same graph.
    """

    explicit = [
        _normalize_engine_endpoint(value)
        for value in (engine_endpoints or ())
        if str(value or "").strip()
    ]
    defaults = [
        _normalize_engine_endpoint(value) for value in DEFAULT_ENGINE_ENDPOINTS
    ]
    additions = [
        _normalize_engine_endpoint(value)
        for value in (additional_engine_endpoints or ())
        if str(value or "").strip()
    ]
    discovered = (
        list(discovered_endpoints)
        if discovered_endpoints is not None
        else list(_discover_engine_endpoints(graph_dir))
    )
    return tuple(dict.fromkeys([*defaults, *explicit, *additions, *discovered]))


def _endpoint_liveness(
    endpoint: str,
    *,
    timeout_sec: float,
) -> dict[str, Any]:
    parsed = urllib.parse.urlsplit(endpoint)
    host = str(parsed.hostname)
    port = int(parsed.port or 0)
    try:
        requested_timeout = float(timeout_sec)
    except (TypeError, ValueError) as exc:
        raise MigrationError("engine probe timeout must be numeric") from exc
    if not math.isfinite(requested_timeout):
        raise MigrationError("engine probe timeout must be finite")
    timeout = max(0.05, min(requested_timeout, 2.0))

    # TCP comes first: accepting a connection is already sufficient proof that
    # something is serving the configured engine address, even if it does not
    # produce a valid HTTP response.
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return {
                "endpoint": endpoint,
                "live": True,
                "probe": "tcp",
            }
    except (TimeoutError, OSError):
        pass

    request = urllib.request.Request(
        endpoint,
        method="HEAD",
        headers={"User-Agent": "3can-engine-quiescence-check/1"},
    )
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=timeout) as response:
            return {
                "endpoint": endpoint,
                "live": True,
                "probe": "http",
                "status": int(getattr(response, "status", 0) or 0),
            }
    except urllib.error.HTTPError as exc:
        return {
            "endpoint": endpoint,
            "live": True,
            "probe": "http",
            "status": int(exc.code),
        }
    except (urllib.error.URLError, TimeoutError, OSError):
        pass
    except Exception as exc:
        raise MigrationError(
            f"engine quiescence probe was inconclusive for {endpoint}: {exc}"
        ) from exc

    return {
        "endpoint": endpoint,
        "live": False,
        "probe": "tcp+http",
    }


def _require_engine_quiescence(
    *,
    graph_dir: Path,
    confirm_engine_stopped: bool,
    engine_endpoints: Sequence[str] | None,
    additional_engine_endpoints: Sequence[str] | None,
    timeout_sec: float,
) -> dict[str, Any]:
    if not confirm_engine_stopped:
        raise MigrationError(
            "mutation requires --confirm-engine-stopped; dry-run made no changes"
        )
    discovered = _discover_engine_endpoints(graph_dir)
    endpoints = _effective_engine_endpoints(
        engine_endpoints,
        additional_engine_endpoints,
        graph_dir=graph_dir,
        discovered_endpoints=tuple(discovered),
    )
    probes = [
        _endpoint_liveness(endpoint, timeout_sec=timeout_sec)
        for endpoint in endpoints
    ]
    live = [probe for probe in probes if probe["live"]]
    if live:
        descriptions = ", ".join(
            f"{probe['endpoint']} ({probe['probe']})" for probe in live
        )
        raise MigrationError(
            "3CAN engine endpoint is still active; stop every serving engine "
            f"before graph mutation: {descriptions}"
        )
    return {
        "confirmed_engine_stopped": True,
        "checked_endpoints": list(endpoints),
        "discovered_endpoints": discovered,
        "probe_timeout_sec": max(0.05, min(float(timeout_sec), 2.0)),
    }


def _canonical_json_bytes(value: Any, *, pretty: bool = True) -> bytes:
    separators = None if pretty else (",", ":")
    text = json.dumps(
        value,
        ensure_ascii=False,
        indent=2 if pretty else None,
        separators=separators,
        sort_keys=True,
    )
    return (text + "\n").encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MigrationError(f"cannot read valid UTF-8 JSON from {path}: {exc}") from exc


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Write one file through a same-directory temporary and ``os.replace``."""

    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        for retry_index in range(len(ATOMIC_REPLACE_RETRY_DELAYS_SEC) + 1):
            try:
                os.replace(temporary, path)
                break
            except OSError as exc:
                transient_windows_error = getattr(exc, "winerror", None)
                if (
                    transient_windows_error not in TRANSIENT_WINDOWS_REPLACE_ERRORS
                    or retry_index >= len(ATOMIC_REPLACE_RETRY_DELAYS_SEC)
                ):
                    raise
                time.sleep(ATOMIC_REPLACE_RETRY_DELAYS_SEC[retry_index])
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _atomic_write_json(path: Path, value: Any) -> None:
    _atomic_write_bytes(path, _canonical_json_bytes(value))


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _require_graph_layout(graph_dir: Path) -> tuple[Path, Path]:
    graph_dir = graph_dir.resolve()
    nodes_dir = graph_dir / "nodes"
    edges_file = graph_dir / "edges.json"
    if not graph_dir.is_dir():
        raise MigrationError(f"graph directory does not exist: {graph_dir}")
    if not nodes_dir.is_dir():
        raise MigrationError(f"node directory does not exist: {nodes_dir}")
    if not edges_file.is_file():
        raise MigrationError(f"edges file does not exist: {edges_file}")
    if not _path_is_within(nodes_dir, graph_dir) or not _path_is_within(edges_file, graph_dir):
        raise MigrationError("resolved graph paths escaped the selected graph directory")
    return nodes_dir, edges_file


def _node_payload_or_reason(path: Path) -> tuple[dict[str, Any] | None, str]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise MigrationError(f"cannot read node file {path}: {exc}") from exc
    if not raw:
        return None, "empty_file"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, "invalid_utf8"
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None, "invalid_json"
    if not isinstance(payload, dict):
        return None, "not_json_object"
    node_id = str(payload.get("id") or "").strip()
    if not node_id:
        return None, "missing_node_id"
    if node_id != path.stem:
        return None, "node_id_filename_mismatch"
    return payload, ""


def _load_graph(
    graph_dir: Path,
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, Path],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    nodes_dir, edges_file = _require_graph_layout(graph_dir)
    nodes: dict[str, dict[str, Any]] = {}
    node_paths: dict[str, Path] = {}
    corrupt_node_files: list[dict[str, Any]] = []
    for path in sorted(nodes_dir.glob("*.json"), key=lambda item: item.name):
        if path.is_symlink() or path.resolve().parent != nodes_dir.resolve():
            raise MigrationError(f"node path escaped the selected nodes directory: {path}")
        payload, invalid_reason = _node_payload_or_reason(path)
        if invalid_reason:
            corrupt_node_files.append(
                {
                    "node_id": path.stem,
                    "source_file": path.relative_to(graph_dir).as_posix(),
                    "source_sha256": _sha256_file(path),
                    "source_size": path.stat().st_size,
                    "reason": invalid_reason,
                }
            )
            node_paths[path.stem] = path
            continue
        assert payload is not None
        node_id = str(payload.get("id") or "").strip()
        if node_id in nodes:
            raise MigrationError(f"duplicate node id {node_id!r}: {node_paths[node_id]} and {path}")
        nodes[node_id] = payload
        node_paths[node_id] = path

    edges = _read_json(edges_file)
    if not isinstance(edges, list) or any(not isinstance(edge, dict) for edge in edges):
        raise MigrationError(f"edges must be a JSON array of objects: {edges_file}")
    return nodes, node_paths, edges, corrupt_node_files


def _snapshot_id(graph_dir: Path) -> str:
    nodes_dir, edges_file = _require_graph_layout(graph_dir)
    digest = hashlib.sha256()
    for path in sorted(nodes_dir.rglob("*"), key=lambda item: item.relative_to(nodes_dir).as_posix()):
        if not path.is_file():
            continue
        relative = path.relative_to(nodes_dir).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_sha256_file(path).encode("ascii"))
        digest.update(b"\0")
    digest.update(b"edges.json\0")
    digest.update(_sha256_file(edges_file).encode("ascii"))
    return digest.hexdigest()


def _content(node: Mapping[str, Any]) -> Mapping[str, Any]:
    value = node.get("content")
    return value if isinstance(value, Mapping) else {}


def _extra(node: Mapping[str, Any]) -> Mapping[str, Any]:
    value = _content(node).get("extra")
    return value if isinstance(value, Mapping) else {}


def _nonempty(value: Any) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, str):
        normalized = value.strip().lower()
        return bool(normalized and normalized not in {"none", "null", "unknown", "n/a"})
    if isinstance(value, Mapping):
        return any(_nonempty(item) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_nonempty(item) for item in value)
    return True


def _first_value(node: Mapping[str, Any], names: Sequence[str]) -> Any:
    sources = (node, _content(node), _extra(node))
    for source in sources:
        for name in names:
            if name in source and _nonempty(source.get(name)):
                return source.get(name)
    return None


def _occurrence_count(node: Mapping[str, Any]) -> int | None:
    value = _first_value(node, ("occurrence_count", "count", "failure_count", "occurrences"))
    if value is None:
        return None
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return len(value)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return max(parsed, 0)


def _notes_have_labeled_value(node: Mapping[str, Any], labels: Sequence[str]) -> bool:
    notes = str(_content(node).get("notes") or "")
    for label in labels:
        match = re.search(rf"(?im)^\s*{re.escape(label)}\s*:\s*(.+?)\s*$", notes)
        if match and _nonempty(match.group(1)):
            return True
    return False


def _raw_case_status(node: Mapping[str, Any]) -> str:
    value = _first_value(node, ("case_status", "error_case_status", "state", "gate_status"))
    return str(value or "").strip().lower().replace("-", "_")


def _has_diagnosis(node: Mapping[str, Any]) -> bool:
    value = _first_value(
        node,
        (
            "diagnosis",
            "last_diagnosis",
            "root_cause",
            "root_cause_summary",
            "diagnostic_summary",
        ),
    )
    if isinstance(value, str) and value.strip().lower() in {
        "unclassified",
        "unclassified-root-cause",
        "unknown-root-cause",
    }:
        value = None
    return _nonempty(value) or _notes_have_labeled_value(node, ("Diagnosis", "Root cause"))


def _has_solution(node: Mapping[str, Any]) -> bool:
    value = _first_value(
        node,
        (
            "solution",
            "solution_summary",
            "resolution",
            "resolution_summary",
            "resolved_at",
            "current_resolution_id",
            "resolution_id",
            "verification_evidence",
        ),
    )
    return (
        _nonempty(value)
        or _raw_case_status(node) in RESOLVED_STATES
        or _notes_have_labeled_value(
            node,
            ("Solution", "Resolution", "Resolution evidence", "Verification evidence"),
        )
    )


def _is_promoted(node: Mapping[str, Any], count: int | None) -> bool:
    explicit = _first_value(node, ("promoted", "is_promoted", "promoted_at", "case_id"))
    state = _raw_case_status(node)
    return bool(
        _nonempty(explicit)
        or (count is not None and count >= 2)
        or state in DIAGNOSED_STATES
        or state == "promoted"
    )


def _edge_type(edge: Mapping[str, Any]) -> str:
    return str(edge.get("type") or "").strip().lower()


def _edge_key(edge: Mapping[str, Any], index: int | None = None) -> dict[str, Any]:
    result = {
        "source": str(edge.get("source") or ""),
        "target": str(edge.get("target") or ""),
        "type": _edge_type(edge),
    }
    if index is not None:
        result["index"] = index
    return result


def _edge_sort_key(edge: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(edge.get("source") or ""),
        str(edge.get("target") or ""),
        _edge_type(edge),
        json.dumps(edge, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    )


def _has_resolution_edge(node_id: str, edges: Sequence[Mapping[str, Any]]) -> bool:
    return any(
        (
            _edge_type(edge) in RESOLUTION_TARGET_ERROR_EDGE_TYPES
            and str(edge.get("target") or "") == node_id
        )
        or (
            _edge_type(edge) in RESOLUTION_SOURCE_ERROR_EDGE_TYPES
            and str(edge.get("source") or "") == node_id
        )
        for edge in edges
    )


def _is_canonical_error_case(node_id: str, node: Mapping[str, Any]) -> bool:
    extra = _extra(node)
    nested = extra.get("error_case")
    payload = nested if isinstance(nested, Mapping) else extra
    if payload.get("schema_version") != "3can.error-case/v1":
        return False
    if str(payload.get("case_id") or "") != node_id:
        return False
    try:
        case = ErrorCase.from_dict(payload)
        expected_fingerprint = deterministic_fingerprint(
            project_id=str(payload.get("project_id") or ""),
            operation=str(payload.get("operation") or ""),
            component=str(payload.get("component") or ""),
            error_type=str(payload.get("error_type") or ""),
        )
    except (KeyError, TypeError, ValueError):
        return False
    if not case.fingerprint or case.occurrence_count < 2:
        return False
    expected_case_id = f"ERR-case-{expected_fingerprint.split(':', 1)[1][:24]}"
    return (
        case.fingerprint.casefold() == expected_fingerprint.casefold()
        and case.case_id == expected_case_id
    )


def _is_core_registry(node_id: str, node: Mapping[str, Any] | None) -> bool:
    if node_id in KNOWN_CORE_REGISTRY_IDS:
        return True
    lowered_id = node_id.casefold()
    if "registry" in lowered_id and (
        "core-memory" in lowered_id
        or "core_memory" in lowered_id
        or lowered_id.startswith("core-")
    ):
        return True
    if not node:
        return False
    name = str(node.get("name") or "").casefold()
    extra = _extra(node)
    return (
        "core memory" in name
        and "registry" in name
    ) or str(extra.get("memory_lane") or "").casefold() == "memory_registry"


def _normalized_status(
    node: Mapping[str, Any],
    *,
    count: int | None,
    has_diagnosis: bool,
    has_solution: bool,
) -> str:
    state = _raw_case_status(node)
    if state == "superseded":
        return "superseded"
    if state == "regressed":
        return "regressed"
    if has_solution or state in {"resolved", "mitigated"}:
        return "resolved" if state != "mitigated" else "mitigated"
    if has_diagnosis or state == "diagnosed":
        return "diagnosed"
    if state in OBSERVED_STATES or state == "promoted":
        return "observed"
    return state or ("observed" if count is not None else "unknown")


def _normalize_case(
    node: Mapping[str, Any],
    *,
    has_resolution_edge: bool = False,
) -> dict[str, Any]:
    normalized = copy.deepcopy(dict(node))
    content = normalized.get("content")
    if not isinstance(content, dict):
        content = {}
        normalized["content"] = content
    extra = content.get("extra")
    if not isinstance(extra, dict):
        extra = {}
        content["extra"] = extra

    count = _occurrence_count(node)
    has_diagnosis = _has_diagnosis(node)
    has_solution = _has_solution(node) or has_resolution_edge
    promoted = _is_promoted(node, count) or has_diagnosis or has_solution
    status = _normalized_status(
        node,
        count=count,
        has_diagnosis=has_diagnosis,
        has_solution=has_solution,
    )

    source_cluster = str(normalized.get("cluster") or "")
    if source_cluster and source_cluster != CANONICAL_ERROR_CLUSTER:
        extra.setdefault("legacy_source_cluster", source_cluster)
    normalized["cluster"] = CANONICAL_ERROR_CLUSTER
    extra["error_knowledge_schema_version"] = ERROR_KNOWLEDGE_VERSION
    extra["case_status"] = status
    extra["promoted"] = promoted
    # Legacy records do not carry the complete ek2 identity. They may inform a
    # review, but must never block unrelated work merely because their title or
    # embedding resembles the current failure.
    extra["route_blocking"] = False
    extra["blocking_eligibility"] = "canonical_ek2_only"
    extra["family_assignment_status"] = "review_required"
    extra["knowledge_tier"] = "historical"
    extra["route_visibility"] = "explicit_error_only"
    extra["searchable"] = True
    if has_solution:
        evidence_quality = "resolution_claimed"
    elif has_diagnosis:
        evidence_quality = "diagnosed"
    elif promoted:
        evidence_quality = "repeated_observed"
    else:
        evidence_quality = "legacy_evidence_poor"
    extra["legacy_evidence_quality"] = evidence_quality
    extra["legacy_error_migration_version"] = MIGRATION_VERSION
    if count is not None:
        extra["occurrence_count"] = count

    if not extra["route_blocking"]:
        blockers = content.get("blockers")
        if isinstance(blockers, list):
            content["blockers"] = [
                blocker
                for blocker in blockers
                if not any(
                    pattern in str(blocker).casefold()
                    for pattern in LEGACY_BLOCKER_PATTERNS
                )
            ]
    return normalized


def _maintenance_paths(graph_dir: Path, run_id: str) -> dict[str, Path]:
    root = graph_dir / "maintenance" / "legacy_error_migration"
    return {
        "root": root,
        "backup": root / "backups" / run_id,
        "archive": root / "archives" / f"{run_id}.jsonl",
        "manifest": root / "manifests" / f"{run_id}.json",
        "journal": root / "journals" / f"{run_id}.json",
        "embedding_marker": graph_dir / "embeddings.rebuild_required.json",
    }


def build_plan(graph_dir: Path) -> dict[str, Any]:
    """Build a deterministic, side-effect-free migration plan."""

    graph_dir = graph_dir.resolve()
    snapshot_before_load = _snapshot_id(graph_dir)
    nodes, node_paths, edges, corrupt_node_files = _load_graph(graph_dir)
    snapshot_id = _snapshot_id(graph_dir)
    if snapshot_id != snapshot_before_load:
        raise MigrationError(
            "graph changed while the dry-run plan was being read; "
            "quiesce graph writers and rerun"
        )
    run_id = f"legacy-errors-{snapshot_id[:16]}"
    paths = _maintenance_paths(graph_dir, run_id)

    corrupt_canonical_case_ids = sorted(
        str(item["node_id"])
        for item in corrupt_node_files
        if str(item["node_id"]).startswith(CANONICAL_ERROR_CASE_PREFIX)
    )
    if corrupt_canonical_case_ids:
        raise MigrationError(
            "corrupt canonical ErrorCase files require explicit recovery; "
            "the legacy migration will not delete or rewrite them: "
            + ", ".join(corrupt_canonical_case_ids)
        )

    legacy_error_ids = sorted(
        node_id
        for node_id in nodes
        if node_id.startswith(LEGACY_ERROR_PREFIX)
        and not node_id.startswith(CANONICAL_ERROR_CASE_PREFIX)
    )
    canonical_prefixed_ids = sorted(
        node_id for node_id in nodes if node_id.startswith(CANONICAL_ERROR_CASE_PREFIX)
    )
    canonical_case_ids = [
        node_id
        for node_id in canonical_prefixed_ids
        if _is_canonical_error_case(node_id, nodes[node_id])
    ]
    invalid_canonical_case_ids = sorted(
        set(canonical_prefixed_ids) - set(canonical_case_ids)
    )
    noncanonical_error_set = set(legacy_error_ids) | set(
        invalid_canonical_case_ids
    )
    repeated_ids = sorted(
        node_id for node_id in legacy_error_ids if node_id.startswith(REPEATED_ERROR_PREFIX)
    )
    corrupt_node_ids = sorted(
        str(item["node_id"]) for item in corrupt_node_files
    )
    corrupt_node_set = set(corrupt_node_ids)
    resolution_edge_ids = {
        node_id for node_id in legacy_error_ids if _has_resolution_edge(node_id, edges)
    }

    candidates: list[str] = []
    preserved_promoted: list[str] = []
    preserved_diagnosed: list[str] = []
    preserved_resolved: list[str] = []
    preserved_unknown_count: list[str] = []
    normalized_nodes: dict[str, dict[str, Any]] = {}
    changed_normalized_ids: list[str] = []

    for node_id in repeated_ids:
        node = nodes[node_id]
        count = _occurrence_count(node)
        has_diagnosis = _has_diagnosis(node)
        has_solution = _has_solution(node)
        has_resolution_edge = node_id in resolution_edge_ids
        promoted = _is_promoted(node, count)
        removable = (
            (count is None or count <= 1)
            and not promoted
            and not has_solution
            and not has_resolution_edge
        )
        if removable:
            candidates.append(node_id)
            continue
        if count is None:
            preserved_unknown_count.append(node_id)
        if has_solution or has_resolution_edge:
            preserved_resolved.append(node_id)
        elif has_diagnosis:
            preserved_diagnosed.append(node_id)
        elif promoted:
            preserved_promoted.append(node_id)
    candidate_set = set(candidates)
    removal_set = candidate_set | corrupt_node_set
    retained_legacy_ids = [
        node_id for node_id in legacy_error_ids if node_id not in removal_set
    ]
    evidence_quality_counts: dict[str, int] = {}
    for node_id in retained_legacy_ids:
        node = nodes[node_id]
        normalized = _normalize_case(
            node,
            has_resolution_edge=node_id in resolution_edge_ids,
        )
        normalized_nodes[node_id] = normalized
        if normalized != node:
            changed_normalized_ids.append(node_id)
        quality = str(
            _extra(normalized).get("legacy_evidence_quality")
            or "legacy_evidence_poor"
        )
        evidence_quality_counts[quality] = evidence_quality_counts.get(quality, 0) + 1
    registry_ids = {
        node_id for node_id, node in nodes.items() if _is_core_registry(node_id, node)
    }
    removed_indexes: set[int] = set()
    registry_edge_indexes: set[int] = set()
    connected_by_candidate: dict[str, list[dict[str, Any]]] = {
        node_id: [] for node_id in sorted(removal_set)
    }

    for index, edge in enumerate(edges):
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        if source in removal_set or target in removal_set:
            removed_indexes.add(index)
            for node_id in removal_set:
                if source == node_id or target == node_id:
                    connected_by_candidate[node_id].append(copy.deepcopy(edge))
        if (
            source in registry_ids
            and target in noncanonical_error_set
            and _edge_type(edge) == "requires"
        ):
            removed_indexes.add(index)
            registry_edge_indexes.add(index)

    remaining_edges = [
        copy.deepcopy(edge) for index, edge in enumerate(edges) if index not in removed_indexes
    ]
    removed_edges = [
        {"index": index, "edge": copy.deepcopy(edges[index])}
        for index in sorted(removed_indexes)
    ]
    registry_removed_edges = [
        {"index": index, "edge": copy.deepcopy(edges[index])}
        for index in sorted(registry_edge_indexes)
    ]

    archive_records = []
    for node_id in candidates:
        archive_records.append(
            {
                "schema_version": ARCHIVE_VERSION,
                "node_id": node_id,
                "source_file": node_paths[node_id].relative_to(graph_dir).as_posix(),
                "source_sha256": _sha256_file(node_paths[node_id]),
                "node": copy.deepcopy(nodes[node_id]),
                "connected_edges": sorted(
                    connected_by_candidate[node_id],
                    key=_edge_sort_key,
                ),
            }
        )
    for corrupt in corrupt_node_files:
        node_id = str(corrupt["node_id"])
        archive_records.append(
            {
                "schema_version": ARCHIVE_VERSION,
                "record_type": "corrupt_graph_node",
                **copy.deepcopy(corrupt),
                "node": None,
                "connected_edges": sorted(
                    connected_by_candidate[node_id],
                    key=_edge_sort_key,
                ),
                "recovery": "exact raw bytes are retained in the complete rollback backup",
            }
        )

    before = {
        "node_count": len(nodes),
        "node_file_count": len(nodes) + len(corrupt_node_files),
        "edge_count": len(edges),
        "repeated_error_count": len(repeated_ids),
        "legacy_error_count": len(legacy_error_ids),
        "canonical_error_case_count": len(canonical_case_ids),
        "invalid_canonical_error_case_count": len(invalid_canonical_case_ids),
        "canonical_error_cluster_count": sum(
            1
            for node_id in (*legacy_error_ids, *canonical_case_ids)
            if str(nodes[node_id].get("cluster") or "") == CANONICAL_ERROR_CLUSTER
        ),
        "corrupt_node_file_count": len(corrupt_node_files),
        "core_registry_requires_to_legacy_count": len(registry_edge_indexes),
        "embedding_cache_present": (graph_dir / "embeddings.npz").is_file(),
    }
    after = {
        "node_count": len(nodes) - len(candidates),
        "node_file_count": len(nodes) - len(candidates),
        "edge_count": len(remaining_edges),
        "repeated_error_count": len(repeated_ids) - len(candidates),
        "legacy_error_count": len(retained_legacy_ids),
        "canonical_error_case_count": len(canonical_case_ids),
        "invalid_canonical_error_case_count": len(invalid_canonical_case_ids),
        "canonical_error_cluster_count": len(retained_legacy_ids) + sum(
            1
            for node_id in canonical_case_ids
            if str(nodes[node_id].get("cluster") or "") == CANONICAL_ERROR_CLUSTER
        ),
        "corrupt_node_file_count": 0,
        "core_registry_requires_to_legacy_count": 0,
        "embedding_cache_present": False,
        "embedding_rebuild_marker_present": True,
    }
    changed = bool(
        candidates
        or corrupt_node_files
        or removed_indexes
        or changed_normalized_ids
    )

    return {
        "schema_version": MIGRATION_VERSION,
        "run_id": run_id,
        "snapshot_id": snapshot_id,
        "graph_dir": str(graph_dir),
        "changed": changed,
        "before": before,
        "after": after if changed else {**before, "embedding_rebuild_marker_present": (graph_dir / "embeddings.rebuild_required.json").is_file()},
        "registry_node_ids": sorted(registry_ids),
        "candidate_node_ids": candidates,
        "corrupt_node_ids": corrupt_node_ids,
        "corrupt_node_files": copy.deepcopy(corrupt_node_files),
        "removal_node_ids": sorted(removal_set),
        "normalized_node_ids": sorted(changed_normalized_ids),
        "retained_legacy_node_ids": retained_legacy_ids,
        "invalid_canonical_error_case_ids": invalid_canonical_case_ids,
        "legacy_evidence_quality_counts": dict(sorted(evidence_quality_counts.items())),
        "preserved": {
            "promoted_node_ids": sorted(set(preserved_promoted)),
            "diagnosed_node_ids": sorted(set(preserved_diagnosed)),
            "resolved_node_ids": sorted(set(preserved_resolved)),
            "unknown_count_node_ids": sorted(set(preserved_unknown_count)),
        },
        "removed_edges": removed_edges,
        "removed_core_registry_requires_edges": registry_removed_edges,
        "paths": {
            "backup": str(paths["backup"]),
            "archive": str(paths["archive"]),
            "manifest": str(paths["manifest"]),
            "journal": str(paths["journal"]),
            "embedding_rebuild_marker": str(paths["embedding_marker"]),
        },
        "rollback": {
            "command": (
                f'{Path(sys.executable).name} "{Path(__file__).resolve()}" '
                f'--graph-dir "{graph_dir}" --rollback "{paths["backup"]}" '
                "--confirm-engine-stopped"
            ),
            "note": (
                "Stop every 3CAN engine using the graph; rollback will probe "
                "configured endpoints before mutation."
            ),
        },
        "_internal": {
            "node_paths": {
                node_id: str(node_paths[node_id])
                for node_id in sorted(set(legacy_error_ids) | corrupt_node_set)
            },
            "normalized_nodes": normalized_nodes,
            "remaining_edges": remaining_edges,
            "archive_records": archive_records,
        },
    }


def _bound_public_list(
    container: dict[str, Any],
    key: str,
    *,
    limit: int = PUBLIC_MANIFEST_LIST_LIMIT,
) -> None:
    value = container.get(key)
    if not isinstance(value, list):
        return
    count = len(value)
    container[f"{key}_count"] = count
    container[f"{key}_truncated"] = count > limit
    if count > limit:
        container[key] = value[:limit]


def _public_manifest(
    plan: Mapping[str, Any],
    *,
    mode: str,
    applied: bool,
    no_op: bool,
    backup_reused: bool = False,
) -> dict[str, Any]:
    manifest = {
        key: copy.deepcopy(value)
        for key, value in {
            **{item: value for item, value in plan.items() if item != "_internal"},
            "mode": mode,
            "applied": applied,
            "no_op": no_op,
            "backup_reused": backup_reused,
        }.items()
    }
    for key in (
        "registry_node_ids",
        "candidate_node_ids",
        "corrupt_node_ids",
        "corrupt_node_files",
        "removal_node_ids",
        "normalized_node_ids",
        "retained_legacy_node_ids",
        "invalid_canonical_error_case_ids",
        "removed_edges",
        "removed_core_registry_requires_edges",
    ):
        _bound_public_list(manifest, key)
    preserved = manifest.get("preserved")
    if isinstance(preserved, dict):
        for key in tuple(preserved):
            _bound_public_list(preserved, key)
    return manifest


def _backup_metadata(
    graph_dir: Path,
    backup_snapshot_dir: Path,
    run_id: str,
    snapshot_id: str,
) -> dict[str, Any]:
    nodes_dir = backup_snapshot_dir / "nodes"
    edges_file = backup_snapshot_dir / "edges.json"
    embeddings = backup_snapshot_dir / "embeddings.npz"
    marker = backup_snapshot_dir / "embeddings.rebuild_required.json"
    node_files = []
    for path in sorted(nodes_dir.rglob("*"), key=lambda item: item.relative_to(nodes_dir).as_posix()):
        if path.is_file():
            node_files.append(
                {
                    "path": path.relative_to(nodes_dir).as_posix(),
                    "sha256": _sha256_file(path),
                }
            )
    return {
        "schema_version": BACKUP_VERSION,
        "complete": True,
        "run_id": run_id,
        "snapshot_id": snapshot_id,
        "graph_dir": str(graph_dir.resolve()),
        "node_files": node_files,
        "edges_sha256": _sha256_file(edges_file),
        "embeddings_present": embeddings.is_file(),
        "embeddings_sha256": _sha256_file(embeddings) if embeddings.is_file() else "",
        "embedding_marker_present": marker.is_file(),
        "embedding_marker_sha256": _sha256_file(marker) if marker.is_file() else "",
    }


def _create_backup(graph_dir: Path, backup_dir: Path, run_id: str, snapshot_id: str) -> bool:
    """Publish a complete rollback backup. Return True when an existing copy was reused."""

    if backup_dir.exists():
        metadata_path = backup_dir / "backup_metadata.json"
        metadata = _read_json(metadata_path) if metadata_path.is_file() else {}
        if (
            isinstance(metadata, dict)
            and metadata.get("complete") is True
            and metadata.get("snapshot_id") == snapshot_id
            and metadata.get("run_id") == run_id
        ):
            _validate_backup_files(backup_dir, metadata)
            return True
        raise MigrationError(f"refusing to overwrite an incomplete or mismatched backup: {backup_dir}")

    nodes_dir, edges_file = _require_graph_layout(graph_dir)
    backup_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary = backup_dir.parent / f".{backup_dir.name}.tmp"
    if temporary.exists():
        # This function is called while holding the owner-verifiable mutation
        # lock.  A prior process can only leave this unpublished directory by
        # dying before the atomic replace, so rebuilding it is safe.
        if temporary.is_dir():
            shutil.rmtree(temporary)
        else:
            temporary.unlink()
    try:
        temporary.mkdir(parents=False)
        shutil.copytree(nodes_dir, temporary / "nodes")
        shutil.copy2(edges_file, temporary / "edges.json")
        embeddings = graph_dir / "embeddings.npz"
        marker = graph_dir / "embeddings.rebuild_required.json"
        if embeddings.is_file():
            shutil.copy2(embeddings, temporary / "embeddings.npz")
        if marker.is_file():
            shutil.copy2(marker, temporary / "embeddings.rebuild_required.json")
        metadata = _backup_metadata(graph_dir, temporary, run_id, snapshot_id)
        _atomic_write_json(temporary / "backup_metadata.json", metadata)
        os.replace(temporary, backup_dir)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    return False


def _validate_backup_files(backup_dir: Path, metadata: Mapping[str, Any]) -> None:
    """Verify a complete rollback set before trusting or restoring it."""

    backup_nodes = backup_dir / "nodes"
    backup_edges = backup_dir / "edges.json"
    if not backup_nodes.is_dir() or not backup_edges.is_file():
        raise MigrationError(f"rollback backup is incomplete: {backup_dir}")

    declared_nodes = metadata.get("node_files")
    if not isinstance(declared_nodes, list):
        raise MigrationError(f"rollback backup has no node-file inventory: {backup_dir}")
    declared_by_path: dict[str, str] = {}
    for entry in declared_nodes:
        if not isinstance(entry, Mapping):
            raise MigrationError(f"rollback backup has an invalid node-file entry: {backup_dir}")
        relative = str(entry.get("path") or "")
        expected_hash = str(entry.get("sha256") or "")
        candidate = (backup_nodes / relative).resolve()
        if (
            not relative
            or not expected_hash
            or not _path_is_within(candidate, backup_nodes)
            or not candidate.is_file()
        ):
            raise MigrationError(f"rollback backup node is missing or unsafe: {relative!r}")
        declared_by_path[relative] = expected_hash

    actual_paths = {
        path.relative_to(backup_nodes).as_posix()
        for path in backup_nodes.rglob("*")
        if path.is_file()
    }
    if actual_paths != set(declared_by_path):
        raise MigrationError("rollback backup node inventory does not match its metadata")
    for relative, expected_hash in declared_by_path.items():
        if _sha256_file(backup_nodes / relative) != expected_hash:
            raise MigrationError(f"rollback backup node checksum failed: {relative}")

    if _sha256_file(backup_edges) != str(metadata.get("edges_sha256") or ""):
        raise MigrationError("rollback backup edges checksum failed")
    embeddings = backup_dir / "embeddings.npz"
    if bool(metadata.get("embeddings_present")):
        if (
            not embeddings.is_file()
            or _sha256_file(embeddings) != str(metadata.get("embeddings_sha256") or "")
        ):
            raise MigrationError("rollback backup embedding checksum failed")
    elif embeddings.exists():
        raise MigrationError("rollback backup contains an undeclared embedding cache")
    marker = backup_dir / "embeddings.rebuild_required.json"
    if bool(metadata.get("embedding_marker_present")):
        if (
            not marker.is_file()
            or _sha256_file(marker) != str(metadata.get("embedding_marker_sha256") or "")
        ):
            raise MigrationError("rollback backup embedding-marker checksum failed")
    elif marker.exists():
        raise MigrationError("rollback backup contains an undeclared embedding marker")


def _archive_bytes(records: Sequence[Mapping[str, Any]]) -> bytes:
    return b"".join(_canonical_json_bytes(record, pretty=False) for record in records)


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _plan_hash(plan: Mapping[str, Any]) -> str:
    return _sha256_bytes(_canonical_json_bytes(plan, pretty=False))


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return ctypes.get_last_error() == 5
        except Exception:
            # A failed liveness probe is not proof that a lock is stale.
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _read_lock_snapshot(lock_path: Path) -> tuple[bytes, os.stat_result, dict[str, Any]]:
    try:
        stat = lock_path.stat()
        raw = lock_path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise MigrationError(
            f"migration lock exists but cannot be safely inspected: {lock_path}: {exc}"
        ) from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        legacy = re.fullmatch(r"\s*pid=(\d+)\s*", text)
        if not legacy:
            raise MigrationError(
                f"migration lock exists but cannot be safely inspected: "
                f"{lock_path}: {exc}"
            ) from exc
        # V1 of this same script wrote only a local PID.  Preserving that
        # provenance lets a dead legacy owner recover without weakening the
        # rule for arbitrary/malformed lock files.
        payload = {
            "schema_version": "3can.legacy-error-migration-lock/legacy-pid",
            "lock_id": f"legacy-{_sha256_bytes(raw)[:24]}",
            "pid": int(legacy.group(1)),
            "host": socket.gethostname(),
            "started_at": dt.datetime.fromtimestamp(
                stat.st_mtime,
                tz=dt.timezone.utc,
            ).isoformat(),
            "operation": "legacy-unknown",
            "plan_hash": "legacy-unknown",
        }
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version")
        not in {LOCK_VERSION, "3can.legacy-error-migration-lock/legacy-pid"}
        or not str(payload.get("lock_id") or "")
        or not str(payload.get("host") or "")
        or not isinstance(payload.get("pid"), int)
        or not str(payload.get("started_at") or "")
        or not str(payload.get("operation") or "")
        or not str(payload.get("plan_hash") or "")
    ):
        raise MigrationError(
            f"migration lock has no verifiable owner; inspect it manually: {lock_path}"
        )
    return raw, stat, payload


def _archive_stale_lock(
    graph_dir: Path,
    lock_path: Path,
    raw: bytes,
    original_stat: os.stat_result,
) -> Path:
    try:
        current_stat = lock_path.stat()
        current_raw = lock_path.read_bytes()
    except OSError as exc:
        raise MigrationError(f"migration lock changed during stale-lock recovery: {exc}") from exc
    identity_fields = ("st_ino", "st_size", "st_mtime_ns")
    if any(
        getattr(current_stat, field, None) != getattr(original_stat, field, None)
        for field in identity_fields
    ) or current_raw != raw:
        raise MigrationError("migration lock owner changed during stale-lock recovery")
    archive_dir = (
        graph_dir
        / "maintenance"
        / "legacy_error_migration"
        / "stale_locks"
    )
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    archived = archive_dir / f"{stamp}-{uuid.uuid4().hex[:12]}.lock"
    os.replace(lock_path, archived)
    return archived


@contextmanager
def _mutation_lock(
    graph_dir: Path,
    *,
    operation: str,
    plan_hash: str,
) -> Iterator[dict[str, Any]]:
    try:
        runtime_lease = acquire_graph_runtime_lock(
            graph_dir,
            owner_kind=f"legacy-error-migration:{operation}",
        )
    except GraphRuntimeLockError as exc:
        raise MigrationError(
            "graph runtime or another maintenance writer still owns the graph: "
            f"{exc}"
        ) from exc

    try:
        with _legacy_mutation_lock(
            graph_dir,
            operation=operation,
            plan_hash=plan_hash,
        ) as owner:
            yield owner
    finally:
        runtime_lease.release()


@contextmanager
def _legacy_mutation_lock(
    graph_dir: Path,
    *,
    operation: str,
    plan_hash: str,
) -> Iterator[dict[str, Any]]:
    lock_path = graph_dir / ".legacy_error_migration.lock"
    owner = {
        "schema_version": LOCK_VERSION,
        "lock_id": uuid.uuid4().hex,
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "started_at": _utc_now(),
        "operation": operation,
        "plan_hash": plan_hash,
    }
    descriptor: int | None = None
    for _attempt in range(2):
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError as exc:
            raw, stat, existing = _read_lock_snapshot(lock_path)
            same_host = str(existing["host"]).casefold() == socket.gethostname().casefold()
            existing_pid = int(existing["pid"])
            if same_host and not _pid_is_alive(existing_pid):
                _archive_stale_lock(graph_dir, lock_path, raw, stat)
                continue
            owner_description = (
                f"pid={existing_pid} host={existing['host']} "
                f"started_at={existing.get('started_at', 'unknown')} "
                f"operation={existing.get('operation', 'unknown')}"
            )
            raise MigrationError(
                f"another legacy error migration lock is active or remote: "
                f"{owner_description}; {lock_path}"
            ) from exc
    if descriptor is None:
        raise MigrationError(f"could not acquire migration lock after stale recovery: {lock_path}")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_canonical_json_bytes(owner))
            handle.flush()
            os.fsync(handle.fileno())
        yield owner
    finally:
        try:
            current = _read_json(lock_path) if lock_path.is_file() else {}
        except MigrationError:
            current = {}
        if (
            isinstance(current, Mapping)
            and current.get("lock_id") == owner["lock_id"]
        ):
            lock_path.unlink(missing_ok=True)


def _save_journal(path: Path, journal: dict[str, Any]) -> None:
    journal["updated_at"] = _utc_now()
    _atomic_write_json(path, journal)


def _set_journal_phase(
    path: Path,
    journal: dict[str, Any],
    phase: str,
    *,
    checkpoint: str | None = None,
    checkpoint_value: Any = True,
) -> None:
    journal["phase"] = phase
    if checkpoint:
        checkpoints = journal.setdefault("checkpoints", {})
        if not isinstance(checkpoints, dict):
            raise MigrationError(f"migration journal has invalid checkpoints: {path}")
        checkpoints[checkpoint] = checkpoint_value
    if phase in TERMINAL_JOURNAL_PHASES:
        journal["completed_at"] = _utc_now()
    _save_journal(path, journal)


def _load_journal(path: Path) -> dict[str, Any]:
    payload = _read_json(path)
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != JOURNAL_VERSION
        or not str(payload.get("operation") or "")
        or not str(payload.get("plan_hash") or "")
        or not str(payload.get("graph_dir") or "")
    ):
        raise MigrationError(f"invalid migration journal: {path}")
    return payload


def _incomplete_journals(graph_dir: Path) -> list[tuple[Path, dict[str, Any]]]:
    journal_dir = (
        graph_dir
        / "maintenance"
        / "legacy_error_migration"
        / "journals"
    )
    if not journal_dir.is_dir():
        return []
    found: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(journal_dir.glob("*.json")):
        journal = _load_journal(path)
        if Path(str(journal["graph_dir"])).resolve() != graph_dir.resolve():
            continue
        if str(journal.get("phase") or "") not in TERMINAL_JOURNAL_PHASES:
            found.append((path, journal))
    return found


def _new_apply_journal(plan: Mapping[str, Any]) -> dict[str, Any]:
    plan_copy = copy.deepcopy(dict(plan))
    return {
        "schema_version": JOURNAL_VERSION,
        "operation": "apply",
        "run_id": str(plan["run_id"]),
        "graph_dir": str(plan["graph_dir"]),
        "plan_hash": _plan_hash(plan_copy),
        "started_at": _utc_now(),
        "updated_at": _utc_now(),
        "phase": "planned",
        "checkpoints": {
            "backup_complete": False,
            "archive_complete": False,
            "normalized_node_ids": [],
            "edges_written": False,
            "removed_node_ids": [],
            "removed_corrupt_node_ids": [],
            "embeddings_invalidated": False,
            "manifest_written": False,
        },
        "plan": plan_copy,
    }


def _plan_from_journal(
    path: Path,
    journal: Mapping[str, Any],
    *,
    require_current_version: bool = True,
) -> dict[str, Any]:
    plan = journal.get("plan")
    if not isinstance(plan, dict):
        raise MigrationError(f"apply journal does not contain its deterministic plan: {path}")
    if _plan_hash(plan) != journal.get("plan_hash"):
        raise MigrationError(f"apply journal plan hash mismatch: {path}")
    if str(plan.get("graph_dir") or "") != str(journal.get("graph_dir") or ""):
        raise MigrationError(f"apply journal graph path mismatch: {path}")
    if (
        require_current_version
        and str(plan.get("schema_version") or "") != MIGRATION_VERSION
    ):
        raise MigrationError(
            "incomplete apply journal uses a different migration version; "
            "use its recorded rollback command before running this version: "
            f"{path}"
        )
    return copy.deepcopy(plan)


def _expected_embedding_marker(plan: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": str(plan["schema_version"]),
        "reason": "legacy ErrorCase nodes, edges, or searchable content changed",
        "run_id": plan["run_id"],
        "removed_node_ids": plan["removal_node_ids"],
        "normalized_node_ids": plan["normalized_node_ids"],
    }


def _validate_resumable_apply_state(
    graph_dir: Path,
    plan: Mapping[str, Any],
) -> None:
    """Reject resume if anything outside the deterministic plan changed."""

    paths = {name: Path(value) for name, value in plan["paths"].items()}
    metadata = _read_json(paths["backup"] / "backup_metadata.json")
    if not isinstance(metadata, dict):
        raise MigrationError("resume backup metadata is invalid")
    _validate_backup_files(paths["backup"], metadata)
    nodes_dir = graph_dir / "nodes"
    declared = {
        str(entry["path"]): str(entry["sha256"])
        for entry in metadata["node_files"]
    }
    actual_paths = {
        path.relative_to(nodes_dir).as_posix()
        for path in nodes_dir.rglob("*")
        if path.is_file()
    }
    if not actual_paths.issubset(set(declared)):
        raise MigrationError("cannot resume: graph gained node files outside the migration plan")

    candidates = set(plan["removal_node_ids"])
    normalized = set(plan["normalized_node_ids"])
    internal = plan["_internal"]
    node_id_by_relative: dict[str, str] = {}
    for node_id, raw_path in internal["node_paths"].items():
        candidate_path = Path(str(raw_path)).resolve()
        if not _path_is_within(candidate_path, nodes_dir):
            raise MigrationError(
                f"cannot resume: planned node path escaped the graph: {candidate_path}"
            )
        node_id_by_relative[
            candidate_path.relative_to(nodes_dir.resolve()).as_posix()
        ] = str(node_id)
    for relative, original_hash in declared.items():
        actual = nodes_dir / relative
        node_id = node_id_by_relative.get(relative, Path(relative).stem)
        if not actual.exists():
            if node_id in candidates:
                continue
            raise MigrationError(f"cannot resume: required node disappeared: {relative}")
        actual_hash = _sha256_file(actual)
        allowed = {original_hash}
        if node_id in normalized:
            expected = _canonical_json_bytes(internal["normalized_nodes"][node_id])
            allowed.add(_sha256_bytes(expected))
        if actual_hash not in allowed:
            raise MigrationError(f"cannot resume: node changed outside checkpoint: {relative}")

    desired_edges_hash = _sha256_bytes(
        _canonical_json_bytes(internal["remaining_edges"])
    )
    actual_edges_hash = _sha256_file(graph_dir / "edges.json")
    if actual_edges_hash not in {
        str(metadata.get("edges_sha256") or ""),
        desired_edges_hash,
    }:
        raise MigrationError("cannot resume: edges changed outside the migration plan")

    embeddings = graph_dir / "embeddings.npz"
    if embeddings.exists():
        if (
            not bool(metadata.get("embeddings_present"))
            or _sha256_file(embeddings)
            != str(metadata.get("embeddings_sha256") or "")
        ):
            raise MigrationError("cannot resume: embedding cache changed outside the plan")

    marker = graph_dir / "embeddings.rebuild_required.json"
    if marker.exists():
        allowed_marker_hashes = {
            _sha256_bytes(_canonical_json_bytes(_expected_embedding_marker(plan)))
        }
        if bool(metadata.get("embedding_marker_present")):
            allowed_marker_hashes.add(
                str(metadata.get("embedding_marker_sha256") or "")
            )
        if _sha256_file(marker) not in allowed_marker_hashes:
            raise MigrationError("cannot resume: embedding marker changed outside the plan")

    if paths["archive"].exists():
        expected_archive = _archive_bytes(internal["archive_records"])
        if paths["archive"].read_bytes() != expected_archive:
            raise MigrationError("cannot resume: migration archive does not match the plan")


def migrate(
    graph_dir: Path | str,
    *,
    apply: bool = False,
    confirm_engine_stopped: bool = False,
    engine_endpoints: Sequence[str] | None = None,
    additional_engine_endpoints: Sequence[str] | None = None,
    engine_probe_timeout_sec: float = DEFAULT_ENGINE_PROBE_TIMEOUT_SEC,
) -> dict[str, Any]:
    """Plan or apply the migration.

    Dry-run remains read-only and does not require engine quiescence.  An apply
    requires explicit confirmation and refuses to mutate while any configured
    HTTP/TCP endpoint is live.
    """

    graph = Path(graph_dir).resolve()
    incomplete = _incomplete_journals(graph)
    rollback_journals = [
        (path, journal)
        for path, journal in incomplete
        if journal.get("operation") == "rollback"
    ]
    if rollback_journals:
        paths = ", ".join(str(path) for path, _journal in rollback_journals)
        raise MigrationError(
            "a rollback is incomplete; rerun the same explicit --rollback "
            f"command before planning/applying another migration: {paths}"
        )
    apply_journals = [
        (path, journal)
        for path, journal in incomplete
        if journal.get("operation") == "apply"
    ]
    if len(apply_journals) > 1:
        raise MigrationError(
            "multiple incomplete apply journals exist; inspect them and use "
            "the recorded rollback command before continuing"
        )

    quiescence: dict[str, Any] | None = None
    if apply:
        quiescence = _require_engine_quiescence(
            graph_dir=graph,
            confirm_engine_stopped=confirm_engine_stopped,
            engine_endpoints=engine_endpoints,
            additional_engine_endpoints=additional_engine_endpoints,
            timeout_sec=engine_probe_timeout_sec,
        )

    resumed = bool(apply_journals)
    if resumed:
        journal_path, journal = apply_journals[0]
        plan = _plan_from_journal(journal_path, journal)
    else:
        plan = build_plan(graph)
        journal_path = Path(plan["paths"]["journal"])
        journal = _new_apply_journal(plan)

    if not apply:
        result = _public_manifest(
            plan,
            mode="dry-run",
            applied=False,
            no_op=not bool(plan["changed"]),
        )
        if resumed:
            result["resume_required"] = True
            result["journal_phase"] = journal["phase"]
            result["journal_path"] = str(journal_path)
            result["note"] = (
                "A prior apply was interrupted. Re-run --apply to resume its "
                "verified plan, or use the recorded --rollback command."
            )
        return result
    if not plan["changed"]:
        manifest = _public_manifest(plan, mode="apply", applied=False, no_op=True)
        manifest["engine_quiescence"] = quiescence
        return manifest

    paths = {name: Path(value) for name, value in plan["paths"].items()}
    internal = plan["_internal"]
    plan_digest = str(journal["plan_hash"])
    with _mutation_lock(graph, operation="apply", plan_hash=plan_digest):
        quiescence = _require_engine_quiescence(
            graph_dir=graph,
            confirm_engine_stopped=confirm_engine_stopped,
            engine_endpoints=engine_endpoints,
            additional_engine_endpoints=additional_engine_endpoints,
            timeout_sec=engine_probe_timeout_sec,
        )
        quiescence["checked_immediately_before_mutation"] = True
        if not resumed and _snapshot_id(graph) != plan["snapshot_id"]:
            raise MigrationError("graph changed after planning; rerun the migration")

        if not resumed:
            if journal_path.exists():
                existing = _load_journal(journal_path)
                if str(existing.get("phase") or "") not in TERMINAL_JOURNAL_PHASES:
                    raise MigrationError(
                        f"refusing to overwrite an incomplete journal: {journal_path}"
                    )
                history_dir = journal_path.parent / "history"
                history_dir.mkdir(parents=True, exist_ok=True)
                archived = history_dir / (
                    f"{journal_path.stem}-{uuid.uuid4().hex[:12]}.json"
                )
                os.replace(journal_path, archived)
            _save_journal(journal_path, journal)

        if resumed and not paths["backup"].exists():
            checkpoints = journal.get("checkpoints")
            if (
                journal.get("phase") != "planned"
                or not isinstance(checkpoints, Mapping)
                or any(bool(value) for value in checkpoints.values())
                or _snapshot_id(graph) != plan["snapshot_id"]
            ):
                raise MigrationError(
                    "cannot resume without the original rollback backup; use "
                    "manual recovery rather than snapshotting a partial graph"
                )
        backup_reused = _create_backup(
            graph,
            paths["backup"],
            str(plan["run_id"]),
            str(plan["snapshot_id"]),
        )
        if _snapshot_id(graph) != plan["snapshot_id"]:
            if not resumed:
                raise MigrationError(
                    "graph changed while the rollback backup was being created"
                )
        checkpoints = journal.get("checkpoints")
        if not isinstance(checkpoints, dict):
            raise MigrationError(f"migration journal has invalid checkpoints: {journal_path}")
        if not bool(checkpoints.get("backup_complete")):
            _set_journal_phase(
                journal_path,
                journal,
                "backup_complete",
                checkpoint="backup_complete",
            )
        _validate_resumable_apply_state(graph, plan)

        archive_payload = _archive_bytes(internal["archive_records"])
        if paths["archive"].exists():
            if paths["archive"].read_bytes() != archive_payload:
                raise MigrationError(f"refusing to overwrite a mismatched archive: {paths['archive']}")
        else:
            _atomic_write_bytes(paths["archive"], archive_payload)
        if not bool(checkpoints.get("archive_complete")):
            _set_journal_phase(
                journal_path,
                journal,
                "archive_complete",
                checkpoint="archive_complete",
            )

        normalized_done = set(
            journal.get("checkpoints", {}).get("normalized_node_ids") or []
        )
        normalized_pending = 0
        for node_id in sorted(plan["normalized_node_ids"]):
            node_path = Path(internal["node_paths"][node_id]).resolve()
            if node_path.parent != (graph / "nodes").resolve():
                raise MigrationError(f"unsafe normalized node path for {node_id}")
            if node_id in normalized_done:
                continue
            _atomic_write_json(node_path, internal["normalized_nodes"][node_id])
            normalized_done.add(node_id)
            normalized_pending += 1
            if normalized_pending < JOURNAL_CHECKPOINT_BATCH_SIZE:
                continue
            checkpoints["normalized_node_ids"] = sorted(normalized_done)
            _set_journal_phase(
                journal_path,
                journal,
                "normalizing_nodes",
            )
            normalized_pending = 0
        if normalized_pending:
            checkpoints["normalized_node_ids"] = sorted(normalized_done)
            _set_journal_phase(
                journal_path,
                journal,
                "normalizing_nodes",
            )

        _atomic_write_json(graph / "edges.json", internal["remaining_edges"])
        if not bool(checkpoints.get("edges_written")):
            _set_journal_phase(
                journal_path,
                journal,
                "edges_written",
                checkpoint="edges_written",
            )

        removed_done = set(
            journal.get("checkpoints", {}).get("removed_node_ids") or []
        )
        corrupt_ids = set(plan["corrupt_node_ids"])
        removed_corrupt_done = set(
            journal.get("checkpoints", {}).get(
                "removed_corrupt_node_ids"
            )
            or []
        )
        removal_pending = 0
        for node_id in plan["removal_node_ids"]:
            candidate_path = Path(internal["node_paths"][node_id]).resolve()
            if candidate_path.parent != (graph / "nodes").resolve():
                raise MigrationError(f"candidate path escaped nodes directory: {candidate_path}")
            checkpoint_values = removed_done
            if node_id in corrupt_ids:
                checkpoint_values = removed_corrupt_done
            if node_id in checkpoint_values:
                continue
            candidate_path.unlink(missing_ok=True)
            checkpoint_values.add(node_id)
            removal_pending += 1
            if removal_pending < JOURNAL_CHECKPOINT_BATCH_SIZE:
                continue
            checkpoints["removed_node_ids"] = sorted(removed_done)
            checkpoints["removed_corrupt_node_ids"] = sorted(removed_corrupt_done)
            _set_journal_phase(
                journal_path,
                journal,
                "removing_candidates",
            )
            removal_pending = 0
        if removal_pending:
            checkpoints["removed_node_ids"] = sorted(removed_done)
            checkpoints["removed_corrupt_node_ids"] = sorted(removed_corrupt_done)
            _set_journal_phase(
                journal_path,
                journal,
                "removing_candidates",
            )

        (graph / "embeddings.npz").unlink(missing_ok=True)
        _atomic_write_json(
            paths["embedding_rebuild_marker"],
            _expected_embedding_marker(plan),
        )
        _set_journal_phase(
            journal_path,
            journal,
            "embeddings_invalidated",
            checkpoint="embeddings_invalidated",
        )

        manifest = _public_manifest(
            plan,
            mode="apply",
            applied=True,
            no_op=False,
            backup_reused=backup_reused,
        )
        manifest["engine_quiescence"] = quiescence
        manifest["resumed_from_journal"] = resumed
        manifest["journal_path"] = str(journal_path)
        manifest["journal_phase"] = "manifest_written"
        _atomic_write_json(paths["manifest"], manifest)
        _set_journal_phase(
            journal_path,
            journal,
            "manifest_written",
            checkpoint="manifest_written",
        )
        _set_journal_phase(journal_path, journal, "completed")
        manifest["journal_phase"] = "completed"
        _atomic_write_json(paths["manifest"], manifest)
        return manifest


def _nodes_match_backup(
    nodes_dir: Path,
    metadata: Mapping[str, Any],
) -> bool:
    if not nodes_dir.is_dir():
        return False
    declared = {
        str(entry.get("path") or ""): str(entry.get("sha256") or "")
        for entry in metadata.get("node_files", [])
        if isinstance(entry, Mapping)
    }
    actual = {
        path.relative_to(nodes_dir).as_posix(): _sha256_file(path)
        for path in nodes_dir.rglob("*")
        if path.is_file()
    }
    return actual == declared


def _rollback_journal_details(
    graph: Path,
    backup_dir: Path,
    metadata: Mapping[str, Any],
) -> tuple[Path, str, dict[str, Any]]:
    plan_identity = {
        "operation": "rollback",
        "graph_dir": str(graph),
        "backup": str(backup_dir),
        "snapshot_id": metadata.get("snapshot_id"),
        "backup_metadata_sha256": _sha256_file(
            backup_dir / "backup_metadata.json"
        ),
    }
    plan_digest = _sha256_bytes(
        _canonical_json_bytes(plan_identity, pretty=False)
    )
    run_id = str(metadata.get("run_id") or "unknown")
    root = graph / "maintenance" / "legacy_error_migration"
    path = root / "journals" / f"rollback-{run_id}-{plan_digest[:12]}.json"
    suffix = plan_digest[:12]
    journal = {
        "schema_version": JOURNAL_VERSION,
        "operation": "rollback",
        "run_id": run_id,
        "graph_dir": str(graph),
        "backup": str(backup_dir),
        "plan_hash": plan_digest,
        "started_at": _utc_now(),
        "updated_at": _utc_now(),
        "phase": "backup_validated",
        "checkpoints": {
            "restore_nodes_prepared": False,
            "nodes_swapped": False,
            "edges_restored": False,
            "embeddings_restored": False,
            "snapshot_verified": False,
        },
        "restore_nodes": str(graph / f".legacy-error-restore-{suffix}"),
        "displaced_nodes": str(graph / f".legacy-error-displaced-{suffix}"),
    }
    return path, plan_digest, journal


def _mark_matching_apply_journals_rolled_back(
    graph: Path,
    backup_dir: Path,
) -> None:
    for path, journal in _incomplete_journals(graph):
        if journal.get("operation") != "apply":
            continue
        plan = _plan_from_journal(
            path,
            journal,
            require_current_version=False,
        )
        if Path(str(plan["paths"]["backup"])).resolve() != backup_dir:
            continue
        journal["rollback_journal"] = str(
            _rollback_journal_details(
                graph,
                backup_dir,
                _read_json(backup_dir / "backup_metadata.json"),
            )[0]
        )
        _set_journal_phase(path, journal, "rolled_back")


def rollback(
    graph_dir: Path | str,
    backup: Path | str,
    *,
    confirm_engine_stopped: bool = False,
    engine_endpoints: Sequence[str] | None = None,
    additional_engine_endpoints: Sequence[str] | None = None,
    engine_probe_timeout_sec: float = DEFAULT_ENGINE_PROBE_TIMEOUT_SEC,
) -> dict[str, Any]:
    """Restore the exact node/edge/cache snapshot from a migration backup."""

    graph = Path(graph_dir).resolve()
    backup_dir = Path(backup).resolve()
    quiescence = _require_engine_quiescence(
        graph_dir=graph,
        confirm_engine_stopped=confirm_engine_stopped,
        engine_endpoints=engine_endpoints,
        additional_engine_endpoints=additional_engine_endpoints,
        timeout_sec=engine_probe_timeout_sec,
    )
    if not graph.is_dir() or not (graph / "edges.json").is_file():
        raise MigrationError(f"graph directory is incomplete: {graph}")
    metadata_path = backup_dir / "backup_metadata.json"
    if not metadata_path.is_file():
        raise MigrationError(f"backup metadata is missing: {metadata_path}")
    metadata = _read_json(metadata_path)
    if not isinstance(metadata, dict) or metadata.get("schema_version") != BACKUP_VERSION:
        raise MigrationError(f"unsupported rollback backup: {backup_dir}")
    if metadata.get("complete") is not True:
        raise MigrationError(f"rollback backup is not complete: {backup_dir}")
    if Path(str(metadata.get("graph_dir") or "")).resolve() != graph:
        raise MigrationError("backup was created for a different graph directory")
    _validate_backup_files(backup_dir, metadata)
    backup_nodes = backup_dir / "nodes"
    backup_edges = backup_dir / "edges.json"
    journal_path, plan_digest, new_journal = _rollback_journal_details(
        graph,
        backup_dir,
        metadata,
    )
    resumed = False
    if journal_path.exists():
        existing = _load_journal(journal_path)
        if str(existing.get("phase") or "") not in TERMINAL_JOURNAL_PHASES:
            if existing.get("plan_hash") != plan_digest:
                raise MigrationError(
                    f"rollback journal plan hash mismatch: {journal_path}"
                )
            journal = existing
            resumed = True
        else:
            journal = new_journal
    else:
        journal = new_journal
    for other_path, other in _incomplete_journals(graph):
        if other_path == journal_path:
            continue
        if other.get("operation") == "rollback":
            raise MigrationError(
                "another rollback journal is incomplete; resume that exact "
                f"backup first: {other_path}"
            )
        if other.get("operation") == "apply":
            other_plan = _plan_from_journal(
                other_path,
                other,
                require_current_version=False,
            )
            if Path(str(other_plan["paths"]["backup"])).resolve() != backup_dir:
                raise MigrationError(
                    "an unrelated apply journal is incomplete; resume it or "
                    f"use its recorded rollback backup first: {other_path}"
                )
    if not resumed:
        _require_graph_layout(graph)

    with _mutation_lock(graph, operation="rollback", plan_hash=plan_digest):
        quiescence = _require_engine_quiescence(
            graph_dir=graph,
            confirm_engine_stopped=confirm_engine_stopped,
            engine_endpoints=engine_endpoints,
            additional_engine_endpoints=additional_engine_endpoints,
            timeout_sec=engine_probe_timeout_sec,
        )
        quiescence["checked_immediately_before_mutation"] = True
        _validate_backup_files(backup_dir, metadata)
        if not resumed:
            if journal_path.exists():
                history_dir = journal_path.parent / "history"
                history_dir.mkdir(parents=True, exist_ok=True)
                archived = history_dir / (
                    f"{journal_path.stem}-{uuid.uuid4().hex[:12]}.json"
                )
                os.replace(journal_path, archived)
            _save_journal(journal_path, journal)

        nodes_dir = graph / "nodes"
        restore_nodes = Path(str(journal["restore_nodes"])).resolve()
        displaced_nodes = Path(str(journal["displaced_nodes"])).resolve()
        if (
            restore_nodes.parent != graph
            or displaced_nodes.parent != graph
            or restore_nodes == displaced_nodes
        ):
            raise MigrationError("rollback journal contains unsafe temporary paths")

        checkpoints = journal.get("checkpoints")
        if not isinstance(checkpoints, dict):
            raise MigrationError(f"rollback journal has invalid checkpoints: {journal_path}")

        if not bool(checkpoints.get("nodes_swapped")):
            if restore_nodes.exists() and not _nodes_match_backup(
                restore_nodes,
                metadata,
            ):
                if nodes_dir.is_dir() and not displaced_nodes.exists():
                    if restore_nodes.is_dir():
                        shutil.rmtree(restore_nodes)
                    else:
                        restore_nodes.unlink()
                else:
                    raise MigrationError(
                        "rollback resume found an incomplete restore copy in "
                        "an unsafe node-directory state"
                    )
            if (
                nodes_dir.is_dir()
                and not restore_nodes.exists()
                and displaced_nodes.is_dir()
            ):
                if not _nodes_match_backup(nodes_dir, metadata):
                    raise MigrationError(
                        "rollback resume found an ambiguous displaced node store"
                    )
            else:
                if not restore_nodes.exists():
                    if not nodes_dir.is_dir() or displaced_nodes.exists():
                        raise MigrationError(
                            "rollback resume found an unsafe node-directory state"
                        )
                    shutil.copytree(backup_nodes, restore_nodes)
                    if not _nodes_match_backup(restore_nodes, metadata):
                        raise MigrationError(
                            "rollback restore copy does not match the backup"
                        )
                    _set_journal_phase(
                        journal_path,
                        journal,
                        "restore_nodes_prepared",
                        checkpoint="restore_nodes_prepared",
                    )
                if not nodes_dir.exists():
                    if not restore_nodes.is_dir() or not displaced_nodes.is_dir():
                        raise MigrationError(
                            "rollback lost the active node directory without both recovery copies"
                        )
                    os.replace(restore_nodes, nodes_dir)
                elif restore_nodes.is_dir() and not displaced_nodes.exists():
                    os.replace(nodes_dir, displaced_nodes)
                    os.replace(restore_nodes, nodes_dir)
                elif restore_nodes.exists() and displaced_nodes.exists():
                    raise MigrationError(
                        "rollback has both restore and displaced directories; "
                        "manual inspection is required"
                    )
            if not _nodes_match_backup(nodes_dir, metadata):
                raise MigrationError("rollback node swap does not match the backup")
            _set_journal_phase(
                journal_path,
                journal,
                "nodes_swapped",
                checkpoint="nodes_swapped",
            )
        elif not _nodes_match_backup(nodes_dir, metadata):
            raise MigrationError(
                "rollback journal says nodes were swapped but the backup does not match"
            )

        _atomic_write_bytes(graph / "edges.json", backup_edges.read_bytes())
        _set_journal_phase(
            journal_path,
            journal,
            "edges_restored",
            checkpoint="edges_restored",
        )
        backup_embeddings = backup_dir / "embeddings.npz"
        if bool(metadata.get("embeddings_present")):
            if not backup_embeddings.is_file():
                raise MigrationError("backup metadata expects embeddings.npz but the file is missing")
            _atomic_write_bytes(graph / "embeddings.npz", backup_embeddings.read_bytes())
        else:
            (graph / "embeddings.npz").unlink(missing_ok=True)

        marker = graph / "embeddings.rebuild_required.json"
        backup_marker = backup_dir / "embeddings.rebuild_required.json"
        if bool(metadata.get("embedding_marker_present")):
            if not backup_marker.is_file():
                raise MigrationError("backup metadata expects an embedding marker but the file is missing")
            _atomic_write_bytes(marker, backup_marker.read_bytes())
        else:
            marker.unlink(missing_ok=True)
        _set_journal_phase(
            journal_path,
            journal,
            "embeddings_restored",
            checkpoint="embeddings_restored",
        )

        restored_snapshot = _snapshot_id(graph)
        if restored_snapshot != metadata.get("snapshot_id"):
            raise MigrationError(
                "rollback files were restored but verification failed: "
                f"expected {metadata.get('snapshot_id')}, got {restored_snapshot}"
            )
        _set_journal_phase(
            journal_path,
            journal,
            "snapshot_verified",
            checkpoint="snapshot_verified",
        )
        if restore_nodes.exists():
            shutil.rmtree(restore_nodes)
        if displaced_nodes.exists():
            shutil.rmtree(displaced_nodes)
        _mark_matching_apply_journals_rolled_back(graph, backup_dir)
        _set_journal_phase(journal_path, journal, "completed")

    return {
        "schema_version": MIGRATION_VERSION,
        "mode": "rollback",
        "restored": True,
        "graph_dir": str(graph),
        "backup": str(backup_dir),
        "snapshot_id": metadata["snapshot_id"],
        "engine_quiescence": quiescence,
        "resumed_from_journal": resumed,
        "journal_path": str(journal_path),
        "journal_phase": "completed",
        "note": "Reload the 3CAN engine before serving new routes.",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Safely plan/apply/rollback the legacy ERR-repeated graph migration."
    )
    parser.add_argument("--graph-dir", required=True, type=Path, help="3CAN graph directory")
    parser.add_argument("--apply", action="store_true", help="apply the deterministic migration plan")
    parser.add_argument(
        "--confirm-engine-stopped",
        action="store_true",
        help="confirm every 3CAN engine using this graph has been stopped",
    )
    parser.add_argument(
        "--engine-endpoint",
        action="append",
        default=None,
        metavar="URL",
        help=(
            "complete http(s) engine endpoint to add; repeat as needed. "
            "The default 127.0.0.1:9700/9701/9702/9711 probes remain active."
        ),
    )
    parser.add_argument(
        "--additional-engine-endpoint",
        action="append",
        default=None,
        metavar="URL",
        help="complete http(s) engine endpoint to append to the selected probe set",
    )
    parser.add_argument(
        "--engine-probe-timeout-sec",
        type=float,
        default=DEFAULT_ENGINE_PROBE_TIMEOUT_SEC,
        help="short per-probe timeout, clamped to 0.05-2.0 seconds",
    )
    parser.add_argument(
        "--rollback",
        type=Path,
        help="restore a backup directory created by a previous apply",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.rollback is not None:
            if args.apply:
                raise MigrationError("--apply and --rollback are mutually exclusive")
            result = rollback(
                args.graph_dir,
                args.rollback,
                confirm_engine_stopped=args.confirm_engine_stopped,
                engine_endpoints=args.engine_endpoint,
                additional_engine_endpoints=args.additional_engine_endpoint,
                engine_probe_timeout_sec=args.engine_probe_timeout_sec,
            )
        else:
            result = migrate(
                args.graph_dir,
                apply=args.apply,
                confirm_engine_stopped=args.confirm_engine_stopped,
                engine_endpoints=args.engine_endpoint,
                additional_engine_endpoints=args.additional_engine_endpoint,
                engine_probe_timeout_sec=args.engine_probe_timeout_sec,
            )
    except MigrationError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
