#!/usr/bin/env python3
"""3CAN Route Precision Benchmark v1.0

Academic metrics (KGQA/RAG standard):
  - MRR (Mean Reciprocal Rank): avg(1/rank_of_first_relevant). >=0.85 = coordination layer
  - Recall@1 / Recall@3: fraction of queries where the exact top-1 or any
    expected_any3 node is present in top-K (query-level Hit@K)
  - Precision@3: fraction of top-3 results that are relevant
  - nDCG@3: position-weighted ranking quality
  - Latency P50/P95: query-to-response time
  - Grep fallback rate: queries where route misses and agent would need grep

Thresholds:
  Index layer:        MRR < 0.70, Recall@3 < 0.80, grep_fallback > 15%
  Coordination layer: MRR >= 0.85, Recall@3 >= 0.95, grep_fallback < 5%

Usage:
  python run_benchmark.py                   # full run
  python run_benchmark.py --category short-code  # single category
  python run_benchmark.py --verbose         # show each query result
"""

import argparse
import datetime as dt
import json
import math
import os
import time
from pathlib import Path

import requests

BASE = os.environ.get("THREECAN_BASE_URL", "http://127.0.0.1:9700").rstrip("/")
BENCHMARK_FILE = Path(__file__).parent / "route_benchmark_v1.json"
ACTIVITY_LOG = Path(__file__).resolve().parents[1] / "graph" / "activity_log.json"
NODES_DIR = Path(__file__).resolve().parents[1] / "graph" / "nodes"


def validate_graph_binding(bench: dict, node_exists=None) -> dict:
    """Reject benchmark scores produced against the wrong graph fixture."""

    binding = bench.get("graph_binding")
    if not isinstance(binding, dict):
        return {"ok": False, "reason": "graph_binding_missing", "missing": []}
    if binding.get("schema_version") != "3can.benchmark-graph-binding/v1":
        return {"ok": False, "reason": "graph_binding_schema_invalid", "missing": []}
    required = binding.get("required_node_ids")
    if not isinstance(required, list) or not required:
        return {"ok": False, "reason": "required_node_ids_missing", "missing": []}

    if node_exists is None:
        def node_exists(node_id):
            response = requests.get(
                f"{BASE}/api/nodes/{node_id}",
                timeout=5,
            )
            return response.status_code == 200

    missing = [
        str(node_id)
        for node_id in required
        if not node_exists(str(node_id))
    ]
    return {
        "ok": not missing,
        "reason": "ok" if not missing else "required_nodes_absent",
        "required_count": len(required),
        "missing": missing,
    }


def route(query: str, max_nodes: int = 6) -> tuple[list[str], dict[str, float], float]:
    """Execute a route query. Returns (node_ids_ordered, scores, latency_ms)."""
    t0 = time.perf_counter()
    r = requests.post(f"{BASE}/api/route", json={
        "task": query, "max_nodes": max_nodes, "agent_id": "benchmark-runner"
    }, timeout=10)
    latency = (time.perf_counter() - t0) * 1000
    data = r.json()
    # v8.2 slim mode returns "nodes", v7 returns "activated_nodes"
    nodes = data.get("nodes", data.get("activated_nodes", []))
    scores = data.get("scores", {})
    ids = [n["id"] for n in nodes]
    return ids, scores, latency


def reciprocal_rank(result_ids: list[str], expected: list[str]) -> float:
    """1/rank of first relevant result. 0 if not found."""
    for i, rid in enumerate(result_ids):
        if rid in expected:
            return 1.0 / (i + 1)
    return 0.0


def recall_at_k(result_ids: list[str], expected: list[str], k: int) -> float:
    """Fraction of expected nodes found in top-K."""
    top_k = set(result_ids[:k])
    found = sum(1 for e in expected if e in top_k)
    return found / len(expected) if expected else 0.0


def any_at_k(result_ids: list[str], expected: list[str], k: int) -> float:
    """Return one when any acceptable node appears in the first k results."""

    expected_set = set(expected)
    return 1.0 if any(node_id in expected_set for node_id in result_ids[:k]) else 0.0


