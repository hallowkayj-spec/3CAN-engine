from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "backend" / "error_knowledge.py"
SPEC = importlib.util.spec_from_file_location(
    "error_knowledge_under_test",
    MODULE_PATH,
)
assert SPEC and SPEC.loader
EK = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = EK
SPEC.loader.exec_module(EK)

UTC = timezone.utc
T1 = datetime(2026, 7, 1, 10, 0, tzinfo=UTC)
T2 = datetime(2026, 7, 1, 10, 5, tzinfo=UTC)
T3 = datetime(2026, 7, 1, 10, 10, tzinfo=UTC)


def _record(
    core,
    *,
    occurred_at: datetime,
    root_cause: str = "unclassified root cause",
):
    return core.record_occurrence(
        project_id="zeven-runtime",
        operation="run backend validation",
        component="backend/app.py",
        error_type="unicode-error",
        root_cause=root_cause,
        occurred_at=occurred_at,
    )


def _promoted_core():
    core = EK.ErrorKnowledgeCore()
    _record(core, occurred_at=T1)
    second = _record(
        core,
        occurred_at=T2,
        root_cause="PowerShell decoded UTF-8 as a legacy code page",
    )
    assert second.promoted is True
    assert second.case is not None
    return core, second.case


@pytest.mark.parametrize(
    "text",
    [
        "run pytest tests/test_graph.py -q",
        "fix README formatting",
        "show error rate by week",
        "failure rate metric dashboard",
        "review the timeout setting and default value",
        "resolve merge conflict in README",
        "debug formatting in the release notes",
        "错误率指标本周是多少",
        "报错率看板",
        "修复 README 排版",
    ],
)
def test_canonical_error_intent_rejects_non_error_tasks(text: str) -> None:
    assert EK.is_error_intent(text) is False


@pytest.mark.parametrize(
    "text",
    [
        "unicode error in backend/app.py",
        "traceback pytest failed",
        "the request timed out while writing the graph",
        "the worker crashed with an exception",
        "上次这个报错怎么解决",
        "排查这个后端故障",
    ],
)
def test_canonical_error_intent_accepts_explicit_incidents(text: str) -> None:
    assert EK.is_error_intent(text) is True


def test_canonical_error_intent_accepts_explicit_case_id() -> None:
    assert EK.is_error_intent("retrieve ERR-case-a14f09c18b42 before retry") is True
    assert EK.is_error_intent("resolve ERR-repeated-timeout-deadbeef") is True


def test_legacy_detector_name_is_an_alias_not_a_second_policy() -> None:
    assert EK.detect_error_intent is EK.is_error_intent


def test_fingerprint_identity_excludes_mutable_root_cause() -> None:
    base = {
        "project_id": "zeven-runtime",
        "operation": "run backend validation",
        "component": "backend/app.py",
        "error_type": "unicode-error",
    }

    first = EK.deterministic_fingerprint(
        **base,
        root_cause="unknown",
    )
    diagnosed = EK.deterministic_fingerprint(
        **base,
        root_cause="PowerShell decoded UTF-8 with a legacy code page",
    )
    other_component = EK.deterministic_fingerprint(
        **{**base, "component": "frontend/app.ts"},
        root_cause="unknown",
    )

    assert EK.FINGERPRINT_VERSION == "ek2"
    assert first == diagnosed
    assert first != other_component


def test_identity_serialization_contains_only_canonical_identity_fields() -> None:
    identity = EK.ErrorIdentity.from_signals(
        project_id="zeven-runtime",
        operation="run backend validation",
        component="backend/app.py",
        error_type="unicode-error",
        root_cause="a diagnosis that can change",
    )

    assert identity.to_dict() == {
        "fingerprint_version": "ek2",
        "fingerprint": identity.fingerprint,
        "project_id": "zeven-runtime",
        "operation": "run backend validation",
        "component": "backend/app.py",
        "error_type": "unicode-error",
    }
    assert identity.root_cause == "a diagnosis that can change"


def test_root_cause_changes_promote_one_case_instead_of_splitting_identity() -> None:
    core, case = _promoted_core()

    assert core.unique_fingerprint_count == 1
    assert core.case_count == 1
    assert case.case_id.startswith("ERR-case-")
    assert case.occurrence_count == 2
    assert case.root_cause == (
        "powershell decoded utf-8 as a legacy code page"
    )
    assert "root_cause" not in case.applicability
    assert case.applicability["component"] == "backend/app.py"
    assert case.applicability["error_type"] == "unicode-error"


def test_legacy_error_keyword_remains_source_compatible() -> None:
    fingerprint = EK.deterministic_fingerprint(
        project_id="zeven-runtime",
        operation="run backend validation",
        component="backend/app.py",
        error="unicode-error",
        root_cause="unknown",
    )
    identity = EK.ErrorIdentity.from_signals(
        project_id="zeven-runtime",
        operation="run backend validation",
        component="backend/app.py",
        error="unicode-error",
        root_cause="unknown",
    )

    assert fingerprint == identity.fingerprint
    assert identity.error == identity.error_type == "unicode-error"


