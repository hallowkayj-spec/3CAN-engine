"""Suite-wide safety defaults for heavyweight optional model loaders."""

import importlib.util
import os
import sys
import types


# Unit and contract tests must never start overlapping native Torch/Transformers
# loaders. Dedicated warmup tests override this value explicitly.
os.environ["THREECAN_RERANKER_WARMUP"] = "off"


if importlib.util.find_spec("mcp") is None:
    class _TestFastMCP:
        """Decorator-only stand-in for tests that do not start an MCP server."""

        def __init__(self, *_args, **_kwargs):
            pass

        def tool(self):
            return lambda function: function

        def run(self, *_args, **_kwargs):  # pragma: no cover - safety boundary
            raise RuntimeError("test MCP stub cannot run a server")

    mcp_module = types.ModuleType("mcp")
    server_module = types.ModuleType("mcp.server")
    fastmcp_module = types.ModuleType("mcp.server.fastmcp")
    fastmcp_module.FastMCP = _TestFastMCP
    mcp_module.server = server_module
    server_module.fastmcp = fastmcp_module
    sys.modules.update(
        {
            "mcp": mcp_module,
            "mcp.server": server_module,
            "mcp.server.fastmcp": fastmcp_module,
        }
    )
