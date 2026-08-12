"""3CAN Keyword Precision Audit — S66d Wave 2

目标: 清 kw 热重 (intf 426 节点 / doc 313 / codex-cli 265 ...).
这些 kw 现在出现在 ~30% 节点上, 导致 kw_score 对 query "intf advisor" 给 426 节点全加 1.0, 稀释信号.

流程:
1. 读 housekeeping_audit 的 kw_heat_hotspots
2. 对每个热重 kw, 采样 K 节点 (含 ID 带 kw 的 / 不含的)
3. 异步 DeepSeek 判断 "kw=X 是否属于此节点的 activation_keywords?"
4. 聚合: 若 LLM 判 "应删除" ≥70%, 标记为低质 kw (写 PROPOSED-kw-cleanup.json)
5. the maintainer 审批 → apply 脚本批量 PUT /api/nodes/{id}

纯只读, 不改节点. 只出 PROPOSED-*.

运行:
  python tools/kw_precision_audit.py --limit 5            # 测前 5 个 hotspot
  python tools/kw_precision_audit.py --limit 30 --sample 8  # 每 kw 采样 8 节点
  python tools/kw_precision_audit.py --apply             # the maintainer 批准后执行删除 (不建议首轮)
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import random
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
NODES_DIR = ROOT / "graph" / "nodes"
OUT_DIR = ROOT / "data" / "_kw_audit"
PROPOSAL_FILE = OUT_DIR / "kw_cleanup_proposal.json"

THREE_CAN = "http://localhost:9700"
DEEPSEEK_ENDPOINT = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"

MAX_CONCURRENT = 5
PER_CALL_TIMEOUT = 30
REMOVE_THRESHOLD = 0.70  # ≥70% 节点 LLM 判"应删", 才写入 proposal

PROMPT = """你判断一个 activation keyword 是否该保留在 3CAN 图谱的某节点上.

规则: activation keywords 用于"从 query 精确匹配到此节点". 高质量 kw:
- 强区分度 (e.g. 'RRF fusion' / 'bge-reranker-v2-m3')
- 中英双份 (e.g. '脚本约束 / script constraint')
- 短码精确 (e.g. 'S66c / session 66c')

低质量 kw (应删除):
- 宽泛类名 (e.g. 'intf' / 'doc' / 'handoff' / 'session'), 匹配 100+ 节点, 无区分度
- 只在节点 ID 前缀 (e.g. 节点 ID 'INTF-xxx', kw 为 'intf', 冗余)
- 与节点主旨无关 (e.g. FEE 讲 '别用 grep', kw 里有 '视频')

节点信息:
- ID: {nid}
- 名称: {name}
- Cluster: {cluster}
- Type: {ntype}
- 此 kw 在全图频率: 出现于 {df}/{N} 节点 ({freq_pct}%)
- 描述前 200 字: {description}

待判断 keyword: "{kw}"

输出严格 JSON:
{{"verdict": "keep" | "remove", "confidence": 0.0-1.0, "reason": "≤30字"}}

