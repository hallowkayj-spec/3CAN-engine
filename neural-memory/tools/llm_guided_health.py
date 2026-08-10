"""3CAN LLM-Guided Data Health Audit — 基座#3

替代 housekeeping_audit.py 的死指标 (孤立率/零激活/30d 未命中).
真实判断: 节点对**当前项目**的活跃价值, 要看项目上下文 + 语义, 不是计数.

场景:
  - "小红书 MCP" 节点 60 天未命中, 但项目里从没说"关闭小红书方向" → 保留
  - "S24 某前端 bug" 节点半年未命中, 项目已转向 S66 → 降级 dormant

流程:
  1. housekeeping_audit 产出嫌疑候选 (孤立 + 零激活 + 低 GDI)
  2. 本工具对每候选:
     a. 读节点内容
     b. 拉最近 20 条 active SES-*/HO-* 作为"当前项目上下文"
     c. DeepSeek 判: 此节点对项目当前状态的"活跃价值" 0-10 + 原因
  3. 分数 <3 → 建议 dormant (写 proposal)
  4. 分数 >=6 → 标记 keep (写 extra.health_score)
  5. 3-6 → 等更多数据

输出 (纯只读不改):
  - data/_health/llm_health_report_{ts}.json
  - 每节点 {id, llm_score, reason, suggest_action}

运行:
  python tools/llm_guided_health.py --limit 50
  python tools/llm_guided_health.py --apply   # 按 proposal 改 status
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import json
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
NODES_DIR = ROOT / "graph" / "nodes"
OUT_DIR = ROOT / "data" / "_health"
OUT_DIR.mkdir(parents=True, exist_ok=True)

THREE_CAN = "http://localhost:9700"
DEEPSEEK_ENDPOINT = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"

MAX_CONCURRENT = 4
PER_CALL_TIMEOUT = 30
CONTEXT_SES_N = 20              # 最近 N 个活跃 SES/HO 节点作为项目上下文
DORMANT_AGE_D = 30              # dormant 候选年龄下限 (与 rules/01-core.md 一致, 30d)
LOW_THRESHOLD = 3.0             # LLM 分 <3 → 建议 dormant
KEEP_THRESHOLD = 6.0            # LLM 分 >=6 → 保留

PROMPT = """你是 3CAN 图谱的数据健康审查员. 判断一个节点对**当前项目**是否还有活跃价值.

关键原则:
- 节点没被 route 命中 ≠ 无价值 (可能只是项目阶段还没用到)
- 节点孤立 (无边) ≠ 无价值 (可能是独立资源, 如小红书 MCP 部署过, 暂时不调用)
- 真无价值的: 废弃方向的技术栈 / 已完结 session 的临时草稿 / 过时事实 (已被新决策覆盖)

待审节点:
- id: {nid}
- name: {name}
- cluster: {cluster}
- type: {ntype}
- status: {status}
- age: {age_days} 天未更新
- activation_count: {act_count}
- description: {desc}
- notes: {notes}
- keywords: {kws}

当前项目上下文 (最近 {n_ctx} 个活跃 SES/HO/DEC 摘要, 用于判节点相关性):
{context}

输出严格 JSON:
{{
  "llm_score": 0.0-10.0,
  "still_relevant": true/false,
  "reason": "≤50字 为什么这个分数",
  "suggest_action": "keep" | "dormant" | "wait"
}}

