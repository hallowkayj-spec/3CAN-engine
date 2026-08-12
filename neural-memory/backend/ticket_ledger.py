"""Process-safe SQLite ticket ledger for 3CAN route authorization.

The ledger is intentionally stdlib-only.  Every mutation uses ``BEGIN
IMMEDIATE``; WAL permits concurrent readers while serializing issue, consume,
completion-CAS, and recovery journal transitions.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence


SCHEMA_VERSION = 3
ACTIVE_STATES = ("issued", "consumed", "completing")
EVENT_TYPES = frozenset({"issued", "consumed", "expired", "completed"})
JOURNAL_STAGES = {
    "authorized": 0,
    "evidence_upserted": 10,
    "solution_upserted": 20,
    "edges_upserted": 30,
    "error_updated": 40,
    "review_required": 40,
    "activity_logged": 50,
    "completed": 60,
}
TICKETED_ERROR_STAGES = {
    "authorized": 0,
    "occurrence_recorded": 10,
    "projected": 20,
    "activity_logged": 30,
    "completed": 40,
}


class LedgerError(RuntimeError):
    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(value: dt.datetime | None = None) -> str:
    return (value or _utc_now()).astimezone(dt.timezone.utc).isoformat()


def _parse_time(value: Any) -> dt.datetime:
    candidate = str(value or "").strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    parsed = dt.datetime.fromisoformat(candidate)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _read_json_file(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class TicketLedger:
    def __init__(
        self,
        path: Path | str,
        *,
        legacy_tickets_path: Path | str | None = None,
        legacy_receipts_path: Path | str | None = None,
        busy_timeout_ms: int = 5000,
        completion_owner_ttl_sec: int = 30,
        completion_grace_sec: int = 3600,
    ) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.legacy_tickets_path = (
            Path(legacy_tickets_path).resolve() if legacy_tickets_path else None
        )
        self.legacy_receipts_path = (
            Path(legacy_receipts_path).resolve() if legacy_receipts_path else None
        )
        self.busy_timeout_ms = max(100, int(busy_timeout_ms))
        self.completion_owner_ttl_sec = max(5, int(completion_owner_ttl_sec))
        self.completion_grace_sec = max(1, int(completion_grace_sec))
        self._initialize()
        self._migrate_legacy_once()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.path),
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.execute("COMMIT")
        except Exception:
            with contextlib.suppress(sqlite3.Error):
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tickets (
                    ticket_id TEXT PRIMARY KEY,
                    lease_key TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    workorder_id TEXT NOT NULL,
                    issued_at TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    ttl_sec INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    target_digest TEXT NOT NULL,
                    scope_digest TEXT NOT NULL,
                    policy_version TEXT NOT NULL,
                    allowed_error_ids_json TEXT NOT NULL,
                    ticket_json TEXT NOT NULL,
                    consume_count INTEGER NOT NULL DEFAULT 0,
                    completion_request_hash TEXT,
                    completion_response_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_tickets_active_lease
                    ON tickets(lease_key)
                    WHERE state IN ('issued', 'consumed', 'completing');
                CREATE INDEX IF NOT EXISTS idx_tickets_state_expiry
                    ON tickets(state, expires_at);
                CREATE INDEX IF NOT EXISTS idx_tickets_agent
                    ON tickets(agent_id, state);

                CREATE TABLE IF NOT EXISTS ticket_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_key TEXT UNIQUE,
                    ticket_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    workorder_id TEXT NOT NULL,
                    target_digest TEXT NOT NULL,
                    scope_digest TEXT NOT NULL,
                    policy_version TEXT NOT NULL,
                    allowed_error_ids_json TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_ticket_events_ticket
                    ON ticket_events(ticket_id, event_id);
                CREATE INDEX IF NOT EXISTS idx_ticket_events_type_time
                    ON ticket_events(event_type, created_at);

                CREATE TABLE IF NOT EXISTS completion_journal (
                    ticket_id TEXT PRIMARY KEY,
                    request_hash TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    context_json TEXT NOT NULL,
                    response_json TEXT,
                    owner_token TEXT,
                    owner_expires_at REAL,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_completion_journal_stage
                    ON completion_journal(stage, updated_at);

                CREATE TABLE IF NOT EXISTS ledger_migrations (
                    migration_key TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS error_cases (
                    fingerprint TEXT PRIMARY KEY,
                    case_id TEXT UNIQUE,
                    project_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    component TEXT NOT NULL DEFAULT 'unspecified',
                    error_type TEXT NOT NULL DEFAULT 'unspecified',
                    error_text TEXT NOT NULL,
                    root_cause TEXT NOT NULL,
                    occurrence_count INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    promoted_at TEXT,
                    resolution_json TEXT,
                    resolution_refs_json TEXT NOT NULL DEFAULT '[]',
                    graph_projection_state TEXT NOT NULL DEFAULT 'pending',
                    graph_projection_error TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_error_cases_case_id
                    ON error_cases(case_id);
                CREATE INDEX IF NOT EXISTS idx_error_cases_state
                    ON error_cases(state, updated_at);

                CREATE TABLE IF NOT EXISTS error_occurrences (
                    occurrence_id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_error_occurrences_fingerprint
                    ON error_occurrences(fingerprint, occurred_at);

                CREATE TABLE IF NOT EXISTS ticketed_error_deliveries (
                    event_idempotency_key TEXT PRIMARY KEY,
                    event_digest TEXT NOT NULL,
                    occurrence_id TEXT NOT NULL UNIQUE,
                    occurrence_fingerprint TEXT NOT NULL,
                    occurrence_payload_hash TEXT NOT NULL,
                    original_ticket_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    context_json TEXT NOT NULL,
                    receipt_json TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_ticketed_error_delivery_stage
                    ON ticketed_error_deliveries(stage, updated_at);

                CREATE TABLE IF NOT EXISTS error_projection_journal (
                    fingerprint TEXT PRIMARY KEY,
                    desired_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    last_error TEXT,
                    updated_at TEXT NOT NULL
                );
                """
            )
            error_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(error_cases)")
            }
            if "component" not in error_columns:
                connection.execute(
                    "ALTER TABLE error_cases ADD COLUMN component TEXT NOT NULL DEFAULT 'unspecified'"
                )
            if "error_type" not in error_columns:
                connection.execute(
                    "ALTER TABLE error_cases ADD COLUMN error_type TEXT NOT NULL DEFAULT 'unspecified'"
                )
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        finally:
            connection.close()

    @staticmethod
    def _event_snapshot(ticket: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "ticket_id": str(ticket["ticket_id"]),
            "agent_id": str(ticket["agent_id"]),
            "project_id": str(ticket["project_id"]),
            "workspace_id": str(ticket["workspace_id"]),
            "workorder_id": str(ticket["workorder_id"]),
            "target_digest": str(ticket["target_digest"]),
            "scope_digest": str(ticket["scope_digest"]),
            "policy_version": str(ticket["policy_version"]),
            "allowed_error_ids": list(ticket.get("allowed_error_ids") or []),
        }

    def _insert_event_locked(
        self,
        connection: sqlite3.Connection,
        *,
        event_type: str,
        ticket: Mapping[str, Any],
        payload: Mapping[str, Any] | None = None,
        event_key: str | None = None,
        created_at: str | None = None,
    ) -> None:
        if event_type not in EVENT_TYPES:
            raise LedgerError("invalid_event_type")
        snapshot = self._event_snapshot(ticket)
        connection.execute(
            """
            INSERT OR IGNORE INTO ticket_events (
                event_key, ticket_id, event_type, created_at, agent_id,
                project_id, workspace_id, workorder_id, target_digest,
                scope_digest, policy_version, allowed_error_ids_json,
                payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_key,
                snapshot["ticket_id"],
                event_type,
                created_at or _iso(),
                snapshot["agent_id"],
                snapshot["project_id"],
                snapshot["workspace_id"],
                snapshot["workorder_id"],
                snapshot["target_digest"],
                snapshot["scope_digest"],
                snapshot["policy_version"],
                canonical_json(snapshot["allowed_error_ids"]),
                canonical_json(dict(payload or {})),
            ),
        )

    @staticmethod
    def _row_ticket(row: sqlite3.Row) -> dict[str, Any]:
        ticket = json.loads(row["ticket_json"])
        consume_count = int(row["consume_count"])
        ticket.update(
            {
                "ticket_id": row["ticket_id"],
                "lease_key": row["lease_key"],
                "agent_id": row["agent_id"],
                "project_id": row["project_id"],
                "workspace_id": row["workspace_id"],
                "workorder_id": row["workorder_id"],
                "issued_at": row["issued_at"],
                "ttl_sec": int(row["ttl_sec"]),
                "state": row["state"],
                "target_digest": row["target_digest"],
                "scope_digest": row["scope_digest"],
                "policy_version": row["policy_version"],
                "allowed_error_ids": json.loads(row["allowed_error_ids_json"]),
                "consume_count": consume_count,
            }
        )
        if consume_count > 0:
            ticket["completion_deadline"] = dt.datetime.fromtimestamp(
                float(row["expires_at"]),
                tz=dt.timezone.utc,
            ).isoformat()
        else:
            ticket.pop("completion_deadline", None)
        return ticket

    def _expire_locked(self, connection: sqlite3.Connection, now_epoch: float) -> None:
        rows = connection.execute(
            """
            SELECT tickets.*
            FROM tickets
            LEFT JOIN completion_journal
                ON completion_journal.ticket_id = tickets.ticket_id
            WHERE (
                tickets.state IN ('issued', 'consumed')
                AND tickets.expires_at <= ?
            ) OR (
                tickets.state = 'completing'
                AND tickets.expires_at <= ?
                AND (
                    completion_journal.ticket_id IS NULL
                    OR completion_journal.owner_token IS NULL
                    OR completion_journal.owner_expires_at IS NULL
                    OR completion_journal.owner_expires_at <= ?
                )
            )
            """,
            (now_epoch, now_epoch, now_epoch),
        ).fetchall()
        for row in rows:
            ticket = self._row_ticket(row)
            ticket["state"] = "expired"
            connection.execute(
                "UPDATE tickets SET state='expired', ticket_json=?, updated_at=? WHERE ticket_id=?",
                (canonical_json(ticket), _iso(), ticket["ticket_id"]),
            )
            connection.execute(
                """
                UPDATE completion_journal
                SET owner_token=NULL, owner_expires_at=NULL,
                    last_error=COALESCE(last_error, 'completion_deadline_expired'),
                    updated_at=?
                WHERE ticket_id=?
                """,
                (_iso(), ticket["ticket_id"]),
            )
            self._insert_event_locked(
                connection,
                event_type="expired",
                ticket=ticket,
                payload={"ttl_sec": ticket["ttl_sec"]},
            )

    def _migrate_legacy_once(self) -> None:
        sources = [
            path
            for path in (self.legacy_tickets_path, self.legacy_receipts_path)
            if path is not None and path.exists()
        ]
        migration_key = "legacy-route-ticket-json-v1"
        with self._transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM ledger_migrations WHERE migration_key=?",
                (migration_key,),
            ).fetchone():
                return

        details: dict[str, Any] = {
            "sources": [
                {"path": str(path), "sha256": _file_digest(path)} for path in sources
            ],
            "ticket_count": 0,
            "event_count": 0,
        }
        if not sources:
            status = "no_legacy_sources"
            legacy_tickets: dict[str, dict[str, Any]] = {}
            legacy_events: list[dict[str, Any]] = []
        else:
            try:
                legacy_tickets = self._validated_legacy_tickets()
                legacy_events = self._validated_legacy_events()
                status = "imported"
            except (OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
                legacy_tickets = {}
                legacy_events = []
                status = "skipped_unverifiable"
                details["error"] = str(exc)[:500]

        with self._transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM ledger_migrations WHERE migration_key=?",
                (migration_key,),
            ).fetchone():
                return
            if status == "imported":
                for ticket in legacy_tickets.values():
                    self._insert_legacy_ticket_locked(connection, ticket)
                for index, event in enumerate(legacy_events):
                    ticket = legacy_tickets.get(str(event["ticket_id"]))
                    if ticket is None:
                        ticket = self._legacy_event_ticket(event)
                    self._insert_event_locked(
                        connection,
                        event_type=str(event["event"]),
                        ticket=ticket,
                        payload=dict(event.get("details") or {}),
                        event_key=f"legacy:{canonical_hash(event)}:{index}",
                        created_at=str(event["timestamp"]),
                    )
                details["ticket_count"] = len(legacy_tickets)
                details["event_count"] = len(legacy_events)
            connection.execute(
                """
                INSERT INTO ledger_migrations
                    (migration_key, status, details_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (migration_key, status, canonical_json(details), _iso()),
            )

    def _validated_legacy_tickets(self) -> dict[str, dict[str, Any]]:
        path = self.legacy_tickets_path
        if path is None or not path.exists():
            return {}
        payload = _read_json_file(path)
        if not isinstance(payload, dict):
            raise ValueError("legacy ticket store must be a JSON object")
        result: dict[str, dict[str, Any]] = {}
        for key, value in payload.items():
            if not isinstance(value, dict) or str(value.get("ticket_id") or "") != str(key):
                raise ValueError(f"legacy ticket {key!r} is not verifiable")
            _parse_time(value.get("issued_at"))
            if not str(value.get("agent_id") or "").strip():
                raise ValueError(f"legacy ticket {key!r} has no agent_id")
            result[str(key)] = self._normalize_legacy_ticket(value)
        return result

    def _validated_legacy_events(self) -> list[dict[str, Any]]:
        path = self.legacy_receipts_path
        if path is None or not path.exists():
            return []
        result: list[dict[str, Any]] = []
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            event = json.loads(line)
            if (
                not isinstance(event, dict)
                or event.get("event") not in EVENT_TYPES
                or not str(event.get("ticket_id") or "").strip()
                or not str(event.get("agent_id") or "").strip()
            ):
                raise ValueError(f"legacy receipt line {line_number} is not verifiable")
            _parse_time(event.get("timestamp"))
            result.append(event)
        return result

    def _normalize_legacy_ticket(self, value: Mapping[str, Any]) -> dict[str, Any]:
        ticket = dict(value)
        scope = ticket.get("scope") if isinstance(ticket.get("scope"), dict) else {}
        allowed = ticket.get("allowed_error_ids")
        if not isinstance(allowed, list):
            allowed = [
                item["id"]
                for item in ticket.get("err_warnings") or []
                if isinstance(item, dict) and str(item.get("id") or "").startswith("ERR-")
            ]
        targets = scope.get("target_files") if isinstance(scope, dict) else []
        target_digest = str(ticket.get("target_digest") or canonical_hash(targets or []))
        scope_digest = str(ticket.get("scope_digest") or canonical_hash(scope or {}))
        issued = _parse_time(ticket["issued_at"])
        ttl = max(1, int(ticket.get("ttl_sec") or 900))
        consumed = ticket.get("consumed_by_tools")
        consume_count = len(consumed) if isinstance(consumed, list) else 0
        ticket.update(
            {
                "project_id": str(ticket.get("project_id") or "legacy-unspecified"),
                "workspace_id": str(ticket.get("workspace_id") or "legacy-unspecified"),
                "workorder_id": str(ticket.get("workorder_id") or "legacy-unspecified"),
                "target_digest": target_digest,
                "scope_digest": scope_digest,
                "policy_version": str(ticket.get("policy_version") or "legacy/v1"),
                "allowed_error_ids": sorted({str(item) for item in allowed if str(item)}),
                "consume_count": consume_count,
                "state": "consumed" if consume_count else "issued",
                "_expires_at": issued.timestamp() + ttl,
            }
        )
        return ticket

    def _legacy_event_ticket(self, event: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "ticket_id": str(event["ticket_id"]),
            "agent_id": str(event["agent_id"]),
            "project_id": "legacy-unspecified",
            "workspace_id": "legacy-unspecified",
            "workorder_id": "legacy-unspecified",
            "target_digest": "",
            "scope_digest": "",
            "policy_version": "legacy/v1",
            "allowed_error_ids": [],
        }

    def _insert_legacy_ticket_locked(
        self,
        connection: sqlite3.Connection,
        ticket: Mapping[str, Any],
    ) -> None:
        now = _utc_now().timestamp()
        expires_at = float(ticket["_expires_at"])
        state = str(ticket["state"]) if expires_at > now else "expired"
        stored = dict(ticket)
        stored.pop("_expires_at", None)
        stored["state"] = state
        connection.execute(
            """
            INSERT OR IGNORE INTO tickets (
                ticket_id, lease_key, agent_id, project_id, workspace_id,
                workorder_id, issued_at, expires_at, ttl_sec, state,
                target_digest, scope_digest, policy_version,
                allowed_error_ids_json, ticket_json, consume_count,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                stored["ticket_id"],
                str(stored.get("lease_key") or f"legacy:{stored['ticket_id']}"),
                stored["agent_id"],
                stored["project_id"],
                stored["workspace_id"],
                stored["workorder_id"],
                stored["issued_at"],
                expires_at,
                int(stored.get("ttl_sec") or 900),
                state,
                stored["target_digest"],
                stored["scope_digest"],
                stored["policy_version"],
                canonical_json(stored["allowed_error_ids"]),
                canonical_json(stored),
                int(stored.get("consume_count") or 0),
                _iso(),
                _iso(),
            ),
        )
        if state == "expired":
            self._insert_event_locked(
                connection,
                event_type="expired",
                ticket=stored,
                payload={
                    "ttl_sec": int(stored.get("ttl_sec") or 900),
                    "legacy_import_expired": True,
                },
                event_key=f"legacy-expire:{stored['ticket_id']}",
            )

    def migration_status(self) -> dict[str, Any]:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM ledger_migrations WHERE migration_key=?",
                ("legacy-route-ticket-json-v1",),
            ).fetchone()
            if not row:
                return {}
            return {
                "status": row["status"],
                "details": json.loads(row["details_json"]),
                "created_at": row["created_at"],
            }
        finally:
            connection.close()

    def find_active_by_lease(self, lease_key: str) -> dict[str, Any] | None:
        with self._transaction() as connection:
            self._expire_locked(connection, _utc_now().timestamp())
            row = connection.execute(
                """
                SELECT * FROM tickets
                WHERE lease_key=? AND state IN ('issued', 'consumed', 'completing')
                ORDER BY issued_at DESC LIMIT 1
                """,
                (lease_key,),
            ).fetchone()
            return self._row_ticket(row) if row else None

    def issue(self, ticket: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        required = (
            "ticket_id",
            "lease_key",
            "agent_id",
            "project_id",
            "workspace_id",
            "workorder_id",
            "issued_at",
            "ttl_sec",
            "target_digest",
            "scope_digest",
            "policy_version",
            "allowed_error_ids",
        )
        missing = [field for field in required if field not in ticket]
        if missing:
            raise LedgerError("ticket_fields_missing", ",".join(missing))
        issued = _parse_time(ticket["issued_at"])
        ttl = max(1, int(ticket["ttl_sec"]))
        stored = dict(ticket)
        stored["state"] = "issued"
        stored["consume_count"] = 0
        now_iso = _iso()
        with self._transaction() as connection:
            self._expire_locked(connection, _utc_now().timestamp())
            existing = connection.execute(
                """
                SELECT * FROM tickets
                WHERE lease_key=? AND state IN ('issued', 'consumed', 'completing')
                LIMIT 1
                """,
                (stored["lease_key"],),
            ).fetchone()
            if existing:
                return self._row_ticket(existing), True
            try:
                connection.execute(
                    """
                    INSERT INTO tickets (
                        ticket_id, lease_key, agent_id, project_id, workspace_id,
                        workorder_id, issued_at, expires_at, ttl_sec, state,
                        target_digest, scope_digest, policy_version,
                        allowed_error_ids_json, ticket_json, consume_count,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'issued', ?, ?, ?, ?, ?, 0, ?, ?)
                    """,
                    (
                        stored["ticket_id"],
                        stored["lease_key"],
                        stored["agent_id"],
                        stored["project_id"],
                        stored["workspace_id"],
                        stored["workorder_id"],
                        stored["issued_at"],
                        issued.timestamp() + ttl,
                        ttl,
                        stored["target_digest"],
                        stored["scope_digest"],
                        stored["policy_version"],
                        canonical_json(stored["allowed_error_ids"]),
                        canonical_json(stored),
                        now_iso,
                        now_iso,
                    ),
                )
            except sqlite3.IntegrityError:
                existing = connection.execute(
                    """
                    SELECT * FROM tickets
                    WHERE lease_key=? AND state IN ('issued', 'consumed', 'completing')
                    LIMIT 1
                    """,
                    (stored["lease_key"],),
                ).fetchone()
                if existing:
                    return self._row_ticket(existing), True
                raise
            self._insert_event_locked(
                connection,
                event_type="issued",
                ticket=stored,
                payload=self._event_snapshot(stored),
            )
            return stored, False

    def get(self, ticket_id: str, *, active_only: bool = True) -> dict[str, Any] | None:
        with self._transaction() as connection:
            self._expire_locked(connection, _utc_now().timestamp())
            row = connection.execute(
                "SELECT * FROM tickets WHERE ticket_id=?",
                (ticket_id,),
            ).fetchone()
            if not row:
                return None
            ticket = self._row_ticket(row)
            if active_only and ticket["state"] not in ACTIVE_STATES:
                return None
            return ticket

    def consume(
        self,
        ticket_id: str,
        *,
        agent_id: str,
        target_digest: str,
        scope_digest: str,
        consumed: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not str(agent_id or "").strip():
            raise LedgerError("agent_id_required")
        with self._transaction() as connection:
            now = _utc_now()
            self._expire_locked(connection, now.timestamp())
            row = connection.execute(
                "SELECT * FROM tickets WHERE ticket_id=?",
                (ticket_id,),
            ).fetchone()
            if not row:
                raise LedgerError("ticket_not_found")
            ticket = self._row_ticket(row)
            if ticket["state"] not in {"issued", "consumed"}:
                raise LedgerError("ticket_not_active")
            if ticket["agent_id"] != agent_id:
                raise LedgerError("ticket_agent_mismatch")
            if ticket["target_digest"] != target_digest:
                raise LedgerError("ticket_target_digest_mismatch")
            if ticket["scope_digest"] != scope_digest:
                raise LedgerError("ticket_scope_digest_mismatch")
            consumed_items = ticket.get("consumed_by_tools")
            if not isinstance(consumed_items, list):
                consumed_items = []
            consumed_items.append(dict(consumed))
            ticket["consumed_by_tools"] = consumed_items
            ticket["consume_count"] = int(ticket.get("consume_count") or 0) + 1
            ticket["state"] = "consumed"
            completion_deadline = now + dt.timedelta(
                seconds=self.completion_grace_sec
            )
            ticket["completion_deadline"] = _iso(completion_deadline)
            connection.execute(
                """
                UPDATE tickets
                SET state='consumed', consume_count=?, expires_at=?,
                    ticket_json=?, updated_at=?
                WHERE ticket_id=?
                """,
                (
                    ticket["consume_count"],
                    completion_deadline.timestamp(),
                    canonical_json(ticket),
                    _iso(now),
                    ticket_id,
                ),
            )
            self._insert_event_locked(
                connection,
                event_type="consumed",
                ticket=ticket,
                payload={
                    **self._event_snapshot(ticket),
                    "tool_name": str(consumed.get("tool_name") or ""),
                    "tool_input_summary": str(
                        consumed.get("tool_input_summary") or ""
                    )[:200],
                    "completion_deadline": ticket["completion_deadline"],
                },
            )
            return ticket

    def attach_consume_activity_hash(
        self,
        ticket_id: str,
        *,
        agent_id: str,
        tool_name: str,
        tool_input_summary: str,
        activity_hash: str,
    ) -> dict[str, Any]:
        """Durably bind the graph activity receipt to the latest consume."""

        if not re.fullmatch(r"[0-9a-f]{64}", activity_hash.casefold()):
            raise LedgerError("consume_activity_hash_invalid")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM tickets WHERE ticket_id=?",
                (ticket_id,),
            ).fetchone()
            if not row:
                raise LedgerError("ticket_not_found")
            ticket = self._row_ticket(row)
            if ticket["agent_id"] != agent_id:
                raise LedgerError("ticket_agent_mismatch")
            consumed = ticket.get("consumed_by_tools")
            if not isinstance(consumed, list) or not consumed:
                raise LedgerError("ticket_not_consumed")
            selected = consumed[-1]
            if (
                str(selected.get("tool_name") or "") != tool_name
                or str(selected.get("tool_input_summary") or "")
                != tool_input_summary
            ):
                raise LedgerError("ticket_consumption_binding_mismatch")
            existing_hash = str(selected.get("activity_hash") or "").casefold()
            if existing_hash and existing_hash != activity_hash.casefold():
                raise LedgerError("consume_activity_hash_conflict")
            selected["activity_hash"] = activity_hash.casefold()
            ticket["consumed_by_tools"] = consumed
            connection.execute(
                """
                UPDATE tickets SET ticket_json=?, updated_at=?
                WHERE ticket_id=?
                """,
                (canonical_json(ticket), _iso(), ticket_id),
            )
            return ticket

    def begin_completion(
        self,
        ticket_id: str,
        *,
        agent_id: str,
        request_hash: str,
        request: Mapping[str, Any],
        requested_error_ids: Sequence[str],
        error_dispositions: Mapping[str, str] | None = None,
        owner_token: str | None = None,
    ) -> dict[str, Any]:
        owner = owner_token or uuid.uuid4().hex
        now = _utc_now()
        with self._transaction() as connection:
            self._expire_locked(connection, now.timestamp())
            row = connection.execute(
                "SELECT * FROM tickets WHERE ticket_id=?",
                (ticket_id,),
            ).fetchone()
            if not row:
                raise LedgerError("ticket_not_found")
            ticket = self._row_ticket(row)
            if ticket["agent_id"] != agent_id:
                raise LedgerError("ticket_agent_mismatch")
            prior_hash = str(row["completion_request_hash"] or "")
            if ticket["state"] == "completed":
                if prior_hash != request_hash:
                    raise LedgerError("completion_request_conflict")
                response = json.loads(row["completion_response_json"])
                return {"mode": "replay", "response": response, "ticket": ticket}
            if ticket["state"] not in {"consumed", "completing"}:
                if (
                    int(row["consume_count"]) > 0
                    and float(row["expires_at"]) <= now.timestamp()
                ):
                    raise LedgerError("ticket_completion_deadline_expired")
                if int(row["consume_count"]) <= 0:
                    raise LedgerError("ticket_not_consumed")
                raise LedgerError("ticket_not_active")
            if float(row["expires_at"]) <= now.timestamp():
                raise LedgerError("ticket_completion_deadline_expired")
            allowed = set(ticket["allowed_error_ids"])
            unauthorized = sorted(set(requested_error_ids) - allowed)
            if unauthorized:
                raise LedgerError(
                    "ticket_error_not_allowed",
                    ",".join(unauthorized),
                )
            required_dispositions = {
                str(item)
                for item in ticket.get(
                    "required_error_disposition_ids",
                    [],
                )
                if str(item).strip()
            }
            dispositions = {
                str(error_id): str(disposition).casefold()
                for error_id, disposition in (
                    error_dispositions or {}
                ).items()
                if str(error_id).strip()
            }
            disposition_ids = set(dispositions)
            if disposition_ids != required_dispositions:
                missing = sorted(required_dispositions - disposition_ids)
                unexpected = sorted(disposition_ids - required_dispositions)
                raise LedgerError(
                    "ticket_error_disposition_incomplete",
                    canonical_json(
                        {
                            "missing": missing,
                            "unexpected": unexpected,
                        }
                    ),
                )
            invalid_dispositions = sorted(
                error_id
                for error_id, disposition in dispositions.items()
                if disposition
                not in {"resolved", "still_open", "not_applicable"}
            )
            if invalid_dispositions:
                raise LedgerError(
                    "ticket_error_disposition_invalid",
                    ",".join(invalid_dispositions),
                )
            requested = set(requested_error_ids)
            mismatched_resolution = sorted(
                error_id
                for error_id, disposition in dispositions.items()
                if (
                    (disposition == "resolved" and error_id not in requested)
                    or (
                        disposition != "resolved"
                        and error_id in requested
                    )
                )
            )
            if mismatched_resolution:
                raise LedgerError(
                    "ticket_error_disposition_resolution_mismatch",
                    ",".join(mismatched_resolution),
                )
            if prior_hash and prior_hash != request_hash:
                raise LedgerError("completion_request_conflict")

            journal = connection.execute(
                "SELECT * FROM completion_journal WHERE ticket_id=?",
                (ticket_id,),
            ).fetchone()
            if journal and journal["request_hash"] != request_hash:
                raise LedgerError("completion_request_conflict")
            if (
                journal
                and journal["owner_token"]
                and float(journal["owner_expires_at"] or 0) > now.timestamp()
                and journal["owner_token"] != owner
            ):
                raise LedgerError("completion_in_progress")
            owner_expires = now.timestamp() + self.completion_owner_ttl_sec
            if journal:
                connection.execute(
                    """
                    UPDATE completion_journal
                    SET owner_token=?, owner_expires_at=?, last_error=NULL,
                        updated_at=?
                    WHERE ticket_id=?
                    """,
                    (owner, owner_expires, _iso(now), ticket_id),
                )
                stage = str(journal["stage"])
                context = json.loads(journal["context_json"])
            else:
                stage = "authorized"
                context = {}
                connection.execute(
                    """
                    INSERT INTO completion_journal (
                        ticket_id, request_hash, request_json, stage,
                        context_json, owner_token, owner_expires_at,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, 'authorized', '{}', ?, ?, ?, ?)
                    """,
                    (
                        ticket_id,
                        request_hash,
                        canonical_json(request),
                        owner,
                        owner_expires,
                        _iso(now),
                        _iso(now),
                    ),
                )
            connection.execute(
                """
                UPDATE tickets
                SET state='completing', completion_request_hash=?, updated_at=?
                WHERE ticket_id=?
                """,
                (request_hash, _iso(now), ticket_id),
            )
            ticket["state"] = "completing"
            return {
                "mode": "resume" if journal else "new",
                "ticket": ticket,
                "stage": stage,
                "context": context,
                "owner_token": owner,
            }

    def advance_completion(
        self,
        ticket_id: str,
        *,
        request_hash: str,
        owner_token: str,
        stage: str,
        context: Mapping[str, Any],
    ) -> None:
        if stage not in JOURNAL_STAGES:
            raise LedgerError("invalid_completion_stage")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM completion_journal WHERE ticket_id=?",
                (ticket_id,),
            ).fetchone()
            if not row or row["request_hash"] != request_hash:
                raise LedgerError("completion_journal_missing")
            if row["owner_token"] != owner_token:
                raise LedgerError("completion_owner_lost")
            current = str(row["stage"])
            selected = (
                stage
                if JOURNAL_STAGES[stage] >= JOURNAL_STAGES.get(current, -1)
                else current
            )
            connection.execute(
                """
                UPDATE completion_journal
                SET stage=?, context_json=?, owner_expires_at=?, updated_at=?
                WHERE ticket_id=?
                """,
                (
                    selected,
                    canonical_json(dict(context)),
                    _utc_now().timestamp() + self.completion_owner_ttl_sec,
                    _iso(),
                    ticket_id,
                ),
            )

    def release_completion(
        self,
        ticket_id: str,
        *,
        request_hash: str,
        owner_token: str,
        error: str,
    ) -> None:
        with self._transaction() as connection:
            journal = connection.execute(
                "SELECT * FROM completion_journal WHERE ticket_id=?",
                (ticket_id,),
            ).fetchone()
            if (
                not journal
                or journal["request_hash"] != request_hash
                or journal["owner_token"] != owner_token
            ):
                return
            ticket = connection.execute(
                "SELECT * FROM tickets WHERE ticket_id=?",
                (ticket_id,),
            ).fetchone()
            state = (
                "consumed"
                if ticket and float(ticket["expires_at"]) > _utc_now().timestamp()
                else "expired"
            )
            connection.execute(
                """
                UPDATE completion_journal
                SET owner_token=NULL, owner_expires_at=NULL, last_error=?,
                    updated_at=?
                WHERE ticket_id=?
                """,
                (str(error)[:1000], _iso(), ticket_id),
            )
            connection.execute(
                "UPDATE tickets SET state=?, updated_at=? WHERE ticket_id=?",
                (state, _iso(), ticket_id),
            )

    def complete(
        self,
        ticket_id: str,
        *,
        request_hash: str,
        owner_token: str,
        response: Mapping[str, Any],
    ) -> dict[str, Any]:
        response_json = canonical_json(dict(response))
        with self._transaction() as connection:
            ticket_row = connection.execute(
                "SELECT * FROM tickets WHERE ticket_id=?",
                (ticket_id,),
            ).fetchone()
            if not ticket_row:
                raise LedgerError("ticket_not_found")
            prior_hash = str(ticket_row["completion_request_hash"] or "")
            if ticket_row["state"] == "completed":
                if prior_hash != request_hash:
                    raise LedgerError("completion_request_conflict")
                return json.loads(ticket_row["completion_response_json"])
            journal = connection.execute(
                "SELECT * FROM completion_journal WHERE ticket_id=?",
                (ticket_id,),
            ).fetchone()
            if (
                not journal
                or journal["request_hash"] != request_hash
                or journal["owner_token"] != owner_token
            ):
                raise LedgerError("completion_owner_lost")
            now_iso = _iso()
            connection.execute(
                """
                UPDATE tickets
                SET state='completed', completion_request_hash=?,
                    completion_response_json=?, ticket_json=?, updated_at=?
                WHERE ticket_id=?
                """,
                (
                    request_hash,
                    response_json,
                    canonical_json(
                        {
                            **self._row_ticket(ticket_row),
                            "state": "completed",
                        }
                    ),
                    now_iso,
                    ticket_id,
                ),
            )
            connection.execute(
                """
                UPDATE completion_journal
                SET stage='completed', response_json=?, owner_token=NULL,
                    owner_expires_at=NULL, updated_at=?
                WHERE ticket_id=?
                """,
                (response_json, now_iso, ticket_id),
            )
            ticket = self._row_ticket(ticket_row)
            ticket["state"] = "completed"
            self._insert_event_locked(
                connection,
                event_type="completed",
                ticket=ticket,
                payload={
                    **self._event_snapshot(ticket),
                    "request_hash": request_hash,
                    "response_hash": canonical_hash(response),
                },
            )
            return json.loads(response_json)

    @staticmethod
    def _ticketed_error_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "event_idempotency_key": row["event_idempotency_key"],
            "event_digest": row["event_digest"],
            "occurrence_id": row["occurrence_id"],
            "occurrence_fingerprint": row["occurrence_fingerprint"],
            "occurrence_payload_hash": row["occurrence_payload_hash"],
            "original_ticket_id": row["original_ticket_id"],
            "stage": row["stage"],
            "context": json.loads(row["context_json"]),
            "receipt": (
                json.loads(row["receipt_json"])
                if row["receipt_json"]
                else None
            ),
            "last_error": row["last_error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def begin_ticketed_error_delivery(
        self,
        *,
        ticket_id: str,
        agent_id: str,
        target_digest: str,
        scope_digest: str,
        completion_request_hash: str,
        expected_tool_name: str,
        expected_tool_input_summary: str,
        event_idempotency_key: str,
        event_digest: str,
        occurrence_id: str,
        occurrence_fingerprint: str,
        occurrence_payload_hash: str,
        consume_activity_hash: str,
    ) -> dict[str, Any]:
        """Atomically validate the consumed ticket and reserve event identity."""

        now = _utc_now()
        with self._transaction() as connection:
            self._expire_locked(connection, now.timestamp())
            ticket_row = connection.execute(
                "SELECT * FROM tickets WHERE ticket_id=?",
                (ticket_id,),
            ).fetchone()
            if not ticket_row:
                raise LedgerError("ticket_not_found")
            ticket = self._row_ticket(ticket_row)
            if ticket["agent_id"] != agent_id:
                raise LedgerError("ticket_agent_mismatch")
            if ticket["target_digest"] != target_digest:
                raise LedgerError("ticket_target_digest_mismatch")
            if ticket["scope_digest"] != scope_digest:
                raise LedgerError("ticket_scope_digest_mismatch")
            if str(ticket_row["completion_request_hash"] or "") != (
                completion_request_hash
            ):
                raise LedgerError("completion_request_conflict")
            if ticket["state"] not in {"completing", "completed"}:
                raise LedgerError("ticket_not_completing")
            consumed = ticket.get("consumed_by_tools")
            if not isinstance(consumed, list) or len(consumed) != 1:
                raise LedgerError("ticket_consumption_not_exclusive")
            consume_item = consumed[0]
            if (
                str(consume_item.get("tool_name") or "")
                != expected_tool_name
                or str(consume_item.get("tool_input_summary") or "")
                != expected_tool_input_summary
            ):
                raise LedgerError("ticket_consumption_binding_mismatch")
            if str(consume_item.get("activity_hash") or "").casefold() != (
                consume_activity_hash.casefold()
            ):
                raise LedgerError("ticket_consumption_activity_mismatch")

            existing = connection.execute(
                """
                SELECT * FROM ticketed_error_deliveries
                WHERE event_idempotency_key=?
                """,
                (event_idempotency_key,),
            ).fetchone()
            if existing:
                expected = (
                    event_digest,
                    occurrence_id,
                    occurrence_fingerprint,
                    occurrence_payload_hash,
                )
                actual = (
                    existing["event_digest"],
                    existing["occurrence_id"],
                    existing["occurrence_fingerprint"],
                    existing["occurrence_payload_hash"],
                )
                if actual != expected:
                    raise LedgerError("ticketed_error_replay_conflict")
                result = self._ticketed_error_row(existing)
                result["mode"] = (
                    "replay" if result["receipt"] else "resume"
                )
                return result

            context = {
                "original_ticket_id": ticket_id,
                "original_consume_activity_hash": consume_activity_hash,
                "original_completion_request_hash": completion_request_hash,
            }
            now_iso = _iso(now)
            connection.execute(
                """
                INSERT INTO ticketed_error_deliveries (
                    event_idempotency_key, event_digest, occurrence_id,
                    occurrence_fingerprint, occurrence_payload_hash,
                    original_ticket_id, stage, context_json, created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'authorized', ?, ?, ?)
                """,
                (
                    event_idempotency_key,
                    event_digest,
                    occurrence_id,
                    occurrence_fingerprint,
                    occurrence_payload_hash,
                    ticket_id,
                    canonical_json(context),
                    now_iso,
                    now_iso,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM ticketed_error_deliveries
                WHERE event_idempotency_key=?
                """,
                (event_idempotency_key,),
            ).fetchone()
            result = self._ticketed_error_row(row)
            result["mode"] = "new"
            return result

    def advance_ticketed_error_delivery(
        self,
        event_idempotency_key: str,
        *,
        stage: str,
        context: Mapping[str, Any],
    ) -> dict[str, Any]:
        if stage not in TICKETED_ERROR_STAGES:
            raise LedgerError("invalid_ticketed_error_stage")
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM ticketed_error_deliveries
                WHERE event_idempotency_key=?
                """,
                (event_idempotency_key,),
            ).fetchone()
            if not row:
                raise LedgerError("ticketed_error_journal_missing")
            current = str(row["stage"])
            selected = (
                stage
                if TICKETED_ERROR_STAGES[stage]
                >= TICKETED_ERROR_STAGES.get(current, -1)
                else current
            )
            merged = json.loads(row["context_json"])
            merged.update(dict(context))
            connection.execute(
                """
                UPDATE ticketed_error_deliveries
                SET stage=?, context_json=?, last_error=NULL, updated_at=?
                WHERE event_idempotency_key=?
                """,
                (
                    selected,
                    canonical_json(merged),
                    _iso(),
                    event_idempotency_key,
                ),
            )
            updated = connection.execute(
                """
                SELECT * FROM ticketed_error_deliveries
                WHERE event_idempotency_key=?
                """,
                (event_idempotency_key,),
            ).fetchone()
            return self._ticketed_error_row(updated)

    def release_ticketed_error_delivery(
        self,
        event_idempotency_key: str,
        *,
        error: str,
    ) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE ticketed_error_deliveries
                SET last_error=?, updated_at=?
                WHERE event_idempotency_key=? AND stage != 'completed'
                """,
                (str(error)[:1000], _iso(), event_idempotency_key),
            )

    def complete_ticketed_error_delivery(
        self,
        event_idempotency_key: str,
        *,
        receipt: Mapping[str, Any],
    ) -> dict[str, Any]:
        receipt_json = canonical_json(dict(receipt))
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM ticketed_error_deliveries
                WHERE event_idempotency_key=?
                """,
                (event_idempotency_key,),
            ).fetchone()
            if not row:
                raise LedgerError("ticketed_error_journal_missing")
            if TICKETED_ERROR_STAGES.get(str(row["stage"]), -1) < (
                TICKETED_ERROR_STAGES["activity_logged"]
            ):
                raise LedgerError("ticketed_error_activity_not_durable")
            if row["receipt_json"]:
                existing = json.loads(row["receipt_json"])
                if canonical_json(existing) != receipt_json:
                    raise LedgerError("ticketed_error_receipt_conflict")
                return existing
            connection.execute(
                """
                UPDATE ticketed_error_deliveries
                SET stage='completed', receipt_json=?, last_error=NULL,
                    updated_at=?
                WHERE event_idempotency_key=?
                """,
                (receipt_json, _iso(), event_idempotency_key),
            )
            return json.loads(receipt_json)

    def ticketed_error_delivery(
        self,
        event_idempotency_key: str,
    ) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT * FROM ticketed_error_deliveries
                WHERE event_idempotency_key=?
                """,
                (event_idempotency_key,),
            ).fetchone()
            return self._ticketed_error_row(row) if row else None
        finally:
            connection.close()

    @staticmethod
    def _error_case_row(row: sqlite3.Row) -> dict[str, Any]:
        occurrence_count = int(row["occurrence_count"])
        state = str(row["state"])
        promoted = bool(row["case_id"])
        return {
            "schema_version": "3can.error-case-ledger/v2",
            "fingerprint": row["fingerprint"],
            "case_id": row["case_id"],
            "project_id": row["project_id"],
            "operation": row["operation"],
            "component": row["component"],
            "error_type": row["error_type"],
            "error": row["error_text"],
            "root_cause": row["root_cause"],
            "occurrence_count": occurrence_count,
            "state": state,
            "promoted": promoted,
            "blocking": (
                promoted
                and occurrence_count >= 2
                and state not in {"resolved", "superseded"}
            ),
            "first_seen_at": row["first_seen_at"],
            "last_seen_at": row["last_seen_at"],
            "promoted_at": row["promoted_at"],
            "resolution": (
                json.loads(row["resolution_json"])
                if row["resolution_json"] else None
            ),
            "resolution_refs": json.loads(row["resolution_refs_json"]),
            "graph_projection_state": row["graph_projection_state"],
            "graph_projection_error": row["graph_projection_error"],
            "updated_at": row["updated_at"],
        }

    def record_error_occurrence(
        self,
        occurrence: Mapping[str, Any],
    ) -> dict[str, Any]:
        required = (
            "occurrence_id",
            "fingerprint",
            "project_id",
            "operation",
            "component",
            "error_type",
            "error",
            "root_cause",
            "occurred_at",
        )
        missing = [
            field for field in required
            if not str(occurrence.get(field) or "").strip()
        ]
        if missing:
            raise LedgerError("occurrence_fields_missing", ",".join(missing))
        occurrence_id = str(occurrence["occurrence_id"]).strip()
        fingerprint = str(occurrence["fingerprint"]).strip().casefold()
        occurred_at = _parse_time(occurrence["occurred_at"])
        with self._transaction() as connection:
            existing_occurrence = connection.execute(
                """
                SELECT fingerprint FROM error_occurrences
                WHERE occurrence_id=?
                """,
                (occurrence_id,),
            ).fetchone()
            if existing_occurrence:
                if existing_occurrence["fingerprint"] != fingerprint:
                    raise LedgerError("occurrence_id_conflict")
                row = connection.execute(
                    "SELECT * FROM error_cases WHERE fingerprint=?",
                    (fingerprint,),
                ).fetchone()
                return {
                    "idempotent": True,
                    "case": self._error_case_row(row),
                }

            row = connection.execute(
                "SELECT * FROM error_cases WHERE fingerprint=?",
                (fingerprint,),
            ).fetchone()
            if row:
                frozen = (
                    row["project_id"],
                    row["operation"],
                    row["component"],
                    row["error_type"],
                )
                supplied = (
                    str(occurrence["project_id"]).strip(),
                    str(occurrence["operation"]).strip(),
                    str(occurrence["component"]).strip(),
                    str(occurrence["error_type"]).strip(),
                )
                if frozen != supplied:
                    raise LedgerError("fingerprint_identity_conflict")
                count = int(row["occurrence_count"]) + 1
                state = "regressed" if row["state"] == "resolved" else row["state"]
                case_id = row["case_id"] or (
                    f"ERR-case-{fingerprint.split(':', 1)[-1][:24]}"
                    if count >= 2 else None
                )
                promoted_at = row["promoted_at"] or (
                    _iso(occurred_at) if count >= 2 else None
                )
                connection.execute(
                    """
                    UPDATE error_cases
                    SET case_id=?, occurrence_count=?, state=?, last_seen_at=?,
                        promoted_at=?, error_text=?, root_cause=?,
                        graph_projection_state='pending',
                        graph_projection_error=NULL, updated_at=?
                    WHERE fingerprint=?
                    """,
                    (
                        case_id,
                        count,
                        state,
                        _iso(occurred_at),
                        promoted_at,
                        str(occurrence["error"]).strip(),
                        str(occurrence["root_cause"]).strip(),
                        _iso(),
                        fingerprint,
                    ),
                )
            else:
                count = 1
                case_id = None
                connection.execute(
                    """
                    INSERT INTO error_cases (
                        fingerprint, case_id, project_id, operation, component,
                        error_type, error_text, root_cause, occurrence_count, state, first_seen_at,
                        last_seen_at, promoted_at, resolution_refs_json,
                        graph_projection_state, updated_at
                    ) VALUES (?, NULL, ?, ?, ?, ?, ?, ?, 1, 'observed', ?, ?, NULL,
                              '[]', 'not_promoted', ?)
                    """,
                    (
                        fingerprint,
                        str(occurrence["project_id"]).strip(),
                        str(occurrence["operation"]).strip(),
                        str(occurrence["component"]).strip(),
                        str(occurrence["error_type"]).strip(),
                        str(occurrence["error"]).strip(),
                        str(occurrence["root_cause"]).strip(),
                        _iso(occurred_at),
                        _iso(occurred_at),
                        _iso(),
                    ),
                )
            connection.execute(
                """
                INSERT INTO error_occurrences (
                    occurrence_id, fingerprint, occurred_at, payload_json
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    occurrence_id,
                    fingerprint,
                    _iso(occurred_at),
                    canonical_json(dict(occurrence)),
                ),
            )
            row = connection.execute(
                "SELECT * FROM error_cases WHERE fingerprint=?",
                (fingerprint,),
            ).fetchone()
            case = self._error_case_row(row)
            if case["promoted"]:
                connection.execute(
                    """
                    INSERT INTO error_projection_journal (
                        fingerprint, desired_json, state, updated_at
                    ) VALUES (?, ?, 'pending', ?)
                    ON CONFLICT(fingerprint) DO UPDATE SET
                        desired_json=excluded.desired_json,
                        state='pending',
                        last_error=NULL,
                        updated_at=excluded.updated_at
                    """,
                    (fingerprint, canonical_json(case), _iso()),
                )
            return {"idempotent": False, "case": case}

    def mark_error_projection(
        self,
        fingerprint: str,
        *,
        state: str,
        error: str | None = None,
    ) -> None:
        if state not in {"projected", "partial", "pending"}:
            raise LedgerError("invalid_projection_state")
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE error_cases
                SET graph_projection_state=?, graph_projection_error=?,
                    updated_at=?
                WHERE fingerprint=?
                """,
                (state, error, _iso(), fingerprint),
            )
            connection.execute(
                """
                UPDATE error_projection_journal
                SET state=?, last_error=?, updated_at=?
                WHERE fingerprint=?
                """,
                (state, error, _iso(), fingerprint),
            )

    def error_case(
        self,
        *,
        fingerprint: str | None = None,
        case_id: str | None = None,
    ) -> dict[str, Any] | None:
        if not fingerprint and not case_id:
            raise LedgerError("fingerprint_or_case_id_required")
        connection = self._connect()
        try:
            if fingerprint:
                row = connection.execute(
                    "SELECT * FROM error_cases WHERE fingerprint=?",
                    (fingerprint.casefold(),),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM error_cases WHERE case_id=?",
                    (case_id,),
                ).fetchone()
            return self._error_case_row(row) if row else None
        finally:
            connection.close()

    def error_occurrence(self, occurrence_id: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT payload_json FROM error_occurrences
                WHERE occurrence_id=?
                """,
                (occurrence_id,),
            ).fetchone()
            return json.loads(row["payload_json"]) if row else None
        finally:
            connection.close()

    def reconcile_graph_error_case(
        self,
        payload: Mapping[str, Any],
        *,
        resolution_refs: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Import identity only; graph state and resolution are not authoritative."""

        del resolution_refs
        fingerprint = str(payload.get("fingerprint") or "").strip().casefold()
        case_id = str(payload.get("case_id") or "").strip()
        if not fingerprint:
            raise LedgerError("graph_case_fingerprint_missing")
        if not case_id:
            raise LedgerError("graph_case_id_missing")
        if not re.fullmatch(r"ek2:[0-9a-f]{64}", fingerprint):
            raise LedgerError("graph_case_fingerprint_invalid")
        if case_id != f"ERR-case-{fingerprint.split(':', 1)[1][:24]}":
            raise LedgerError("graph_case_id_fingerprint_mismatch")
        count = 2
        first_seen = str(payload.get("first_seen_at") or _iso())
        last_seen = str(payload.get("last_seen_at") or first_seen)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM error_cases WHERE fingerprint=?",
                (fingerprint,),
            ).fetchone()
            if row:
                connection.execute(
                    """
                    UPDATE error_cases
                    SET case_id=COALESCE(case_id, ?),
                        graph_projection_state='graph_seen',
                        graph_projection_error=NULL, updated_at=?
                    WHERE fingerprint=?
                    """,
                    (
                        case_id,
                        _iso(),
                        fingerprint,
                    ),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO error_cases (
                        fingerprint, case_id, project_id, operation, component,
                        error_type, error_text, root_cause, occurrence_count, state, first_seen_at,
                        last_seen_at, promoted_at, resolution_json,
                        resolution_refs_json, graph_projection_state, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'projected', ?)
                    """,
                    (
                        fingerprint,
                        case_id,
                        str(payload.get("project_id") or "legacy-unspecified"),
                        str(payload.get("operation") or "legacy-unspecified"),
                        str(payload.get("component") or "legacy-unspecified"),
                        str(payload.get("error_type") or "legacy-unspecified"),
                        str(payload.get("error") or "legacy-unspecified"),
                        str(payload.get("root_cause") or "legacy-unspecified"),
                        count,
                        "observed",
                        first_seen,
                        last_seen,
                        str(payload.get("promoted_at") or first_seen),
                        None,
                        canonical_json([]),
                        _iso(),
                    ),
                )
            row = connection.execute(
                "SELECT * FROM error_cases WHERE fingerprint=?",
                (fingerprint,),
            ).fetchone()
            return self._error_case_row(row)

    def resolve_error_cases(
        self,
        resolutions: Sequence[Mapping[str, Any]],
    ) -> None:
        with self._transaction() as connection:
            for resolution in resolutions:
                case_id = str(resolution.get("case_id") or "")
                row = connection.execute(
                    "SELECT * FROM error_cases WHERE case_id=?",
                    (case_id,),
                ).fetchone()
                if not row:
                    raise LedgerError("error_case_not_in_ledger", case_id)
                refs = json.loads(row["resolution_refs_json"])
                resolution_id = str(resolution.get("resolution_id") or "")
                if resolution_id and resolution_id not in refs:
                    refs.append(resolution_id)
                connection.execute(
                    """
                    UPDATE error_cases
                    SET state='resolved', resolution_json=?,
                        resolution_refs_json=?, graph_projection_state='projected',
                        graph_projection_error=NULL, updated_at=?
                    WHERE case_id=?
                    """,
                    (
                        canonical_json(dict(resolution)),
                        canonical_json(refs),
                        _iso(),
                        case_id,
                    ),
                )

    def events(self, ticket_id: str | None = None) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            if ticket_id is None:
                rows = connection.execute(
                    "SELECT * FROM ticket_events ORDER BY event_id"
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM ticket_events
                    WHERE ticket_id=? ORDER BY event_id
                    """,
                    (ticket_id,),
                ).fetchall()
            return [
                {
                    "event_id": int(row["event_id"]),
                    "event": row["event_type"],
                    "ticket_id": row["ticket_id"],
                    "timestamp": row["created_at"],
                    "agent_id": row["agent_id"],
                    "project_id": row["project_id"],
                    "workspace_id": row["workspace_id"],
                    "workorder_id": row["workorder_id"],
                    "target_digest": row["target_digest"],
                    "scope_digest": row["scope_digest"],
                    "policy_version": row["policy_version"],
                    "allowed_error_ids": json.loads(row["allowed_error_ids_json"]),
                    "details": json.loads(row["payload_json"]),
                }
                for row in rows
            ]
        finally:
            connection.close()

    def journal(self, ticket_id: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM completion_journal WHERE ticket_id=?",
                (ticket_id,),
            ).fetchone()
            if not row:
                return None
            return {
                "ticket_id": row["ticket_id"],
                "request_hash": row["request_hash"],
                "stage": row["stage"],
                "context": json.loads(row["context_json"]),
                "response": (
                    json.loads(row["response_json"])
                    if row["response_json"]
                    else None
                ),
                "last_error": row["last_error"],
            }
        finally:
            connection.close()

    def active_count(self) -> int:
        with self._transaction() as connection:
            self._expire_locked(connection, _utc_now().timestamp())
            return int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM tickets
                    WHERE state IN ('issued', 'consumed', 'completing')
                    """
                ).fetchone()[0]
            )


__all__ = [
    "LedgerError",
    "TicketLedger",
    "canonical_hash",
    "canonical_json",
]
