"""3CAN L2 补填 — 给 description 空/太短的节点, DeepSeek 生成 1-2 句 summary.

目标: 完整 3 层结构 (name / summary / details+notes), route skeleton 模式能真返回有内容.

策略:
- 扫所有 nodes, 找 description 空或 < 30 字符
- 有 notes/current_state → 喂进 LLM 提炼 1-2 句 summary
- 完全空节点 → 从 name+kws 推一句占位
- 异步批量 (DeepSeek 8 并发)
- PUT /api/nodes/{id} 回写 content.description

运行:
  python tools/llm_summary_enrichment.py --dry-run       # 列出目标, 不改
  python tools/llm_summary_enrichment.py --limit 20      # 试 20 个
  python tools/llm_summary_enrichment.py                 # 全量
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
NODES_DIR = ROOT / "graph" / "nodes"
LOG_DIR = ROOT / "data" / "_l2_enrich"
LOG_DIR.mkdir(parents=True, exist_ok=True)

THREE_CAN = "http://localhost:9700"
DEEPSEEK_ENDPOINT = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"

MIN_L2_LEN = 30
TARGET_L2_LEN_RANGE = (80, 150)
MAX_CONCURRENT = 8
PER_CALL_TIMEOUT = 30

PROMPT_WITH_NOTES = """你为 3CAN 图谱节点写一句 summary (中英混合, 60-120 字符, 可被 agent 在 route skeleton 模式下看一眼就懂节点干什么).

节点 ID: {nid}
name: {name}
cluster: {cluster}
type: {ntype}
现有 keywords: {kws}
notes / current_state / description 原文 (可能较长):
\"\"\"
{context}
\"\"\"

要求:
1. 单行, 60-120 字符
2. 先写"干什么"或"讲什么", 再写"重点/结论"
3. 保留短代码 (S62c/KB4/MRR 0.91 等), 不要浮夸
4. 禁止 "本节点 / 此 node" 之类自指
5. 严格 JSON: {{"summary": "..."}}
"""

PROMPT_NO_NOTES = """3CAN 图谱节点缺 description, 仅有 name + keywords. 推一句占位 summary (40-80 字符).

ID: {nid}
name: {name}
cluster: {cluster}
kws: {kws}

严格 JSON: {{"summary": "...", "confidence": 0.0-1.0}}
confidence=低时表示需人工再审.
"""


def _load_key() -> str | None:
    p = Path.home() / ".claude" / "secrets.json"
    if not p.exists():
        return None
    try:
        return (json.loads(p.read_text(encoding="utf-8")).get("deepseek") or {}).get("api_key")
    except Exception:
        return None


def deepseek_call(api_key: str, prompt: str) -> dict:
    try:
        r = requests.post(
            DEEPSEEK_ENDPOINT,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": DEEPSEEK_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"},
                "temperature": 0.3,
                "max_tokens": 200,
            },
            timeout=PER_CALL_TIMEOUT,
        )
        if r.status_code != 200:
            return {"_error": f"http {r.status_code}"}
        return json.loads(r.json()["choices"][0]["message"]["content"])
    except Exception as e:
        return {"_error": str(e)[:120]}


def find_targets(min_len: int = MIN_L2_LEN) -> list[dict]:
    out = []
    for p in NODES_DIR.glob("*.json"):
        try:
            n = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if n.get("status") == "dormant":
            continue
        c = n.get("content", {}) or {}
        desc = (c.get("description", "") or "").strip()
        if len(desc) >= min_len:
            continue
        out.append(n)
    return out


def build_prompt(node: dict) -> str:
    c = node.get("content", {}) or {}
    context_parts = []
    if c.get("current_state"):
        context_parts.append(f"current_state: {c['current_state'][:400]}")
    if c.get("description"):
        context_parts.append(f"description: {c['description'][:200]}")
    if c.get("notes"):
        context_parts.append(f"notes: {c['notes'][:800]}")
    if c.get("decisions"):
        context_parts.append(
            f"decisions: {json.dumps(c['decisions'][:3], ensure_ascii=False)}"
        )
    context = "\n".join(context_parts)
    kws = node.get("activation_keywords", [])[:10]
    common = dict(nid=node['id'], name=node.get('name','')[:80],
                  cluster=node.get('cluster',''), ntype=node.get('type',''),
                  kws=json.dumps(kws, ensure_ascii=False))
    if context:
        return PROMPT_WITH_NOTES.format(context=context, **common)
    return PROMPT_NO_NOTES.format(**common)


def process_one(node: dict, api_key: str, dry_run: bool) -> dict:
    nid = node["id"]
    prompt = build_prompt(node)
    if dry_run:
        return {"nid": nid, "dry_run": True, "prompt_len": len(prompt)}

    ans = deepseek_call(api_key, prompt)
    if ans.get("_error"):
        return {"nid": nid, "error": ans["_error"]}

    summary = (ans.get("summary", "") or "").strip()
    if not summary or len(summary) < 10:
        return {"nid": nid, "error": "summary_too_short", "summary": summary}
    if len(summary) > 300:
        summary = summary[:300]

    # PUT /api/nodes/{id} 更新 content.description
    try:
        put = requests.put(
            f"{THREE_CAN}/api/nodes/{nid}",
            json={"content": {"description": summary}},
            timeout=15,
        )
        if put.status_code not in (200, 201):
            return {"nid": nid, "error": f"put http {put.status_code}: {put.text[:120]}"}
    except Exception as e:
        return {"nid": nid, "error": f"put failed: {str(e)[:80]}"}

    return {"nid": nid, "summary": summary, "len": len(summary),
            "confidence": ans.get("confidence")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="0=全量")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--min-len", type=int, default=MIN_L2_LEN)
    args = ap.parse_args()

    api_key = _load_key()
    if not api_key and not args.dry_run:
        print("FATAL: no DeepSeek key")
        return 2

    targets = find_targets(args.min_len)
    if args.limit and args.limit > 0:
        targets = targets[:args.limit]

    print(f"[l2-enrich] 目标 {len(targets)} 节点 (desc 空或 < {args.min_len} 字符)")
    if args.dry_run:
        for n in targets[:10]:
            c = n.get('content', {}) or {}
            print(f"  - {n['id']:50s} name={n.get('name','')[:30]} desc_len={len(c.get('description','') or '')}")
        print(f"  ... (共 {len(targets)})")
        return 0

    results = []
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=MAX_CONCURRENT) as ex:
        futs = {ex.submit(process_one, n, api_key, False): n["id"] for n in targets}
        for i, fut in enumerate(cf.as_completed(futs), 1):
            try:
                res = fut.result()
                results.append(res)
                if i % 10 == 0 or i == len(futs):
                    ok = sum(1 for r in results if r.get("summary"))
                    err = sum(1 for r in results if r.get("error"))
                    elapsed = time.time() - t0
                    print(f"  [{i}/{len(futs)}]  ok={ok} err={err}  {elapsed:.0f}s", flush=True)
            except Exception as e:
                results.append({"error": str(e)[:100]})

    ok = [r for r in results if r.get("summary")]
    err = [r for r in results if r.get("error")]
    print(f"\n[l2-enrich] 完成: {len(ok)} 补填 / {len(err)} 失败 / 共 {len(targets)}")

    log = LOG_DIR / f"l2_enrich_{int(time.time())}.jsonl"
    with log.open("w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"[l2-enrich] 日志: {log}")

    # 错误样本
    if err:
        print("错误样本 (前 5):")
        for e in err[:5]:
            print(f"  {e.get('nid','?'):50s}  {e.get('error','?')[:80]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
