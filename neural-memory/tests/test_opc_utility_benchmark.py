from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest


RUNNER = Path(__file__).resolve().parents[1] / "benchmark" / "opc_utility_benchmark.py"


def load_runner():
    spec = importlib.util.spec_from_file_location("opc_utility_benchmark", RUNNER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_gold() -> dict:
    cases = []
    for index in range(10):
        case_id = f"T{index:02d}"
        cases.append(
            {
                "id": case_id,
                "split": "baseline" if index < 6 else "heldout",
                "dimension": "current_fact",
                "intent": "current",
                "question": f"Synthetic question {index}",
                "route_context": {
                    "project_id": "demo-project",
                    "project_namespace": "demo-project",
                },
                "truth_mode": "durable",
                "gold": {
                    "required_facts": [
                        {
                            "key": "answer",
                            "accepted_values": [f"value-{index}"],
                            "evidence": [
                                {
                                    "repo": "demo",
                                    "path": f"docs/fact-{index}.md",
                                    "sha256": "b" * 64,
                                }
                            ],
                        }
                    ],
                    "canonical_node_ids": [f"DOC-current-{index}"],
                    "forbidden_node_ids": [f"SES-old-{index}"],
                    "external_verification_required": False,
                },
            }
        )
    return {
        "schema_version": "3can.opc-utility-gold/v1",
        "frozen_at": "2026-08-10T00:00:00Z",
        "required_baseline_ids": [case["id"] for case in cases[:6]],
        "expected_observations": [
            {
                "experiment": "route_regression",
                "variant": "candidate",
                "lanes": ["threecan"],
            }
        ],
        "source_snapshots": {"demo": {"commit": "a" * 40}},
        "graph_binding": {
            "schema_version": "3can.benchmark-graph-binding/v1",
            "graph_root_sha256": "c" * 64,
            "required_node_ids": ["DOC-current-0"],
        },
        "cases": cases,
    }


def make_observation(
    gold: dict,
    case_id: str = "T00",
    *,
    experiment: str = "route_regression",
    lane: str = "threecan",
) -> dict:
    index = int(case_id[1:])
    canonical = f"DOC-current-{index}"
    forbidden = f"SES-old-{index}"
    return {
        "schema_version": "3can.opc-observation/v1",
        "case_id": case_id,
        "experiment": experiment,
        "variant": "candidate",
        "lane": lane,
        "model": "fixture-model",
        "effort": "fixture-effort",
        "binding": {
            "graph_root_sha256": gold["graph_binding"]["graph_root_sha256"],
            "source_commits": {"demo": "a" * 40},
        },
        "trace_complete": True,
        "status": "COMPLETE",
        "elapsed_ms": 100,
        "events": [
            {
                "kind": "route",
                "node_ids": [canonical, forbidden, "HO-archive"],
                "node_projects": {
                    canonical: ["demo-project"],
                    forbidden: ["other-project"],
                    "HO-archive": [],
                },
                "route_meta": {
                    "core_memory_graph": {
                        "must_consume_node_ids": [canonical, forbidden]
                    },
                    "graph_traversal_boost": {"applied": True},
                },
            }
        ],
        "answer_facts": {"answer": f"value-{index}"},
        "usage": None,
    }


def test_valid_gold_and_route_metrics_are_scored() -> None:
    runner = load_runner()
    gold = make_gold()
    observations = [make_observation(gold, f"T{index:02d}") for index in range(10)]
    report = runner.build_report(gold, observations)

    assert report["status"] == "COMPLETE"
    row = report["cases"][0]
    assert row["canonical_rank"] == 1
    assert row["reciprocal_rank"] == 1.0
    assert row["hit_at_3"] is True
    assert row["fact_coverage"] == 1.0
    assert row["ses_ho_share_top5"] == pytest.approx(2 / 3, abs=0.0001)
    assert row["forbidden_hits"] == ["SES-old-0"]
    assert row["stale_must_consume"] == ["SES-old-0"]
    assert row["wrong_project_hits"] == ["SES-old-0"]
    assert row["unknown_project_hits"] == ["HO-archive"]


@pytest.mark.parametrize(
    "unsafe_path",
    ["C:/private/file.md", "../private.md", "//server/share/file.md", "a\\b.md"],
)
def test_gold_rejects_non_relative_evidence_paths(unsafe_path: str) -> None:
    runner = load_runner()
    gold = make_gold()
    gold["cases"][0]["gold"]["required_facts"][0]["evidence"][0]["path"] = unsafe_path

    with pytest.raises(runner.InvalidGold, match="source_evidence_path_invalid"):
        runner.validate_gold(gold)


def test_gold_requires_unique_ids_and_both_splits() -> None:
    runner = load_runner()
    duplicate = make_gold()
    duplicate["cases"][1]["id"] = duplicate["cases"][0]["id"]
    with pytest.raises(runner.InvalidGold, match="case_id_duplicate"):
        runner.validate_gold(duplicate)

    one_split = make_gold()
    for case in one_split["cases"]:
        case["split"] = "baseline"
    with pytest.raises(runner.InvalidGold, match="baseline_and_heldout_required"):
        runner.validate_gold(one_split)


def test_gold_requires_frozen_source_and_graph_bindings() -> None:
    runner = load_runner()
    bad_commit = make_gold()
    bad_commit["source_snapshots"]["demo"]["commit"] = "not-a-commit"
    with pytest.raises(runner.InvalidGold, match="source_snapshot_commit_invalid"):
        runner.validate_gold(bad_commit)

    bad_graph = make_gold()
    bad_graph["graph_binding"]["graph_root_sha256"] = "short"
    with pytest.raises(runner.InvalidGold, match="graph_root_sha256_invalid"):
        runner.validate_gold(bad_graph)

    unbound_repo = make_gold()
    unbound_repo["cases"][0]["gold"]["required_facts"][0]["evidence"][0][
        "repo"
    ] = "unfrozen-repo"
    with pytest.raises(runner.InvalidGold, match="source_evidence_repo_unbound"):
        runner.validate_gold(unbound_repo)


def test_observation_binding_mismatch_is_partial() -> None:
    runner = load_runner()
    gold = make_gold()
    observation = make_observation(gold)
    observation["binding"]["graph_root_sha256"] = "d" * 64

    report = runner.build_report(gold, [observation])

    assert report["status"] == "PARTIAL"
    assert report["invalid_binding_cases"] == ["T00"]
    assert report["cases"][0]["status"] == "INVALID_BINDING"


def test_opc_lane_contract_rejects_route_in_lane_a_and_fourth_retrieve() -> None:
    runner = load_runner()
    gold = make_gold()
    lane_a = make_observation(gold, experiment="opc_utility", lane="no_threecan")
    lane_b = make_observation(gold, experiment="opc_utility", lane="threecan")
    lane_b["events"].extend({"kind": "retrieve"} for _ in range(4))

    report = runner.build_report(gold, [lane_a, lane_b])

    assert report["status"] == "PARTIAL"
    assert report["invalid_lane_cases"] == ["T00", "T00"]


def test_incomplete_trace_and_unproven_usage_stay_unavailable() -> None:
    runner = load_runner()
    gold = make_gold()
    observation = make_observation(gold)
    observation["trace_complete"] = False
    observation["usage"] = {"input_tokens": 100, "output_tokens": 10}

    row = runner.build_report(gold, [observation])["cases"][0]

    assert row["status"] == "INCOMPLETE_EVIDENCE"
    assert row["incomplete_evidence"] == ["trace_incomplete"]
    assert row["wrong_path_attempts"] == "UNAVAILABLE"
    assert row["user_corrections"] == "UNAVAILABLE"
    assert row["usage"] == "UNAVAILABLE"


def test_missing_opc_lane_is_partial() -> None:
    runner = load_runner()
    gold = make_gold()
    gold["expected_observations"] = [
        {
            "experiment": "opc_utility",
            "variant": "candidate",
            "lanes": ["no_threecan", "threecan"],
        }
    ]
    observation = make_observation(gold, experiment="opc_utility", lane="threecan")
    observation["events"].append({"kind": "retrieve"})

    report = runner.build_report(gold, [observation])

    assert report["status"] == "PARTIAL"
    assert "T00/opc_utility/candidate/no_threecan" in report["missing_opc_lanes"]
    assert len(report["missing_opc_lanes"]) == 19
    assert report["token_savings"] == "UNAVAILABLE"


def test_reliable_paired_usage_can_measure_token_delta() -> None:
    runner = load_runner()
    gold = make_gold()
    gold["expected_observations"] = [
        {
            "experiment": "opc_utility",
            "variant": "candidate",
            "lanes": ["no_threecan", "threecan"],
        }
    ]
    lane_a = make_observation(gold, experiment="opc_utility", lane="no_threecan")
    lane_a["events"] = [{"kind": "search"}]
    lane_b = make_observation(gold, experiment="opc_utility", lane="threecan")
    lane_a["usage"] = {
        "provider": "fixture",
        "source": "provider_receipt",
        "input_tokens": 900,
        "output_tokens": 100,
    }
    lane_b["usage"] = {
        "provider": "fixture",
        "source": "provider_receipt",
        "input_tokens": 700,
        "output_tokens": 100,
    }

    report = runner.build_report(gold, [lane_a, lane_b])

    assert report["status"] == "PARTIAL"
    cohort = report["token_savings"]["cohorts"][
        "candidate/fixture-model/fixture-effort"
    ]
    assert cohort["reduction_ratio"] == 0.2


def test_opc_lanes_require_same_model_and_effort_cohort() -> None:
    runner = load_runner()
    gold = make_gold()
    gold["expected_observations"] = [
        {
            "experiment": "opc_utility",
            "variant": "candidate",
            "lanes": ["no_threecan", "threecan"],
        }
    ]
    lane_a = make_observation(gold, experiment="opc_utility", lane="no_threecan")
    lane_a["events"] = [{"kind": "search"}]
    lane_b = make_observation(gold, experiment="opc_utility", lane="threecan")
    lane_b["model"] = "different-model"

    report = runner.build_report(gold, [lane_a, lane_b])

    assert report["status"] == "PARTIAL"
    assert report["invalid_opc_cohorts"] == ["T00/opc_utility/candidate"]
    assert {row["status"] for row in report["cases"]} == {"INVALID_COHORT"}
    assert report["groups"] == {}


def test_groups_do_not_mix_split_variant_or_lane() -> None:
    runner = load_runner()
    gold = make_gold()
    gold["expected_observations"].append(
        {
            "experiment": "route_regression",
            "variant": "before",
            "lanes": ["threecan"],
        }
    )
    rows = [make_observation(gold, "T00"), make_observation(gold, "T06")]
    rows[1]["variant"] = "before"

    groups = runner.build_report(gold, rows)["groups"]

    assert set(groups) == {
        "route_regression/before/threecan/heldout",
        "route_regression/candidate/threecan/baseline",
    }


def test_cli_requires_explicit_output(tmp_path: Path) -> None:
    runner = load_runner()
    gold_path = tmp_path / "gold.json"
    obs_path = tmp_path / "observations.ndjson"
    gold_path.write_text(json.dumps(make_gold()), encoding="utf-8")
    obs_path.write_text("", encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        runner.main(["--gold", str(gold_path), "--observations", str(obs_path)])

    assert exc.value.code == 2
    assert list(tmp_path.glob("*report*")) == []


def test_unknown_case_is_reported_without_scoring() -> None:
    runner = load_runner()
    gold = make_gold()
    observation = make_observation(gold)
    observation["case_id"] = "UNKNOWN"

    report = runner.build_report(gold, [observation])

    assert report["status"] == "PARTIAL"
    assert report["unknown_observation_cases"] == ["UNKNOWN"]
    assert report["observation_count"] == 0


def test_live_external_case_requires_freshness_binding() -> None:
    runner = load_runner()
    gold = copy.deepcopy(make_gold())
    gold["cases"][0]["truth_mode"] = "live_external"

    with pytest.raises(runner.InvalidGold, match="live_external_binding_missing"):
        runner.validate_gold(gold)


def test_lane_a_can_prove_canonical_source_without_route() -> None:
    runner = load_runner()
    gold = make_gold()
    observation = make_observation(
        gold,
        experiment="opc_utility",
        lane="no_threecan",
    )
    evidence = gold["cases"][0]["gold"]["required_facts"][0]["evidence"][0]
    observation["events"] = [{"kind": "file_read", **evidence}]

    row = runner.score_observation(gold, gold["cases"][0], observation)

    assert row["canonical_node_found"] is False
    assert row["canonical_source_found"] is True
    assert row["canonical_source_reads"] == [evidence]


def test_route_hit_does_not_prove_canonical_source_read() -> None:
    runner = load_runner()
    gold = make_gold()
    observation = make_observation(gold)

    row = runner.score_observation(gold, gold["cases"][0], observation)

    assert row["canonical_node_found"] is True
    assert row["canonical_source_found"] is False
    assert row["canonical_source_reads"] == []


def test_opc_threecan_lane_requires_exactly_one_route() -> None:
    runner = load_runner()
    gold = make_gold()
    observation = make_observation(
        gold,
        experiment="opc_utility",
        lane="threecan",
    )
    observation["events"] = [{"kind": "retrieve"}]

    row = runner.score_observation(gold, gold["cases"][0], observation)

    assert row["status"] == "INVALID_LANE"


def test_missing_answer_facts_and_elapsed_are_incomplete_evidence() -> None:
    runner = load_runner()
    gold = make_gold()
    observation = make_observation(gold)
    observation["answer_facts"] = {}
    observation["elapsed_ms"] = None

    report = runner.build_report(gold, [observation])
    row = report["cases"][0]

    assert report["status"] == "PARTIAL"
    assert row["status"] == "INCOMPLETE_EVIDENCE"
    assert row["incomplete_evidence"] == [
        "required_answer_facts_missing",
        "elapsed_ms_invalid",
    ]
    assert row["missing_answer_fact_keys"] == ["answer"]
    assert report["groups"] == {}


def test_live_external_signal_is_not_a_typed_evidence_receipt() -> None:
    runner = load_runner()
    gold = make_gold()
    case = gold["cases"][0]
    case["truth_mode"] = "live_external"
    case["gold"]["external_verification"] = {
        "captured_at": "2026-08-10T00:00:00Z",
        "max_age_seconds": 3600,
        "evidence_sha256": "d" * 64,
    }
    observation = make_observation(gold)
    observation["events"][0]["route_meta"]["current_reality_policy"] = {
        "external_verification_required": True,
    }

    signaled = runner.score_observation(gold, case, observation)
    observation["events"].append(
        {
            "kind": "external_verification",
            "status": "verified",
            "evidence_sha256": "d" * 64,
        }
    )
    verified = runner.score_observation(gold, case, observation)

    assert signaled["external_verification_signaled"] is True
    assert signaled["canonical_source_found"] is False
    assert signaled["external_evidence_receipts"] == []
    assert verified["canonical_source_found"] is True
    assert verified["external_evidence_receipts"] == [
        {"evidence_sha256": "d" * 64, "status": "verified"}
    ]


def test_missing_cases_and_failed_rows_are_partial_and_not_aggregated() -> None:
    runner = load_runner()
    gold = make_gold()
    observation = make_observation(gold)
    observation["status"] = "FAILED"

    report = runner.build_report(gold, [observation])

    assert report["status"] == "PARTIAL"
    assert len(report["missing_observations"]) == 9
    assert report["non_complete_cases"] == ["T00"]
    assert report["groups"] == {}


def test_live_external_binding_expires() -> None:
    runner = load_runner()
    gold = make_gold()
    gold["cases"][0]["truth_mode"] = "live_external"
    gold["cases"][0]["gold"]["external_verification"] = {
        "captured_at": "2026-08-10T00:00:00Z",
        "max_age_seconds": 60,
        "evidence_sha256": "d" * 64,
    }

    with pytest.raises(runner.InvalidGold, match="live_external_binding_expired"):
        runner.validate_gold(
            gold,
            now=runner._utc_timestamp("2026-08-10T00:02:00Z"),
        )
