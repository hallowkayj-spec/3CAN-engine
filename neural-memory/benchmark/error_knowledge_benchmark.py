#!/usr/bin/env python3
"""Run a reviewed ErrorKnowledge retrieval suite and emit a typed receipt.

The dataset is intentionally external to the public release: production error
queries and node IDs can contain project-specific information. The receipt
stores only hashes, result IDs, aggregate metrics, and runtime binding evidence.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

import requests


DATASET_SCHEMA = "3can.error-knowledge-benchmark/v1"
RECEIPT_SCHEMA = "3can.error-knowledge-benchmark-receipt/v1"
POLICY_VERSION = "3can.error-knowledge-benchmark-policy/v1"
RELEASE_GATES = {
    "top1_recall_min": 0.80,
    "top3_recall_min": 0.95,
    "incorrect_family_rate_max": 0.01,
    "false_positive_error_rate_max": 0.01,
    "latency_p95_regression_max": 0.10,
}


class BenchmarkError(RuntimeError):
    """Raised when benchmark evidence cannot be trusted."""


def _canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _suite_sha256(dataset: Mapping[str, Any]) -> str:
    return _sha256_bytes(
        _canonical_json_bytes(
            {
                "schema_version": dataset["schema_version"],
                "policy_version": dataset["policy_version"],
                "thresholds": dataset["thresholds"],
                "queries": dataset["queries"],
            }
        )
    )


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BenchmarkError(f"cannot read valid UTF-8 JSON from {path}: {exc}") from exc


def _atomic_write_json(path: Path, value: Any) -> None:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _string_list(value: Any, *, field: str, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise BenchmarkError(f"{field} must be a list of non-empty strings")
    result = list(dict.fromkeys(item.strip() for item in value))
    if not result and not allow_empty:
        raise BenchmarkError(f"{field} must not be empty")
    return result


def validate_dataset(dataset: Any) -> dict[str, Any]:
    if not isinstance(dataset, dict) or dataset.get("schema_version") != DATASET_SCHEMA:
        raise BenchmarkError(f"dataset must use {DATASET_SCHEMA}")
    if str(dataset.get("policy_version") or "") != POLICY_VERSION:
        raise BenchmarkError(f"dataset must bind policy_version={POLICY_VERSION}")
    review = dataset.get("review")
    if (
        not isinstance(review, Mapping)
        or review.get("status") != "approved"
        or not str(review.get("reviewed_by") or "").strip()
        or not str(review.get("reviewed_at") or "").strip()
        or review.get("ground_truth_source")
        not in {"manual_review", "verified_incident_receipts"}
    ):
        raise BenchmarkError("dataset requires approved independent ground-truth review")
    if review.get("ground_truth_source") in {
        "route_output",
        "embedding_neighbors",
        "self_generated",
    }:
        raise BenchmarkError("runtime output cannot be its own benchmark ground truth")

    binding = dataset.get("graph_binding")
    if not isinstance(binding, Mapping):
        raise BenchmarkError("dataset graph_binding is required")
    profile_id = str(binding.get("required_profile_id") or "").strip()
    source_manifest = str(
        binding.get("required_source_manifest_sha256") or ""
    ).strip()
    if not profile_id or len(source_manifest) != 64:
        raise BenchmarkError("dataset must bind runtime profile and source manifest")
    required_node_ids = _string_list(
        binding.get("required_node_ids"),
        field="graph_binding.required_node_ids",
    )

    thresholds = dataset.get("thresholds")
    if not isinstance(thresholds, Mapping):
        raise BenchmarkError("dataset thresholds are required")
    normalized_thresholds: dict[str, float] = {}
    for key, release_value in RELEASE_GATES.items():
        try:
            value = float(thresholds[key])
        except (KeyError, TypeError, ValueError) as exc:
            raise BenchmarkError(f"invalid benchmark threshold: {key}") from exc
        weaker = value < release_value if key.endswith("_min") else value > release_value
        if weaker:
            raise BenchmarkError(f"dataset weakens release gate {key}")
        normalized_thresholds[key] = value

    queries = dataset.get("queries")
    if not isinstance(queries, list) or not queries:
        raise BenchmarkError("dataset queries must be a non-empty list")
    normalized_queries = []
    seen_ids: set[str] = set()
    positive_count = 0
    negative_count = 0
    pollution_cases = 0
    for item in queries:
        if not isinstance(item, Mapping):
            raise BenchmarkError("every benchmark query must be an object")
        query_id = str(item.get("id") or "").strip()
        query = str(item.get("query") or "").strip()
        kind = str(item.get("kind") or "").strip()
        if not query_id or query_id in seen_ids or not query or kind not in {
            "positive",
            "negative",
        }:
            raise BenchmarkError("benchmark query ids, text, and kind must be valid")
        seen_ids.add(query_id)
        if kind == "positive":
            positive_count += 1
            expected_top1 = _string_list(
                item.get("expected_top1"),
                field=f"queries[{query_id}].expected_top1",
            )
            expected_top3 = _string_list(
                item.get("expected_top3"),
                field=f"queries[{query_id}].expected_top3",
            )
            if not set(expected_top1).issubset(set(expected_top3)):
                raise BenchmarkError(f"{query_id} top-1 ground truth must be allowed in top-3")
            forbidden_top3 = _string_list(
                item.get("forbidden_top3", []),
                field=f"queries[{query_id}].forbidden_top3",
                allow_empty=True,
            )
            if set(expected_top3) & set(forbidden_top3):
                raise BenchmarkError(f"{query_id} has conflicting expected and forbidden nodes")
            if forbidden_top3:
                pollution_cases += 1
        else:
            negative_count += 1
            expected_top1 = []
            expected_top3 = []
            forbidden_top3 = []
        normalized_queries.append(
            {
                "id": query_id,
                "query": query,
                "kind": kind,
                "expected_top1": expected_top1,
                "expected_top3": expected_top3,
                "forbidden_top3": forbidden_top3,
            }
        )
    if positive_count < 5 or negative_count < 1 or pollution_cases < 1:
        raise BenchmarkError(
            "representative suite requires at least 5 positive, 1 negative, and 1 pollution case"
        )
    if not set(required_node_ids).issuperset(
        {
            node_id
            for item in normalized_queries
            for key in ("expected_top1", "expected_top3", "forbidden_top3")
            for node_id in item[key]
        }
    ):
        raise BenchmarkError("graph binding must include every reviewed node id")
    return {
        **dataset,
        "graph_binding": {
            **dict(binding),
            "required_profile_id": profile_id,
            "required_source_manifest_sha256": source_manifest,
            "required_node_ids": required_node_ids,
        },
        "thresholds": normalized_thresholds,
        "queries": normalized_queries,
    }


def _deep_readiness(base_url: str, timeout_sec: float) -> dict[str, Any]:
    response = requests.get(
        f"{base_url}/api/health/ready?deep=true",
        timeout=timeout_sec,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise BenchmarkError("deep readiness response is not an object")
    return payload


def _validate_runtime_binding(
    base_url: str,
    dataset: Mapping[str, Any],
    readiness: Mapping[str, Any],
    timeout_sec: float,
) -> dict[str, Any]:
    binding = dataset["graph_binding"]
    evidence = readiness.get("embedding_evidence")
    evidence = evidence if isinstance(evidence, Mapping) else {}
    reasons = []
    if readiness.get("production_ready") is not True:
        reasons.append("production_not_ready")
    if str(readiness.get("profile_id") or "") != binding["required_profile_id"]:
        reasons.append("profile_mismatch")
    if (
        str(evidence.get("source_manifest_sha256") or "")
        != binding["required_source_manifest_sha256"]
    ):
        reasons.append("source_manifest_mismatch")
    missing = []
    for node_id in binding["required_node_ids"]:
        response = requests.get(f"{base_url}/api/nodes/{node_id}", timeout=timeout_sec)
        if response.status_code != 200:
            missing.append(node_id)
    if missing:
        reasons.append("required_nodes_absent")
    return {"ok": not reasons, "reasons": reasons, "missing_node_ids": missing}


def _route(base_url: str, query: str, max_nodes: int, timeout_sec: float) -> tuple[list[str], float]:
    started = time.perf_counter()
    response = requests.post(
        f"{base_url}/api/route",
        json={
            "task": query,
            "max_nodes": max_nodes,
            "agent_id": "error-knowledge-benchmark",
            "mode": "slim",
        },
        timeout=timeout_sec,
    )
    latency_ms = (time.perf_counter() - started) * 1000
    response.raise_for_status()
    payload = response.json()
    nodes = payload.get("nodes", payload.get("activated_nodes", []))
    if not isinstance(nodes, list):
        raise BenchmarkError("route response has no node list")
    ids = []
    for node in nodes:
        if not isinstance(node, Mapping) or not str(node.get("id") or "").strip():
            raise BenchmarkError("route response contains an invalid node")
        ids.append(str(node["id"]))
    return ids, latency_ms


def _percentile_95(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]


def _error_artifact(node_id: str) -> bool:
    return node_id.casefold().startswith(("err-", "errcase-", "fix-", "evd-"))


def _gate(value: float, threshold: float, *, minimum: bool) -> dict[str, Any]:
    passed = value >= threshold if minimum else value <= threshold
    return {
        "status": "pass" if passed else "fail",
        "value": round(value, 6),
        "threshold": threshold,
    }


def evaluate_results(
    dataset: Mapping[str, Any],
    routed: Sequence[tuple[Sequence[str], float]],
    *,
    baseline_receipt: Mapping[str, Any] | None,
    suite_sha256: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any], str]:
    if len(routed) != len(dataset["queries"]):
        raise BenchmarkError("query and route result counts differ")
    positive_top1: list[float] = []
    positive_top3: list[float] = []
    pollution: list[float] = []
    false_positive: list[float] = []
    latencies: list[float] = []
    cases = []
    for query, (result_ids, latency_ms) in zip(dataset["queries"], routed, strict=True):
        top3 = list(result_ids[:3])
        latencies.append(float(latency_ms))
        case = {
            "id": query["id"],
            "kind": query["kind"],
            "query_sha256": _sha256_bytes(query["query"].encode("utf-8")),
            "result_ids": list(result_ids),
            "latency_ms": round(float(latency_ms), 3),
        }
        if query["kind"] == "positive":
            top1_hit = bool(result_ids and result_ids[0] in query["expected_top1"])
            top3_hit = any(node_id in query["expected_top3"] for node_id in top3)
            polluted = any(node_id in query["forbidden_top3"] for node_id in top3)
            positive_top1.append(float(top1_hit))
            positive_top3.append(float(top3_hit))
            if query["forbidden_top3"]:
                pollution.append(float(polluted))
            case.update(
                {
                    "top1_hit": top1_hit,
                    "top3_hit": top3_hit,
                    "incorrect_family_pollution": polluted,
                }
            )
        else:
            error_hit = any(_error_artifact(node_id) for node_id in top3)
            false_positive.append(float(error_hit))
            case["false_positive_error_route"] = error_hit
        cases.append(case)

    metrics = {
        "positive_query_count": len(positive_top1),
        "negative_query_count": len(false_positive),
        "top1_recall": sum(positive_top1) / len(positive_top1),
        "top3_recall": sum(positive_top3) / len(positive_top3),
        "incorrect_family_rate": sum(pollution) / len(pollution),
        "false_positive_error_rate": sum(false_positive) / len(false_positive),
        "latency_p50_ms": sorted(latencies)[len(latencies) // 2],
        "latency_p95_ms": _percentile_95(latencies),
    }
    thresholds = dataset["thresholds"]
    gates = {
        "top1_recall": _gate(
            metrics["top1_recall"], thresholds["top1_recall_min"], minimum=True
        ),
        "top3_recall": _gate(
            metrics["top3_recall"], thresholds["top3_recall_min"], minimum=True
        ),
        "incorrect_family_rate": _gate(
            metrics["incorrect_family_rate"],
            thresholds["incorrect_family_rate_max"],
            minimum=False,
        ),
        "false_positive_error_rate": _gate(
            metrics["false_positive_error_rate"],
            thresholds["false_positive_error_rate_max"],
            minimum=False,
        ),
    }
    latency_gate: dict[str, Any] = {
        "status": "validating",
        "reason": "matching_baseline_receipt_required",
        "threshold": thresholds["latency_p95_regression_max"],
    }
    if isinstance(baseline_receipt, Mapping):
        baseline_metrics = baseline_receipt.get("metrics")
        if (
            baseline_receipt.get("schema_version") == RECEIPT_SCHEMA
            and baseline_receipt.get("suite_sha256") == suite_sha256
            and isinstance(baseline_metrics, Mapping)
            and float(baseline_metrics.get("latency_p95_ms") or 0) > 0
        ):
            baseline_p95 = float(baseline_metrics["latency_p95_ms"])
            regression = metrics["latency_p95_ms"] / baseline_p95 - 1.0
            latency_gate = _gate(
                regression,
                thresholds["latency_p95_regression_max"],
                minimum=False,
            )
            latency_gate["baseline_p95_ms"] = baseline_p95
            latency_gate["current_p95_ms"] = metrics["latency_p95_ms"]
    gates["latency_p95_regression"] = latency_gate
    if any(gate["status"] == "fail" for gate in gates.values()):
        status = "FAIL"
    elif all(gate["status"] == "pass" for gate in gates.values()):
        status = "PASS"
    else:
        status = "VALIDATING"
    return metrics, cases, gates, status


def run(
    *,
    dataset_path: Path,
    output_path: Path,
    base_url: str,
    baseline_path: Path | None = None,
    timeout_sec: float = 10.0,
) -> dict[str, Any]:
    dataset_raw = _read_json(dataset_path)
    dataset = validate_dataset(dataset_raw)
    dataset_sha256 = _sha256_bytes(_canonical_json_bytes(dataset_raw))
    suite_sha256 = _suite_sha256(dataset)
    base_url = base_url.rstrip("/")
    receipt: dict[str, Any] = {
        "schema_version": RECEIPT_SCHEMA,
        "policy_version": POLICY_VERSION,
        "status": "UNAVAILABLE",
        "executed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "dataset_sha256": dataset_sha256,
        "suite_sha256": suite_sha256,
        "runner_sha256": _sha256_file(Path(__file__).resolve()),
        "base_url_sha256": _sha256_bytes(base_url.encode("utf-8")),
    }
    try:
        readiness = _deep_readiness(base_url, timeout_sec)
        binding = _validate_runtime_binding(
            base_url,
            dataset,
            readiness,
            timeout_sec,
        )
        receipt["runtime"] = {
            "profile_id": readiness.get("profile_id"),
            "production_ready": readiness.get("production_ready"),
            "development_ready": readiness.get("development_ready"),
            "source_manifest_sha256": (
                readiness.get("embedding_evidence", {}) or {}
            ).get("source_manifest_sha256"),
            "binding": binding,
        }
        if not binding["ok"]:
            receipt["status"] = "INVALID_GRAPH_BINDING"
            return receipt
        routed = [
            _route(base_url, query["query"], max_nodes=6, timeout_sec=timeout_sec)
            for query in dataset["queries"]
        ]
        baseline = _read_json(baseline_path) if baseline_path else None
        metrics, cases, gates, status = evaluate_results(
            dataset,
            routed,
            baseline_receipt=baseline,
            suite_sha256=suite_sha256,
        )
        receipt.update(
            {
                "status": status,
                "metrics": metrics,
                "gates": gates,
                "cases": cases,
            }
        )
        return receipt
    except (BenchmarkError, OSError, requests.RequestException, ValueError) as exc:
        receipt["status"] = "UNAVAILABLE"
        receipt["error"] = {
            "code": type(exc).__name__,
            "message_sha256": _sha256_bytes(str(exc).encode("utf-8")),
        }
        return receipt
    finally:
        _atomic_write_json(output_path, receipt)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:9700")
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--timeout-sec", type=float, default=10.0)
    return parser


def main() -> int:
    args = _parser().parse_args()
    receipt = run(
        dataset_path=args.dataset.resolve(),
        output_path=args.output.resolve(),
        base_url=args.base_url,
        baseline_path=args.baseline.resolve() if args.baseline else None,
        timeout_sec=max(0.1, min(float(args.timeout_sec), 30.0)),
    )
    print(json.dumps({key: receipt.get(key) for key in ("status", "metrics", "gates")}, indent=2))
    return 0 if receipt.get("status") in {"PASS", "VALIDATING"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
