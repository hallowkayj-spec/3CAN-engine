"""Standalone error knowledge primitives for 3CAN.

This module deliberately has no runtime or third-party dependencies.  It owns
the small, deterministic contract that a helper, API adapter, or graph backend
can integrate:

* failures are append-only occurrences;
* the second exact occurrence promotes a reusable error case;
* only promoted, unresolved, exact cases can block a retry;
* resolution is explicit and requires verified evidence;
* a post-resolution occurrence reopens the case as ``regressed``;
* normal (non-error) routes never receive error cards.

The module does not write files, call the network, or mutate a graph.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Optional, Sequence


SCHEMA_VERSION = "3can.error-knowledge/v2"
FINGERPRINT_VERSION = "ek2"
ROUTE_CARD_SCHEMA_VERSION = "3can.error-route-card/v1"
PROMOTION_THRESHOLD = 2
MAX_ROUTE_CARDS = 3

UTC = timezone.utc

_QUOTED_URL_RE = re.compile(
    r"""(?P<quote>["'])(?:https?|file)://.*?(?P=quote)""",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"(?i)\b(?:https?|file)://[^\s\"']+")
_QUOTED_PATH_RE = re.compile(
    r"""(?P<quote>["'])(?:(?:[a-z]:[\\/])|(?:\\\\)|/|~[\\/]).*?(?P=quote)""",
    re.IGNORECASE,
)
_WINDOWS_PATH_RE = re.compile(
    r"(?i)(?<![\w])(?:[a-z]:[\\/]|\\\\|~[\\/])[^\r\n,;|<>\"']+"
)
_POSIX_PATH_RE = re.compile(
    r"(?<![\w:])(?:~/|/)[^\r\n,;|<>\"']+"
)
_UUID_RE = re.compile(
    r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\b"
)
_TICKET_RE = re.compile(r"(?i)\b(?:rt|ticket)[_-][a-z0-9_-]{5,}\b")
_LONG_HEX_RE = re.compile(r"(?i)\b(?:0x)?[0-9a-f]{12,}\b")
_ISO_TIME_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[t ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
    r"(?:z|[+-]\d{2}:\d{2})?\b",
    re.IGNORECASE,
)
_LONG_NUMBER_RE = re.compile(r"\b\d{6,}\b")
_WHITESPACE_RE = re.compile(r"\s+")
_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)

_ENGLISH_STRONG_ERROR_INTENT_TERMS = frozenset(
    {
        "error",
        "failed",
        "failure",
        "exception",
        "traceback",
        "crash",
        "broken",
    }
)
_ERROR_TOKEN_SUFFIXES = ("error", "exception", "failure")
_ENGLISH_METRIC_TERMS = frozenset(
    {
        "rate",
        "ratio",
        "metric",
        "metrics",
        "percentage",
        "percent",
        "budget",
        "dashboard",
        "kpi",
    }
)
_ENGLISH_TIMEOUT_CONFIGURATION_TERMS = frozenset(
    {
        "config",
        "configuration",
        "default",
        "duration",
        "limit",
        "policy",
        "setting",
        "settings",
        "threshold",
        "value",
    }
)
_CHINESE_ERROR_SIGNAL_TERMS = (
    "报错",
    "错误",
    "失败",
    "异常",
    "故障",
    "崩溃",
    "超时",
    "排错",
)
_CHINESE_METRIC_RE = re.compile(
    r"(?:报错|错误|失败|异常|故障|崩溃|超时)\s*(?:率|比例|指标|预算|看板|统计)"
)


def _require_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _canonical_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", _require_text(value, "value"))
    return _WHITESPACE_RE.sub(" ", text.strip().casefold())


def _redact_volatile(value: str) -> str:
    """Remove instance-specific data while retaining the semantic signal."""

    text = unicodedata.normalize("NFKC", _require_text(value, "value"))
    replacements = (
        (_QUOTED_URL_RE, "<url>"),
        (_URL_RE, "<url>"),
        (_QUOTED_PATH_RE, "<path>"),
        (_WINDOWS_PATH_RE, "<path>"),
        (_POSIX_PATH_RE, "<path>"),
        (_UUID_RE, "<id>"),
        (_TICKET_RE, "<ticket>"),
        (_ISO_TIME_RE, "<time>"),
        (_LONG_HEX_RE, "<id>"),
        (_LONG_NUMBER_RE, "<number>"),
    )
    for pattern, replacement in replacements:
        text = pattern.sub(replacement, text)
    return _canonical_text(text)


def _canonical_project_id(project_id: str) -> str:
    """Normalize a project identifier without retaining an absolute path."""

    text = unicodedata.normalize("NFKC", _require_text(project_id, "project_id"))
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        text = text[1:-1].strip()
    if re.search(r"(?i)^(?:[a-z]:[\\/]|\\\\|/|~[\\/])", text):
        parts = [part for part in re.split(r"[\\/]+", text) if part]
        text = parts[-1] if parts else text
    return _canonical_text(text)


def _canonical_component(component: str) -> str:
    """Return a stable component name without retaining an absolute path."""

    text = unicodedata.normalize("NFKC", _require_text(component, "component"))
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        text = text[1:-1].strip()
    if re.search(r"(?i)^(?:[a-z]:[\\/]|\\\\|/|~[\\/])", text):
        parts = [part for part in re.split(r"[\\/]+", text) if part]
        lowered = [part.casefold() for part in parts]
        for marker in ("users", "home", "documents and settings"):
            if marker in lowered:
                index = lowered.index(marker)
                parts = parts[index + 2:]
                break
        else:
            if text.startswith(("\\\\", "//")):
                parts = parts[3:]
        # Retain a privacy-safe logical owner plus basename. Keeping only the
        # basename would collapse e.g. backend/app.py and frontend/app.py into
        # one ErrorCase, while retaining the full path would expose usernames.
        text = "/".join(parts[-2:]) if parts else "<component>"
    return _redact_volatile(text)


