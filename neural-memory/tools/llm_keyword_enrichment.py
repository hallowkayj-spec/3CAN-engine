"""3CAN 节点 LLM 关键词扩展 — S66c pilot 批量工具

设计:
- 前台 Opus 决策 / 后台 DeepSeek 批量 (分工清晰)
- 节点 description + current_state + name → DeepSeek 扩 15-20 高熵中英双份 keyword
- 合并 (不覆盖) 原 activation_keywords → PUT /api/nodes/{id}
- 每节点前后差异记录到 data/_3can_llm_enrich_log/{date}.jsonl
- 失败降级: DeepSeek 不通 → 跳过不卡死

the maintainer 规则 (Hermes 反吞金兽):
- 不跑全量 1330, pilot 先 20-30 个
- the maintainer 审批后再全量
- 成本记入 SEC-llm-cost-ledger (TODO)
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Optional

import requests

THREE_CAN = "http://localhost:9700"
DEEPSEEK_ENDPOINT = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"
LOG_DIR = Path(__file__).resolve().parents[1] / "data" / "_3can_llm_enrich_log"
LOG_DIR.mkdir(parents=True, exist_ok=True)


def _load_deepseek_key() -> Optional[str]:
    secrets = Path.home() / ".claude" / "secrets.json"
    if not secrets.exists():
        return None
    s = json.loads(secrets.read_text(encoding="utf-8"))
    ds = s.get("deepseek") or {}
    return ds.get("api_key")


PROMPT = """你是 3CAN 记忆图谱的关键词抽取专家. 为给定节点扩充 activation_keywords (中英双份, 提升 route 召回).

节点信息:
- ID: {nid}
- 名称: {name}
- Cluster: {cluster}
- Type: {ntype}
- 现有 keywords: {existing}
- 描述: {description}
- 当前状态: {current_state}

任务: 产出 15-20 个**高信息熵**的新关键词, 满足:
1. **中英双份** (e.g. "脚本约束/script constraint") 覆盖不同语言查询
2. 覆盖**同义词/上位词/下位词** (e.g. "bug/error/缺陷/故障")
3. **短代码+全称双写** (e.g. "S66c/Session 66c")
4. 避开已有 keywords (避免重复)
5. 每条 ≤ 12 字 (中) 或 ≤ 5 词 (英)
6. **禁止**: 泛词 "任务/项目/系统", 无意义前缀 "关于/对于"

