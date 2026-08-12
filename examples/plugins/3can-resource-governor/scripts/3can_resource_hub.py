#!/usr/bin/env python3
"""Small, optional resource lease hub for local Codex development.

The hub coordinates conflicts; it never limits the total number of agents and
never invokes Docker cleanup commands. Lifecycle hooks mark owner leases as
cleanup-pending and emit a manifest. Only the task harness may release a lease
after it has verified the owner-scoped resource cleanup.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


PROFILES = {"performance", "constrained"}
RESOURCE_KINDS = {
    "docker-build",
    "compose-project",
    "port",
    "3can-writer",
}
DOCKER_RESOURCE_KINDS = {"docker-build", "compose-project"}
RESOURCE_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{1,199}$")
SENSITIVE_KEY_RE = re.compile(
    r"(?:password|passwd|secret|token|cookie|credential|private[_-]?key)",
    re.IGNORECASE,
)
SCHEMA_VERSION = "3can.resource-hub/v1"
SESSION_AUDIT_SCHEMA_VERSION = "3can.codex-session-audit/v1"


class ResourceHubError(RuntimeError):
    """Typed user-facing resource hub error."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _safe_json_object(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise ResourceHubError(f"metadata_json_invalid: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise ResourceHubError("metadata_json_must_be_an_object")

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if SENSITIVE_KEY_RE.search(str(key)):
                    raise ResourceHubError(
                        f"metadata_sensitive_key_rejected: {key}"
                    )
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return value


def _state_dir(explicit: str | None = None) -> Path:
    configured = (
        explicit
        or os.environ.get("THREECAN_RESOURCE_HUB_DIR")
    )
    if configured:
        return Path(configured).expanduser().resolve()
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return (
            Path(local_app_data) / "3can" / "resource-governor"
        ).resolve()
    xdg_state = os.environ.get("XDG_STATE_HOME")
    if xdg_state:
        return (
            Path(xdg_state) / "3can" / "resource-governor"
        ).expanduser().resolve()
    return (
        Path.home() / ".local" / "state" / "3can" / "resource-governor"
    ).resolve()


def _project_key(
    explicit: str | None = None,
    *,
    cwd: str | None = None,
) -> str:
    configured = explicit or os.environ.get("THREECAN_PROJECT_ID")
    if configured:
        return str(configured).strip()[:160]
    normalized = str(Path(cwd or Path.cwd()).resolve()).casefold()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]
    return f"project:{digest}"


def _session_key(
    explicit: str | None = None,
    *,
    payload: dict[str, Any] | None = None,
) -> str:
    payload = payload or {}
    value = (
        explicit
        or payload.get("session_id")
        or payload.get("thread_id")
        or os.environ.get("CODEX_THREAD_ID")
        or os.environ.get("CODEX_SESSION_ID")
    )
    if value:
        return str(value).strip()[:200]
    fallback = (
        f"{Path.cwd().resolve()}:{os.getppid()}:"
        f"{os.environ.get('THREECAN_WORKORDER_ID', 'unscoped')}"
    )
    return "fallback:" + hashlib.sha256(
        fallback.encode("utf-8")
    ).hexdigest()[:24]


def _actor_id(
    explicit: str | None = None,
    *,
    payload: dict[str, Any] | None = None,
    required: bool = False,
) -> str:
    payload = payload or {}
    value = (
        explicit
        or payload.get("agent_id")
        or os.environ.get("THREECAN_AGENT_ID")
        or os.environ.get("CODEX_AGENT_ID")
    )
    if not value and required:
        raise ResourceHubError(
            "actor_id_required: set THREECAN_AGENT_ID or pass --actor-id"
        )
    value = value or "main"
    return str(value).strip()[:160]


def _profile(explicit: str | None = None) -> str:
    value = (
        explicit
        or os.environ.get("THREECAN_RESOURCE_PROFILE")
        or "performance"
    ).strip().casefold()
    if value not in PROFILES:
        raise ResourceHubError(
            "profile_must_be_performance_or_constrained"
        )
    return value