def _resolved_error_type(
    *,
    error_type: Optional[str],
    error: Optional[str],
) -> str:
    if error_type is None and error is None:
        raise ValueError("error_type must be a non-empty string")
    if error_type is not None and error is not None:
        canonical_type = _redact_volatile(error_type)
        canonical_error = _redact_volatile(error)
        if canonical_type != canonical_error:
            raise ValueError("error_type and legacy error disagree")
        return canonical_type
    return _redact_volatile(error_type if error_type is not None else error or "")


def _canonical_identity(
    *,
    project_id: str,
    operation: str,
    component: str,
    error_type: str,
) -> dict[str, str]:
    return {
        "project_id": _canonical_project_id(project_id),
        "operation": _redact_volatile(operation),
        "component": _canonical_component(component),
        "error_type": _redact_volatile(error_type),
    }


def canonical_error_identity(
    *,
    project_id: str,
    operation: str,
    component: str,
    error_type: str,
) -> dict[str, str]:
    """Return the public-safe identity persisted by the ErrorKnowledge ledger."""

    return _canonical_identity(
        project_id=project_id,
        operation=operation,
        component=component,
        error_type=error_type,
    )


def deterministic_fingerprint(
    *,
    project_id: str,
    operation: str,
    component: str = "unknown-component",
    error_type: Optional[str] = None,
    error: Optional[str] = None,
    root_cause: Optional[str] = None,
) -> str:
    """Return a stable fingerprint over semantic identity fields.

    Identity is exactly ``project_id + operation + component + error_type``.
    ``root_cause`` is accepted for source compatibility but deliberately
    excluded because diagnosis can improve after an occurrence is recorded.
    Absolute paths, URLs, ticket ids, timestamps, long request ids, and other
    volatile values are redacted before hashing.  Raw command/context data is
    intentionally not accepted by this function and therefore cannot leak into
    the fingerprint.
    """

    del root_cause
    canonical = _canonical_identity(
        project_id=project_id,
        operation=operation,
        component=component,
        error_type=_resolved_error_type(
            error_type=error_type,
            error=error,
        ),
    )
    body = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"{FINGERPRINT_VERSION}:{hashlib.sha256(body).hexdigest()}"


def is_error_intent(text: str) -> bool:
    """Return whether text explicitly asks about an operational error.

    Generic actions (for example ``fix README formatting``), test execution,
    timeout configuration, and error/failure-rate metrics are intentionally not
    error-memory intent.  Callers in the graph and helper layers should use this
    function rather than maintaining their own trigger sets.
    """

    if not isinstance(text, str) or not text.strip():
        return False
    normalized = _canonical_text(text)
    if re.search(r"\berr-(?:case|repeated)-[a-z0-9]", normalized):
        return True

    chinese_without_metrics = _CHINESE_METRIC_RE.sub("", normalized)
    if any(
        term in chinese_without_metrics
        for term in _CHINESE_ERROR_SIGNAL_TERMS
    ):
        return True
    if re.search(r"\btimed\s+out\b", normalized):
        return True

    tokens = re.findall(r"[a-z0-9]+", normalized)
    for index, token in enumerate(tokens):
        previous = tokens[index - 1] if index else ""
        following = tokens[index + 1] if index + 1 < len(tokens) else ""
        if (
            token in {"error", "failure", "exception"}
            and (following in _ENGLISH_METRIC_TERMS or previous in _ENGLISH_METRIC_TERMS)
        ):
            continue
        if token == "timeout":
            if (
                following in _ENGLISH_TIMEOUT_CONFIGURATION_TERMS
                or previous in _ENGLISH_TIMEOUT_CONFIGURATION_TERMS
                or following in _ENGLISH_METRIC_TERMS
            ):
                continue
            if "timed out" in normalized:
                return True
            # A bare timeout can be a configuration topic. Require another
            # incident signal such as error/failed/exception.
            other_tokens = set(tokens) - {"timeout"}
            if not any(
                candidate in _ENGLISH_STRONG_ERROR_INTENT_TERMS
                for candidate in other_tokens
            ):
                continue
        if (
            token in _ENGLISH_STRONG_ERROR_INTENT_TERMS
            or token.endswith(_ERROR_TOKEN_SUFFIXES)
        ):
            return True
    return False


# Compatibility alias with one implementation. New graph/helper integrations
# should import ``is_error_intent``.
detect_error_intent = is_error_intent


def _coerce_datetime(
    value: Optional[datetime | str],
    *,
    field_name: str,
    default_now: bool = True,
) -> Optional[datetime]:
    if value is None:
        return datetime.now(UTC) if default_now else None
    if isinstance(value, str):
        candidate = value.strip()
        if candidate.endswith("Z"):
            candidate = candidate[:-1] + "+00:00"
        try:
            value = datetime.fromisoformat(candidate)
        except ValueError as exc:
            raise ValueError(f"{field_name} must be an ISO-8601 datetime") from exc
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime, ISO string, or None")
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _json_mapping(
    value: Optional[Mapping[str, Any]],
    *,
    field_name: str,
) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a mapping")
    copy = dict(value)
    try:
        return json.loads(json.dumps(copy, ensure_ascii=False, sort_keys=True))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must contain JSON-serializable values") from exc


def _strict_bool(value: Any, *, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{field_name} must be a bool")
    return value


def _nonnegative_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool):
        raise TypeError(f"{field_name} must be an int")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{field_name} must be an int") from exc
    if parsed < 0:
        raise ValueError(f"{field_name} must be non-negative")
    return parsed


class ErrorState(str, Enum):
    OBSERVED = "observed"
    DIAGNOSED = "diagnosed"
    MITIGATED = "mitigated"
    RESOLVED = "resolved"
    REGRESSED = "regressed"
    SUPERSEDED = "superseded"


