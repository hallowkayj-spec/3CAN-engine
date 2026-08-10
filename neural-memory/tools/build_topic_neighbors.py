"""离线构建主题邻居边 — S66 摘要索引升级 H1。

目的:
  给每个active节点找top-K主题相近邻居, 存为 EdgeType.informs 边,
  description="topic_neighbor:<cos_sim>". 这样route结果自然带出
  "主题邻居簇", agent看到节点就知道相关还有哪几个, 精准read.

用法:
  python -m tools.build_topic_neighbors [--top-k 5] [--min-sim 0.70] [--dry-run]

策略:
  - 只处理 status=active 节点 (635个左右)
  - 复用 graph/embeddings.npz 缓存 (BGE-M3 1024d) 不重新encode
  - 余弦相似度矩阵, 每节点取top-K>=min_sim邻居
  - 去重: 同pair (A,B) 只建一边, skip B,A
  - 跳过已有边 (任意type), 避免污染 depends_on/feeds_into 等显式关系
  - 批量POST /api/edges
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import requests

GRAPH_DIR = Path(__file__).resolve().parent.parent / "graph"
EMBEDDINGS_FILE = GRAPH_DIR / "embeddings.npz"
NODES_DIR = GRAPH_DIR / "nodes"
EDGES_FILE = GRAPH_DIR / "edges.json"
API_BASE = "http://localhost:9700"
EMBEDDING_DIM = 1024
EMBEDDING_CACHE_KEYS = frozenset({"ids", "embeddings", "backend_id"})


def _decode_cache_strings(values: np.ndarray, field: str) -> list[str]:
    if values.ndim != 1 or values.dtype.kind not in {"U", "S"}:
        raise ValueError(f"embedding_cache_{field}_must_be_1d_strings")
    decoded: list[str] = []
    for raw in values.tolist():
        value = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        if not value:
            raise ValueError(f"embedding_cache_{field}_contains_empty_value")
        decoded.append(value)
    return decoded


def _read_embedding_cache(path: Path) -> tuple[list[str], np.ndarray]:
    with np.load(path, allow_pickle=False) as data:
        keys = set(data.files)
        if not {"ids", "embeddings"}.issubset(keys):
            raise ValueError("embedding_cache_missing_required_arrays")
        if keys - EMBEDDING_CACHE_KEYS:
            raise ValueError("embedding_cache_has_unexpected_arrays")

        ids = _decode_cache_strings(data["ids"], "ids")
        embeddings = data["embeddings"]
        if embeddings.ndim != 2 or embeddings.dtype.kind != "f":
            raise ValueError("embedding_cache_embeddings_must_be_2d_floats")
        if embeddings.shape != (len(ids), EMBEDDING_DIM):
            raise ValueError("embedding_cache_shape_mismatch")
        if not np.isfinite(embeddings).all():
            raise ValueError("embedding_cache_embeddings_must_be_finite")

        if "backend_id" in keys:
            backend_id = _decode_cache_strings(data["backend_id"], "backend_id")
            if len(backend_id) != 1:
                raise ValueError("embedding_cache_backend_id_must_have_one_value")
        return ids, embeddings.copy()


def load_embeddings():
    if not EMBEDDINGS_FILE.exists():
        print(f"[err] {EMBEDDINGS_FILE} 不存在, 先启动backend让它构建")
        sys.exit(1)
    try:
        ids, embs = _read_embedding_cache(EMBEDDINGS_FILE)
    except (OSError, ValueError, KeyError) as exc:
        print(f"[err] embeddings.npz 无效, 拒绝加载: {exc}")
        raise SystemExit(1) from exc
    print(f"[+] embeddings.npz loaded: {len(ids)} nodes, dim={embs.shape[1]}")
    return ids, embs


def load_active_nodes():
    """读 graph/nodes/*.json, 返回 {id: (status, name, cluster)}"""
    nodes = {}
    for p in NODES_DIR.glob("*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            nodes[d["id"]] = {
                "status": d.get("status", "active"),
                "name": d.get("name", ""),
                "cluster": d.get("cluster", ""),
            }
        except Exception as e:
            print(f"[warn] {p.name}: {e}")
    active_ids = {nid for nid, info in nodes.items() if info["status"] == "active"}
    print(f"[+] {len(nodes)} total nodes, {len(active_ids)} active")
    return nodes, active_ids


def load_existing_edges():
    """读 graph/edges.json, 返回 set of frozenset({src, tgt}) 用于去重."""
    if not EDGES_FILE.exists():
        return set()
    data = json.loads(EDGES_FILE.read_text(encoding="utf-8"))
    existing = set()
    for e in data:
        existing.add(frozenset({e["source"], e["target"]}))
    print(f"[+] {len(existing)} existing edge pairs loaded")
    return existing


def _strip_prefix(node_id: str) -> str:
    """HO-2026-04-15-S65-foo -> 2026-04-15-S65-foo (for slug compare)."""
    parts = node_id.split("-", 1)
    return parts[1] if len(parts) == 2 else node_id


def find_neighbors(ids, embs, active_set, top_k, min_sim, max_sim=0.95):
    """对每个active节点, 找top-K邻居 (min_sim <= cos_sim <= max_sim). 返回 list of (src, tgt, sim).
    max_sim 过滤镜像对 (DOC<->HO 相同slug), slug相同也直接skip.
    """
    # Filter to active nodes only for matrix
    idx_active = [i for i, nid in enumerate(ids) if nid in active_set]
    sub_ids = [ids[i] for i in idx_active]
    sub_embs = embs[idx_active]  # (N, 1024), already normalized from BGE-M3

    # Cosine sim matrix (since vectors are unit-normalized, dot = cosine)
    print(f"[+] computing {len(sub_ids)}x{len(sub_ids)} cosine matrix...")
    sim = sub_embs @ sub_embs.T  # (N, N)
    np.fill_diagonal(sim, -1.0)  # exclude self

    pairs = []
    skipped_mirror = 0
    skipped_slug = 0
    for i, src_id in enumerate(sub_ids):
        row = sim[i]
        top_idx = np.argpartition(-row, min(top_k * 3, len(row) - 1))[:top_k * 3]
        accepted = 0
        for j in top_idx:
            if accepted >= top_k:
                break
            s = float(row[j])
            if s < min_sim:
                continue
            if s > max_sim:
                skipped_mirror += 1
                continue
            tgt_id = sub_ids[j]
            if src_id >= tgt_id:
                continue
            # 跳过相同slug (DOC-xxx vs HO-xxx 镜像)
            if _strip_prefix(src_id) == _strip_prefix(tgt_id):
                skipped_slug += 1
                continue
            pairs.append((src_id, tgt_id, s))
            accepted += 1
    pairs.sort(key=lambda x: -x[2])
    print(f"[+] {len(pairs)} topic-neighbor pairs (min_sim={min_sim}, max_sim={max_sim})")
    print(f"    skipped: {skipped_mirror} high-sim mirrors + {skipped_slug} same-slug pairs")
    return pairs


def post_edge(src, tgt, score):
    body = {
        "source": src,
        "target": tgt,
        "type": "informs",
        "weight": round(score, 4),
        "description": f"topic_neighbor:{score:.3f}",
    }
    try:
        r = requests.post(f"{API_BASE}/api/edges", json=body, timeout=10)
        return r.status_code == 200
    except Exception as e:
        print(f"[err] POST edge {src}->{tgt}: {e}")
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-k", type=int, default=5, help="每节点最多几个邻居")
    ap.add_argument("--min-sim", type=float, default=0.70, help="最低余弦相似度")
    ap.add_argument("--max-sim", type=float, default=0.95, help="上限 (过滤镜像对)")
    ap.add_argument("--dry-run", action="store_true", help="不写入, 只打印前20对")
    ap.add_argument("--limit", type=int, default=0, help="最多写多少对 (0=全部)")
    args = ap.parse_args()

    ids, embs = load_embeddings()
    nodes, active_set = load_active_nodes()
    existing = load_existing_edges()

    pairs = find_neighbors(ids, embs, active_set, args.top_k, args.min_sim, args.max_sim)

    # Skip existing edges
    new_pairs = [(s, t, sim) for s, t, sim in pairs if frozenset({s, t}) not in existing]
    print(f"[+] {len(new_pairs)} new pairs after skipping existing edges")

    if args.limit > 0:
        new_pairs = new_pairs[:args.limit]

    # Preview
    print("\n=== top 20 preview ===")
    for s, t, sim in new_pairs[:20]:
        s_name = nodes.get(s, {}).get("name", "?")[:40]
        t_name = nodes.get(t, {}).get("name", "?")[:40]
        print(f"  [{sim:.3f}] {s[:35]:<35} <-> {t[:35]:<35}")
        print(f"          {s_name}  <->  {t_name}")

    if args.dry_run:
        print("\n[dry-run] 未写入")
        return

    # Write
    print(f"\n[+] writing {len(new_pairs)} edges via POST /api/edges ...")
    ok = 0
    fail = 0
    for i, (s, t, sim) in enumerate(new_pairs):
        if post_edge(s, t, sim):
            ok += 1
        else:
            fail += 1
        if (i + 1) % 50 == 0:
            print(f"  progress: {i+1}/{len(new_pairs)}  (ok={ok} fail={fail})")
    print(f"\n[done] ok={ok}  fail={fail}")


if __name__ == "__main__":
    main()