判断:
- verdict=remove 当: 宽泛 / 冗余 / 无关 / 频率 >20%
- verdict=keep 当: 确实是此节点核心语义标识, 即使频率高也保留
"""


def _load_deepseek_key() -> str | None:
    p = Path.home() / ".claude" / "secrets.json"
    if not p.exists():
        return None
    try:
        return (json.loads(p.read_text(encoding="utf-8")).get("deepseek") or {}).get("api_key")
    except Exception:
        return None


def load_nodes() -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in NODES_DIR.glob("*.json")]


def build_kw_index(nodes: list[dict]) -> dict[str, list[str]]:
    idx: dict[str, list[str]] = {}
    for n in nodes:
        for kw in n.get("activation_keywords", []):
            if not isinstance(kw, str):
                continue
            k = kw.lower().strip()
            if len(k) < 2:
                continue
            idx.setdefault(k, []).append(n["id"])
    return idx


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
                "max_tokens": 150,
            },
            timeout=PER_CALL_TIMEOUT,
        )
        if r.status_code != 200:
            return {"_error": f"http {r.status_code}"}
        return json.loads(r.json()["choices"][0]["message"]["content"])
    except Exception as e:
        return {"_error": str(e)[:120]}


def audit_one_kw(kw: str, node_ids: list[str], nodes_by_id: dict[str, dict],
                 N: int, sample_size: int, api_key: str) -> dict:
    """对一个热重 kw 采样审."""
    df = len(node_ids)
    freq_pct = round(100 * df / N, 1)
    sampled = random.sample(node_ids, min(sample_size, len(node_ids)))

    payloads = []
    for nid in sampled:
        n = nodes_by_id.get(nid)
        if not n:
            continue
        c = n.get("content", {}) or {}
        payloads.append({
            "nid": nid,
            "name": (n.get("name") or "")[:60],
            "cluster": n.get("cluster", ""),
            "ntype": n.get("type", ""),
            "df": df, "N": N, "freq_pct": freq_pct,
            "description": (c.get("description") or "")[:200],
            "kw": kw,
        })

    results = []
    with cf.ThreadPoolExecutor(max_workers=MAX_CONCURRENT) as ex:
        futs = {ex.submit(deepseek_judge, api_key, p): p for p in payloads}
        for fut in cf.as_completed(futs):
            p = futs[fut]
            try:
                ans = fut.result()
            except Exception as e:
                ans = {"_error": str(e)[:100]}
            results.append({"nid": p["nid"], **ans})

    remove_ids = [r["nid"] for r in results if r.get("verdict") == "remove" and r.get("confidence", 0) >= 0.6]
    keep_ids = [r["nid"] for r in results if r.get("verdict") == "keep"]
    err_ids = [r["nid"] for r in results if r.get("_error")]

    n_judged = len(remove_ids) + len(keep_ids)
    remove_rate = (len(remove_ids) / n_judged) if n_judged else 0.0

    return {
        "kw": kw,
        "df": df,
        "freq_pct": freq_pct,
        "sampled": len(sampled),
        "judged": n_judged,
        "remove_ids_in_sample": remove_ids,
        "keep_ids_in_sample": keep_ids,
        "errors": err_ids,
        "remove_rate": round(remove_rate, 3),
        "verdict_global": "remove_from_all_df_nodes" if remove_rate >= REMOVE_THRESHOLD else
                          ("remove_from_sampled_only" if remove_ids else "keep_kw"),
    }


def apply_proposal(proposal: dict, dry_run: bool = True) -> dict:
    """批量 PUT /api/nodes/{id} 从 activation_keywords 移除标记的 kw.
    dry_run=True 只打印将要做的操作."""
    stats = {"nodes_updated": 0, "kws_removed": 0, "errors": []}
    for kw_item in proposal.get("cleanup", []):
        kw = kw_item["kw"]
        target_ids = kw_item.get("target_node_ids") or []
        if kw_item.get("verdict_global") == "remove_from_all_df_nodes":
            # 全局扫删 (加载全节点, 去除此 kw)
            all_nodes = load_nodes()
            target_ids = [n["id"] for n in all_nodes
                          if kw in [k.lower().strip() for k in n.get("activation_keywords", [])]]
        for nid in target_ids:
            if dry_run:
                print(f"  [DRY] would remove kw='{kw}' from {nid}")
                continue
            try:
                r = requests.get(f"{THREE_CAN}/api/nodes/{nid}", timeout=10)
                if r.status_code != 200:
                    continue
                node = r.json()
                new_kws = [k for k in node.get("activation_keywords", [])
                           if k.lower().strip() != kw]
                if len(new_kws) == len(node["activation_keywords"]):
                    continue
                put = requests.put(f"{THREE_CAN}/api/nodes/{nid}",
                                   json={"activation_keywords": new_kws}, timeout=15)
                if put.status_code in (200, 201):
                    stats["nodes_updated"] += 1
                    stats["kws_removed"] += 1
                else:
                    stats["errors"].append(f"{nid}: http {put.status_code}")
            except Exception as e:
                stats["errors"].append(f"{nid}: {str(e)[:80]}")
    return stats


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=10, help="审前 N 个热重 kw")
    p.add_argument("--sample", type=int, default=8, help="每 kw 采样多少节点")
    p.add_argument("--min-df", type=int, default=20, help="只审 df ≥ min-df 的 kw")
    p.add_argument("--apply", action="store_true", help="从 PROPOSAL_FILE 读取并 apply")
    p.add_argument("--apply-dry", action="store_true", help="dry-run apply")
    args = p.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    if args.apply or args.apply_dry:
        if not PROPOSAL_FILE.exists():
            print(f"[kw-audit] no proposal at {PROPOSAL_FILE}")
            return 1
        proposal = json.loads(PROPOSAL_FILE.read_text(encoding="utf-8"))
        stats = apply_proposal(proposal, dry_run=args.apply_dry)
        print(f"[kw-audit] apply stats: {stats}")
        return 0

    api_key = _load_deepseek_key()
    if not api_key:
        print("[kw-audit] FATAL: no DeepSeek key in ~/.claude/secrets.json")
        return 2

    print("[kw-audit] loading nodes...")
    nodes = load_nodes()
    nodes_by_id = {n["id"]: n for n in nodes}
    N = len(nodes)
    idx = build_kw_index(nodes)

    # 热重 kw 列表: 按 df 降序
    hotspots = [(kw, ids) for kw, ids in idx.items() if len(ids) >= args.min_df]
    hotspots.sort(key=lambda x: -len(x[1]))
    hotspots = hotspots[:args.limit]

    print(f"[kw-audit] N={N}, 待审 {len(hotspots)} 热重 kw (min_df={args.min_df})")

    random.seed(42)  # 可复现
    cleanup: list[dict] = []
    for i, (kw, ids) in enumerate(hotspots, 1):
        print(f"  [{i}/{len(hotspots)}] kw='{kw}' df={len(ids)} ({round(100*len(ids)/N,1)}%)  审中...")
        t0 = time.time()
        result = audit_one_kw(kw, ids, nodes_by_id, N, args.sample, api_key)
        elapsed = time.time() - t0
        print(f"      judged={result['judged']}/{result['sampled']}  remove_rate={result['remove_rate']}  verdict={result['verdict_global']}  ({elapsed:.1f}s)")
        cleanup.append(result)

    proposal = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "N": N,
        "hotspots_audited": len(hotspots),
        "sample_per_kw": args.sample,
        "remove_threshold": REMOVE_THRESHOLD,
        "cleanup": cleanup,
    }
    PROPOSAL_FILE.write_text(json.dumps(proposal, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[kw-audit] proposal written: {PROPOSAL_FILE}")

    # 摘要
    global_removals = [c for c in cleanup if c["verdict_global"] == "remove_from_all_df_nodes"]
    partial = [c for c in cleanup if c["verdict_global"] == "remove_from_sampled_only"]
    keep = [c for c in cleanup if c["verdict_global"] == "keep_kw"]
    print(f"[kw-audit] 摘要: 全局删 {len(global_removals)} / 部分删 {len(partial)} / 保留 {len(keep)}")
    for c in global_removals:
        print(f"  - 全局删 kw='{c['kw']}' (df={c['df']}, remove_rate={c['remove_rate']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
