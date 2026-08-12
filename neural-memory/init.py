#!/usr/bin/env python3
"""3CAN一键接入 — 任何环境执行这一行即可:

    python neural-memory/init.py

功能:
  1. 检测3CAN引擎是否在线, 不在线则启动
  2. 检测MCP配置是否存在, 不存在则写入
  3. 输出接入指令 (给人或给agent的system prompt)
"""

import json
import subprocess
import sys
import time
from pathlib import Path

ENGINE_URL = "http://localhost:9700"
MCP_SERVER = Path(__file__).resolve().parent / "mcp_server.py"
PROXY_SERVER = Path(__file__).resolve().parent / "proxy" / "server.py"

# Claude Code MCP config paths
CLAUDE_MCP = Path.home() / ".claude" / "mcp.json"


def check_engine():
    """Returns True if 3CAN engine is reachable."""
    try:
        import httpx
        r = httpx.get(f"{ENGINE_URL}/api/stats", timeout=3)
        data = r.json()
        return data.get("total_nodes", 0) > 0, data
    except Exception:
        return False, {}


def start_engine():
    """Start the 3CAN single-writer slot proxy."""
    print("[init] Starting 3CAN engine...")
    proc = subprocess.Popen(
        [sys.executable, str(PROXY_SERVER)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(10):
        time.sleep(1)
        ok, _ = check_engine()
        if ok:
            print(f"[init] Engine started (PID {proc.pid})")
            return True
    print("[init] WARNING: Engine failed to start within 10s")
    return False


def ensure_mcp_config():
    """Add 3CAN to Claude Code MCP config if not present."""
    config = {}
    if CLAUDE_MCP.exists():
        try:
            config = json.loads(CLAUDE_MCP.read_text(encoding="utf-8"))
        except Exception:
            config = {}

    servers = config.setdefault("mcpServers", {})
    if "3can" in servers:
        print("[init] MCP config: already configured")
        return False

    servers["3can"] = {
        "command": "python",
        "args": [str(MCP_SERVER)],
    }
    CLAUDE_MCP.parent.mkdir(parents=True, exist_ok=True)
    CLAUDE_MCP.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[init] MCP config: added to {CLAUDE_MCP}")
    return True


def main():
    print("=" * 50)
    print("3CAN Memory Engine — Init")
    print("=" * 50)

    # 1. Check/start engine
    online, stats = check_engine()
    if online:
        print(f"[init] Engine online: {stats.get('total_nodes', '?')} nodes, {stats.get('total_edges', '?')} edges")
    else:
        online = start_engine()
        if online:
            _, stats = check_engine()

    # 2. Ensure MCP config
    ensure_mcp_config()

    # 3. Output connection info
    print()
    if online:
        n = stats.get("total_nodes", "?")
        print(f"Ready. {n} nodes online.")
        print()
        print("For Claude Code / Claude Desktop:")
        print("  MCP tools auto-available: route, read_node, writeback, briefing, stats")
        print()
        print("For any other agent (paste into system prompt):")
        print(f'  3CAN Memory Engine at {ENGINE_URL} — POST /api/route {{"task":"query","max_nodes":4}} to search memory')
    else:
        print("Engine offline. Start manually:")
        print(f"  python {PROXY_SERVER}")


if __name__ == "__main__":
    main()
