#!/usr/bin/env python3
"""3CAN client setup — observe readiness and configure MCP:

    python neural-memory/init.py

This helper never owns production runtime lifecycle. If 3CAN is unavailable,
local Git/coding/build/offline tests may continue while route/ticket/writeback
remain typed UNAVAILABLE.
"""

import json
import os
from pathlib import Path

ENGINE_URL = os.environ.get(
    "THREECAN_URL",
    os.environ.get("THREECAN_BASE_URL", "http://127.0.0.1:9700"),
)
MCP_SERVER = Path(__file__).resolve().parent / "mcp_server.py"

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

    # 1. Observe engine readiness; lifecycle belongs to the machine operator.
    online, stats = check_engine()
    if online:
        print(f"[init] Engine online: {stats.get('total_nodes', '?')} nodes, {stats.get('total_edges', '?')} edges")

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
        print("3CAN_RUNTIME_UNAVAILABLE")
        print("Request the machine operator/Supervisor; do not start production 9700 from a project session.")
        print("Local Git, coding, builds, and offline tests may continue.")


if __name__ == "__main__":
    main()
