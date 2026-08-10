"""3CAN Leiden Community Detection — Path 2 Wave 3

参考: Graphify "Leiden Community Detection Without Embeddings" (graphify.net).
原理: 基于 edge density 找社区, 无需 embedding. 配合 route 的 same-community boost,
主要涨 R@3 (让兄弟节点一起浮出).

步骤:
1. 读 graph/nodes/*.json + graph/edges.json
2. 构 igraph (节点=id, 边=edges, 孤立节点也加进去)
3. 跑 leidenalg.find_partition (ModularityVertexPartition)
4. 给每节点写 content.extra.community_id
5. 可选: 给 kw 加 "community-N" 字符串 (增加 route 区分度)

运行:
  python tools/leiden_community.py --dry-run      # 看分布, 不改
  python tools/leiden_community.py                 # 直写节点
  python tools/leiden_community.py --apply-via-api # 通过 PUT /api/nodes (慢但走完整管道)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NODES_DIR = ROOT / "graph" / "nodes"
EDGES_FILE = ROOT / "graph" / "edges.json"

THREE_CAN = "http://localhost:9700"


def load_graph() -> tuple[list[dict], list[dict]]:
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


def build_igraph(nodes: list[dict], edges: list[dict]):
    import igraph as ig
    node_ids = [n["id"] for n in nodes]
    id_to_idx = {nid: i for i, nid in enumerate(node_ids)}
    ig_edges = []
    for e in edges:
        s, t = e.get("source"), e.get("target")
        if s in id_to_idx and t in id_to_idx:
            ig_edges.append((id_to_idx[s], id_to_idx[t]))
    g = ig.Graph(n=len(node_ids), edges=ig_edges, directed=False)
    g.vs["node_id"] = node_ids
    return g


def run_leiden(g, seed: int = 42):
    import leidenalg as la
    partition = la.find_partition(
        g,
        la.ModularityVertexPartition,
        seed=seed,
    )
    return partition


def apply_direct(nodes: list[dict], assignments: dict[str, int]) -> int:
    """直接改 JSON 文件 (快)."""
    ct = 0
    for n in nodes:
        cid = assignments.get(n["id"])
        if cid is None:
            continue
        content = n.get("content", {}) or {}
        extra = content.get("extra", {}) or {}
        if extra.get("community_id") == cid:
            continue
        extra["community_id"] = cid
        content["extra"] = extra
        n["content"] = content
        path = NODES_DIR / f"{n['id']}.json"
        path.write_text(json.dumps(n, ensure_ascii=False, indent=2), encoding="utf-8")
        ct += 1
    return ct


def apply_via_api(nodes: list[dict], assignments: dict[str, int]) -> dict:
    import requests
    ok = 0
    fail = 0
    for n in nodes:
        cid = assignments.get(n["id"])
        if cid is None:
            continue
        try:
            r = requests.put(f"{THREE_CAN}/api/nodes/{n['id']}", json={
                "content": {"extra": {"community_id": cid}}
            }, timeout=10)
            if r.status_code in (200, 201):
                ok += 1
            else:
                fail += 1
        except Exception:
            fail += 1
    return {"ok": ok, "fail": fail}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--apply-via-api", action="store_true")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    print("[leiden] 读图...")
    nodes, edges = load_graph()
    print(f"[leiden] 节点 {len(nodes)} / 边 {len(edges)}")

    active = [n for n in nodes if n.get("status", "active") == "active"]
    print(f"[leiden] 活跃节点 {len(active)}")

    g = build_igraph(active, edges)
    print(f"[leiden] igraph: V={g.vcount()} E={g.ecount()}")
    if g.ecount() == 0:
        print("[leiden] 无边, 每节点自己一个社区. 考虑先跑 edge_inferrer.py")
    print("[leiden] 跑 Leiden...")
    t0 = time.time()
    partition = run_leiden(g, seed=args.seed)
    elapsed = time.time() - t0
    print(f"[leiden] Leiden 完成 {elapsed:.1f}s")

    # 构 assignment map
    assignments: dict[str, int] = {}
    for cid, members in enumerate(partition):
        for idx in members:
            nid = g.vs[idx]["node_id"]
            assignments[nid] = cid

    dist = Counter(assignments.values())
    n_comms = len(dist)
    top5 = dist.most_common(5)
    print(f"\n[leiden] 社区数: {n_comms}")
    print("[leiden] 最大 5 个社区:")
    for cid, ct in top5:
        pct = 100 * ct / len(assignments)
        print(f"  community-{cid}: {ct} 节点 ({pct:.1f}%)")
    isolated = sum(1 for n in active if n["id"] not in assignments)
    print(f"[leiden] 未归类 (孤立 isolated vertex): {isolated}")

    if args.dry_run:
        return 0

    if args.apply_via_api:
        stats = apply_via_api(active, assignments)
        print(f"[leiden] API 写入: {stats}")
    else:
        ct = apply_direct(active, assignments)
        print(f"[leiden] 直写节点: {ct} 更新")

    # modularity 分数
    q = partition.modularity
    print(f"[leiden] 最终 modularity: {q:.4f} (0-1, 越高越分层)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