def precision_at_k(result_ids: list[str], expected: list[str], k: int) -> float:
    """Fraction of top-K results that are relevant."""
    top_k = result_ids[:k]
    relevant = sum(1 for r in top_k if r in expected)
    return relevant / k if k > 0 else 0.0


def node_age_days(node_id: str, now: dt.datetime) -> float | None:
    """v2 指标辅助: 读 graph/nodes/{id}.json 的 created_at, 返 age (天). None 若读不到."""
    p = NODES_DIR / f"{node_id}.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        ts = data.get("created_at")
        if not ts:
            return None
        created = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return (now - created).total_seconds() / 86400
    except Exception:
        return None


def gap_to_learn_latency_hours() -> float | None:
    """v2 指标: scan activity_log, 对齐 empty-route 事件 与 后续同关键词的 node_created, 返中位数小时.
    粗口径: 无可对齐时返 None (N/A).
    """
    if not ACTIVITY_LOG.exists():
        return None
    try:
        log = json.loads(ACTIVITY_LOG.read_text(encoding="utf-8"))
    except Exception:
        return None
    events = log if isinstance(log, list) else log.get("entries", [])
    # 提取 empty route: action=route, affected_nodes 为空
    gaps: list[tuple[dt.datetime, str]] = []
    creations: list[tuple[dt.datetime, str]] = []
    for e in events:
        ts_raw = e.get("timestamp") or e.get("ts") or ""
        try:
            ts = dt.datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        except Exception:
            continue
        action = e.get("action", "")
        desc = (e.get("description") or "")[:200]
        affected = e.get("affected_nodes") or []
        if action == "route" and not affected:
            gaps.append((ts, desc))
        elif action in ("create_node", "node_created"):
            creations.append((ts, desc))
    if not gaps or not creations:
        return None
    # 对齐: 每个 gap 找后续 24h 内第一个 creation 事件, 取时差
    latencies: list[float] = []
    for gt, _ in gaps:
        candidate = [(ct - gt).total_seconds() / 3600 for ct, _ in creations if ct > gt and (ct - gt).total_seconds() < 86400 * 7]
        if candidate:
            latencies.append(min(candidate))
    if not latencies:
        return None
    latencies.sort()
    return round(latencies[len(latencies) // 2], 2)


def ndcg_at_k(result_ids: list[str], expected: list[str], k: int) -> float:
    """Normalized DCG: position-weighted relevance."""
    def dcg(rels):
        return sum(r / math.log2(i + 2) for i, r in enumerate(rels))

    top_k = result_ids[:k]
    gains = [1.0 if r in expected else 0.0 for r in top_k]
    ideal = sorted(gains, reverse=True)
    ideal_dcg = dcg(ideal)
    if ideal_dcg == 0:
        return 0.0
    return dcg(gains) / ideal_dcg


def run_benchmark(category_filter=None, verbose=False, output_path=None):
    """Run full benchmark and report metrics."""
    bench = json.load(open(BENCHMARK_FILE, encoding="utf-8"))
    binding = validate_graph_binding(bench)
    if not binding["ok"]:
        print("3CAN Route Benchmark: INVALID GRAPH BINDING")
        print(json.dumps(binding, ensure_ascii=False, indent=2))
        return {"status": "INVALID_GRAPH_BINDING", **binding}
    queries = bench["queries"]
    if category_filter:
        queries = [q for q in queries if q["category"] == category_filter]

    print(f"3CAN Route Benchmark v1.0 — {len(queries)} queries")
    print(f"{'='*70}\n")

    mrrs, recall1s, recall3s, prec3s, ndcg3s, latencies = [], [], [], [], [], []
    grep_needed = 0
    by_category = {}
    by_difficulty = {}
    # v2 新增: 按 expected_top1 节点的 age 分桶
    now_utc = dt.datetime.now(dt.timezone.utc)
    by_age_bucket = {"current_<1d": [], "recent_1-7d": [], "archive_>7d": [], "unknown": []}

    for q in queries:
        qid = q["id"]
        query = q["query"]
        exp_top1 = q["expected_top1"]
        exp_any3 = q["expected_any3"]
        all_expected = list(dict.fromkeys([exp_top1, *exp_any3]))

        try:
            result_ids, scores, latency = route(query)
        except Exception as e:
            if verbose:
                print(f"  {qid} ERROR: {e}")
            continue

        # Metrics
        mrr = reciprocal_rank(result_ids, all_expected)
        r1 = 1.0 if result_ids and result_ids[0] in [exp_top1] else 0.0
        r3 = any_at_k(result_ids, exp_any3, 3)
        p3 = precision_at_k(result_ids, all_expected, 3)
        n3 = ndcg_at_k(result_ids, all_expected, 3)
        needs_grep = mrr == 0  # complete miss

        mrrs.append(mrr)
        recall1s.append(r1)
        recall3s.append(r3)
        prec3s.append(p3)
        ndcg3s.append(n3)
        latencies.append(latency)
        if needs_grep:
            grep_needed += 1

        # Category/difficulty tracking
        cat = q["category"]
        diff = q["difficulty"]
        by_category.setdefault(cat, []).append({"mrr": mrr, "r1": r1, "r3": r3})
        by_difficulty.setdefault(diff, []).append({"mrr": mrr, "r1": r1, "r3": r3})

        # v2 cross-session continuity: 按 expected_top1 节点年龄分桶
        age = node_age_days(exp_top1, now_utc)
        if age is None:
            bucket = "unknown"
        elif age < 1:
            bucket = "current_<1d"
        elif age < 7:
            bucket = "recent_1-7d"
        else:
            bucket = "archive_>7d"
        by_age_bucket[bucket].append(mrr)

        if verbose:
            hit = "HIT" if r1 > 0 else ("top3" if mrr > 0 else "MISS")
            top1_id = result_ids[0][:35] if result_ids else "N/A"
            top1_score = scores.get(result_ids[0], 0) if result_ids else 0
            print(f"  {qid:4s} [{hit:4s}] MRR={mrr:.2f} | top1={top1_id} ({top1_score:.3f}) | {latency:.0f}ms | {query[:40]}")

    n = len(mrrs)
    if n == 0:
        print("No queries executed.")
        return

    def avg(xs):
        return sum(xs) / len(xs)

    def p95(xs):
        return sorted(xs)[int(len(xs) * 0.95)] if xs else 0

    print(f"\n{'='*70}")
    print(f"OVERALL ({n} queries)")
    print(f"{'='*70}")
    mrr_tag = "PASS" if avg(mrrs) >= 0.85 else "WARN" if avg(mrrs) >= 0.70 else "FAIL"
    r3_tag = "PASS" if avg(recall3s) >= 0.95 else "WARN" if avg(recall3s) >= 0.80 else "FAIL"
    grep_tag = "PASS" if grep_needed/n < 0.05 else "WARN" if grep_needed/n < 0.15 else "FAIL"
    print(f"  MRR:            {avg(mrrs):.4f}  [{mrr_tag}]")
    print(f"  Recall@1:       {avg(recall1s):.4f}  (top1 exact match)")
    print(f"  Recall@3:       {avg(recall3s):.4f}  [{r3_tag}]")
    print(f"  Precision@3:    {avg(prec3s):.4f}")
    print(f"  nDCG@3:         {avg(ndcg3s):.4f}")
    print(f"  Latency P50:    {sorted(latencies)[n//2]:.0f}ms")
    print(f"  Latency P95:    {p95(latencies):.0f}ms")
    print(f"  Grep fallback:  {grep_needed}/{n} ({100*grep_needed/n:.1f}%)  [{grep_tag}]")

    # ── v2 三指标 ──
    grep_repl_ratio = 1 - grep_needed / n
    cross_session_mrr = (sum(by_age_bucket["archive_>7d"]) / len(by_age_bucket["archive_>7d"])
                         if by_age_bucket["archive_>7d"] else None)
    gap_lat_h = gap_to_learn_latency_hours()
    print("\nV2 METRICS:")
    print(f"  grep_replacement_ratio:   {grep_repl_ratio:.4f}  (1 - grep_fallback)")
    if cross_session_mrr is not None:
        n_archive = len(by_age_bucket['archive_>7d'])
        print(f"  cross_session_continuity: {cross_session_mrr:.4f}  (MRR on queries for nodes >7d old, n={n_archive})")
    else:
        print("  cross_session_continuity: N/A  (无 archive 节点测试用例)")
    if gap_lat_h is not None:
        print(f"  gap_to_learn_latency_h:   {gap_lat_h:.2f}h  (median: empty-route → 下个节点创建)")
    else:
        print("  gap_to_learn_latency_h:   N/A  (activity_log 无足够对齐数据)")
    print("  age buckets MRR:  " + " | ".join(
        f"{k}: {sum(v)/len(v):.3f} (n={len(v)})" if v else f"{k}: - (n=0)"
        for k, v in by_age_bucket.items()
    ))

    verdict_mrr = avg(mrrs) >= 0.85
    verdict_r3 = avg(recall3s) >= 0.95
    verdict_grep = grep_needed / n < 0.05
    if verdict_mrr and verdict_r3 and verdict_grep:
        print("\n  VERDICT: COORDINATION LAYER ESTABLISHED [PASS]")
    elif avg(mrrs) >= 0.70:
        print("\n  VERDICT: HIGH-QUALITY INDEX LAYER (approaching coordination)")
    else:
        print("\n  VERDICT: INDEX LAYER (needs improvement)")

    # By category
    print("\nBY CATEGORY:")
    for cat, items in sorted(by_category.items()):
        cat_mrr = avg([i["mrr"] for i in items])
        cat_r1 = avg([i["r1"] for i in items])
        print(f"  {cat:20s} n={len(items):2d}  MRR={cat_mrr:.3f}  R@1={cat_r1:.3f}")

    # By difficulty
    print("\nBY DIFFICULTY:")
    for diff, items in sorted(by_difficulty.items()):
        d_mrr = avg([i["mrr"] for i in items])
        d_r1 = avg([i["r1"] for i in items])
        print(f"  Difficulty {diff}       n={len(items):2d}  MRR={d_mrr:.3f}  R@1={d_r1:.3f}")

    # Save results
    report = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "total_queries": n,
        "MRR": round(avg(mrrs), 4),
        "Recall@1": round(avg(recall1s), 4),
        "Recall@3": round(avg(recall3s), 4),
        "Hit@3": round(avg(recall3s), 4),
        "Precision@3": round(avg(prec3s), 4),
        "nDCG@3": round(avg(ndcg3s), 4),
        "latency_p50_ms": round(sorted(latencies)[n//2]),
        "latency_p95_ms": round(p95(latencies)),
        "grep_fallback_rate": round(grep_needed / n, 4),
        "verdict": "coordination" if (verdict_mrr and verdict_r3 and verdict_grep) else "index",
        # v2 Wave 1
        "grep_replacement_ratio": round(grep_repl_ratio, 4),
        "cross_session_continuity": round(cross_session_mrr, 4) if cross_session_mrr is not None else None,
        "gap_to_learn_latency_h": gap_lat_h,
        "age_bucket_mrr": {k: round(sum(v) / len(v), 4) if v else None for k, v in by_age_bucket.items()},
    }
    out = Path(output_path) if output_path else Path(__file__).parent / "results_latest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nResults saved to {out}")
    return {"status": "COMPLETED", **report}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=BASE)
    parser.add_argument("--category")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args()
    BASE = args.base_url.rstrip("/")
    result = run_benchmark(
        category_filter=args.category,
        verbose=args.verbose,
        output_path=args.output,
    )
    if result.get("status") != "COMPLETED":
        raise SystemExit(2)