@dataclass(frozen=True)
class ErrorIdentity:
    fingerprint: str
    project_id: str
    operation: str
    component: str
    error_type: str
    root_cause: str

    @property
    def error(self) -> str:
        """Legacy name for ``error_type``."""

        return self.error_type

    @classmethod
    def from_signals(
        cls,
        *,
        project_id: str,
        operation: str,
        component: str = "unknown-component",
        error_type: Optional[str] = None,
        error: Optional[str] = None,
        root_cause: str,
    ) -> "ErrorIdentity":
        resolved_error_type = _resolved_error_type(
            error_type=error_type,
            error=error,
        )
        canonical = _canonical_identity(
            project_id=project_id,
            operation=operation,
            component=component,
            error_type=resolved_error_type,
        )
        return cls(
            fingerprint=deterministic_fingerprint(
                project_id=project_id,
                operation=operation,
                component=component,
                error_type=resolved_error_type,
                root_cause=root_cause,
            ),
            root_cause=_redact_volatile(root_cause),
            **canonical,
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "fingerprint_version": FINGERPRINT_VERSION,
            "fingerprint": self.fingerprint,
            "project_id": self.project_id,
            "operation": self.operation,
            "component": self.component,
            "error_type": self.error_type,
        }


@dataclass
class ErrorOccurrence:
    occurrence_id: str
    fingerprint: str
    project_id: str
    operation: str
    component: str
    error_type: str
    root_cause: str
    occurred_at: datetime
    sequence: int
    context: dict[str, Any] = field(default_factory=dict)
    case_id: Optional[str] = None

    @property
    def error(self) -> str:
        """Legacy name for ``error_type``."""

        return self.error_type

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "3can.error-occurrence/v1",
            "occurrence_id": self.occurrence_id,
            "case_id": self.case_id,
            "fingerprint": self.fingerprint,
            "fingerprint_version": FINGERPRINT_VERSION,
            "project_id": self.project_id,
            "operation": self.operation,
            "component": self.component,
            "error_type": self.error_type,
            "root_cause": self.root_cause,
            "occurred_at": _iso(self.occurred_at),
            "sequence": self.sequence,
            "context": self.context,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ErrorOccurrence":
        return cls(
            occurrence_id=str(payload["occurrence_id"]),
            case_id=(
                str(payload["case_id"]) if payload.get("case_id") is not None else None
            ),
            fingerprint=str(payload["fingerprint"]),
            project_id=str(payload["project_id"]),
            operation=str(payload["operation"]),
            component=str(payload.get("component") or "unknown-component"),
            error_type=str(payload.get("error_type") or payload["error"]),
            root_cause=str(payload["root_cause"]),
            occurred_at=_coerce_datetime(
                payload["occurred_at"],
                field_name="occurred_at",
            ),
            sequence=_nonnegative_int(
                payload["sequence"],
                field_name="sequence",
            ),
            context=_json_mapping(payload.get("context"), field_name="context"),
        )