def test_private_paths_and_volatile_ids_never_enter_identity_or_diagnosis() -> None:
    private_uuid = "123e4567-e89b-42d3-a456-426614174000"
    identity = EK.ErrorIdentity.from_signals(
        project_id='"C:\\Users\\Alice Doe\\Private Workspace\\Zeven"',
        operation=(
            'open "C:\\Users\\Alice Doe\\Private Workspace\\backend\\app.py" '
            f"from https://internal.example/users/alice?request={private_uuid} "
            "with ticket_rt_private_998877"
        ),
        component='"C:\\Users\\Alice Doe\\Private Workspace\\backend\\app.py"',
        error_type=(
            "UnicodeError reading '/home/alice doe/Private Workspace/app.py'"
        ),
        root_cause=(
            "copied from \\\\private-server\\Alice Doe\\secret share\\trace.log; "
            f"request {private_uuid}; rt_private_998877"
        ),
    )
    serialized = json.dumps(
        {
            **identity.to_dict(),
            "diagnostic_root_cause": identity.root_cause,
        },
        ensure_ascii=False,
        sort_keys=True,
    ).casefold()

    for private_fragment in (
        "alice",
        "users",
        "private workspace",
        "private-server",
        "secret share",
        "internal.example",
        private_uuid,
        "rt_private_998877",
    ):
        assert private_fragment.casefold() not in serialized
    assert "<path>" in serialized
    assert "<url>" in serialized
    assert "<id>" in serialized
    assert "<ticket>" in serialized


def test_quoted_url_with_spaces_and_tilde_path_are_fully_redacted() -> None:
    identity = EK.ErrorIdentity.from_signals(
        project_id="zeven-runtime",
        operation=(
            'fetch "https://private.example/users/Alice Doe/report?id=123" '
            "then open ~\\Private User\\trace.log"
        ),
        component="backend/app.py",
        error_type="UnicodeError",
        root_cause="private input",
    )

    serialized = json.dumps(identity.to_dict(), sort_keys=True).casefold()
    assert "alice" not in serialized
    assert "private user" not in serialized
    assert "private.example" not in serialized
    assert "<url>" in serialized
    assert "<path>" in serialized


def test_different_user_paths_canonicalize_to_the_same_fingerprint() -> None:
    first = EK.deterministic_fingerprint(
        project_id="C:\\Users\\Alice Doe\\Workspace\\Zeven",
        operation='load "C:\\Users\\Alice Doe\\Workspace\\Zeven\\app.py"',
        component="C:\\Users\\Alice Doe\\Workspace\\Zeven\\app.py",
        error_type="UnicodeError at C:\\Users\\Alice Doe\\secret\\input.txt",
        root_cause="first diagnosis",
    )
    second = EK.deterministic_fingerprint(
        project_id="/home/bob/workspace/Zeven",
        operation="load '/home/bob/workspace/Zeven/app.py'",
        component="/home/bob/workspace/Zeven/app.py",
        error_type="UnicodeError at /home/bob/secret/input.txt",
        root_cause="better diagnosis",
    )

    assert first == second


def test_same_basename_in_different_logical_components_does_not_collide() -> None:
    backend = EK.deterministic_fingerprint(
        project_id="portable-project",
        operation="run tests",
        component=r"D:\workspace\repo\backend\app.py",
        error_type="unicode-error",
    )
    frontend = EK.deterministic_fingerprint(
        project_id="portable-project",
        operation="run tests",
        component=r"D:\workspace\repo\frontend\app.py",
        error_type="unicode-error",
    )

    assert backend != frontend


def test_compact_roundtrip_preserves_count_sequence_and_blocking() -> None:
    core, case = _promoted_core()
    assert case.blocking is True

    compact = core.to_dict(include_occurrences=False)
    counter = compact["occurrence_counters"][0]
    assert compact["occurrences_included"] is False
    assert "occurrences" not in compact
    assert counter["base_count"] == 2
    assert counter["max_sequence"] == 2

    restored = EK.ErrorKnowledgeCore.from_dict(compact)
    restored_case = restored.get_case(case.case_id)
    assert restored.occurrence_count == 2
    assert restored_case.occurrence_count == 2
    assert restored_case.blocking is True

    third = _record(
        restored,
        occurred_at=T3,
        root_cause="confirmed code-page mismatch",
    )
    assert third.occurrence.sequence == 3
    assert third.occurrence_count == 3
    assert third.case is not None
    assert third.case.occurrence_count == 3
    assert third.block_decision.blocked is True


