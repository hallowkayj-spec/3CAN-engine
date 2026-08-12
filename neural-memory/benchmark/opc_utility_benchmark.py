#!/usr/bin/env python3
"""Score private OPC route and clean-session observations.

The runner is intentionally offline: it does not call a model, start a
runtime, or implement another retrieval path.  Private questions and graph
identifiers stay in caller-supplied evidence files.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from models import semantic_id_family  # noqa: E402


GOLD_SCHEMA = "3can.opc-utility-gold/v1"
OBSERVATION_SCHEMA = "3can.opc-observation/v1"
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class InvalidGold(ValueError):
    """The frozen benchmark definition is not safe to score."""


def _safe_repo_path(value: Any) -> bool:
    if not isinstance(value, str) or not value or "\\" in value:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and ".." not in path.parts and ":" not in value


def _source_evidence(case: dict[str, Any]):
    for fact in case.get("gold", {}).get("required_facts", []):
        yield from fact.get("evidence", [])


def _utc_timestamp(value: Any) -> datetime:
    text = str(value or "")
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("timezone_required")
    return parsed.astimezone(timezone.utc)


def validate_gold(gold: dict[str, Any], *, now: datetime | None = None) -> None:
    """Validate a frozen private suite before reading any observations."""

    if gold.get("schema_version") != GOLD_SCHEMA:
        raise InvalidGold("schema_version_invalid")
    cases = gold.get("cases")
    if not isinstance(cases, list) or not 10 <= len(cases) <= 20:
        raise InvalidGold("case_count_must_be_10_to_20")
    ids = [case.get("id") for case in cases]
    if any(not isinstance(case_id, str) or not case_id for case_id in ids):
        raise InvalidGold("case_id_invalid")
    if len(ids) != len(set(ids)):
        raise InvalidGold("case_id_duplicate")
    splits = {case.get("split") for case in cases}
    if not {"baseline", "heldout"} <= splits:
        raise InvalidGold("baseline_and_heldout_required")
    required = gold.get("required_baseline_ids")
    baseline_ids = {case["id"] for case in cases if case.get("split") == "baseline"}
    if not isinstance(required, list) or not set(required) <= baseline_ids:
        raise InvalidGold("required_baseline_ids_missing")

    expected = gold.get("expected_observations")
    if not isinstance(expected, list) or not expected:
        raise InvalidGold("expected_observations_missing")
    expected_keys: set[tuple[str, str, str]] = set()
    for item in expected:
        if not isinstance(item, dict) or not all(
            isinstance(item.get(key), str)
            for key in ("experiment", "variant")
        ):
            raise InvalidGold("expected_observation_invalid")
        lanes = item.get("lanes")
        if not isinstance(lanes, list) or not lanes:
            raise InvalidGold("expected_observation_lanes_invalid")
        for lane in lanes:
            key = (item["experiment"], item["variant"], str(lane))
            if key in expected_keys:
                raise InvalidGold("expected_observation_duplicate")
            expected_keys.add(key)

    snapshots = gold.get("source_snapshots")
    if not isinstance(snapshots, dict) or not snapshots:
        raise InvalidGold("source_snapshots_missing")
    for snapshot in snapshots.values():
        if not isinstance(snapshot, dict) or not _COMMIT_RE.fullmatch(
            str(snapshot.get("commit") or "")
        ):
            raise InvalidGold("source_snapshot_commit_invalid")

    binding = gold.get("graph_binding")
    if not isinstance(binding, dict) or binding.get("schema_version") != (
        "3can.benchmark-graph-binding/v1"
    ):
        raise InvalidGold("graph_binding_invalid")
    if not _SHA256_RE.fullmatch(str(binding.get("graph_root_sha256") or "")):
        raise InvalidGold("graph_root_sha256_invalid")
    required_nodes = binding.get("required_node_ids")
    if not isinstance(required_nodes, list) or not required_nodes:
        raise InvalidGold("required_node_ids_missing")

    for case in cases:
        if case.get("truth_mode") not in {"durable", "historical", "live_external"}:
            raise InvalidGold("truth_mode_invalid")
        context = case.get("route_context")
        if not isinstance(context, dict) or not context.get("project_id"):
            raise InvalidGold("route_context_project_id_missing")
        case_gold = case.get("gold")
        if not isinstance(case_gold, dict) or not case_gold.get("required_facts"):
            raise InvalidGold("required_facts_missing")
        for evidence in _source_evidence(case):
            if not isinstance(evidence, dict) or not _safe_repo_path(evidence.get("path")):
                raise InvalidGold("source_evidence_path_invalid")
            if (
                not isinstance(evidence.get("repo"), str)
                or evidence["repo"] not in snapshots
            ):
                raise InvalidGold("source_evidence_repo_unbound")
            if not _SHA256_RE.fullmatch(str(evidence.get("sha256") or "")):
                raise InvalidGold("source_evidence_sha256_invalid")
        if case.get("truth_mode") == "live_external":
            external = case_gold.get("external_verification")
            if not isinstance(external, dict):
                raise InvalidGold("live_external_binding_missing")
            try:
                captured = _utc_timestamp(external.get("captured_at"))
                max_age = int(external.get("max_age_seconds"))
            except (TypeError, ValueError) as exc:
                raise InvalidGold("live_external_binding_invalid") from exc
            if max_age <= 0 or not _SHA256_RE.fullmatch(
                str(external.get("evidence_sha256") or "")
            ):
                raise InvalidGold("live_external_binding_invalid")
            reference_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
            age_seconds = (reference_now - captured).total_seconds()
            if age_seconds < -300 or age_seconds > max_age:
                raise InvalidGold("live_external_binding_expired")


def load_observations(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("schema_version") != OBSERVATION_SCHEMA:
            raise ValueError(f"observation_schema_invalid:{line_number}")
        rows.append(row)
    return rows


def _route_event(observation: dict[str, Any]) -> dict[str, Any] | None:
    return next(
        (event for event in observation.get("events", []) if event.get("kind") == "route"),
        None,
    )


def _rank(node_ids: list[str], accepted: set[str]) -> int | None:
    return next((index for index, node_id in enumerate(node_ids, 1) if node_id in accepted), None)


def _fact_score(case: dict[str, Any], answer: dict[str, Any]) -> tuple[int, int]:
    required = case["gold"]["required_facts"]
    correct = 0
    for fact in required:
        accepted = fact.get("accepted_values", [])
        if answer.get(fact["key"]) in accepted:
            correct += 1
    return correct, len(required)


def _binding_matches(gold: dict[str, Any], observation: dict[str, Any]) -> bool:
    binding = observation.get("binding")
    if not isinstance(binding, dict):
        return False
    if binding.get("graph_root_sha256") != gold["graph_binding"]["graph_root_sha256"]:
        return False
    observed_commits = binding.get("source_commits")
    expected_commits = {
        name: snapshot["commit"]
        for name, snapshot in gold["source_snapshots"].items()
    }
    return observed_commits == expected_commits


def _canonical_source_reads(
    case: dict[str, Any],
    events: list[dict[str, Any]],
) -> list[dict[str, str]]:
    expected = {
        (str(item.get("repo")), str(item.get("path")), str(item.get("sha256")))
        for item in _source_evidence(case)
    }
    matched = []
    for event in events:
        if event.get("kind") != "file_read":
            continue
        key = (
            str(event.get("repo")),
            str(event.get("path")),
            str(event.get("sha256")),
        )
        if key in expected:
            matched.append({"repo": key[0], "path": key[1], "sha256": key[2]})
    return matched


def _external_evidence_receipts(
    case: dict[str, Any],
    events: list[dict[str, Any]],
) -> list[dict[str, str]]:
    if case.get("truth_mode") != "live_external":
        return []
    expected = str(
        case.get("gold", {})
        .get("external_verification", {})
        .get("evidence_sha256")
        or ""
    )
    return [
        {"evidence_sha256": expected, "status": "verified"}
        for event in events
        if event.get("kind") == "external_verification"
        and event.get("status") == "verified"
        and str(event.get("evidence_sha256") or "") == expected
    ]


def score_observation(
    gold: dict[str, Any],
    case: dict[str, Any],
    observation: dict[str, Any],
) -> dict[str, Any]:
    events = observation.get("events") if isinstance(observation.get("events"), list) else []
    route_events = [event for event in events if event.get("kind") == "route"]
    route = route_events[0] if route_events else None
    node_ids = [str(value) for value in (route or {}).get("node_ids", [])]
    canonical = set(case["gold"].get("canonical_node_ids", []))
    forbidden = set(case["gold"].get("forbidden_node_ids", []))
    rank = _rank(node_ids, canonical)
    answer_facts = observation.get("answer_facts")
    required_fact_keys = {
        str(fact["key"])
        for fact in case["gold"]["required_facts"]
    }
    missing_fact_keys = sorted(
        required_fact_keys - set(answer_facts)
        if isinstance(answer_facts, dict)
        else required_fact_keys
    )
    fact_evidence_available = not missing_fact_keys
    correct, required = _fact_score(case, answer_facts or {})
    top5 = node_ids[:5]
    top12 = node_ids[:12]
    family_count = lambda ids, families: sum(  # noqa: E731
        semantic_id_family(node_id) in families for node_id in ids
    )

    route_meta = (route or {}).get("route_meta") or {}
    current_policy = route_meta.get("current_reality_policy") or {}
    external_verification_signaled = bool(
        current_policy.get("external_verification_required")
    )
    canonical_source_reads = _canonical_source_reads(case, events)
    external_evidence_receipts = _external_evidence_receipts(case, events)
    core = route_meta.get("core_memory_graph") or {}
    must_consume = set(core.get("must_consume_node_ids") or [])
    project_id = case["route_context"]["project_id"]
    node_projects = (route or {}).get("node_projects") or {}
    wrong_project: list[str] = []
    unknown_project: list[str] = []
    for node_id in node_ids:
        values = node_projects.get(node_id) or []
        if not values:
            unknown_project.append(node_id)
        elif project_id not in values:
            wrong_project.append(node_id)

    lane = observation.get("lane")
    experiment = observation.get("experiment")
    retrieve_count = sum(event.get("kind") == "retrieve" for event in events)
    lane_violation = (
        lane == "no_threecan"
        and any(event.get("kind") in {"route", "retrieve"} for event in events)
    ) or (
        experiment == "opc_utility"
        and lane == "threecan"
        and (len(route_events) != 1 or retrieve_count > 3)
    )
    trace_complete = observation.get("trace_complete") is True
    elapsed_ms = observation.get("elapsed_ms")
    elapsed_valid = (
        isinstance(elapsed_ms, (int, float))
        and not isinstance(elapsed_ms, bool)
        and elapsed_ms >= 0
    )
    incomplete_reasons = []
    if not trace_complete:
        incomplete_reasons.append("trace_incomplete")
    if missing_fact_keys:
        incomplete_reasons.append("required_answer_facts_missing")
    if not elapsed_valid:
        incomplete_reasons.append("elapsed_ms_invalid")
    usage = observation.get("usage")
    usage_reliable = (
        isinstance(usage, dict)
        and isinstance(usage.get("provider"), str)
        and isinstance(usage.get("source"), str)
        and isinstance(usage.get("input_tokens"), int)
        and isinstance(usage.get("output_tokens"), int)
    )

    binding_valid = _binding_matches(gold, observation)
    status = observation.get("status")
    if not binding_valid:
        status = "INVALID_BINDING"
    elif lane_violation:
        status = "INVALID_LANE"
    elif status == "COMPLETE" and incomplete_reasons:
        status = "INCOMPLETE_EVIDENCE"

    return {
        "case_id": case["id"],
        "split": case["split"],
        "experiment": experiment,
        "variant": observation.get("variant"),
        "lane": lane,
        "model": observation.get("model"),
        "effort": observation.get("effort"),
        "status": status,
        "binding_valid": binding_valid,
        "trace_complete": trace_complete,
        "elapsed_ms": elapsed_ms if elapsed_valid else "UNAVAILABLE",
        "incomplete_evidence": incomplete_reasons,
        "missing_answer_fact_keys": missing_fact_keys,
        "fact_coverage": (
            round(correct / required, 4) if fact_evidence_available else "UNAVAILABLE"
        ),
        "all_facts_correct": (
            correct == required if fact_evidence_available else "UNAVAILABLE"
        ),
        "canonical_rank": rank,
        "reciprocal_rank": (
            round(1 / rank, 4) if rank else 0.0
        ) if canonical else "UNAVAILABLE",
        "hit_at_3": bool(rank and rank <= 3) if canonical else "UNAVAILABLE",
        "hit_at_5": bool(rank and rank <= 5) if canonical else "UNAVAILABLE",
        "external_verification_signaled": external_verification_signaled,
        "canonical_node_found": bool(rank),
        "canonical_source_reads": canonical_source_reads,
        "external_evidence_receipts": external_evidence_receipts,
        "canonical_source_found": bool(
            canonical_source_reads or external_evidence_receipts
        ),
        "ses_ho_share_top5": round(
            family_count(top5, {"SES", "HO"}) / len(top5), 4
        ) if top5 else 0.0,
        "ses_ho_share_top12": round(
            family_count(top12, {"SES", "HO"}) / len(top12), 4
        ) if top12 else 0.0,
        "forbidden_hits": [node_id for node_id in node_ids if node_id in forbidden],
        "stale_must_consume": sorted(must_consume & forbidden),
        "wrong_project_hits": wrong_project,
        "unknown_project_hits": unknown_project,
        "graph_traversal": route_meta.get("graph_traversal_boost", "UNAVAILABLE"),
        "search_calls": sum(event.get("kind") == "search" for event in events),
        "file_reads": sum(event.get("kind") == "file_read" for event in events),
        "retrieve_count": retrieve_count,
        "wrong_path_attempts": (
            sum(event.get("kind") == "wrong_path" for event in events)
            if trace_complete else "UNAVAILABLE"
        ),
        "user_corrections": (
            sum(event.get("kind") == "user_correction" for event in events)
            if trace_complete else "UNAVAILABLE"
        ),
        "usage": usage if usage_reliable else "UNAVAILABLE",
    }


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    elapsed = [row["elapsed_ms"] for row in rows if isinstance(row.get("elapsed_ms"), (int, float))]
    usage_available = rows and all(isinstance(row.get("usage"), dict) for row in rows)
    return {
        "cases": len(rows),
        "fact_coverage": _mean([
            row["fact_coverage"] for row in rows
            if isinstance(row["fact_coverage"], (int, float))
        ]),
        "all_facts_correct_rate": _mean([
            float(row["all_facts_correct"]) for row in rows
            if isinstance(row["all_facts_correct"], bool)
        ]),
        "mrr": _mean([
            row["reciprocal_rank"] for row in rows
            if isinstance(row["reciprocal_rank"], (int, float))
        ]),
        "hit_at_3": _mean([
            float(row["hit_at_3"]) for row in rows
            if isinstance(row["hit_at_3"], bool)
        ]),
        "hit_at_5": _mean([
            float(row["hit_at_5"]) for row in rows
            if isinstance(row["hit_at_5"], bool)
        ]),
        "canonical_source_found_rate": _mean([
            float(row["canonical_source_found"]) for row in rows
        ]),
        "ses_ho_share_top5": _mean([row["ses_ho_share_top5"] for row in rows]),
        "ses_ho_share_top12": _mean([row["ses_ho_share_top12"] for row in rows]),
        "forbidden_hits": sum(len(row["forbidden_hits"]) for row in rows),
        "stale_must_consume": sum(len(row["stale_must_consume"]) for row in rows),
        "wrong_project_hits": sum(len(row["wrong_project_hits"]) for row in rows),
        "unknown_project_hits": sum(len(row["unknown_project_hits"]) for row in rows),
        "elapsed_ms_mean": _mean(elapsed),
        "search_calls": sum(row["search_calls"] for row in rows),
        "file_reads": sum(row["file_reads"] for row in rows),
        "retrieve_count": sum(row["retrieve_count"] for row in rows),
        "usage": (
            {
                "input_tokens": sum(row["usage"]["input_tokens"] for row in rows),
                "output_tokens": sum(row["usage"]["output_tokens"] for row in rows),
            }
            if usage_available else "UNAVAILABLE"
        ),
    }


def _token_savings(rows: list[dict[str, Any]]) -> dict[str, Any] | str:
    opc_rows = [row for row in rows if row["experiment"] == "opc_utility"]
    if not opc_rows:
        return "UNAVAILABLE"
    by_cohort: dict[
        tuple[str, str, str],
        dict[str, dict[str, dict[str, Any]]],
    ] = defaultdict(lambda: defaultdict(dict))
    for row in opc_rows:
        cohort = (
            str(row.get("variant") or ""),
            str(row.get("model") or ""),
            str(row.get("effort") or ""),
        )
        if not all(cohort):
            return "UNAVAILABLE"
        by_cohort[cohort][row["case_id"]][str(row["lane"])] = row
    cohort_reports: dict[str, Any] = {}
    for cohort, by_case in by_cohort.items():
        if any(set(lanes) != {"no_threecan", "threecan"} for lanes in by_case.values()):
            return "UNAVAILABLE"
        no_threecan_total = 0
        threecan_total = 0
        for lanes in by_case.values():
            left = lanes["no_threecan"].get("usage")
            right = lanes["threecan"].get("usage")
            if not isinstance(left, dict) or not isinstance(right, dict):
                return "UNAVAILABLE"
            if (left.get("provider"), left.get("source")) != (
                right.get("provider"), right.get("source")
            ):
                return "UNAVAILABLE"
            no_threecan_total += left["input_tokens"] + left["output_tokens"]
            threecan_total += right["input_tokens"] + right["output_tokens"]
        cohort_reports["/".join(cohort)] = {
            "paired_cases": len(by_case),
            "no_threecan_tokens": no_threecan_total,
            "threecan_tokens": threecan_total,
            "reduction_ratio": (
                round((no_threecan_total - threecan_total) / no_threecan_total, 4)
                if no_threecan_total else None
            ),
        }
    return {"cohorts": cohort_reports}


def _expected_observation_keys(gold: dict[str, Any]) -> set[tuple[str, str, str, str]]:
    return {
        (case["id"], item["experiment"], item["variant"], str(lane))
        for case in gold["cases"]
        for item in gold["expected_observations"]
        for lane in item["lanes"]
    }


def build_report(gold: dict[str, Any], observations: list[dict[str, Any]]) -> dict[str, Any]:
    validate_gold(gold)
    cases = {case["id"]: case for case in gold["cases"]}
    scored: list[dict[str, Any]] = []
    unknown_cases: list[str] = []
    expected_keys = _expected_observation_keys(gold)
    seen_keys: set[tuple[str, str, str, str]] = set()
    duplicate_keys: list[str] = []
    unexpected_keys: list[str] = []
    for observation in observations:
        case = cases.get(observation.get("case_id"))
        if case is None:
            unknown_cases.append(str(observation.get("case_id")))
            continue
        key = (
            case["id"],
            str(observation.get("experiment")),
            str(observation.get("variant")),
            str(observation.get("lane")),
        )
        rendered_key = "/".join(key)
        if key not in expected_keys:
            unexpected_keys.append(rendered_key)
        if key in seen_keys:
            duplicate_keys.append(rendered_key)
        seen_keys.add(key)
        scored.append(score_observation(gold, case, observation))

    missing_keys = sorted("/".join(key) for key in expected_keys - seen_keys)
    opc_cohorts: dict[
        tuple[str, str, str],
        dict[str, set[tuple[str, str]]],
    ] = defaultdict(lambda: defaultdict(set))
    for row in scored:
        if row["experiment"] != "opc_utility":
            continue
        key = (row["case_id"], row["experiment"], str(row["variant"]))
        opc_cohorts[key][str(row["lane"])].add(
            (str(row.get("model") or ""), str(row.get("effort") or ""))
        )
    invalid_opc_cohorts = {
        key
        for key, lanes in opc_cohorts.items()
        if {"no_threecan", "threecan"} <= set(lanes)
        and (
            len(lanes["no_threecan"] | lanes["threecan"]) != 1
            or not all(next(iter(lanes["no_threecan"])))
        )
    }
    for row in scored:
        key = (row["case_id"], row["experiment"], str(row["variant"]))
        if key in invalid_opc_cohorts and row["status"] == "COMPLETE":
            row["status"] = "INVALID_COHORT"
    valid_scored = [
        row
        for row in scored
        if row["status"] == "COMPLETE"
        and (
            row["case_id"],
            str(row["experiment"]),
            str(row["variant"]),
            str(row["lane"]),
        ) in expected_keys
    ]

    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in valid_scored:
        key = (row["experiment"], row["variant"], row["lane"], row["split"])
        groups[key].append(row)
    grouped = {
        "/".join(str(value) for value in key): _aggregate(rows)
        for key, rows in sorted(groups.items())
    }

    missing_opc_lanes = sorted(
        key for key in missing_keys if "/opc_utility/" in key
    )
    invalid_rows = [row["case_id"] for row in scored if row["status"] == "INVALID_LANE"]
    invalid_bindings = [
        row["case_id"] for row in scored if row["status"] == "INVALID_BINDING"
    ]
    non_complete = [
        row["case_id"]
        for row in scored
        if row["status"] not in {"COMPLETE", "INVALID_BINDING", "INVALID_LANE"}
    ]
    status = "COMPLETE"
    if any((
        unknown_cases,
        missing_keys,
        duplicate_keys,
        unexpected_keys,
        invalid_rows,
        invalid_bindings,
        non_complete,
    )):
        status = "PARTIAL"

    return {
        "schema_version": "3can.opc-utility-report/v1",
        "status": status,
        "gold_sha256": hashlib.sha256(
            json.dumps(gold, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "case_count": len(cases),
        "observation_count": len(scored),
        "unknown_observation_cases": unknown_cases,
        "missing_observations": missing_keys,
        "duplicate_observations": duplicate_keys,
        "unexpected_observations": unexpected_keys,
        "missing_opc_lanes": missing_opc_lanes,
        "invalid_lane_cases": invalid_rows,
        "invalid_binding_cases": invalid_bindings,
        "invalid_opc_cohorts": sorted(
            "/".join(key) for key in invalid_opc_cohorts
        ),
        "non_complete_cases": non_complete,
        "groups": grouped,
        "cases": scored,
        "token_savings": _token_savings(valid_scored),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--observations", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    gold = json.loads(args.gold.read_text(encoding="utf-8"))
    report = build_report(gold, load_observations(args.observations))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({key: report[key] for key in ("status", "case_count", "observation_count")}, indent=2))
    return 0 if report["status"] == "COMPLETE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