评分参考:
- 0-3: 明显过时, 项目已无此方向, 建议 dormant
- 3-6: 边缘, 不确定, wait 收集更多数据
- 6-10: 仍有潜在价值, 保留 active (即使长期没命中)
"""


def _load_key() -> str | None:
    p = Path.home() / ".claude" / "secrets.json"
    if not p.exists():
        return None
    try:
        return (json.loads(p.read_text(encoding="utf-8")).get("deepseek") or {}).get("api_key")
    except Exception:
        return None


def load_all_nodes() -> list[dict]:
    out = []
    for p in NODES_DIR.glob("*.json"):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def find_candidates(nodes: list[dict], now: dt.datetime, min_age_d: int = DORMANT_AGE_D) -> list[dict]:
    """嫌疑候选 (LLM 再细判):
    - active 节点
    - (a) activation_count=0 或 (b) quality_score < 4.0 或 (c) 原始 created_at > min_age_d 天且零命中
    """
    # 先算全图所有节点的 edge 连接 (判孤立)
    edges = []
    edges_f = ROOT / "graph" / "edges.json"
    if edges_f.exists():
        try:
            raw = json.loads(edges_f.read_text(encoding="utf-8"))
            edges = raw if isinstance(raw, list) else raw.get("edges", [])
        except Exception:
            pass
    connected: set[str] = set()
    for e in edges:
        connected.add(e.get("source", ""))
        connected.add(e.get("target", ""))

    out = []
    for n in nodes:
        if n.get("status") != "active":
            continue
        nid = n["id"]
        ac = n.get("activation_count", 0) or 0
        extra = (n.get("content", {}) or {}).get("extra", {}) or {}
        qs = extra.get("quality_score", None)
        # created_at age (不看 updated_at, 避免本 session 更新污染)
        ts_c = n.get("created_at") or ""
        age_d = 0
        try:
            created = dt.datetime.fromisoformat(ts_c.replace("Z", "+00:00"))
            age_d = (now - created).days
        except Exception:
            pass

        is_orphan = nid not in connected
        is_zero_act = ac == 0
        is_low_quality = qs is not None and qs < 4.0
        is_old = age_d >= min_age_d

        # 进候选规则:
        # - 孤立 + 零命中 (明显嫌疑) OR
        # - 低 quality_score (<4) OR
        # - 老 (>min_age_d) 且零命中
        candidate = (is_orphan and is_zero_act) or is_low_quality or (is_old and is_zero_act)
        if not candidate:
            continue
        n["_age_days"] = age_d
        n["_is_orphan"] = is_orphan
        n["_quality_score"] = qs
        out.append(n)
    # 排序: quality_score 低的优先, 再老的
    return sorted(out, key=lambda x: (x.get("_quality_score") or 10.0, -x.get("_age_days", 0)))


def build_project_context(nodes: list[dict], n_ctx: int = CONTEXT_SES_N) -> str:
    """最近 N 个活跃 SES-*/HO-*/DEC-* 的摘要作为项目当前上下文."""
    ctx_nodes = []
    for n in nodes:
        if n.get("status") != "active":
            continue
        nid = n.get("id", "")
        if not (nid.startswith("SES-") or nid.startswith("HO-") or nid.startswith("DEC-")):
            continue
        ts = n.get("updated_at") or n.get("created_at") or ""
        ctx_nodes.append((ts, n))
    ctx_nodes.sort(key=lambda x: x[0], reverse=True)
    lines = []
    for _, n in ctx_nodes[:n_ctx]:
        c = n.get("content", {}) or {}
        desc = (c.get("description") or c.get("current_state") or "")[:120]
        lines.append(f"- [{n['id'][:40]}] {n.get('name','')[:50]}: {desc}")
    return "\n".join(lines)


def deepseek_call(api_key: str, prompt: str) -> dict:
    try:
        r = requests.post(
            DEEPSEEK_ENDPOINT,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": DEEPSEEK_MODEL,
                "messages": [{"role": "user", "content": prompt}],
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


def judge_node(node: dict, context: str, api_key: str) -> dict:
    c = node.get("content", {}) or {}
    prompt = PROMPT.format(
        nid=node["id"],
        name=(node.get("name") or "")[:60],
        cluster=node.get("cluster", ""),
        ntype=node.get("type", ""),
        status=node.get("status", ""),
        age_days=node.get("_age_days", 0),
        act_count=node.get("activation_count", 0),
        desc=(c.get("description") or "")[:200],
        notes=(c.get("notes") or "")[:400],
        kws=json.dumps(node.get("activation_keywords", [])[:10], ensure_ascii=False),
        n_ctx=CONTEXT_SES_N,
        context=context,
    )
    ans = deepseek_call(api_key, prompt)
    if ans.get("_error"):
        return {"id": node["id"], "error": ans["_error"]}

    score = float(ans.get("llm_score", 0) or 0)
    action = ans.get("suggest_action", "wait")
    # 硬规则: 分数说不同意 action 时, 以分数为准
    if score < LOW_THRESHOLD:
        action = "dormant"
    elif score >= KEEP_THRESHOLD:
        action = "keep"
    else:
        action = "wait"

    # the maintainer 补充: 不确定 (action=wait) 节点生成用户菜单 (让用户批注)
    user_menu = None
    if action == "wait":
        user_menu = {
            "prompt": f"节点 '{node['id']}' LLM 评 {round(score,2)}/10 不确定, 请你判断:",
            "options": [
                {"key": "k", "label": "保留 active (我确认此节点对项目仍有价值)"},
                {"key": "d", "label": "降 dormant (暂停但不删, 可复活)"},
                {"key": "a", "label": "归档 archive (物理隔离, 不参与 route, 可 restore)"},
                {"key": "r", "label": "重判 (重跑 LLM, 可能我之前 prompt 没给够上下文)"},
                {"key": "n", "label": "打注释 note (留一句给自己日后看, 不改 status)"},
            ],
            "llm_reason": ans.get("reason", "")[:120],
        }

    return {
        "id": node["id"],
        "age_days": node.get("_age_days", 0),
        "is_orphan": node.get("_is_orphan"),
        "quality_score": node.get("_quality_score"),
        "llm_score": round(score, 2),
        "still_relevant": bool(ans.get("still_relevant", False)),
        "reason": ans.get("reason", "")[:150],
        "suggest_action": action,
        "user_menu": user_menu,
    }


def apply_proposal(proposals: list[dict], dry_run: bool = True) -> dict:
    stats = {"dormant": 0, "keep_marked": 0, "wait": 0, "errors": []}
    for p in proposals:
        nid = p.get("id")
        action = p.get("suggest_action")
        score = p.get("llm_score")
        if action == "dormant":
            stats["dormant"] += 1
            if dry_run:
                print(f"  [DRY] {nid} → dormant (score={score}, {p.get('reason','')[:40]})")
                continue
            try:
                r = requests.put(f"{THREE_CAN}/api/nodes/{nid}", json={
                    "status": "dormant",
                    "content": {"extra": {"health_score": score, "health_reason": p.get("reason", "")}}
                }, timeout=15)
                if r.status_code not in (200, 201):
                    stats["errors"].append(f"{nid}: http {r.status_code}")
            except Exception as e:
                stats["errors"].append(f"{nid}: {str(e)[:80]}")
        elif action == "keep":
            stats["keep_marked"] += 1
            if dry_run:
                continue
            try:
                requests.put(f"{THREE_CAN}/api/nodes/{nid}", json={
                    "content": {"extra": {"health_score": score, "health_reason": p.get("reason", "")}}
                }, timeout=15)
            except Exception:
                pass
        else:
            stats["wait"] += 1
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--min-age-d", type=int, default=DORMANT_AGE_D)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry", action="store_true", help="配合 --apply")
    args = ap.parse_args()

    api_key = _load_key()
    if not api_key and not args.apply:
        print("[health] FATAL: no DeepSeek key")
        return 2

    print("[health] 读图...")
    nodes = load_all_nodes()
    now = dt.datetime.now(dt.timezone.utc)

    if args.apply:
        # 从最近 report 读取 proposals
        reports = sorted(OUT_DIR.glob("llm_health_report_*.json"))
        if not reports:
            print("[health] 无 report 可 apply")
            return 1
        last = reports[-1]
        print(f"[health] 读 {last}")
        data = json.loads(last.read_text(encoding="utf-8"))
        stats = apply_proposal(data.get("proposals", []), dry_run=args.dry)
        print(f"[health] apply: {stats}")
        return 0

    candidates = find_candidates(nodes, now, args.min_age_d)[:args.limit]
    print(f"[health] 死指标筛出 {len(candidates)} 嫌疑 (active + 0命中 + {args.min_age_d}d+ 未更新)")

    context = build_project_context(nodes)
    print(f"[health] 项目上下文 {context.count(chr(10)) + 1} 行")

    print(f"[health] DeepSeek 判 {len(candidates)} 节点...")
    results = []
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=MAX_CONCURRENT) as ex:
        futs = {ex.submit(judge_node, n, context, api_key): n["id"] for n in candidates}
        for i, fut in enumerate(cf.as_completed(futs), 1):
            try:
                r = fut.result()
                results.append(r)
            except Exception as e:
                results.append({"error": str(e)[:100]})
            if i % 10 == 0 or i == len(futs):
                print(f"  [{i}/{len(futs)}]  {time.time()-t0:.0f}s", flush=True)

    # 汇总
    by_action = {"keep": 0, "dormant": 0, "wait": 0, "error": 0}
    for r in results:
        if r.get("error"):
            by_action["error"] += 1
        else:
            by_action[r.get("suggest_action", "wait")] = by_action.get(r.get("suggest_action", "wait"), 0) + 1

    report = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "candidates_total": len(candidates),
        "min_age_d": args.min_age_d,
        "context_ses_n": CONTEXT_SES_N,
        "low_threshold": LOW_THRESHOLD,
        "keep_threshold": KEEP_THRESHOLD,
        "by_action": by_action,
        "proposals": results,
    }
    out = OUT_DIR / f"llm_health_report_{int(time.time())}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[health] 分布: {by_action}")
    print(f"[health] 写入 {out}")
    print("[health] 样本 (前 5):")
    for r in results[:5]:
        if r.get("error"):
            print(f"  - {r.get('id','?')}: error={r['error'][:50]}")
        else:
            print(f"  - {r.get('id','?'):55s} score={r.get('llm_score')} → {r.get('suggest_action')}  {r.get('reason','')[:40]}")
    print("\n[health] 下一步: python tools/llm_guided_health.py --apply --dry  (预览)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
