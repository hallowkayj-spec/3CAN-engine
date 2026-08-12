"""3CAN Node Quality Score (GDI) — v9.3 c

灵感: EvoMap GDI (Genetic Diversity Index), 5 维打分 → 用于 route tiebreaker + housekeeping.
但 3CAN 场景不同: 不搞 agent 能力市场, 只给每节点一个"健康度"便于治理.

5 维 (0-10):
1. structural_completeness  — name + description + notes + kws 齐全度
2. semantic_clarity         — description 具不具体 (非空话/非泛词)
3. signal_specificity       — activation_keywords 区分度 (平均 IDF)
4. utility_evidence         — activation_count 使用痕迹 (log 缩放)
5. verification_strength    — edge count 入度+出度 (被引用=被验证)

加权平均 → 0-10 综合分, 写到 content.extra.quality_score + 5 子分.

route() 目前不用这分, 留作 tiebreaker / housekeeping 目标 (低分的进 dormant 候选).

运行:
  python tools/node_gdi_scorer.py --dry-run
  python tools/node_gdi_scorer.py
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NODES_DIR = ROOT / "graph" / "nodes"
EDGES_FILE = ROOT / "graph" / "edges.json"

WEIGHTS = {
    "structural_completeness": 0.20,
    "semantic_clarity": 0.20,
    "signal_specificity": 0.20,
    "utility_evidence": 0.20,
    "verification_strength": 0.20,
}


def load_graph():
    nodes = []
    for p in NODES_DIR.glob("*.json"):
        try:
            nodes.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    edges = []
    if EDGES_FILE.exists():
        try:
            raw = json.loads(EDGES_FILE.read_text(encoding="utf-8"))
            edges = raw if isinstance(raw, list) else raw.get("edges", [])
        except Exception:
            pass
    return nodes, edges


def score_structural(node: dict) -> float:
    """0-10: name + description + notes + kws + key_files 齐全度."""
    c = node.get("content", {}) or {}
    score = 0.0
    if node.get("name", "").strip():
        score += 2.0
    desc = (c.get("description", "") or "").strip()
    if len(desc) >= 50:
        score += 2.5
    elif len(desc) >= 20:
        score += 1.5
    elif desc:
        score += 0.5
    notes = (c.get("notes", "") or "").strip() + (c.get("current_state", "") or "").strip()
    if len(notes) >= 200:
        score += 2.0
    elif len(notes) >= 50:
        score += 1.0
    kws = node.get("activation_keywords", [])
    if len(kws) >= 8:
        score += 2.0
    elif len(kws) >= 5:
        score += 1.3
    elif len(kws) >= 2:
        score += 0.7
    if c.get("key_files") or c.get("decisions") or c.get("api_refs"):
        score += 1.5
    return min(10.0, score)


def score_semantic(node: dict) -> float:
    """0-10: description 具体度 (非 '这是一个节点' 类空话)."""
    c = node.get("content", {}) or {}
    desc = (c.get("description", "") or "").strip()
    if not desc:
        return 0.0
    # 惩罚空话: 含 "此节点" "本节点" "TODO" "待补充" "PROPOSED" 等扣分
    empty_markers = ["此节点", "本节点", "TODO", "待补充", "PROPOSED", "待定", "auto-ingested", "Auto-ingested"]
    penalty = sum(2 for m in empty_markers if m in desc)
    # 奖励: 含数字 / 具体命名 / 短代码
    import re
    has_number = bool(re.search(r"\d", desc))
    has_code = bool(re.search(r"[A-Z]{2,}\d+", desc))  # S62, KB4 etc
    has_path = "/" in desc or "." in desc
    base = 5.0
    base += 2.0 if has_number else 0
    base += 1.5 if has_code else 0
    base += 1.5 if has_path else 0
    # 长度加分 (但不堆砌)
    if 40 <= len(desc) <= 300:
        base += 1.0
    return max(0.0, min(10.0, base - penalty))


def score_specificity(node: dict, kw_df: dict[str, int], N: int) -> float:
    """0-10: 节点 kws 的平均 IDF (稀有度). 高 = 专有 kw, 低 = 全是 meta."""
    kws = node.get("activation_keywords", [])
    if not kws:
        return 0.0
    idfs = []
    for kw in kws:
        if not isinstance(kw, str):
            continue
        k = kw.lower().strip()
        if len(k) < 2:
            continue
        df = kw_df.get(k, 1)
        idf = math.log((N + 1) / (df + 1)) + 1
        idfs.append(min(3.0, idf))  # cap like graph_engine._kw_idf
    if not idfs:
        return 0.0
    avg = sum(idfs) / len(idfs)
    # avg 理论范围 [1.0, 3.0], 映射到 0-10
    return round((avg - 1.0) / 2.0 * 10.0, 2)


def score_utility(node: dict) -> float:
    """0-10: activation_count log-scaled."""
    ac = node.get("activation_count", 0) or 0
    if ac <= 0:
        return 0.0
    # log 缩放: 1次=3, 10次=5, 100次=8, 1000次=10
    score = min(10.0, math.log10(ac + 1) * 3.3)
    return round(score, 2)


def score_verification(node: dict, edge_degree: dict[str, int]) -> float:
    """0-10: edge 入+出度 (被引用 = 被验证)."""
    deg = edge_degree.get(node["id"], 0)
    if deg == 0:
        return 0.0
    # 1 edge=3, 3=6, 5+=8, 10+=10
    score = min(10.0, deg * 1.5)
    return round(score, 2)


def compute_gdi(node: dict, kw_df: dict, N: int, edge_degree: dict) -> dict:
    sub = {
        "structural_completeness": score_structural(node),
        "semantic_clarity": score_semantic(node),
        "signal_specificity": score_specificity(node, kw_df, N),
        "utility_evidence": score_utility(node),
        "verification_strength": score_verification(node, edge_degree),
    }
    total = sum(sub[k] * WEIGHTS[k] for k in WEIGHTS)
    return {"quality_score": round(total, 2), **{k: round(v, 2) for k, v in sub.items()}}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    nodes, edges = load_graph()
    print(f"[gdi] 节点 {len(nodes)} / 边 {len(edges)}")

    # kw_df
    kw_df: dict[str, int] = {}
    active = [n for n in nodes if n.get("status", "active") == "active"]
    for n in active:
        seen = set()
        for kw in n.get("activation_keywords", []):
            if isinstance(kw, str):
                k = kw.lower().strip()
                if len(k) < 2 or k in seen:
                    continue
                seen.add(k)
                kw_df[k] = kw_df.get(k, 0) + 1
    N = len(active)

    # edge degree
    edge_degree: dict[str, int] = Counter()
    for e in edges:
        edge_degree[e.get("source", "")] += 1
        edge_degree[e.get("target", "")] += 1

    # 打分
    score_dist = []
    updates = 0
    for n in active:
        gdi = compute_gdi(n, kw_df, N, edge_degree)
        score_dist.append(gdi["quality_score"])
        if args.dry_run:
            continue
        c = n.get("content", {}) or {}
        extra = c.get("extra", {}) or {}
        extra["quality_score"] = gdi["quality_score"]
        extra["gdi"] = {k: v for k, v in gdi.items() if k != "quality_score"}
        c["extra"] = extra
        n["content"] = c
        (NODES_DIR / f"{n['id']}.json").write_text(
            json.dumps(n, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        updates += 1

    # 分布
    if score_dist:
        avg = sum(score_dist) / len(score_dist)
        score_dist.sort()
        p50 = score_dist[len(score_dist)//2]
        p10 = score_dist[len(score_dist)//10]
        p90 = score_dist[len(score_dist)*9//10]
        print(f"[gdi] 分布: avg={avg:.2f}  p10={p10:.2f}  p50={p50:.2f}  p90={p90:.2f}")
        low = sum(1 for s in score_dist if s < 3.0)
        print(f"[gdi] 低分 <3: {low} 节点 ({100*low/len(score_dist):.1f}%, 建议 housekeeping dormant 候选)")

    if args.dry_run:
        print("[gdi] dry-run, 未写回")
    else:
        print(f"[gdi] {updates} 节点写回 content.extra.quality_score + gdi")
    return 0


if __name__ == "__main__":
    sys.exit(main())
