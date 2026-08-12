"""Process-safe, crash-safe ownership for a writable 3CAN graph directory.

The runtime and offline maintenance commands are both graph writers.  They
share one non-blocking OS advisory lock so an engine cannot start during a
migration and a migration cannot begin while an engine owns the graph.  The
kernel releases the lock when a process exits, so stale PID files are not an
authority boundary.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import socket
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO


LOCK_DIRECTORY = ".3can-locks"
LOCK_SCHEMA_VERSION = "3can.graph-runtime-lock/v1"
CUTOVER_SENTINEL_FILENAME = ".3can-cutover.json"
CUTOVER_SENTINEL_SCHEMA = "3can.cutover-maintenance/v1"
CUTOVER_SENTINEL_ENV = "THREECAN_CUTOVER_SENTINEL"
CUTOVER_RUN_ID_ENV = "THREECAN_CUTOVER_RUN_ID"
ENGINE_ROOT_ENV = "THREECAN_ENGINE_ROOT"
MAX_CUTOVER_SENTINEL_BYTES = 64 * 1024
CUTOVER_CONTROLLER_OWNER_KIND = "3can-cutover-controller"


class GraphRuntimeLockError(RuntimeError):
    """Raised when another process or owner already owns a graph."""


@dataclass
class _HeldLock:
    handle: BinaryIO
    owner_kind: str
    lock_id: str
    refs: int = 1


_HELD_LOCKS: dict[str, _HeldLock] = {}
_HELD_LOCKS_GUARD = threading.RLock()


def _key(graph_dir: Path) -> str:
    return os.path.normcase(str(graph_dir.expanduser().resolve()))


def _lock_path(graph_dir: Path) -> Path:
    graph_key = _key(graph_dir)
    digest = hashlib.sha256(graph_key.encode("utf-8")).hexdigest()
    return graph_dir.parent / LOCK_DIRECTORY / f"{digest}.lock"


def _lock_region(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_region(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _best_effort_owner(path: Path) -> str:
    try:
        if path.stat().st_size > 16 * 1024:
            return "owner metadata exceeds 16 KiB"
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return "owner metadata is not an object"
        return (
            f"kind={payload.get('owner_kind', 'unknown')} "
            f"pid={payload.get('pid', 'unknown')} "
            f"host={payload.get('host', 'unknown')} "
            f"acquired_at={payload.get('acquired_at', 'unknown')}"
        )
    except Exception:
        return "owner metadata unavailable"


def _cutover_error(reason: str) -> GraphRuntimeLockError:
    return GraphRuntimeLockError(f"graph_runtime_cutover_sentinel_invalid:{reason}")


def _engine_root() -> Path:
    configured = os.environ.get(ENGINE_ROOT_ENV, "").strip()
    if not configured:
        return Path(__file__).resolve(strict=False).parents[1]

    try:
        candidate = Path(configured).expanduser()
        if not candidate.is_absolute():
            raise _cutover_error("engine_root_not_absolute")
        return candidate.resolve(strict=False)
    except GraphRuntimeLockError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise _cutover_error("engine_root_unresolvable") from exc


def _configured_cutover_sentinels() -> list[tuple[str, Path]]:
    try:
        default_path = (_engine_root() / CUTOVER_SENTINEL_FILENAME).resolve(
            strict=False
        )
    except GraphRuntimeLockError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise _cutover_error("default_path_unresolvable") from exc

    paths = [("default", default_path)]
    configured = os.environ.get(CUTOVER_SENTINEL_ENV, "").strip()
    if not configured:
        return paths

    try:
        override = Path(configured).expanduser()
        if not override.is_absolute():
            raise _cutover_error("override_not_absolute")
        resolved = override.resolve(strict=False)
    except GraphRuntimeLockError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise _cutover_error("override_unresolvable") from exc

    if resolved != default_path:
        paths.append(("override", resolved))
    return paths


def _strict_json_object_pairs(pairs):
    payload = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError("duplicate_json_key")
        payload[key] = value
    return payload


def _reject_json_constant(_value: str):
    raise ValueError("nonstandard_json_constant")


def _read_active_cutover_run_id(label: str, path: Path) -> str | None:
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _cutover_error(f"{label}_probe_failed") from exc

    try:
        with path.open("rb") as handle:
            encoded = handle.read(MAX_CUTOVER_SENTINEL_BYTES + 1)
    except OSError as exc:
        raise _cutover_error(f"{label}_read_failed") from exc

    if len(encoded) > MAX_CUTOVER_SENTINEL_BYTES:
        raise _cutover_error(f"{label}_too_large")
    try:
        text = encoded.decode("utf-8", errors="strict")
        payload = json.loads(
            text,
            object_pairs_hook=_strict_json_object_pairs,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise _cutover_error(f"{label}_invalid_json") from exc

    if not isinstance(payload, dict):
        raise _cutover_error(f"{label}_not_object")
    if payload.get("schema") != CUTOVER_SENTINEL_SCHEMA:
        raise _cutover_error(f"{label}_schema_mismatch")
    run_id = payload.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise _cutover_error(f"{label}_run_id_invalid")
    if payload.get("active") is not True:
        raise _cutover_error(f"{label}_not_active")
    return run_id


def _guard_against_active_cutover(*, owner_kind: str) -> str | None:
    active_run_ids = []
    for label, path in _configured_cutover_sentinels():
        run_id = _read_active_cutover_run_id(label, path)
        if run_id is not None:
            active_run_ids.append(run_id)

    if not active_run_ids:
        return None
    if len(set(active_run_ids)) != 1:
        raise GraphRuntimeLockError("graph_runtime_cutover_sentinel_conflict")
    if (
        owner_kind != CUTOVER_CONTROLLER_OWNER_KIND
        or os.environ.get(CUTOVER_RUN_ID_ENV) != active_run_ids[0]
    ):
        raise GraphRuntimeLockError("graph_runtime_cutover_active")
    return active_run_ids[0]


def _require_cutover_authority(
    *,
    owner_kind: str,
    require_active_cutover: bool,
    expected_cutover_run_id: str | None,
) -> None:
    active_run_id = _guard_against_active_cutover(owner_kind=owner_kind)
    if require_active_cutover and active_run_id is None:
        raise GraphRuntimeLockError("graph_runtime_cutover_sentinel_required")
    if (
        expected_cutover_run_id is not None
        and active_run_id != expected_cutover_run_id
    ):
        raise GraphRuntimeLockError("graph_runtime_cutover_run_id_mismatch")


class GraphRuntimeLease:
    """A reference-counted lease for one in-process graph owner."""

    def __init__(self, graph_key: str, lock_id: str) -> None:
        self._graph_key = graph_key
        self._lock_id = lock_id
        self._released = False

    @property
    def active(self) -> bool:
        with _HELD_LOCKS_GUARD:
            held = _HELD_LOCKS.get(self._graph_key)
            return (
                not self._released
                and held is not None
                and held.lock_id == self._lock_id
            )

    def release(self) -> None:
        if self._released:
            return
        with _HELD_LOCKS_GUARD:
            held = _HELD_LOCKS.get(self._graph_key)
            if held is None or held.lock_id != self._lock_id:
                self._released = True
                return
            held.refs -= 1
            if held.refs <= 0:
                try:
                    _unlock_region(held.handle)
                finally:
                    held.handle.close()
                    _HELD_LOCKS.pop(self._graph_key, None)
            self._released = True

    def __enter__(self) -> "GraphRuntimeLease":
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.release()


def acquire_graph_runtime_lock(
    graph_dir: Path,
    *,
    owner_kind: str,
    create_graph_dir: bool = True,
    require_active_cutover: bool = False,
    expected_cutover_run_id: str | None = None,
) -> GraphRuntimeLease:
    """Acquire exclusive graph ownership without waiting.

    ``create_graph_dir=False`` is reserved for directory-transaction
    controllers that must keep ownership while the live path is temporarily
    absent between two sibling renames.  Requiring an active cutover also
    verifies the exact sentinel run id before and after taking the OS lock.
    """

    normalized_owner = str(owner_kind or "").strip()
    if not normalized_owner:
        raise ValueError("graph_runtime_owner_kind_required")
    normalized_run_id = (
        str(expected_cutover_run_id).strip()
        if expected_cutover_run_id is not None
        else None
    )
    if expected_cutover_run_id is not None and not normalized_run_id:
        raise ValueError("graph_runtime_expected_cutover_run_id_required")
    require_cutover = require_active_cutover or normalized_run_id is not None

    graph = graph_dir.expanduser().resolve()
    graph_key = _key(graph)
    path = _lock_path(graph)

    # The sentinel is a fast fail-closed gate.  The OS lock remains the actual
    # serialization boundary, so it is acquired before the graph path can be
    # created and the sentinel is checked again while that boundary is held.
    # This prevents a losing startup from recreating the live graph directory
    # during the gap between the two directory renames of a cutover.
    _require_cutover_authority(
        owner_kind=normalized_owner,
        require_active_cutover=require_cutover,
        expected_cutover_run_id=normalized_run_id,
    )

    with _HELD_LOCKS_GUARD:
        existing = _HELD_LOCKS.get(graph_key)
        if existing is not None:
            if existing.owner_kind != normalized_owner:
                raise GraphRuntimeLockError(
                    "graph_runtime_lock_owned_by_different_in_process_owner:"
                    f"{existing.owner_kind}"
                )
            existing.refs += 1
            return GraphRuntimeLease(graph_key, existing.lock_id)

        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, "wb") as initializer:
                initializer.write(b"\0")
                initializer.flush()
                os.fsync(initializer.fileno())

        handle = path.open("r+b", buffering=0)
        try:
            _lock_region(handle)
        except (OSError, BlockingIOError) as exc:
            handle.close()
            raise GraphRuntimeLockError(
                f"graph_runtime_lock_busy:{_best_effort_owner(path)}"
            ) from exc

        try:
            _require_cutover_authority(
                owner_kind=normalized_owner,
                require_active_cutover=require_cutover,
                expected_cutover_run_id=normalized_run_id,
            )
            if create_graph_dir:
                graph.mkdir(parents=True, exist_ok=True)
            elif graph.exists() and not graph.is_dir():
                raise GraphRuntimeLockError("graph_runtime_path_not_directory")

            lock_id = uuid.uuid4().hex
            metadata = {
                "schema_version": LOCK_SCHEMA_VERSION,
                "lock_id": lock_id,
                "owner_kind": normalized_owner,
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "acquired_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "graph_root_sha256": hashlib.sha256(
                    str(graph).encode("utf-8")
                ).hexdigest(),
            }
            encoded = json.dumps(
                metadata,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            handle.seek(0)
            handle.truncate(0)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        except Exception:
            try:
                _unlock_region(handle)
            finally:
                handle.close()
            raise

        _HELD_LOCKS[graph_key] = _HeldLock(
            handle=handle,
            owner_kind=normalized_owner,
            lock_id=lock_id,
        )
        return GraphRuntimeLease(graph_key, lock_id)