def _resource_kind(resource_key: str) -> str:
    kind, separator, identifier = resource_key.partition(":")
    return (
        kind
        if separator and identifier.strip() and kind in RESOURCE_KINDS
        else "unmanaged"
    )


def _normalize_path_text(raw_path: str) -> dict[str, Any]:
    """Normalize Windows long-path variants without changing the source."""

    raw = str(raw_path or "").strip()
    if not raw:
        return {
            "raw": "",
            "normalized": "",
            "comparison_key": "",
            "had_long_path_prefix": False,
        }
    de_prefixed = raw
    long_path_prefix = False
    if de_prefixed.startswith("\\\\?\\UNC\\"):
        de_prefixed = "\\\\" + de_prefixed[8:]
        long_path_prefix = True
    elif de_prefixed.startswith("\\\\?\\"):
        de_prefixed = de_prefixed[4:]
        long_path_prefix = True
    elif de_prefixed.startswith("//?/UNC/"):
        de_prefixed = "//" + de_prefixed[8:]
        long_path_prefix = True
    elif de_prefixed.startswith("//?/"):
        de_prefixed = de_prefixed[4:]
        long_path_prefix = True

    path = Path(de_prefixed).expanduser()
    try:
        normalized = path.resolve(strict=False)
    except OSError:
        normalized = Path(os.path.abspath(os.path.normpath(str(path))))
    normalized_text = str(normalized)
    comparison_key = os.path.normcase(
        os.path.normpath(normalized_text)
    ).replace("\\", "/").casefold()
    return {
        "raw": raw,
        "normalized": normalized_text,
        "comparison_key": comparison_key,
        "had_long_path_prefix": long_path_prefix,
    }


