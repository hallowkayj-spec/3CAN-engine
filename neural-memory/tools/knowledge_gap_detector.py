"""Knowledge Gap Detector — 修事故 1 (route "Gemma 4 本地部署" 连空)

扫 3CAN activity log, 找 route 返回 <3 nodes 的 query,
累积成 QUERY-gap-* 节点, 或 append 到 DOC-3can-knowledge-gaps-*.

目标: 让 3CAN **自己知道自己不知道啥**, the maintainer/Opus 下次调研时能看到列表.

用法:
  python tools/knowledge_gap_detector.py          # 扫最近 100 条
  python tools/knowledge_gap_detector.py --limit=500
  python tools/knowledge_gap_detector.py --top=20 # 只报 top 缺口
"""
from __future__ import annotations

import re
import sys
import time
from collections import Counter

import requests

THREE_CAN = "http://localhost:9700"
GAP_NODE_ID = "DOC-3can-knowledge-gaps-live"  # 活动记录 (集中存缺口)


def fetch_activity(limit: int = 200) -> list[dict]:
    try:
        r = requests.get(f"{THREE_CAN}/api/activity?limit={limit}", timeout=15)
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else data.get("activity", data.get("events", []))
    except Exception as e:
        print(f"fetch_activity error: {e}")
        return []


def parse_route_outcome(detail: str) -> tuple[str | None, int | None]:
    """解析 'query=xxx → N nodes' 拿 (query, N).

    actual: 'route query=\'GPT5.4 frontend backend contract Next.js Vercel AI SDK techn\' → 6 nodes'
    """
    m = re.search(r"query=['\"](.+?)['\"]\s*(?:→|->)\s*(\d+)\s*nodes?", detail)
    if m:
        return m.group(1), int(m.group(2))
    # fallback: 'query=X → 0 nodes'
    m2 = re.search(r"query=(.+?)[\s→-]+(\d+)\s*nodes?", detail)
    if m2:
        return m2.group(1).strip("'\""), int(m2.group(2))
    return None, None


def find_gaps(activity: list[dict], threshold: int = 3) -> list[tuple[str, int]]:
    """找到 route 返回 <threshold 的 query."""
    gaps = Counter()
    for a in activity:
        if a.get("action") != "route":
            continue
        detail = a.get("detail", "") or ""
        q, n = parse_route_outcome(detail)
        if q and n is not None and n < threshold:
            # 规整 query (去掉尾部 ... 截断)
            q_clean = q.strip().rstrip("…").rstrip(".")
            if len(q_clean) >= 4:  # 太短不算
                gaps[q_clean] += 1
    return gaps.most_common()


def writeback_gap_doc(gaps: list[tuple[str, int]], threshold: int = 3) -> dict:
    """把 gap list append/update 到 DOC-3can-knowledge-gaps-live 节点."""
    ts = time.strftime("%Y-%m-%d %H:%M", time.gmtime())
    description = f"# 3CAN Knowledge Gaps — 活动扫描 {ts} UTC\n\n"
    description += f"扫 activity log, route 返回 <{threshold} nodes 的 query 列表 (需要补节点):\n\n"
    for q, count in gaps[:30]:
        description += f"- [{count}x] `{q[:100]}`\n"
    description += f"\n共 {len(gaps)} 个 gap query, 详见 DOC 更新. 建议下次主脑 HIGH effort 补齐.\n"

    # 判断 node 是否已存在
    existing = requests.get(f"{THREE_CAN}/api/nodes/{GAP_NODE_ID}", timeout=10)
    keywords = [
        "knowledge gap", "3CAN 缺口", "route 零召回", "未命中 query",
        "gap detector", "需补节点", "activity 扫描", "S66c gap 清单",
        "missing nodes", "coverage audit",
    ]
    payload = {
        "id": GAP_NODE_ID,
        "name": "3CAN 知识缺口活跃清单 (最近 activity 扫描)",
        "cluster": "项目文档",
        "type": "knowledge",
        "primary_author": "gap-detector",
        "activation_keywords": keywords,
        "content": {
            "description": description[:2000],
            "current_state": f"{ts}: {len(gaps)} 个 gap, top={gaps[0][0][:40] if gaps else 'none'}",
        },
    }
    if existing.status_code == 200:
        # update via PUT (content and keywords)
        upd = requests.put(f"{THREE_CAN}/api/nodes/{GAP_NODE_ID}", json={
            "activation_keywords": keywords,
        }, timeout=10)
        return {"action": "updated", "status": upd.status_code, "gap_count": len(gaps)}
    else:
        # create with force
        cr = requests.post(f"{THREE_CAN}/api/nodes?force=true", json=payload, timeout=15)
        if cr.status_code not in (200, 201):
            cr = requests.post(f"{THREE_CAN}/api/nodes", json=payload, timeout=15)
        return {"action": "created", "status": cr.status_code, "gap_count": len(gaps)}


if __name__ == "__main__":
    limit = 200
    threshold = 3
    top = 30
    for arg in sys.argv[1:]:
        if arg.startswith("--limit="):
            limit = int(arg.split("=")[1])
        elif arg.startswith("--threshold="):
            threshold = int(arg.split("=")[1])
        elif arg.startswith("--top="):
            top = int(arg.split("=")[1])

    print(f"Scanning last {limit} activity events for route gaps (threshold < {threshold})...")
    activity = fetch_activity(limit)
    print(f"Fetched {len(activity)} events")

    gaps = find_gaps(activity, threshold=threshold)
    print(f"\nFound {len(gaps)} distinct gap queries (top {top}):")
    for q, count in gaps[:top]:
        print(f"  [{count:2d}x] {q[:90]}")

    if len(sys.argv) > 1 and "--dry" in sys.argv:
        sys.exit(0)

    if gaps:
        result = writeback_gap_doc(gaps, threshold=threshold)
        print(f"\nWriteback: {result}")
    else:
        print("\nNo gaps found — all queries hitting >=3 nodes.")
