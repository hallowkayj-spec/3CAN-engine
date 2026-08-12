"""Substrate Bench Runner — L2 Project Substrate benchmark.

Reads substrate_bench_v1.json, queries live 3CAN at localhost:9700, scores:
- top1_accuracy: expected_top1 appears at rank 1
- top3_recall: fraction of expected_top3 appearing in top-3
- err_proactive_rate_at_top3: for err-proactive dim, ERR-* appears in top-3
- latency_ms: route latency per query
- confidence_distribution: count of high/medium/low

Run: python benchmark/substrate_bench_runner.py [--port 9700] [--verbose]
"""
from __future__ import annotations
import argparse
import json
import time
import urllib.request
from collections import Counter
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
SUITE = ROOT / "benchmark" / "substrate_bench_v1.json"
OUT_DIR = ROOT / "benchmark" / "_substrate"


def validate_graph_binding(port: int, suite: dict, node_exists=None) -> dict:
    binding = suite.get("graph_binding")
    if not isinstance(binding, dict):
        return {"ok": False, "reason": "graph_binding_missing", "missing": []}
    if binding.get("schema_version") != "3can.benchmark-graph-binding/v1":
        return {"ok": False, "reason": "graph_binding_schema_invalid", "missing": []}
    required = binding.get("required_node_ids")
    if not isinstance(required, list) or not required:
        return {"ok": False, "reason": "required_node_ids_missing", "missing": []}

    if node_exists is None:
        def node_exists(node_id: str) -> bool:
            request = urllib.request.Request(
                f"http://localhost:{port}/api/nodes/{quote(node_id, safe='')}",
                method="GET",
            )
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    return response.status == 200
            except Exception:
                return False

    missing = [str(node_id) for node_id in required if not node_exists(str(node_id))]
    return {
        "ok": not missing,
        "reason": "ok" if not missing else "required_nodes_absent",
        "required_count": len(required),
        "missing": missing,
    }


def route(port: int, question: str, max_nodes: int = 5) -> tuple[list[str], str, int, dict]:
    body = json.dumps({
        "task": question, "max_nodes": max_nodes, "mode": "slim",
        "agent_id": "substrate-bench",
        "confirm_low_confidence": True, "allow_degraded": True,
    }).encode("utf-8")
    t0 = time.perf_counter()
    req = urllib.request.Request(f"http://localhost:{port}/api/route", data=body,
                                  method="POST", headers={"Content-Type": "application/json"})
    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=30).read().decode("utf-8"))
    except Exception as e:
        return [], "error", int((time.perf_counter() - t0) * 1000), {"error": str(e)[:200]}
    latency_ms = int((time.perf_counter() - t0) * 1000)
    nodes = resp.get("nodes") or resp.get("activated_nodes") or []
    ids = [n.get("id", "") for n in nodes]
    conf = resp.get("confidence", "unknown")
    return ids, conf, latency_ms, resp


def score_case(case: dict, ids: list[str]) -> dict:
    etop1 = case.get("expected_top1")
    etop3 = case.get("expected_top3") or []
    top1_hit = (etop1 is not None) and (len(ids) > 0) and (ids[0] == etop1)
    top3_hits = sum(1 for e in etop3 if e in ids[:3])
    top3_recall = top3_hits / len(etop3) if etop3 else 1.0
    # ERR proactive rate: for err-proactive dim, any ERR-* in top 3
    err_in_top3 = any(x.startswith("ERR-") for x in ids[:3])
    return {
        "top1_match": top1_hit,
        "top3_hits": top3_hits, "top3_expected": len(etop3),
        "top3_recall": round(top3_recall, 3),
        "err_in_top3": err_in_top3,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9700)
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--output-dir", type=Path, default=OUT_DIR)
    args = ap.parse_args()

    suite = json.loads(SUITE.read_text(encoding="utf-8"))
    binding = validate_graph_binding(args.port, suite)
    if not binding["ok"]:
        print("3CAN Substrate Benchmark: INVALID GRAPH BINDING")
        print(json.dumps(binding, ensure_ascii=False, indent=2))
        return 2
    cases = suite["cases"]
    args.output_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for c in cases:
        ids, conf, latency_ms, _raw = route(args.port, c["question"], max_nodes=5)
        s = score_case(c, ids)
        s.update({
            "id": c["id"], "dimension": c["dimension"],
            "confidence": conf, "latency_ms": latency_ms,
            "top5_ids": ids[:5],
        })
        results.append(s)
        flag = "OK" if s["top1_match"] or s["top3_recall"] >= 0.5 else "X "
        print(f"  [{c['id']}] {flag} {c['dimension']:25s} top1={s['top1_match']} t3r={s['top3_recall']:.2f} "
              f"conf={conf} lat={latency_ms}ms")
        if args.verbose:
            print(f"    Q: {c['question'][:80]}")
            print(f"    top5: {ids[:5]}")

    total = len(results)
    top1_acc = sum(1 for r in results if r["top1_match"]) / total if total else 0
    t3r = sum(r["top3_recall"] for r in results) / total if total else 0
    by_dim_top1 = {}
    for d in set(r["dimension"] for r in results):
        sub = [r for r in results if r["dimension"] == d]
        by_dim_top1[d] = round(sum(1 for r in sub if r["top1_match"]) / len(sub), 3) if sub else 0
    conf_dist = Counter(r["confidence"] for r in results)
    err_dim_rate = None
    err_cases = [r for r in results if r["dimension"] == "err-proactive"]
    if err_cases:
        err_dim_rate = round(sum(1 for r in err_cases if r["err_in_top3"]) / len(err_cases), 3)
    latencies = sorted(r["latency_ms"] for r in results)
    p50 = latencies[len(latencies) // 2] if latencies else 0
    p95 = latencies[int(len(latencies) * 0.95)] if latencies else 0

    print()
    print(f"=== Substrate-Bench v1 — n={total} ===")
    print(f"  top1_accuracy: {top1_acc:.3f}")
    print(f"  top3_recall (mean): {t3r:.3f}")
    print("  by_dimension top1:", {k: v for k, v in sorted(by_dim_top1.items())})
    print(f"  err_proactive_rate_at_top3: {err_dim_rate}")
    print(f"  confidence: {dict(conf_dist)}")
    print(f"  latency: p50={p50}ms p95={p95}ms")

    out = args.output_dir / f"substrate_{int(time.time())}.json"
    out.write_text(json.dumps({
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "n_cases": total,
        "top1_accuracy": round(top1_acc, 4),
        "top3_recall_mean": round(t3r, 4),
        "by_dimension_top1": by_dim_top1,
        "err_proactive_rate_at_top3": err_dim_rate,
        "confidence_distribution": dict(conf_dist),
        "latency_p50_ms": p50, "latency_p95_ms": p95,
        "results": results,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print(f"Saved: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