@dataclass(frozen=True)
class ResolutionEvidence:
    kind: str
    reference: str
    summary: str
    verified: bool
    verified_at: datetime
    digest: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", _canonical_text(self.kind))
        object.__setattr__(
            self,
            "reference",
            _require_text(self.reference, "reference").strip(),
        )
        object.__setattr__(
            self,
            "summary",
            _require_text(self.summary, "summary").strip(),
        )
        object.__setattr__(
            self,
            "verified_at",
            _coerce_datetime(self.verified_at, field_name="verified_at"),
        )
        if not isinstance(self.verified, bool):
            raise TypeError("verified must be a bool")
        if self.digest is not None:
            object.__setattr__(self, "digest", _require_text(self.digest, "digest"))
        object.__setattr__(
            self,
            "metadata",
            _json_mapping(self.metadata, field_name="metadata"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "reference": self.reference,
            "summary": self.summary,
            "verified": self.verified,
            "verified_at": _iso(self.verified_at),
            "digest": self.digest,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ResolutionEvidence":
        return cls(
            kind=str(payload["kind"]),
            reference=str(payload["reference"]),
            summary=str(payload["summary"]),
            verified=_strict_bool(
                payload["verified"],
                field_name="verified",
            ),
            verified_at=_coerce_datetime(
                payload["verified_at"],
                field_name="verified_at",
            ),
            digest=(
                str(payload["digest"]) if payload.get("digest") is not None else None
            ),
            metadata=_json_mapping(payload.get("metadata"), field_name="metadata"),
        )


@dataclass(frozen=True)
class ResolutionRecord:
    resolution_id: str
    solution_summary: str
    evidence: tuple[ResolutionEvidence, ...]
    resolved_by: str
    resolved_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "resolution_id": self.resolution_id,
            "solution_summary": self.solution_summary,
            "evidence": [item.to_dict() for item in self.evidence],
            "resolved_by": self.resolved_by,
            "resolved_at": _iso(self.resolved_at),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ResolutionRecord":
        return cls(
            resolution_id=str(payload["resolution_id"]),
            solution_summary=str(payload["solution_summary"]),
            evidence=tuple(
                ResolutionEvidence.from_dict(item)
                for item in payload.get("evidence", [])
            ),
            resolved_by=str(payload["resolved_by"]),
            resolved_at=_coerce_datetime(
                payload["resolved_at"],
                field_name="resolved_at",
            ),
        )


@dataclass
class ErrorCase:
    case_id: str
    fingerprint: str
    project_id: str
    operation: str
    component: str
    error_type: str
    root_cause: str
    state: ErrorState
    occurrence_count: int
    first_seen_at: datetime
    last_seen_at: datetime
    promoted_at: datetime
    state_changed_at: datetime
    diagnosis: Optional[str] = None
    diagnosed_by: Optional[str] = None
    diagnosed_at: Optional[datetime] = None
    mitigation: Optional[str] = None
    mitigated_by: Optional[str] = None
    mitigated_at: Optional[datetime] = None
    active_resolution: Optional[ResolutionRecord] = None
    resolution_history: list[ResolutionRecord] = field(default_factory=list)
    regression_count: int = 0
    superseded_by: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def error(self) -> str:
        """Legacy name for ``error_type``."""

        return self.error_type

    @property
    def blocking(self) -> bool:
        return (
            self.occurrence_count >= PROMOTION_THRESHOLD
            and self.state not in {ErrorState.RESOLVED, ErrorState.SUPERSEDED}
        )

    @property
    def applicability(self) -> dict[str, str]:
        return {
            "project_id": self.project_id,
            "operation": self.operation,
            "component": self.component,
            "error_type": self.error_type,
            "fingerprint_version": FINGERPRINT_VERSION,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "3can.error-case/v1",
            "case_id": self.case_id,
            "fingerprint": self.fingerprint,
            "fingerprint_version": FINGERPRINT_VERSION,
            "project_id": self.project_id,
            "operation": self.operation,
            "component": self.component,
            "error_type": self.error_type,
            "root_cause": self.root_cause,
            "applicability": self.applicability,
            "state": self.state.value,
            "blocking": self.blocking,
            "occurrence_count": self.occurrence_count,
            "first_seen_at": _iso(self.first_seen_at),
            "last_seen_at": _iso(self.last_seen_at),
            "promoted_at": _iso(self.promoted_at),
            "state_changed_at": _iso(self.state_changed_at),
            "diagnosis": self.diagnosis,
            "diagnosed_by": self.diagnosed_by,
            "diagnosed_at": _iso(self.diagnosed_at),
            "mitigation": self.mitigation,
            "mitigated_by": self.mitigated_by,
            "mitigated_at": _iso(self.mitigated_at),
            "active_resolution": (
                self.active_resolution.to_dict() if self.active_resolution else None
            ),
            "resolution_history": [
                record.to_dict() for record in self.resolution_history
            ],
            "regression_count": self.regression_count,
            "superseded_by": self.superseded_by,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ErrorCase":
        history = [
            ResolutionRecord.from_dict(item)
            for item in payload.get("resolution_history", [])
        ]
        active_payload = payload.get("active_resolution")
        active = (
            ResolutionRecord.from_dict(active_payload)
            if isinstance(active_payload, Mapping)
            else None
        )
        if active is not None:
            historical = {
                record.resolution_id: record for record in history
            }.get(active.resolution_id)
            if historical is not None:
                active = historical
        return cls(
            case_id=str(payload["case_id"]),
            fingerprint=str(payload["fingerprint"]),
            project_id=str(payload["project_id"]),
            operation=str(payload["operation"]),
            component=str(payload.get("component") or "unknown-component"),
            error_type=str(payload.get("error_type") or payload["error"]),
            root_cause=str(payload["root_cause"]),
            state=ErrorState(str(payload["state"])),
            occurrence_count=_nonnegative_int(
                payload["occurrence_count"],
                field_name="occurrence_count",
            ),
            first_seen_at=_coerce_datetime(
                payload["first_seen_at"],
                field_name="first_seen_at",
            ),
            last_seen_at=_coerce_datetime(
                payload["last_seen_at"],
                field_name="last_seen_at",
            ),
            promoted_at=_coerce_datetime(
                payload["promoted_at"],
                field_name="promoted_at",
            ),
            state_changed_at=_coerce_datetime(
                payload["state_changed_at"],
                field_name="state_changed_at",
            ),
            diagnosis=(
                str(payload["diagnosis"])
                if payload.get("diagnosis") is not None
                else None
            ),
            diagnosed_by=(
                str(payload["diagnosed_by"])
                if payload.get("diagnosed_by") is not None
                else None
            ),
            diagnosed_at=_coerce_datetime(
                payload.get("diagnosed_at"),
                field_name="diagnosed_at",
                default_now=False,
            ),
            mitigation=(
                str(payload["mitigation"])
                if payload.get("mitigation") is not None
                else None
            ),
            mitigated_by=(
                str(payload["mitigated_by"])
                if payload.get("mitigated_by") is not None
                else None
            ),
            mitigated_at=_coerce_datetime(
                payload.get("mitigated_at"),
                field_name="mitigated_at",
                default_now=False,
            ),
            active_resolution=active,
            resolution_history=history,
            regression_count=_nonnegative_int(
                payload.get("regression_count", 0),
                field_name="regression_count",
            ),
            superseded_by=(
                str(payload["superseded_by"])
                if payload.get("superseded_by") is not None
                else None
            ),
            metadata=_json_mapping(payload.get("metadata"), field_name="metadata"),
        )


@dataclass(frozen=True)
class BlockDecision:
    blocked: bool
    reason: str
    fingerprint: str
    case_id: Optional[str]
    state: Optional[ErrorState]
    occurrence_count: int
    exact_match: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "3can.error-block-decision/v1",
            "blocked": self.blocked,
            "reason": self.reason,
            "fingerprint": self.fingerprint,
            "case_id": self.case_id,
            "state": self.state.value if self.state else None,
            "occurrence_count": self.occurrence_count,
            "exact_match": self.exact_match,
            "promotion_threshold": PROMOTION_THRESHOLD,
        }


@dataclass(frozen=True)
class RecordResult:
    occurrence: ErrorOccurrence
    occurrence_count: int
    promoted: bool
    reopened: bool
    case: Optional[ErrorCase]
    block_decision: BlockDecision

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "3can.error-record-result/v1",
            "occurrence": self.occurrence.to_dict(),
            "occurrence_count": self.occurrence_count,
            "promoted": self.promoted,
            "reopened": self.reopened,
            "case": self.case.to_dict() if self.case else None,
            "block_decision": self.block_decision.to_dict(),
        }


@dataclass(frozen=True)
class ErrorRouteCard:
    case_id: str
    fingerprint: str
    state: ErrorState
    occurrence_count: int
    applicability: Mapping[str, str]
    cause_summary: str
    diagnosis: Optional[str]
    mitigation: Optional[str]
    solution_summary: Optional[str]
    evidence: tuple[ResolutionEvidence, ...]
    blocking: bool
    block_decision: BlockDecision
    route_score: float
    regression_count: int
    last_seen_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": ROUTE_CARD_SCHEMA_VERSION,
            "case_id": self.case_id,
            "fingerprint": self.fingerprint,
            "fingerprint_version": FINGERPRINT_VERSION,
            "state": self.state.value,
            "occurrence_count": self.occurrence_count,
            "applicability": dict(self.applicability),
            "cause_summary": self.cause_summary,
            "diagnosis": self.diagnosis,
            "mitigation": self.mitigation,
            "solution_summary": self.solution_summary,
            "evidence": [item.to_dict() for item in self.evidence],
            "blocking": self.blocking,
            "block_decision": self.block_decision.to_dict(),
            "route_score": self.route_score,
            "regression_count": self.regression_count,
            "last_seen_at": _iso(self.last_seen_at),
        }


