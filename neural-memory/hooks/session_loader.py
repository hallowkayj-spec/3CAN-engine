"""Neural Memory — Session 加载器。

在Claude Code session启动时，根据初始任务描述自动加载相关子图。
可作为hook使用，也可独立运行。

用法:
  python session_loader.py "KB蒸馏推到5000条"
  python session_loader.py --all          # 加载全图摘要
"""
from __future__ import annotations

import argparse
import json
from urllib.request import urlopen, Request
from urllib.error import URLError

API_BASE = "http://localhost:9700"


def route_task(task: str, max_nodes: int = 10) -> dict | None:
    """调用路由API获取相关子图。"""
    try:
        req = Request(
            f"{API_BASE}/api/route",
            data=json.dumps({"task": task, "max_nodes": max_nodes}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except (URLError, ConnectionError):
        return None


def get_stats() -> dict | None:
    """获取图统计。"""
    try:
        with urlopen(f"{API_BASE}/api/stats", timeout=5) as resp:
            return json.loads(resp.read())
    except (URLError, ConnectionError):
        return None


def format_context(result: dict) -> str:
    """把路由结果格式化为Claude可读的上下文摘要。"""
    lines = ["# Neural Memory — 相关子图\n"]

    for node in result.get("activated_nodes", []):
        score = result.get("scores", {}).get(node["id"], 0)
        lines.append(f"## [{node['id']}] {node['name']} (score: {score:.1f})")
        lines.append(f"- Cluster: {node['cluster']} | Layer: {node.get('layer','?')} | Status: {node['status']}")

        content = node.get("content", {})
        if isinstance(content, dict):
            if content.get("description"):
                lines.append(f"- Description: {content['description']}")
            if content.get("current_state"):
                lines.append(f"- State: {content['current_state']}")
            if content.get("tech_stack"):
                lines.append(f"- Tech: {', '.join(content['tech_stack'])}")
            if content.get("blockers"):
                lines.append(f"- Blockers: {', '.join(content['blockers'])}")
            if content.get("key_files"):
                lines.append(f"- Files: {', '.join(content['key_files'][:5])}")
        lines.append("")

    edges = result.get("relevant_edges", [])
    if edges:
        lines.append(f"## 相关边 ({len(edges)})")
        for e in edges[:15]:
            lines.append(f"- {e['source']} →[{e['type']}]→ {e['target']}: {e.get('description','')}")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Neural Memory Session Loader")
    parser.add_argument("task", nargs="?", default="", help="任务描述")
    parser.add_argument("--all", action="store_true", help="加载全图统计")
    parser.add_argument("--max-nodes", type=int, default=10, help="最大激活节点数")
    parser.add_argument("--json", action="store_true", help="输出JSON格式")
    args = parser.parse_args()

    if args.all:
        stats = get_stats()
        if stats:
            if args.json:
                print(json.dumps(stats, ensure_ascii=False, indent=2))
            else:
                print(f"Neural Memory: {stats['total_nodes']} nodes, {stats['total_edges']} edges")
                print(f"Active: {stats['active_nodes']} | Blocked: {stats['blocked_nodes']}")
                for cluster, cnt in sorted(stats.get("clusters", {}).items()):
                    print(f"  {cluster}: {cnt}")
        else:
            print("[WARN] Neural Memory server not running (localhost:9700)")
        return

    if not args.task:
        print("Usage: python session_loader.py '任务描述'")
        return

    result = route_task(args.task, args.max_nodes)
    if result:
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(format_context(result))
    else:
        print("[WARN] Neural Memory server not running (localhost:9700)")


if __name__ == "__main__":
    main()
