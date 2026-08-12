"""3CAN Housekeeping Audit — S66d Wave 1 F

纯报告, 不改动。输出 4 类候选供 the maintainer 审批:
1. 可 dormant (active 且 30d+ 未命中 且 activation_count=0)
2. 孤立节点 (无入边 无出边)
3. keyword 热重 (同 kw 在 ≥6 节点, 降低区分度)
4. PROPOSED-* (observer_llm_analyzer 待审)

运行:
  python tools/housekeeping_audit.py
  python tools/housekeeping_audit.py --json > audit_report.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from collections import defaultdict
from pathlib import Path


THREE_CAN = "http://localhost:9700"
ROOT = Path(__file__).resolve().parents[1]
NODES_DIR = ROOT / "graph" / "nodes"
EDGES_FILE = ROOT / "graph" / "edges.json"

DORMANT_AGE_DAYS = 30
KW_HEAT_THRESHOLD = 6


def load_nodes() -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in NODES_DIR.glob("*.json")]


def load_edges() -> list[dict]:
    if not EDGES_FILE.exists():
        return []
    data = json.loads(EDGES_FILE.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else data.get("edges", [])


def dormant_candidates(nodes: list[dict], now: dt.datetime) -> list[dict]:
    """active 且 updated_at > 30d 且 activation_count=0."""
    out = []
    for n in nodes:
        if n.get("status") != "active":
            continue
        if n.get("activation_count", 0) > 0:
            continue
        ts = n.get("updated_at") or n.get("created_at")
        if not ts:
            continue
        try:
            updated = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            continue
        age = (now - updated).days
        if age >= DORMANT_AGE_DAYS:
            out.append({"id": n["id"], "name": n.get("name", "")[:50], "age_days": age,
                        "priority": n.get("priority", "medium")})
    return sorted(out, key=lambda x: -x["age_days"])


def orphan_nodes(nodes: list[dict], edges: list[dict]) -> list[dict]:
    connected: set[str] = set()
    for e in edges:
        connected.add(e.get("source", ""))
        connected.add(e.get("target", ""))
    out = []
    for n in nodes:
        if n["id"] not in connected and n.get("status") == "active":
            out.append({"id": n["id"], "name": n.get("name", "")[:50],
                        "cluster": n.get("cluster", ""), "type": n.get("type", "")})
    return out


def kw_heat(nodes: list[dict]) -> list[dict]:
    """同 kw 出现在 ≥threshold 节点上, 说明区分度低."""
    kw_to_nodes: dict[str, list[str]] = defaultdict(list)
    for n in nodes:
        for kw in n.get("activation_keywords", []):
            if isinstance(kw, str) and len(kw) >= 2:
                kw_to_nodes[kw.lower().strip()].append(n["id"])
    out = []
    for kw, nids in kw_to_nodes.items():
        if len(nids) >= KW_HEAT_THRESHOLD:
            out.append({"keyword": kw, "node_count": len(nids), "sample_ids": nids[:5]})
    return sorted(out, key=lambda x: -x["node_count"])[:30]


def proposed_pending(nodes: list[dict]) -> list[dict]:
    out = []
    for n in nodes:
        if n["id"].startswith("PROPOSED-"):
            out.append({"id": n["id"], "name": n.get("name", "")[:60],
                        "status": n.get("status", ""), "type": n.get("type", "")})
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    args = parser.parse_args()

    nodes = load_nodes()
    edges = load_edges()
    now = dt.datetime.now(dt.timezone.utc)

    report = {
        "timestamp": now.isoformat(),
        "total_nodes": len(nodes),
        "total_edges": len(edges),
        "dormant_candidates": dormant_candidates(nodes, now),
        "orphan_nodes": orphan_nodes(nodes, edges),
        "kw_heat_hotspots": kw_heat(nodes),
        "proposed_pending_review": proposed_pending(nodes),
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    # 人读报告
    print(f"3CAN Housekeeping Audit  —  {report['timestamp'][:19]}")
    print(f"{'='*70}")
    print(f"nodes={report['total_nodes']}  edges={report['total_edges']}\n")

    d = report["dormant_candidates"]
    print(f"[1] 可 dormant 候选 (active + 0 命中 + {DORMANT_AGE_DAYS}d+ 未动): {len(d)} 个")
    for item in d[:15]:
        print(f"  - {item['id']:55s}  age={item['age_days']:3d}d  pri={item['priority']}  {item['name']}")
    if len(d) > 15:
        print(f"  ... (共 {len(d)}, 用 --json 看全)")

    o = report["orphan_nodes"]
    print(f"\n[2] 孤立节点 (active, 无任何边): {len(o)} 个")
    for item in o[:15]:
        print(f"  - {item['id']:55s}  [{item['type']:10s}] {item['name']}")
    if len(o) > 15:
        print(f"  ... (共 {len(o)})")

    k = report["kw_heat_hotspots"]
    print(f"\n[3] keyword 热重 (同 kw 在 ≥{KW_HEAT_THRESHOLD} 节点, 区分度低): {len(k)} 个")
    for item in k[:10]:
        print(f"  - {item['keyword']:30s}  n={item['node_count']}  sample={item['sample_ids'][:3]}")

    p = report["proposed_pending_review"]
    print(f"\n[4] PROPOSED-* 待审: {len(p)} 个")
    for item in p[:10]:
        print(f"  - {item['id']:55s}  [{item['type']}]  {item['name']}")

    print(f"\n{'='*70}")
    print("建议操作 (the maintainer 选):")
    print("  - dormant: POST /api/nodes/batch-dormant  body=['id1','id2']")
    print("  - PROPOSED 批准: PUT /api/nodes/{id} status=active + 改 id 去 PROPOSED- 前缀")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