def test_compact_singleton_promotes_on_the_next_occurrence() -> None:
    core = EK.ErrorKnowledgeCore()
    first = _record(core, occurred_at=T1)
    assert first.case is None

    compact = core.to_dict(include_occurrences=False)
    assert compact["occurrence_counters"][0]["base_count"] == 1
    restored = EK.ErrorKnowledgeCore.from_dict(compact)

    second = _record(restored, occurred_at=T2)
    assert second.occurrence.sequence == 2
    assert second.occurrence_count == 2
    assert second.promoted is True
    assert second.case is not None
    assert second.case.blocking is True


def test_full_roundtrip_keeps_base_and_new_occurrence_sequences_distinct() -> None:
    core, _ = _promoted_core()
    full = core.to_dict(include_occurrences=True)

    assert full["occurrences_included"] is True
    assert full["occurrence_counters"][0]["base_count"] == 0
    assert full["occurrence_counters"][0]["max_sequence"] == 2
    assert len(full["occurrences"]) == 2

    restored = EK.ErrorKnowledgeCore.from_dict(full)
    third = _record(restored, occurred_at=T3)
    assert third.occurrence.sequence == 3
    assert third.occurrence_count == 3


def test_compact_loaded_then_full_serialization_does_not_double_count() -> None:
    core, _ = _promoted_core()
    compact_loaded = EK.ErrorKnowledgeCore.from_dict(
        core.to_dict(include_occurrences=False)
    )
    _record(compact_loaded, occurred_at=T3)

    full = compact_loaded.to_dict(include_occurrences=True)
    counter = full["occurrence_counters"][0]
    assert counter["base_count"] == 2
    assert counter["max_sequence"] == 3
    assert len(full["occurrences"]) == 1

    restored = EK.ErrorKnowledgeCore.from_dict(full)
    assert restored.occurrence_count == 3
    assert restored.cases()[0].occurrence_count == 3


def _evidence_payload(verified) -> dict:
    return {
        "kind": "pytest",
        "reference": "tests/test_error_knowledge.py",
        "summary": "focused suite completed",
        "verified": verified,
        "verified_at": "2026-07-01T10:20:00Z",
        "digest": None,
        "metadata": {},
    }


def test_resolution_evidence_from_dict_preserves_literal_false() -> None:
    evidence = EK.ResolutionEvidence.from_dict(_evidence_payload(False))

    assert evidence.verified is False


@pytest.mark.parametrize("value", ["false", "true", 0, 1, None])
def test_resolution_evidence_from_dict_rejects_non_boolean_values(value) -> None:
    with pytest.raises(TypeError, match="verified must be a bool"):
        EK.ResolutionEvidence.from_dict(_evidence_payload(value))


def test_compact_boolean_flag_is_strict() -> None:
    core, _ = _promoted_core()
    payload = core.to_dict(include_occurrences=False)
    payload["occurrences_included"] = "false"

    with pytest.raises(
        TypeError,
        match="occurrences_included must be a bool",
    ):
        EK.ErrorKnowledgeCore.from_dict(payload)


@pytest.mark.parametrize("value", ["false", "true", 0, 1, None])
def test_serialization_rejects_non_boolean_include_occurrences(value) -> None:
    core, _ = _promoted_core()

    with pytest.raises(TypeError, match="include_occurrences must be a bool"):
        core.to_dict(include_occurrences=value)


def test_boolean_is_not_accepted_as_an_occurrence_count() -> None:
    core, _ = _promoted_core()
    payload = core.to_dict(include_occurrences=False)
    payload["cases"][0]["occurrence_count"] = True

    with pytest.raises(TypeError, match="occurrence_count must be an int"):
        EK.ErrorKnowledgeCore.from_dict(payload)


def test_false_evidence_cannot_resolve_a_case() -> None:
    core, case = _promoted_core()
    evidence = EK.ResolutionEvidence.from_dict(_evidence_payload(False))

    with pytest.raises(ValueError, match="verified evidence"):
        core.resolve(
            case.case_id,
            solution_summary="use explicit UTF-8 decoding",
            evidence=[evidence],
            resolved_by="codex",
            resolved_at=T3,
        )


def test_route_uses_the_canonical_intent_boundary() -> None:
    core, _ = _promoted_core()

    assert core.route(
        "run pytest tests/test_graph.py -q",
        project_id="zeven-runtime",
    ) == []
    assert core.route(
        "fix README formatting",
        project_id="zeven-runtime",
    ) == []

    cards = core.route(
        "unicode error in backend/app.py",
        project_id="zeven-runtime",
        component="backend/app.py",
    )
    assert len(cards) == 1
    assert cards[0].applicability["component"] == "backend/app.py"
    assert cards[0].blocking is True


def test_route_rejects_string_false_override() -> None:
    core, _ = _promoted_core()

    with pytest.raises(TypeError, match="error_intent must be a bool"):
        core.route("unicode error", error_intent="false")
