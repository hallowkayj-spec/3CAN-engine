"""3CAN Edge Inferrer — S66d Wave 2

目标: housekeeping 报出 215 个孤立节点 (active 无入/出边), 严重削弱图谱连通性.
流程: 对每孤立节点, embedding 取 top3 相似节点 → LLM 判断是否应建边 + 选 edge_type → PROPOSED edges.

运行:
  python tools/edge_inferrer.py --limit 30   # 审前 30 个孤立节点
  python tools/edge_inferrer.py --apply      # the maintainer 批准后写入 edges

纯只读. 写 PROPOSED 到 data/_edges/edge_proposals.json.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import sys
import time
from pathlib import Path

import numpy as np
import requests

ROOT = Path(__file__).resolve().parents[1]
NODES_DIR = ROOT / "graph" / "nodes"
EDGES_FILE = ROOT / "graph" / "edges.json"
EMBEDDINGS_FILE = ROOT / "graph" / "embeddings.npz"
OUT_DIR = ROOT / "data" / "_edges"
PROPOSAL_FILE = OUT_DIR / "edge_proposals.json"

THREE_CAN = "http://localhost:9700"
DEEPSEEK_ENDPOINT = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"

VALID_EDGE_TYPES = ["depends_on", "feeds_into", "blocks", "informs", "requires", "updates", "validates", "triggers"]
MAX_CONCURRENT = 4
PER_CALL_TIMEOUT = 30
EMBEDDING_DIM = 1024
EMBEDDING_CACHE_KEYS = frozenset({"ids", "embeddings", "backend_id"})

PROMPT = """你判断两个 3CAN 节点之间应建什么类型的有向边.

source 节点:
- id: {src_id}
- name: {src_name}
- type: {src_type}
- desc: {src_desc}

target 节点 (与 source 语义相近, cosine={sim:.3f}):
- id: {tgt_id}
- name: {tgt_name}
- type: {tgt_type}
- desc: {tgt_desc}

输出严格 JSON:
{{
  "should_link": true/false,
  "edge_type": "depends_on" | "feeds_into" | "blocks" | "informs" | "requires" | "updates" | "validates" | "triggers" | "none",
  "direction": "src_to_tgt" | "tgt_to_src",
  "confidence": 0.0-1.0,
  "reason": "≤30字"
}}

判断:
- should_link=false 当: 只是相似语义但无实际依赖 (e.g. 两个独立 session record)
- edge_type 选最紧对应的 (看 notes):
  - depends_on: A 逻辑上需要 B 存在
  - feeds_into: A 的输出喂给 B
  - informs: A 的知识告知 B 的决策
  - updates: A 是 B 的更新版
  - validates: A 验证 B
  - triggers: A 触发 B
  - requires: A 运行时依赖 B
  - blocks: A 阻塞 B
