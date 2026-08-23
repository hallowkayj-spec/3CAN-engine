from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


RUNNER_PATH = (
    Path(__file__).resolve().parents[1]
    / "benchmark"
    / "error_knowledge_benchmark.py"
)
SPEC = importlib.util.spec_from_file_location("error_knowledge_benchmark", RUNNER_PATH)
assert SPEC and SPEC.loader
BENCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BENCH)


def _dataset() -> dict:
    positives = []
    for index in range(5):
        positives.append(
            {
                "id": f"P{index}",
                "kind": "positive",
                "query": f"reviewed error query {index}",
                "expected_top1": [f"ERR-case-{index}"],
                "expected_top3": [f"ERR-case-{index}"],
                "forbidden_top3": ["ERR-case-wrong"] if index == 0 else [],
            }
        )
    return {
        "schema_version": BENCH.DATASET_SCHEMA,
        "policy_version": BENCH.POLICY_VERSION,
        "review": {
            "status": "approved",
            "reviewed_by": "fixture-reviewer",
            "reviewed_at": "2026-08-23T00:00:00+00:00",
            "ground_truth_source": "manual_review",
        },
        "graph_binding": {
            "required_profile_id": "fixture-profile",
            "required_source_manifest_sha256": "a" * 64,
            "required_node_ids": [
                *(f"ERR-case-{index}" for index in range(5)),
                "ERR-case-wrong",
            ],
        },
        "thresholds": dict(BENCH.RELEASE_GATES),
        "queries": [
            *positives,
            {
                "id": "N0",
                "kind": "negative",
                "query": "ordinary feature implementation",
            },
        ],
    }


def _routed(latency_ms: float = 10.0):
    return [
        *[
            ([f"ERR-case-{index}", "DOC-unrelated"], latency_ms)
            for index in range(5)
        ],
        (["DOC-unrelated"], latency_ms),
    ]


def test_dataset_requires_reviewed_ground_truth_and_strict_gates() -> None:
    dataset = _dataset()
    normalized = BENCH.validate_dataset(dataset)
    assert len(normalized["queries"]) == 6

    dataset["review"]["ground_truth_source"] = "self_generated"
    with pytest.raises(BENCH.BenchmarkError, match="ground-truth review"):
        BENCH.validate_dataset(dataset)

    dataset = _dataset()
    dataset["thresholds"]["top3_recall_min"] = 0.50
    with pytest.raises(BENCH.BenchmarkError, match="weakens release gate"):
        BENCH.validate_dataset(dataset)


def test_evaluation_stays_validating_without_matching_latency_baseline() -> None:
    dataset = BENCH.validate_dataset(_dataset())
    metrics, cases, gates, status = BENCH.evaluate_results(
        dataset,
        _routed(),
        baseline_receipt=None,
        suite_sha256="suite-hash",
    )

    assert metrics["top1_recall"] == 1.0
    assert metrics["top3_recall"] == 1.0
    assert metrics["incorrect_family_rate"] == 0.0
    assert metrics["false_positive_error_rate"] == 0.0
    assert gates["latency_p95_regression"]["status"] == "validating"
    assert status == "VALIDATING"
    assert all("query" not in case for case in cases)


def test_evaluation_passes_with_matching_latency_baseline() -> None:
    dataset = BENCH.validate_dataset(_dataset())
    baseline = {
        "schema_version": BENCH.RECEIPT_SCHEMA,
        "suite_sha256": "suite-hash",
        "metrics": {"latency_p95_ms": 10.0},
    }
    _metrics, _cases, gates, status = BENCH.evaluate_results(
        dataset,
        _routed(latency_ms=10.5),
        baseline_receipt=baseline,
        suite_sha256="suite-hash",
    )

    assert gates["latency_p95_regression"]["status"] == "pass"
    assert status == "PASS"


def test_pollution_and_negative_error_routes_fail_the_suite() -> None:
    dataset = BENCH.validate_dataset(_dataset())
    routed = _routed()
    routed[0] = (["ERR-case-wrong", "ERR-case-0"], 10.0)
    routed[-1] = (["ERR-case-4"], 10.0)
    baseline = {
        "schema_version": BENCH.RECEIPT_SCHEMA,
        "suite_sha256": "suite-hash",
        "metrics": {"latency_p95_ms": 10.0},
    }

    metrics, _cases, gates, status = BENCH.evaluate_results(
        dataset,
        routed,
        baseline_receipt=baseline,
        suite_sha256="suite-hash",
    )

    assert metrics["incorrect_family_rate"] == 1.0
    assert metrics["false_positive_error_rate"] == 1.0
    assert gates["incorrect_family_rate"]["status"] == "fail"
    assert gates["false_positive_error_rate"]["status"] == "fail"
    assert status == "FAIL"


def test_run_writes_sanitized_receipt(monkeypatch, tmp_path: Path) -> None:
    dataset_path = tmp_path / "dataset.json"
    output_path = tmp_path / "receipt.json"
    dataset_path.write_text(json.dumps(_dataset()), encoding="utf-8")
    monkeypatch.setattr(
        BENCH,
        "_deep_readiness",
        lambda _base, _timeout: {
            "profile_id": "fixture-profile",
            "production_ready": True,
            "development_ready": False,
            "embedding_evidence": {"source_manifest_sha256": "a" * 64},
        },
    )
    monkeypatch.setattr(
        BENCH,
        "_validate_runtime_binding",
        lambda *_args: {"ok": True, "reasons": [], "missing_node_ids": []},
    )

    def route(_base, query, max_nodes, timeout_sec):
        del max_nodes, timeout_sec
        if query == "ordinary feature implementation":
            return ["DOC-unrelated"], 10.0
        index = int(query.rsplit(" ", 1)[1])
        return [f"ERR-case-{index}"], 10.0

    monkeypatch.setattr(BENCH, "_route", route)
    receipt = BENCH.run(
        dataset_path=dataset_path,
        output_path=output_path,
        base_url="http://127.0.0.1:9700",
    )

    persisted = output_path.read_text(encoding="utf-8")
    assert receipt["status"] == "VALIDATING"
    assert "reviewed error query" not in persisted
    assert "query_sha256" in persisted