严格 JSON 输出, 无解释:
{{"new_keywords": ["kw1","kw2", ...], "reasoning_1line": "≤40字为什么选这些"}}
"""


def enrich_node(node_id: str, api_key: str, timeout: int = 30) -> Optional[dict]:
    """从 3CAN 取节点, DeepSeek 扩 keyword, 返回 diff dict."""
    r = requests.get(f"{THREE_CAN}/api/nodes/{node_id}", timeout=10)
    if r.status_code != 200:
        return {"error": f"fetch fail {r.status_code}"}
    node = r.json()
    content = node.get("content", {}) or {}
    prompt = PROMPT.format(
        nid=node_id,
        name=node.get("name", ""),
        cluster=node.get("cluster", ""),
        ntype=node.get("type", ""),
        existing=json.dumps(node.get("activation_keywords", []), ensure_ascii=False),
        description=(content.get("description", "") or "")[:800],
        current_state=(content.get("current_state", "") or "")[:400],
    )

    # Force no proxy for deepseek (S66c 教训)
    proxies = {"http": None, "https": None}
    resp = requests.post(
        DEEPSEEK_ENDPOINT,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": DEEPSEEK_MODEL,
            "stream": False,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 600,
            "temperature": 0.3,
            "response_format": {"type": "json_object"},
        },
        timeout=timeout,
        proxies=proxies,
    )
    resp.raise_for_status()
    raw = resp.json()["choices"][0]["message"]["content"]
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"error": "parse fail", "raw": raw[:300]}

    new_kw = parsed.get("new_keywords", [])
    existing = node.get("activation_keywords", []) or []
    merged = list(dict.fromkeys(existing + new_kw))  # 保序去重

    # PUT 更新节点
    upd = requests.put(
        f"{THREE_CAN}/api/nodes/{node_id}",
        json={"activation_keywords": merged},
        timeout=10,
    )
    if upd.status_code != 200:
        return {"error": f"put fail {upd.status_code}"}

    return {
        "node_id": node_id,
        "before_count": len(existing),
        "added": len(merged) - len(existing),
        "after_count": len(merged),
        "reasoning": parsed.get("reasoning_1line", "")[:100],
        "new_kw_sample": new_kw[:5],
    }


def run_pilot(node_ids: list[str]) -> dict:
    api_key = _load_deepseek_key()
    if not api_key:
        return {"error": "no deepseek key"}

    log_path = LOG_DIR / f"{time.strftime('%Y-%m-%d')}_pilot.jsonl"
    results = []
    total_added = 0
    t0 = time.time()
    with log_path.open("a", encoding="utf-8") as f:
        for i, nid in enumerate(node_ids, 1):
            try:
                res = enrich_node(nid, api_key)
                res["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                f.write(json.dumps(res, ensure_ascii=False) + "\n")
                f.flush()
                added = res.get("added", 0)
                total_added += added
                print(f"[{i:02d}/{len(node_ids)}] {nid:48s} +{added:2d} kw | {res.get('reasoning','')[:50]}")
                results.append(res)
                time.sleep(0.8)  # DeepSeek rate limit safety
            except Exception as e:
                err = {"node_id": nid, "error": str(e)[:200]}
                f.write(json.dumps(err, ensure_ascii=False) + "\n")
                print(f"[{i:02d}/{len(node_ids)}] {nid:48s} ERR: {str(e)[:80]}")
                results.append(err)

    elapsed = time.time() - t0
    print()
    print(f"=== Pilot done in {elapsed:.1f}s ===")
    print(f"Total nodes: {len(node_ids)}")
    print(f"Total kw added: {total_added}")
    print(f"Avg per node: {total_added/max(len(node_ids),1):.1f}")
    print(f"Log: {log_path}")
    return {"results": results, "total_added": total_added, "elapsed_s": elapsed}


def discover_target_ids() -> list[str]:
    """全量模式: 筛 active + desc>=80 + kw<15 节点."""
    r = requests.get(f"{THREE_CAN}/api/nodes", timeout=20).json()
    nodes = r if isinstance(r, list) else r.get('nodes', [])
    target = []
    for n in nodes:
        if n.get('status') != 'active':
            continue
        kws = n.get('activation_keywords') or []
        if len(kws) >= 15:
            continue
        c = n.get('content', {}) or {}
        txt = (c.get('description','') or '') + (c.get('current_state','') or '') + (c.get('notes','') or '')
        if len(txt) < 80:
            continue
        target.append(n['id'])
    return target


def run_concurrent(node_ids: list[str], workers: int = 10) -> dict:
    """并发模式 — ThreadPoolExecutor."""
    import concurrent.futures
    api_key = _load_deepseek_key()
    if not api_key:
        return {"error": "no deepseek key"}

    log_path = LOG_DIR / f"{time.strftime('%Y-%m-%d')}_full.jsonl"
    t0 = time.time()
    done, errors, total_added = 0, 0, 0

    def _one(nid):
        try:
            return enrich_node(nid, api_key)
        except Exception as e:
            return {"node_id": nid, "error": str(e)[:200]}

    with log_path.open("a", encoding="utf-8") as f, \
         concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_one, nid): nid for nid in node_ids}
        for fut in concurrent.futures.as_completed(futures):
            nid = futures[fut]
            res = fut.result() or {"node_id": nid, "error": "no result"}
            res["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            f.write(json.dumps(res, ensure_ascii=False) + "\n")
            f.flush()
            done += 1
            if "error" in res:
                errors += 1
            else:
                total_added += res.get("added", 0)
            if done % 20 == 0 or done == len(node_ids):
                elapsed = time.time() - t0
                print(f"  progress {done}/{len(node_ids)} | +{total_added} kw | err={errors} | {elapsed:.0f}s")

    elapsed = time.time() - t0
    print(f"\n=== Full done in {elapsed:.1f}s ({elapsed/60:.1f}min) ===")
    print(f"Nodes: {len(node_ids)}  OK: {len(node_ids)-errors}  Err: {errors}")
    print(f"Total kw added: {total_added}  avg: {total_added/max(len(node_ids)-errors,1):.1f}")
    print(f"Log: {log_path}")
    return {"nodes": len(node_ids), "errors": errors, "total_added": total_added, "elapsed_s": elapsed}


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "pilot"

    if mode == "--dry":
        ids = discover_target_ids()
        print(f"Target: {len(ids)} nodes to enrich (estimate cost ${len(ids)*0.0015:.2f})")
        sys.exit(0)
    elif mode == "--full":
        ids = discover_target_ids()
        workers = int(sys.argv[2]) if len(sys.argv) > 2 else 10
        print(f"[FULL] {len(ids)} nodes, {workers} concurrent workers")
        run_concurrent(ids, workers=workers)
    else:
        pilot_ids = [
            "MOD-frontend", "MOD-advisor", "MOD-kb",
            "FEE-kling-poll-3bugs-s66", "DEC-distill-purify-judge-upgrade-s66",
            "DEC-script-harness-v2-ka-3axis-s66", "DEC-3can-route-hardening-s66",
            "MOD-3can-topic-neighbors-s66", "ERR-deepseek-endpoint-404",
            "FEE-llm-token-budget-split", "ARCH-saas-coach-v1-draft",
            "FEE-token-hotspot-transcript-swell-s66", "ERR-route-short-code-recall",
        ]
        run_pilot(pilot_ids)