def _connect(state_dir: Path) -> sqlite3.Connection:
    state_dir.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        state_dir / "resource_hub.sqlite3",
        timeout=2.0,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA busy_timeout=2000")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS leases (
            lease_id TEXT PRIMARY KEY,
            project_key TEXT NOT NULL,
            resource_key TEXT NOT NULL,
            session_key TEXT NOT NULL,
            actor_id TEXT NOT NULL,
            workorder_id TEXT NOT NULL,
            profile TEXT NOT NULL,
            state TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            acquired_at TEXT NOT NULL,
            heartbeat_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            released_at TEXT,
            release_reason TEXT
        );
        DROP INDEX IF EXISTS ux_active_resource_lease;
        CREATE UNIQUE INDEX IF NOT EXISTS ux_blocking_resource_lease
        ON leases(resource_key)
        WHERE state IN ('active', 'cleanup_pending');
        CREATE INDEX IF NOT EXISTS ix_lease_owner
        ON leases(project_key, session_key, actor_id, state);
        """
    )
    return connection


def _begin(connection: sqlite3.Connection) -> None:
    connection.execute("BEGIN IMMEDIATE")


def _reap_expired(
    connection: sqlite3.Connection,
    *,
    now: datetime,
) -> int:
    """Keep stale resources blocked until their cleanup is verified."""

    cursor = connection.execute(
        """
        UPDATE leases
        SET state = 'cleanup_pending',
            release_reason = 'ttl_expired_cleanup_required'
        WHERE state = 'active' AND expires_at <= ?
        """,
        (_iso(now),),
    )
    return int(cursor.rowcount or 0)


def _row_public(row: sqlite3.Row) -> dict[str, Any]:
    value = {
        "lease_id": row["lease_id"],
        "project_key": row["project_key"],
        "resource_key": row["resource_key"],
        "session_key": row["session_key"],
        "actor_id": row["actor_id"],
        "workorder_id": row["workorder_id"],
        "profile": row["profile"],
        "state": row["state"],
        "metadata": json.loads(row["metadata_json"] or "{}"),
        "acquired_at": row["acquired_at"],
        "heartbeat_at": row["heartbeat_at"],
        "expires_at": row["expires_at"],
        "released_at": row["released_at"],
        "release_reason": row["release_reason"],
    }
    kind = _resource_kind(row["resource_key"])
    value["resource_kind"] = kind
    if kind in DOCKER_RESOURCE_KINDS:
        identifier = row["resource_key"].split(":", 1)[1]
        value["docker_ownership"] = {
            "kind": kind,
            "identifier": identifier,
            "project_key": row["project_key"],
            "session_key": row["session_key"],
            "actor_id": row["actor_id"],
            "workorder_id": row["workorder_id"],
            "cleanup_verified": row["state"] == "released",
            "hook_execution_allowed": False,
        }
    return value


def acquire(args: argparse.Namespace) -> int:
    resource_key = str(args.resource_key or "").strip()
    if not RESOURCE_KEY_RE.fullmatch(resource_key):
        raise ResourceHubError("resource_key_invalid")
    resource_kind = _resource_kind(resource_key)
    if resource_kind == "unmanaged":
        raise ResourceHubError(
            "resource_kind_not_leasable: use docker-build, "
            "compose-project, port, or 3can-writer"
        )
    ttl_sec = max(30, min(int(args.ttl_sec), 86_400))
    metadata = _safe_json_object(args.metadata_json)
    state_dir = _state_dir(args.state_dir)
    project_key = _project_key(args.project_key)
    session_key = _session_key(args.session_key)
    actor_id = _actor_id(args.actor_id, required=True)
    profile = _profile(args.profile)
    workorder_id = str(
        args.workorder_id
        or os.environ.get("THREECAN_WORKORDER_ID")
        or "unscoped"
    )[:200]
    now = _now()
    expires_at = now + timedelta(seconds=ttl_sec)

    connection = _connect(state_dir)
    try:
        _begin(connection)
        _reap_expired(connection, now=now)
        existing = connection.execute(
            """
            SELECT * FROM leases
            WHERE resource_key = ?
              AND state IN ('active', 'cleanup_pending')
            """,
            (resource_key,),
        ).fetchone()
        if existing:
            if (
                existing["state"] == "active"
                and existing["project_key"] == project_key
                and existing["session_key"] == session_key
                and existing["actor_id"] == actor_id
            ):
                connection.execute(
                    """
                    UPDATE leases
                    SET heartbeat_at = ?, expires_at = ?, metadata_json = ?
                    WHERE lease_id = ?
                    """,
                    (
                        _iso(now),
                        _iso(expires_at),
                        json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                        existing["lease_id"],
                    ),
                )
                connection.execute("COMMIT")
                _print_json({
                    "schema_version": SCHEMA_VERSION,
                    "status": "RENEWED",
                    "profile": profile,
                    "lease_id": existing["lease_id"],
                    "resource_key": resource_key,
                    "resource_kind": resource_kind,
                    "expires_at": _iso(expires_at),
                })
                return 0
            connection.execute("COMMIT")
            status = "ADVISORY" if profile == "performance" else "BLOCKED"
            _print_json({
                "schema_version": SCHEMA_VERSION,
                "status": status,
                "profile": profile,
                "reason": (
                    "resource_cleanup_pending"
                    if existing["state"] == "cleanup_pending"
                    else "resource_already_leased"
                ),
                "resource_key": resource_key,
                "resource_kind": resource_kind,
                "owner": {
                    "project_key": existing["project_key"],
                    "session_key": existing["session_key"],
                    "actor_id": existing["actor_id"],
                    "workorder_id": existing["workorder_id"],
                    "state": existing["state"],
                    "expires_at": existing["expires_at"],
                },
            })
            return 0 if profile == "performance" else 3

        lease_id = "lease_" + uuid.uuid4().hex
        connection.execute(
            """
            INSERT INTO leases (
                lease_id, project_key, resource_key, session_key, actor_id,
                workorder_id, profile, state, metadata_json, acquired_at,
                heartbeat_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)
            """,
            (
                lease_id,
                project_key,
                resource_key,
                session_key,
                actor_id,
                workorder_id,
                profile,
                json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                _iso(now),
                _iso(now),
                _iso(expires_at),
            ),
        )
        connection.execute("COMMIT")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()

    _print_json({
        "schema_version": SCHEMA_VERSION,
        "status": "ACQUIRED",
        "profile": profile,
        "lease_id": lease_id,
        "resource_key": resource_key,
        "resource_kind": resource_kind,
        "expires_at": _iso(expires_at),
    })
    return 0


def heartbeat(args: argparse.Namespace) -> int:
    state_dir = _state_dir(args.state_dir)
    project_key = _project_key(args.project_key)
    session_key = _session_key(args.session_key)
    actor_id = _actor_id(args.actor_id, required=True)
    ttl_sec = max(30, min(int(args.ttl_sec), 86_400))
    now = _now()
    connection = _connect(state_dir)
    try:
        _begin(connection)
        _reap_expired(connection, now=now)
        cursor = connection.execute(
            """
            UPDATE leases SET heartbeat_at = ?, expires_at = ?
            WHERE lease_id = ? AND project_key = ? AND session_key = ?
              AND actor_id = ? AND state = 'active'
            """,
            (
                _iso(now),
                _iso(now + timedelta(seconds=ttl_sec)),
                args.lease_id,
                project_key,
                session_key,
                actor_id,
            ),
        )
        connection.execute("COMMIT")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()
    found = bool(cursor.rowcount)
    _print_json({
        "schema_version": SCHEMA_VERSION,
        "status": "HEARTBEAT" if found else "NOT_FOUND",
        "lease_id": args.lease_id,
    })
    return 0 if found else 4


def release(args: argparse.Namespace) -> int:
    if not args.cleanup_verified:
        raise ResourceHubError(
            "cleanup_verification_required: pass --cleanup-verified only "
            "after the owner-scoped resource is stopped or removed"
        )
    state_dir = _state_dir(args.state_dir)
    project_key = _project_key(args.project_key)
    session_key = _session_key(args.session_key)
    actor_id = _actor_id(args.actor_id, required=True)
    now = _now()
    connection = _connect(state_dir)
    try:
        _begin(connection)
        cursor = connection.execute(
            """
            UPDATE leases
            SET state = 'released', released_at = ?, release_reason = ?
            WHERE lease_id = ? AND project_key = ? AND session_key = ?
              AND actor_id = ?
              AND state IN ('active', 'cleanup_pending')
            """,
            (
                _iso(now),
                str(args.reason or "owner_release")[:120],
                args.lease_id,
                project_key,
                session_key,
                actor_id,
            ),
        )
        connection.execute("COMMIT")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()
    found = bool(cursor.rowcount)
    _print_json({
        "schema_version": SCHEMA_VERSION,
        "status": "RELEASED" if found else "NOT_FOUND",
        "lease_id": args.lease_id,
    })
    return 0 if found else 4


def _write_cleanup_manifest(
    state_dir: Path,
    *,
    project_key: str,
    session_key: str,
    actor_id: str | None,
    event_name: str,
    cleanup_pending: list[dict[str, Any]],
) -> Path:
    manifest_dir = state_dir / "cleanup-manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    owner_digest = hashlib.sha256(
        f"{project_key}:{session_key}:{actor_id or '*'}".encode("utf-8")
    ).hexdigest()[:16]
    stamp = _now().strftime("%Y%m%dT%H%M%S%fZ")
    path = manifest_dir / f"{stamp}_{owner_digest}.json"
    docker_cleanup_candidates = []
    for lease in cleanup_pending:
        ownership = lease.get("docker_ownership")
        if not ownership:
            continue
        docker_cleanup_candidates.append({
            "lease_id": lease["lease_id"],
            "resource_key": lease["resource_key"],
            "ownership": ownership,
            "cleanup_intent": lease.get("metadata", {}).get(
                "cleanup_intent"
            ),
            "candidate_only": True,
            "requires_owner_scoped_verification": True,
        })

    payload = {
        "schema_version": "3can.cleanup-manifest/v1",
        "created_at": _iso(_now()),
        "event_name": event_name,
        "project_key": project_key,
        "session_key": session_key,
        "actor_id": actor_id,
        "cleanup_pending_leases": cleanup_pending,
        "docker_cleanup_candidates": docker_cleanup_candidates,
        "actions_executed": [],
        "policy": {
            "owner_scoped": True,
            "docker_commands_executed": False,
            "codex_session_files_deleted": False,
            "manual_or_harness_cleanup_required": True,
            "hook_must_not_run_docker": True,
            "docker_system_prune_prohibited": True,
            "docker_volume_prune_prohibited": True,
            "candidate_is_not_cleanup_authorization": True,
        },
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def _finish_owner(
    *,
    state_dir: Path,
    project_key: str,
    session_key: str,
    actor_id: str | None,
    event_name: str,
    reason: str,
) -> dict[str, Any]:
    now = _now()
    connection = _connect(state_dir)
    try:
        _begin(connection)
        _reap_expired(connection, now=now)
        query = (
            "SELECT * FROM leases "
            "WHERE project_key = ? AND session_key = ? "
            "AND state IN ('active', 'cleanup_pending')"
        )
        params: list[Any] = [project_key, session_key]
        if actor_id:
            query += " AND actor_id = ?"
            params.append(actor_id)
        rows = connection.execute(query, params).fetchall()
        lease_ids = [row["lease_id"] for row in rows]
        for lease_id in lease_ids:
            connection.execute(
                """
                UPDATE leases
                SET state = 'cleanup_pending', release_reason = ?
                WHERE lease_id = ? AND state = 'active'
                """,
                (f"cleanup_pending:{reason}"[:120], lease_id),
            )
        connection.execute("COMMIT")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()

    cleanup_pending = []
    for row in rows:
        pending_item = _row_public(row)
        pending_item.update({
            "state": "cleanup_pending",
            "released_at": None,
            "release_reason": f"cleanup_pending:{reason}"[:120],
        })
        cleanup_pending.append(pending_item)
    manifest = _write_cleanup_manifest(
        state_dir,
        project_key=project_key,
        session_key=session_key,
        actor_id=actor_id,
        event_name=event_name,
        cleanup_pending=cleanup_pending,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "CLEANUP_PENDING",
        "cleanup_pending_count": len(cleanup_pending),
        "cleanup_resource_keys": [
            row["resource_key"] for row in cleanup_pending
        ],
        "cleanup_manifest": str(manifest),
    }


def finish(args: argparse.Namespace) -> int:
    scope = str(args.scope or "session")
    actor_id = _actor_id(args.actor_id) if scope == "actor" else None
    result = _finish_owner(
        state_dir=_state_dir(args.state_dir),
        project_key=_project_key(args.project_key),
        session_key=_session_key(args.session_key),
        actor_id=actor_id,
        event_name=str(args.event_name or "manual_finish"),
        reason=str(args.reason or "owner_finish"),
    )
    _print_json(result)
    return 0


def status(args: argparse.Namespace) -> int:
    state_dir = _state_dir(args.state_dir)
    project_key = _project_key(args.project_key)
    now = _now()
    connection = _connect(state_dir)
    try:
        _begin(connection)
        expired = _reap_expired(connection, now=now)
        rows = connection.execute(
            """
            SELECT * FROM leases
            WHERE project_key = ?
              AND state IN ('active', 'cleanup_pending')
            ORDER BY resource_key, acquired_at
            """,
            (project_key,),
        ).fetchall()
        connection.execute("COMMIT")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()
    _print_json({
        "schema_version": SCHEMA_VERSION,
        "status": "OK",
        "profile": _profile(args.profile),
        "project_key": project_key,
        "blocking_count": len(rows),
        "expired_marked_cleanup_pending": expired,
        "leases": [_row_public(row) for row in rows],
    })
    return 0


def _readonly_codex_state(state_db: Path) -> sqlite3.Connection:
    if not state_db.is_file():
        raise ResourceHubError("codex_state_db_not_found")
    connection = sqlite3.connect(
        state_db.resolve().as_uri() + "?mode=ro",
        uri=True,
        timeout=2.0,
    )
    connection.row_factory = sqlite3.Row
    table_names = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    required = {"threads", "thread_spawn_edges"}
    if not required.issubset(table_names):
        connection.close()
        raise ResourceHubError(
            "codex_state_schema_missing_threads_or_spawn_edges"
        )
    return connection


def _path_size(path_info: dict[str, Any]) -> int | None:
    if not path_info["normalized"]:
        return None
    try:
        return Path(path_info["normalized"]).stat().st_size
    except OSError:
        return None


def _write_session_audit(
    state_dir: Path,
    *,
    report: dict[str, Any],
    state_db: Path,
) -> Path:
    audit_dir = state_dir / "session-audits"
    audit_dir.mkdir(parents=True, exist_ok=True)
    source_digest = hashlib.sha256(
        str(state_db.resolve()).casefold().encode("utf-8")
    ).hexdigest()[:16]
    stamp = _now().strftime("%Y%m%dT%H%M%S%fZ")
    path = audit_dir / f"{stamp}_{source_digest}.json"
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    return path


def audit_sessions(args: argparse.Namespace) -> int:
    """Read Codex state and emit review candidates without mutating it."""

    state_db = Path(args.state_db).expanduser()
    large_bytes = max(1, int(args.large_rollout_bytes))
    max_candidates = max(1, min(int(args.max_candidates), 10_000))
    connection = _readonly_codex_state(state_db)
    try:
        thread_rows = connection.execute(
            "SELECT id, rollout_path FROM threads"
        ).fetchall()
        edge_rows = connection.execute(
            """
            SELECT parent_thread_id, child_thread_id, status
            FROM thread_spawn_edges
            ORDER BY parent_thread_id, child_thread_id
            """
        ).fetchall()
    finally:
        connection.close()

    threads: dict[str, dict[str, Any]] = {}
    referenced_paths: set[str] = set()
    normalized_long_paths = 0
    missing_rollouts = 0
    referenced_bytes = 0
    for row in thread_rows:
        path_info = _normalize_path_text(row["rollout_path"])
        size = _path_size(path_info)
        normalized_long_paths += int(path_info["had_long_path_prefix"])
        missing_rollouts += int(size is None)
        referenced_bytes += int(size or 0)
        referenced_paths.add(path_info["comparison_key"])
        threads[row["id"]] = {
            "path": path_info,
            "size_bytes": size,
        }

    open_edges = [
        row for row in edge_rows
        if str(row["status"]).casefold() == "open"
    ]
    open_candidates = []
    full_history_risk_count = 0
    open_child_bytes = 0
    for row in open_edges:
        child = threads.get(row["child_thread_id"], {})
        child_path = child.get("path") or _normalize_path_text("")
        child_size = child.get("size_bytes")
        open_child_bytes += int(child_size or 0)
        possible_full_history_fork = bool(
            child_size is not None and child_size >= large_bytes
        )
        full_history_risk_count += int(possible_full_history_fork)
        open_candidates.append({
            "candidate_type": "open_spawn_edge_review",
            "parent_thread_id": row["parent_thread_id"],
            "child_thread_id": row["child_thread_id"],
            "status": row["status"],
            "rollout_path": child_path["normalized"],
            "source_had_long_path_prefix": child_path[
                "had_long_path_prefix"
            ],
            "size_bytes": child_size,
            "possible_full_history_fork": possible_full_history_fork,
            "recommended_action": (
                "summarize_or_archive_then_close_spawn_edge"
            ),
            "delete_allowed": False,
        })

    sessions_dir = Path(
        args.sessions_dir
        or state_db.parent / "sessions"
    ).expanduser()
    unreferenced_candidates = []
    unreferenced_bytes = 0
    sessions_scanned = sessions_dir.is_dir()
    if sessions_scanned:
        for rollout_path in sessions_dir.rglob("rollout-*.jsonl"):
            path_info = _normalize_path_text(str(rollout_path))
            if path_info["comparison_key"] in referenced_paths:
                continue
            size = _path_size(path_info)
            unreferenced_bytes += int(size or 0)
            unreferenced_candidates.append({
                "candidate_type": "unreferenced_rollout_review",
                "rollout_path": path_info["normalized"],
                "size_bytes": size,
                "reason": "not_referenced_by_selected_codex_state_db",
                "recommended_action": (
                    "verify_all_state_db_references_before_any_cleanup"
                ),
                "delete_allowed": False,
            })

    report = {
        "schema_version": SESSION_AUDIT_SCHEMA_VERSION,
        "created_at": _iso(_now()),
        "status": "CANDIDATES_ONLY",
        "source": {
            "state_db": str(state_db.resolve()),
            "sqlite_mode": "read_only",
            "sessions_dir": str(sessions_dir.resolve()),
            "sessions_scanned": sessions_scanned,
        },
        "metrics": {
            "thread_count": len(thread_rows),
            "spawn_edge_count": len(edge_rows),
            "open_spawn_edge_count": len(open_edges),
            "referenced_rollout_count": len(referenced_paths),
            "referenced_rollout_bytes": referenced_bytes,
            "missing_referenced_rollout_count": missing_rollouts,
            "normalized_long_path_count": normalized_long_paths,
            "open_child_rollout_bytes": open_child_bytes,
            "possible_full_history_fork_count": full_history_risk_count,
            "unreferenced_rollout_candidate_count": len(
                unreferenced_candidates
            ),
            "unreferenced_rollout_candidate_bytes": unreferenced_bytes,
            "large_rollout_threshold_bytes": large_bytes,
        },
        "open_spawn_edge_candidates": open_candidates[:max_candidates],
        "unreferenced_rollout_candidates": unreferenced_candidates[
            :max_candidates
        ],
        "recommendations": [
            "Do not cap agent count; use fork_turns=none or a bounded "
            "history plus a self-contained task prompt when practical.",
            "Reuse a live subagent for related follow-up work instead of "
            "forking the same full history repeatedly.",
            "Resolve open spawn-edge lifecycle and create a durable summary "
            "before considering any rollout candidate.",
        ],
        "policy": {
            "database_mutations_executed": False,
            "rollout_files_deleted": False,
            "candidate_is_not_deletion_authorization": True,
            "sqlite_and_jsonl_cleanup_requires_separate_verified_workflow": (
                True
            ),
        },
        "actions_executed": [],
    }
    manifest = _write_session_audit(
        _state_dir(args.state_dir),
        report=report,
        state_db=state_db,
    )
    report["audit_manifest"] = str(manifest)
    _print_json(report)
    return 0


def _read_hook_payload() -> dict[str, Any]:
    raw = sys.stdin.read(1_000_001)
    if not raw.strip() or len(raw) > 1_000_000:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def hook(args: argparse.Namespace) -> int:
    """Advisory lifecycle hook: never block Codex on cleanup bookkeeping."""

    payload = _read_hook_payload()
    try:
        event_name = str(
            payload.get("hook_event_name") or "unknown"
        )
        actor_id = (
            _actor_id(payload=payload, required=True)
            if event_name == "SubagentStop"
            else None
        )
        state_dir = _state_dir(args.state_dir)
        project_key = _project_key(
            args.project_key,
            cwd=str(payload.get("cwd") or Path.cwd()),
        )
        session_key = _session_key(payload=payload)
        _finish_owner(
            state_dir=state_dir,
            project_key=project_key,
            session_key=session_key,
            actor_id=actor_id,
            event_name=event_name,
            reason=f"hook:{event_name}",
        )
    except Exception as exc:  # cleanup is advisory by contract
        print(
            f"3CAN resource cleanup advisory failed: {type(exc).__name__}",
            file=sys.stderr,
        )
    _print_json({})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Coordinate conflicting resources without limiting agent count "
            "or executing destructive cleanup."
        )
    )
    parser.add_argument("--state-dir")
    parser.add_argument("--project-key")
    parser.add_argument("--profile", choices=sorted(PROFILES))
    subparsers = parser.add_subparsers(dest="command", required=True)

    acquire_parser = subparsers.add_parser("acquire")
    acquire_parser.add_argument("--resource-key", required=True)
    acquire_parser.add_argument("--ttl-sec", type=int, default=3600)
    acquire_parser.add_argument("--metadata-json", default="{}")
    acquire_parser.add_argument("--session-key")
    acquire_parser.add_argument("--actor-id")
    acquire_parser.add_argument("--workorder-id")
    acquire_parser.set_defaults(func=acquire)

    heartbeat_parser = subparsers.add_parser("heartbeat")
    heartbeat_parser.add_argument("--lease-id", required=True)
    heartbeat_parser.add_argument("--ttl-sec", type=int, default=3600)
    heartbeat_parser.add_argument("--session-key")
    heartbeat_parser.add_argument("--actor-id")
    heartbeat_parser.set_defaults(func=heartbeat)

    release_parser = subparsers.add_parser("release")
    release_parser.add_argument("--lease-id", required=True)
    release_parser.add_argument("--session-key")
    release_parser.add_argument("--actor-id")
    release_parser.add_argument("--reason", default="owner_release")
    release_parser.add_argument(
        "--cleanup-verified",
        action="store_true",
        help=(
            "Attest that the owner-scoped process/container/port cleanup "
            "completed before releasing the lease."
        ),
    )
    release_parser.set_defaults(func=release)

    finish_parser = subparsers.add_parser("finish")
    finish_parser.add_argument("--session-key")
    finish_parser.add_argument("--actor-id")
    finish_parser.add_argument(
        "--scope",
        choices=["session", "actor"],
        default="session",
    )
    finish_parser.add_argument("--event-name", default="manual_finish")
    finish_parser.add_argument("--reason", default="owner_finish")
    finish_parser.set_defaults(func=finish)

    status_parser = subparsers.add_parser("status")
    status_parser.set_defaults(func=status)

    audit_parser = subparsers.add_parser("audit-sessions")
    audit_parser.add_argument(
        "--state-db",
        required=True,
        help="Path to Codex state_*.sqlite, opened read-only.",
    )
    audit_parser.add_argument(
        "--sessions-dir",
        help=(
            "Rollout root to compare with SQLite references; defaults to "
            "a sessions sibling of the selected database."
        ),
    )
    audit_parser.add_argument(
        "--large-rollout-bytes",
        type=int,
        default=64 * 1024 * 1024,
        help=(
            "Advisory threshold for possible full-history child rollouts."
        ),
    )
    audit_parser.add_argument("--max-candidates", type=int, default=1000)
    audit_parser.set_defaults(func=audit_sessions)

    hook_parser = subparsers.add_parser("hook")
    hook_parser.set_defaults(func=hook)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ResourceHubError as exc:
        _print_json({
            "schema_version": SCHEMA_VERSION,
            "status": "ERROR",
            "error": str(exc),
        })
        return 2
    except sqlite3.Error as exc:
        _print_json({
            "schema_version": SCHEMA_VERSION,
            "status": "ERROR",
            "error": f"sqlite:{type(exc).__name__}",
        })
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