class ErrorKnowledgeCore:
    """In-memory, serializable error occurrence/case store."""

    def __init__(self) -> None:
        self._occurrences: list[ErrorOccurrence] = []
        self._occurrences_by_fingerprint: dict[str, list[ErrorOccurrence]] = {}
        self._base_count_by_fingerprint: dict[str, int] = {}
        self._max_sequence_by_fingerprint: dict[str, int] = {}
        self._first_seen_by_fingerprint: dict[str, datetime] = {}
        self._last_seen_by_fingerprint: dict[str, datetime] = {}
        self._cases_by_fingerprint: dict[str, ErrorCase] = {}
        self._cases_by_id: dict[str, ErrorCase] = {}

    @property
    def occurrence_count(self) -> int:
        return sum(self._base_count_by_fingerprint.values()) + len(
            self._occurrences
        )

    @property
    def unique_fingerprint_count(self) -> int:
        return len(
            set(self._base_count_by_fingerprint)
            | set(self._occurrences_by_fingerprint)
            | set(self._cases_by_fingerprint)
        )

    @property
    def case_count(self) -> int:
        return len(self._cases_by_id)

    def cases(self) -> tuple[ErrorCase, ...]:
        return tuple(
            self._cases_by_id[key] for key in sorted(self._cases_by_id)
        )

    def occurrences(self) -> tuple[ErrorOccurrence, ...]:
        return tuple(self._occurrences)

    def _count_for(self, fingerprint: str) -> int:
        return self._base_count_by_fingerprint.get(fingerprint, 0) + len(
            self._occurrences_by_fingerprint.get(fingerprint, ())
        )

    def get_case(self, case_id_or_fingerprint: str) -> ErrorCase:
        case = self._cases_by_id.get(case_id_or_fingerprint)
        if case is None:
            case = self._cases_by_fingerprint.get(case_id_or_fingerprint)
        if case is None:
            raise KeyError(f"unknown error case: {case_id_or_fingerprint}")
        return case

    def record_occurrence(
        self,
        *,
        project_id: str,
        operation: str,
        component: str = "unknown-component",
        error_type: Optional[str] = None,
        error: Optional[str] = None,
        root_cause: str,
        occurred_at: Optional[datetime | str] = None,
        context: Optional[Mapping[str, Any]] = None,
    ) -> RecordResult:
        identity = ErrorIdentity.from_signals(
            project_id=project_id,
            operation=operation,
            component=component,
            error_type=error_type,
            error=error,
            root_cause=root_cause,
        )
        timestamp = _coerce_datetime(occurred_at, field_name="occurred_at")
        bucket = self._occurrences_by_fingerprint.setdefault(
            identity.fingerprint,
            [],
        )
        sequence = self._max_sequence_by_fingerprint.get(
            identity.fingerprint,
            0,
        ) + 1
        total_count = self._count_for(identity.fingerprint) + 1
        occurrence = ErrorOccurrence(
            occurrence_id=(
                f"OCC-{identity.fingerprint.split(':', 1)[1][:20]}-{sequence:06d}"
            ),
            fingerprint=identity.fingerprint,
            project_id=identity.project_id,
            operation=identity.operation,
            component=identity.component,
            error_type=identity.error_type,
            root_cause=identity.root_cause,
            occurred_at=timestamp,
            sequence=sequence,
            context=_json_mapping(context, field_name="context"),
        )
        bucket.append(occurrence)
        self._occurrences.append(occurrence)
        self._max_sequence_by_fingerprint[identity.fingerprint] = sequence
        self._first_seen_by_fingerprint.setdefault(
            identity.fingerprint,
            timestamp,
        )
        self._last_seen_by_fingerprint[identity.fingerprint] = timestamp

        promoted = False
        reopened = False
        case = self._cases_by_fingerprint.get(identity.fingerprint)
        if case is None and total_count >= PROMOTION_THRESHOLD:
            promoted = True
            case = ErrorCase(
                case_id=(
                    f"ERR-case-{identity.fingerprint.split(':', 1)[1][:24]}"
                ),
                fingerprint=identity.fingerprint,
                project_id=identity.project_id,
                operation=identity.operation,
                component=identity.component,
                error_type=identity.error_type,
                root_cause=identity.root_cause,
                state=ErrorState.OBSERVED,
                occurrence_count=total_count,
                first_seen_at=self._first_seen_by_fingerprint[
                    identity.fingerprint
                ],
                last_seen_at=timestamp,
                promoted_at=timestamp,
                state_changed_at=timestamp,
            )
            self._cases_by_fingerprint[identity.fingerprint] = case
            self._cases_by_id[case.case_id] = case
            for prior in bucket:
                prior.case_id = case.case_id
        elif case is not None:
            occurrence.case_id = case.case_id
            case.occurrence_count = total_count
            case.last_seen_at = timestamp
            if identity.root_cause != case.root_cause:
                history = case.metadata.setdefault("root_cause_history", [])
                if (
                    isinstance(history, list)
                    and case.root_cause
                    and case.root_cause not in history
                ):
                    history.append(case.root_cause)
                case.root_cause = identity.root_cause
            if case.state is ErrorState.RESOLVED:
                case.state = ErrorState.REGRESSED
                case.state_changed_at = timestamp
                case.active_resolution = None
                case.regression_count += 1
                reopened = True

        decision = self._decision_for(identity.fingerprint, total_count)
        return RecordResult(
            occurrence=occurrence,
            occurrence_count=total_count,
            promoted=promoted,
            reopened=reopened,
            case=case,
            block_decision=decision,
        )

    def _decision_for(
        self,
        fingerprint: str,
        occurrence_count: int,
    ) -> BlockDecision:
        case = self._cases_by_fingerprint.get(fingerprint)
        if case is None:
            return BlockDecision(
                blocked=False,
                reason="no_promoted_exact_case",
                fingerprint=fingerprint,
                case_id=None,
                state=None,
                occurrence_count=occurrence_count,
            )
        if case.state is ErrorState.RESOLVED:
            reason = "exact_case_resolved"
            blocked = False
        elif case.state is ErrorState.SUPERSEDED:
            reason = "exact_case_superseded"
            blocked = False
        else:
            reason = "exact_unresolved_repeated_operation"
            blocked = case.occurrence_count >= PROMOTION_THRESHOLD
        return BlockDecision(
            blocked=blocked,
            reason=reason,
            fingerprint=fingerprint,
            case_id=case.case_id,
            state=case.state,
            occurrence_count=case.occurrence_count,
        )

    def should_block_retry(
        self,
        *,
        project_id: str,
        operation: str,
        component: str = "unknown-component",
        error_type: Optional[str] = None,
        error: Optional[str] = None,
        root_cause: str = "unclassified-root-cause",
    ) -> BlockDecision:
        identity = ErrorIdentity.from_signals(
            project_id=project_id,
            operation=operation,
            component=component,
            error_type=error_type,
            error=error,
            root_cause=root_cause,
        )
        count = self._count_for(identity.fingerprint)
        return self._decision_for(identity.fingerprint, count)

    def diagnose(
        self,
        case_id_or_fingerprint: str,
        *,
        diagnosis: str,
        diagnosed_by: str,
        diagnosed_at: Optional[datetime | str] = None,
    ) -> ErrorCase:
        case = self.get_case(case_id_or_fingerprint)
        if case.state not in {
            ErrorState.OBSERVED,
            ErrorState.DIAGNOSED,
            ErrorState.REGRESSED,
        }:
            raise ValueError(f"cannot diagnose a {case.state.value} case")
        timestamp = _coerce_datetime(diagnosed_at, field_name="diagnosed_at")
        case.diagnosis = _require_text(diagnosis, "diagnosis").strip()
        case.diagnosed_by = _require_text(diagnosed_by, "diagnosed_by").strip()
        case.diagnosed_at = timestamp
        case.state = ErrorState.DIAGNOSED
        case.state_changed_at = timestamp
        return case

    def mitigate(
        self,
        case_id_or_fingerprint: str,
        *,
        mitigation: str,
        mitigated_by: str,
        mitigated_at: Optional[datetime | str] = None,
    ) -> ErrorCase:
        case = self.get_case(case_id_or_fingerprint)
        if case.state not in {
            ErrorState.OBSERVED,
            ErrorState.DIAGNOSED,
            ErrorState.MITIGATED,
            ErrorState.REGRESSED,
        }:
            raise ValueError(f"cannot mitigate a {case.state.value} case")
        timestamp = _coerce_datetime(mitigated_at, field_name="mitigated_at")
        case.mitigation = _require_text(mitigation, "mitigation").strip()
        case.mitigated_by = _require_text(mitigated_by, "mitigated_by").strip()
        case.mitigated_at = timestamp
        case.state = ErrorState.MITIGATED
        case.state_changed_at = timestamp
        return case

    def resolve(
        self,
        case_id_or_fingerprint: str,
        *,
        solution_summary: str,
        evidence: Sequence[ResolutionEvidence],
        resolved_by: str,
        resolved_at: Optional[datetime | str] = None,
    ) -> ErrorCase:
        case = self.get_case(case_id_or_fingerprint)
        if case.state is ErrorState.SUPERSEDED:
            raise ValueError("cannot resolve a superseded case")
        if case.state is ErrorState.RESOLVED:
            raise ValueError("case is already resolved")
        evidence_tuple = tuple(evidence)
        if not evidence_tuple or any(
            not isinstance(item, ResolutionEvidence) or not item.verified
            for item in evidence_tuple
        ):
            raise ValueError(
                "resolution requires at least one verified evidence item"
            )
        timestamp = _coerce_datetime(resolved_at, field_name="resolved_at")
        solution = _require_text(solution_summary, "solution_summary").strip()
        actor = _require_text(resolved_by, "resolved_by").strip()
        resolution = ResolutionRecord(
            resolution_id=(
                f"RES-{case.case_id.removeprefix('ERR-case-')}-"
                f"{len(case.resolution_history) + 1:03d}"
            ),
            solution_summary=solution,
            evidence=evidence_tuple,
            resolved_by=actor,
            resolved_at=timestamp,
        )
        case.resolution_history.append(resolution)
        case.active_resolution = resolution
        case.state = ErrorState.RESOLVED
        case.state_changed_at = timestamp
        return case

    def supersede(
        self,
        case_id_or_fingerprint: str,
        *,
        replacement_case_id: str,
        superseded_at: Optional[datetime | str] = None,
    ) -> ErrorCase:
        case = self.get_case(case_id_or_fingerprint)
        replacement = self.get_case(replacement_case_id)
        if case.case_id == replacement.case_id:
            raise ValueError("a case cannot supersede itself")
        timestamp = _coerce_datetime(
            superseded_at,
            field_name="superseded_at",
        )
        case.superseded_by = replacement.case_id
        case.state = ErrorState.SUPERSEDED
        case.state_changed_at = timestamp
        case.active_resolution = None
        return case

    def route(
        self,
        query: str,
        *,
        project_id: Optional[str] = None,
        operation: Optional[str] = None,
        component: Optional[str] = None,
        error_intent: Optional[bool] = None,
        limit: int = MAX_ROUTE_CARDS,
    ) -> list[ErrorRouteCard]:
        """Return at most three applicable error/solution cards.

        The caller can explicitly pass ``error_intent``.  Otherwise a
        conservative detector is used.  A false/ordinary intent always returns
        an empty list, even if the store contains highly activated cases.
        """

        if not isinstance(limit, int):
            raise TypeError("limit must be an int")
        if limit <= 0:
            return []
        effective_limit = min(limit, MAX_ROUTE_CARDS)
        has_error_intent = (
            is_error_intent(query) if error_intent is None else error_intent
        )
        if not isinstance(has_error_intent, bool):
            raise TypeError("error_intent must be a bool or None")
        if not has_error_intent:
            return []

        normalized_query = _canonical_text(query)
        query_tokens = set(_TOKEN_RE.findall(normalized_query))
        project_filter = (
            _canonical_project_id(project_id) if project_id is not None else None
        )
        operation_filter = (
            _redact_volatile(operation) if operation is not None else None
        )
        component_filter = (
            _canonical_component(component) if component is not None else None
        )
        ranked: list[tuple[float, str, ErrorCase]] = []

        for case in self._cases_by_id.values():
            if case.state is ErrorState.SUPERSEDED:
                continue
            if project_filter is not None and case.project_id != project_filter:
                continue
            if operation_filter is not None and case.operation != operation_filter:
                continue
            if component_filter is not None and case.component != component_filter:
                continue

            solution = (
                case.active_resolution.solution_summary
                if case.active_resolution is not None
                else ""
            )
            fields = (
                case.project_id,
                case.operation,
                case.component,
                case.error_type,
                case.root_cause,
                case.diagnosis or "",
                case.mitigation or "",
                solution,
            )
            field_tokens = set(_TOKEN_RE.findall(" ".join(fields)))
            overlap = query_tokens & field_tokens
            score = float(len(overlap))
            if case.error_type and case.error_type in normalized_query:
                score += 12.0
            if case.operation and case.operation in normalized_query:
                score += 8.0
            if project_filter is not None:
                score += 6.0
            if operation_filter is not None:
                score += 8.0
            if component_filter is not None:
                score += 8.0
            if case.state is ErrorState.RESOLVED and solution:
                score += 2.0

            # Explicit applicability filters can select a known case without
            # lexical overlap; broad error queries must still match its content.
            if not overlap and operation_filter is None:
                continue
            if score <= 0:
                continue
            ranked.append((score, case.case_id, case))

        ranked.sort(key=lambda item: (-item[0], item[1]))
        return [
            self._route_card(case, score)
            for score, _, case in ranked[:effective_limit]
        ]

    def _route_card(self, case: ErrorCase, score: float) -> ErrorRouteCard:
        decision = self._decision_for(case.fingerprint, case.occurrence_count)
        resolution = case.active_resolution
        return ErrorRouteCard(
            case_id=case.case_id,
            fingerprint=case.fingerprint,
            state=case.state,
            occurrence_count=case.occurrence_count,
            applicability=case.applicability,
            cause_summary=case.root_cause,
            diagnosis=case.diagnosis,
            mitigation=case.mitigation,
            solution_summary=(
                resolution.solution_summary if resolution is not None else None
            ),
            evidence=resolution.evidence if resolution is not None else (),
            blocking=decision.blocked,
            block_decision=decision,
            route_score=score,
            regression_count=case.regression_count,
            last_seen_at=case.last_seen_at,
        )

    def _occurrence_counters(
        self,
        *,
        include_occurrences: bool,
    ) -> list[dict[str, Any]]:
        fingerprints = sorted(
            set(self._base_count_by_fingerprint)
            | set(self._occurrences_by_fingerprint)
            | set(self._cases_by_fingerprint)
        )
        counters: list[dict[str, Any]] = []
        for fingerprint in fingerprints:
            in_memory = self._occurrences_by_fingerprint.get(fingerprint, [])
            base_count = self._base_count_by_fingerprint.get(fingerprint, 0)
            if not include_occurrences:
                base_count += len(in_memory)
            max_sequence = self._max_sequence_by_fingerprint.get(
                fingerprint,
                0,
            )
            if in_memory:
                max_sequence = max(
                    max_sequence,
                    max(item.sequence for item in in_memory),
                )
            counters.append(
                {
                    "fingerprint": fingerprint,
                    "base_count": base_count,
                    "max_sequence": max_sequence,
                    "first_seen_at": _iso(
                        self._first_seen_by_fingerprint.get(fingerprint)
                    ),
                    "last_seen_at": _iso(
                        self._last_seen_by_fingerprint.get(fingerprint)
                    ),
                }
            )
        return counters

    def to_dict(self, *, include_occurrences: bool = True) -> dict[str, Any]:
        _strict_bool(
            include_occurrences,
            field_name="include_occurrences",
        )
        payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "fingerprint_version": FINGERPRINT_VERSION,
            "promotion_threshold": PROMOTION_THRESHOLD,
            "max_route_cards": MAX_ROUTE_CARDS,
            "occurrences_included": include_occurrences,
            "occurrence_counters": self._occurrence_counters(
                include_occurrences=include_occurrences,
            ),
            "stats": {
                "occurrence_count": self.occurrence_count,
                "unique_fingerprint_count": self.unique_fingerprint_count,
                "case_count": self.case_count,
                "duplicate_occurrence_count": (
                    self.occurrence_count - self.unique_fingerprint_count
                ),
            },
            "cases": [case.to_dict() for case in self.cases()],
        }
        if include_occurrences:
            payload["occurrences"] = [
                occurrence.to_dict() for occurrence in self._occurrences
            ]
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ErrorKnowledgeCore":
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported schema_version: {payload.get('schema_version')!r}"
            )
        core = cls()
        occurrences_included = (
            _strict_bool(
                payload["occurrences_included"],
                field_name="occurrences_included",
            )
            if "occurrences_included" in payload
            else "occurrences" in payload
        )
        occurrence_payloads = payload.get("occurrences", [])
        if not isinstance(occurrence_payloads, list):
            raise TypeError("occurrences must be a list")
        if not occurrences_included and occurrence_payloads:
            raise ValueError(
                "occurrences_included=false cannot carry occurrence records"
            )

        counter_payloads = payload.get("occurrence_counters", [])
        if not isinstance(counter_payloads, list):
            raise TypeError("occurrence_counters must be a list")
        for counter_payload in counter_payloads:
            if not isinstance(counter_payload, Mapping):
                raise TypeError("occurrence counter must be a mapping")
            fingerprint = _require_text(
                str(counter_payload["fingerprint"]),
                "fingerprint",
            )
            base_count = _nonnegative_int(
                counter_payload.get("base_count", 0),
                field_name="base_count",
            )
            max_sequence = _nonnegative_int(
                counter_payload.get("max_sequence", 0),
                field_name="max_sequence",
            )
            if max_sequence < base_count:
                raise ValueError(
                    "max_sequence must be greater than or equal to base_count"
                )
            if fingerprint in core._base_count_by_fingerprint:
                raise ValueError(
                    f"duplicate occurrence counter: {fingerprint}"
                )
            core._base_count_by_fingerprint[fingerprint] = base_count
            core._max_sequence_by_fingerprint[fingerprint] = max_sequence
            first_seen = _coerce_datetime(
                counter_payload.get("first_seen_at"),
                field_name="first_seen_at",
                default_now=False,
            )
            last_seen = _coerce_datetime(
                counter_payload.get("last_seen_at"),
                field_name="last_seen_at",
                default_now=False,
            )
            if first_seen is not None:
                core._first_seen_by_fingerprint[fingerprint] = first_seen
            if last_seen is not None:
                core._last_seen_by_fingerprint[fingerprint] = last_seen

        for occurrence_payload in occurrence_payloads:
            occurrence = ErrorOccurrence.from_dict(occurrence_payload)
            if occurrence.sequence <= 0:
                raise ValueError("occurrence sequence must be positive")
            core._occurrences.append(occurrence)
            core._occurrences_by_fingerprint.setdefault(
                occurrence.fingerprint,
                [],
            ).append(occurrence)
            core._max_sequence_by_fingerprint[occurrence.fingerprint] = max(
                core._max_sequence_by_fingerprint.get(
                    occurrence.fingerprint,
                    0,
                ),
                occurrence.sequence,
            )
            current_first = core._first_seen_by_fingerprint.get(
                occurrence.fingerprint
            )
            if current_first is None or occurrence.occurred_at < current_first:
                core._first_seen_by_fingerprint[
                    occurrence.fingerprint
                ] = occurrence.occurred_at
            current_last = core._last_seen_by_fingerprint.get(
                occurrence.fingerprint
            )
            if current_last is None or occurrence.occurred_at > current_last:
                core._last_seen_by_fingerprint[
                    occurrence.fingerprint
                ] = occurrence.occurred_at

        case_payloads = payload.get("cases", [])
        if not isinstance(case_payloads, list):
            raise TypeError("cases must be a list")
        for case_payload in case_payloads:
            case = ErrorCase.from_dict(case_payload)
            if case.case_id in core._cases_by_id:
                raise ValueError(f"duplicate error case: {case.case_id}")
            if case.fingerprint in core._cases_by_fingerprint:
                raise ValueError(
                    f"duplicate error-case fingerprint: {case.fingerprint}"
                )
            core._cases_by_id[case.case_id] = case
            core._cases_by_fingerprint[case.fingerprint] = case
            represented_count = core._count_for(case.fingerprint)
            if represented_count > case.occurrence_count:
                raise ValueError(
                    "serialized occurrences exceed case occurrence_count"
                )
            if represented_count < case.occurrence_count:
                core._base_count_by_fingerprint[case.fingerprint] = (
                    core._base_count_by_fingerprint.get(
                        case.fingerprint,
                        0,
                    )
                    + case.occurrence_count
                    - represented_count
                )
            core._max_sequence_by_fingerprint[case.fingerprint] = max(
                core._max_sequence_by_fingerprint.get(case.fingerprint, 0),
                case.occurrence_count,
            )
            core._first_seen_by_fingerprint.setdefault(
                case.fingerprint,
                case.first_seen_at,
            )
            core._last_seen_by_fingerprint.setdefault(
                case.fingerprint,
                case.last_seen_at,
            )
        return core


__all__ = [
    "BlockDecision",
    "ErrorCase",
    "ErrorIdentity",
    "ErrorKnowledgeCore",
    "ErrorOccurrence",
    "ErrorRouteCard",
    "ErrorState",
    "FINGERPRINT_VERSION",
    "MAX_ROUTE_CARDS",
    "PROMOTION_THRESHOLD",
    "RecordResult",
    "ResolutionEvidence",
    "ResolutionRecord",
    "SCHEMA_VERSION",
    "detect_error_intent",
    "deterministic_fingerprint",
    "is_error_intent",
]