- direction: "src_to_tgt" = source → target 指向, 反之亦然
"""


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


def _load_key() -> str | None:
    p = Path.home() / ".claude" / "secrets.json"
    if not p.exists():
        return None
    try:
        return (json.loads(p.read_text(encoding="utf-8")).get("deepseek") or {}).get("api_key")
    except Exception:
        return None


def load_nodes() -> dict[str, dict]:
    return {p.stem: json.loads(p.read_text(encoding="utf-8"))
            for p in NODES_DIR.glob("*.json")}


def load_edges() -> list[dict]:
    if not EDGES_FILE.exists():
        return []
    try:
        d = json.loads(EDGES_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, list) else d.get("edges", [])
    except Exception:
        return []


def find_orphans(nodes_by_id: dict[str, dict], edges: list[dict]) -> list[str]:
    connected: set[str] = set()
    for e in edges:
        connected.add(e.get("source", ""))
        connected.add(e.get("target", ""))
    return [nid for nid, n in nodes_by_id.items()
            if nid not in connected and n.get("status") == "active"]


def load_embeddings() -> tuple[list[str], np.ndarray] | None:
    if not EMBEDDINGS_FILE.exists():
        return None
    try:
        return _read_embedding_cache(EMBEDDINGS_FILE)
    except (OSError, ValueError, KeyError):
        return None


def top_k_similar(src_id: str, k: int, ids: list[str], mat: np.ndarray,
                  exclude: set[str]) -> list[tuple[str, float]]:
    if src_id not in ids:
        return []
    i = ids.index(src_id)
    sims = mat @ mat[i]
    sims[i] = -1  # 排除自己
    order = np.argsort(sims)[::-1]
    out = []
    for j in order:
        nid = ids[j]
        if nid in exclude:
            continue
        out.append((nid, float(sims[j])))
        if len(out) >= k:
            break
    return out


def deepseek_judge(api_key: str, payload: dict) -> dict:
    try:
        r = requests.post(
            DEEPSEEK_ENDPOINT,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": DEEPSEEK_MODEL,
                "messages": [{"role": "user", "content": PROMPT.format(**payload)}],
                "response_format": {"type": "json_object"},
                "temperature": 0.1,
                "max_tokens": 200,
            },
            timeout=PER_CALL_TIMEOUT,
        )
        if r.status_code != 200:
            return {"_error": f"http {r.status_code}"}
        return json.loads(r.json()["choices"][0]["message"]["content"])
    except Exception as e:
        return {"_error": str(e)[:120]}


def infer_for_orphan(orphan_id: str, nodes_by_id: dict, ids: list[str], mat: np.ndarray,
                     api_key: str, top_k: int = 3) -> dict:
    src = nodes_by_id[orphan_id]
    candidates = top_k_similar(orphan_id, top_k, ids, mat, exclude={orphan_id})
    proposals = []
    for tgt_id, sim in candidates:
        tgt = nodes_by_id.get(tgt_id)
        if not tgt:
            continue
        payload = {
            "src_id": orphan_id,
            "src_name": (src.get("name", "") or "")[:60],
            "src_type": src.get("type", ""),
            "src_desc": (src.get("content", {}).get("description", "") or "")[:150],
            "tgt_id": tgt_id,
            "tgt_name": (tgt.get("name", "") or "")[:60],
            "tgt_type": tgt.get("type", ""),
            "tgt_desc": (tgt.get("content", {}).get("description", "") or "")[:150],
            "sim": sim,
        }
        ans = deepseek_judge(api_key, payload)
        if ans.get("_error"):
            continue
        if not ans.get("should_link"):
            continue
        et = ans.get("edge_type", "none")
        if et not in VALID_EDGE_TYPES:
            continue
        direction = ans.get("direction", "src_to_tgt")
        if direction == "src_to_tgt":
            source, target = orphan_id, tgt_id
        else:
            source, target = tgt_id, orphan_id
        proposals.append({
            "source": source,
            "target": target,
            "edge_type": et,
            "similarity": round(sim, 3),
            "confidence": ans.get("confidence", 0.0),
            "reason": ans.get("reason", ""),
        })
    return {"orphan_id": orphan_id, "proposals": proposals}


def apply_edges(all_proposals: list[dict], dry_run: bool = True,
                min_conf: float = 0.7) -> dict:
    stats = {"edges_added": 0, "errors": []}
    for batch in all_proposals:
        for p in batch.get("proposals", []):
            if p.get("confidence", 0) < min_conf:
                continue
            if dry_run:
                print(f"  [DRY] {p['source']} --{p['edge_type']}--> {p['target']} "
                      f"(sim={p['similarity']}, conf={p['confidence']})")
                stats["edges_added"] += 1
                continue
            try:
                r = requests.post(f"{THREE_CAN}/api/edges", json={
                    "source": p["source"], "target": p["target"], "type": p["edge_type"],
                }, timeout=10)
                if r.status_code in (200, 201):
                    stats["edges_added"] += 1
                else:
                    stats["errors"].append(f"{p['source']}->{p['target']}: http {r.status_code}")
            except Exception as e:
                stats["errors"].append(f"{p['source']}->{p['target']}: {str(e)[:80]}")
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--top-k", type=int, default=3, help="每孤立节点找 top-k 相似候选")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry", action="store_true", help="配合 --apply, dry-run")
    ap.add_argument("--min-conf", type=float, default=0.7)
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    if args.apply:
        if not PROPOSAL_FILE.exists():
            print(f"[edge-inferrer] 无 proposal {PROPOSAL_FILE}")
            return 1
        data = json.loads(PROPOSAL_FILE.read_text(encoding="utf-8"))
        stats = apply_edges(data.get("orphans", []), dry_run=args.dry, min_conf=args.min_conf)
        print(f"[edge-inferrer] apply: {stats}")
        return 0

    api_key = _load_key()
    if not api_key:
        print("[edge-inferrer] FATAL: no DeepSeek key")
        return 2

    print("[edge-inferrer] loading nodes + edges + embeddings...")
    nodes_by_id = load_nodes()
    edges = load_edges()
    orphans = find_orphans(nodes_by_id, edges)
    emb = load_embeddings()
    if not emb:
        print("[edge-inferrer] FATAL: 无 embeddings.npz (需先跑 engine 至少一次)")
        return 2
    ids, mat = emb

    print(f"[edge-inferrer] 孤立节点 {len(orphans)} 个, 审前 {min(args.limit, len(orphans))}")
    target_orphans = orphans[:args.limit]

    results = []
    with cf.ThreadPoolExecutor(max_workers=MAX_CONCURRENT) as ex:
        futs = {ex.submit(infer_for_orphan, o, nodes_by_id, ids, mat, api_key, args.top_k): o
                for o in target_orphans}
        for fut in cf.as_completed(futs):
            try:
                results.append(fut.result())
            except Exception as e:
                results.append({"error": str(e)[:100]})

    total_proposals = sum(len(r.get("proposals", [])) for r in results)
    high_conf = sum(1 for r in results for p in r.get("proposals", [])
                    if p.get("confidence", 0) >= 0.7)

    payload = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "audited_orphans": len(target_orphans),
        "total_proposed_edges": total_proposals,
        "high_confidence": high_conf,
        "orphans": results,
    }
    PROPOSAL_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[edge-inferrer] 写入 {PROPOSAL_FILE}")
    print(f"[edge-inferrer] 摘要: {total_proposals} 边, {high_conf} 高置信 (≥0.7)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
