#!/usr/bin/env python3
"""3CAN MCP Server — 本地stdio传输, 零网络成本.

让任何MCP客户端(Claude Code, Cursor, VS Code等)直接访问3CAN记忆引擎.
通过HTTP proxy (localhost:9700) 转发, 不直连backend.

启动: python mcp_server.py
Claude Code配置: settings.json → mcpServers → "3can": {"command":"python","args":["path/to/mcp_server.py"]}
"""

import httpx
from mcp.server.fastmcp import FastMCP

BASE = "http://localhost:9700"
client = httpx.Client(timeout=15.0)

mcp = FastMCP(
    name="3CAN Memory Engine",
    instructions=(
        "3CAN是一个图结构记忆引擎, 存储1200+节点的项目知识图谱. "
        "用route查询相关记忆, 用read_node获取完整节点内容, "
        "用writeback回写变更, 用route_outcome记录路由反馈."
    ),
)


def _post(path: str, body: dict) -> dict:
    r = client.post(f"{BASE}{path}", json=body)
    r.raise_for_status()
    return r.json()


def _get(path: str, params: dict | None = None) -> dict:
    r = client.get(f"{BASE}{path}", params=params)
    r.raise_for_status()
    return r.json()


def _owner_defaults_line(payload: dict) -> str:
    owner = payload.get("owner_defaults") or {}
    defaults = owner.get("defaults") or {}
    if owner.get("status") != "applied" or not isinstance(defaults, dict):
        return ""
    pairs = " ".join(
        f"{key}={defaults[key]}"
        for key, value in defaults.items()
        if value
    )
    origin = str(owner.get("assertion_origin") or "unspecified")
    return (
        f"owner defaults ({owner.get('source', '3CAN.md')}; {origin}): "
        f"{pairs}"
    )


# ── Core Tools ──


@mcp.tool()
def route(
    query: str,
    max_nodes: int = 4,
    agent_id: str = "mcp-client",
    project_id: str = "",
    project_namespace: str = "",
    workspace_id: str = "",
    workorder_id: str = "",
    session_instance_id: str = "",
    route_id: str = "",
) -> str:
    """查询3CAN记忆引擎, 返回最相关的节点(slim模式, 省token).

    用于: 查找项目记忆/决策/接口/错误记录/反馈规则等.
    短代码(如S55c, KB4, E6)和自然语言查询均可.
    """
    body = {
        "task": query,
        "max_nodes": max_nodes,
        "agent_id": agent_id,
        **{
            key: value
            for key, value in {
                "project_id": project_id,
                "project_namespace": project_namespace,
                "workspace_id": workspace_id,
                "workorder_id": workorder_id,
                "session_instance_id": session_instance_id,
                "route_id": route_id,
            }.items()
            if value
        },
    }
    data = _post("/api/route", body)
    # Handle both slim ("nodes") and full ("activated_nodes") response formats
    raw_nodes = data.get("nodes") or data.get("activated_nodes", [])
    scores = data.get("scores", {})
    route_meta = data.get("route_meta") or {}
    owner_line = _owner_defaults_line(route_meta)
    if not raw_nodes:
        return "\n".join(item for item in (owner_line, "未找到匹配节点") if item)
    actual_route_id = str(
        route_meta.get("route_id") or data.get("route_id") or ""
    ).strip()
    correlation = f"route_id={actual_route_id}"
    if session_instance_id:
        correlation += f" session_instance_id={session_instance_id}"
    lines = [
        f"{owner_line}\n" if owner_line else "",
        f"共{data.get('total_nodes', '?')}节点, 返回{len(raw_nodes)}个:\n",
        f"correlation: {correlation}\n" if actual_route_id else "",
    ]
    for i, n in enumerate(raw_nodes):
        nid = n.get("id", "?")
        name = n.get("name", "")
        score = scores.get(nid, 0)
        # Slim format has "summary", full format has "content.description"
        summary = n.get("summary") or ""
        if not summary and isinstance(n.get("content"), dict):
            summary = (n["content"].get("description") or "")[:150]
        state = n.get("current_state") or ""
        if not state and isinstance(n.get("content"), dict):
            state = (n["content"].get("current_state") or "")[:80]
        state_str = f" | 状态: {state}" if state else ""
        cluster = n.get("cluster", "")
        lines.append(
            f"{i+1}. [{nid}] {name}\n"
            f"   {summary[:150]}{state_str}\n"
            f"   score={score:.4f} cluster={cluster}"
        )
    return "\n".join(lines)


@mcp.tool()
def read_node(
    node_id: str,
    agent_id: str = "mcp-client",
    session_instance_id: str = "",
    route_id: str = "",
) -> str:
    """读取单个节点的完整内容(全量, ~800token).

    当route结果的slim摘要不够时, 用此工具获取详情.
    只有携带 route 返回的 route_id（以及对应 session_instance_id）时才记录
    精确相关的 outcome；无 correlation 参数时保持只读，避免跨 Session 串反馈.
    """
    if session_instance_id and not route_id:
        raise ValueError("route_id_required_with_session_instance_id")
    params = None
    if route_id:
        params = {
            "agent_id": agent_id,
            "route_id": route_id,
            **(
                {"session_instance_id": session_instance_id}
                if session_instance_id
                else {}
            ),
        }
    try:
        data = _get(f"/api/nodes/{node_id}", params)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return f"节点 {node_id} 不存在"
        raise
    content = data.get("content", {})
    lines = [
        f"# {data['name']} [{data['id']}]",
        f"cluster: {data.get('cluster', '')} | type: {data.get('type', '')} | priority: {data.get('priority', '')}",
        f"status: {data.get('status', '')} | activation_count: {data.get('activation_count', 0)}",
        f"\n## Description\n{content.get('description', '')}",
    ]
    if content.get("current_state"):
        lines.append(f"\n## Current State\n{content['current_state']}")
    if content.get("notes"):
        lines.append(f"\n## Notes\n{content['notes'][:1500]}")
    if content.get("key_files"):
        lines.append("\n## Key Files\n" + "\n".join(f"- {f}" for f in content["key_files"]))
    if content.get("decisions"):
        lines.append("\n## Decisions\n" + "\n".join(f"- {d}" for d in content["decisions"]))
    if content.get("blockers"):
        lines.append("\n## Blockers\n" + "\n".join(f"- {b}" for b in content["blockers"]))
    kws = data.get("activation_keywords", [])
    if kws:
        lines.append(f"\n## Keywords\n{', '.join(kws[:20])}")
    return "\n".join(lines)


@mcp.tool()
def writeback(
    node_id: str,
    field: str,
    value: str,
    agent_id: str = "mcp-client",
    source_provenance: str = "untrusted_inferred",
    verification_state: str = "unverified",
    evidence_refs: list[str] | None = None,
    authorized_by: str = "",
    project_id: str = "",
    project_namespace: str = "",
    workspace_id: str = "",
    workorder_id: str = "",
) -> str:
    """回写节点变更. field: current_state / notes / status / description.

    Durable current fields accept user_authoritative + authorized_by=user as an audit
    assertion. machine_verifiable remains fail-closed until a canonical evidence owner
    binds the target node/field/value; existing resolution receipts cannot be borrowed
    across facts. Notes/last_session remain low-ceremony.
    """
    data = _post("/api/writeback", {
        "agent_id": agent_id,
        "source_provenance": source_provenance,
        "verification_state": verification_state,
        "evidence_refs": evidence_refs or [],
        "authorized_by": authorized_by,
        "project_id": project_id,
        "project_namespace": project_namespace,
        "workspace_id": workspace_id,
        "workorder_id": workorder_id,
        "changes": [{"node_id": node_id, "field": field, "value": value}],
    })
    updated = data.get("updated", [])
    if updated:
        return f"已更新 {', '.join(updated)}"
    return f"回写失败: 节点 {node_id} 不存在或字段无效"


@mcp.tool()
def route_outcome(
    query: str,
    node_id: str,
    signal: float = 1.0,
) -> str:
    """记录路由outcome信号, 用于学习优化.

    signal: +1.0=确认使用, -1.0=跳过/grep了别的, +0.3=部分使用.
    3次positive confirmation后mapping进active.
    """
    _post("/api/route/outcome", {
        "query": query, "node_id": node_id, "signal": signal,
    })
    return f"已记录: {query} → {node_id} (signal={signal})"


@mcp.tool()
def stats() -> str:
    """获取3CAN引擎状态统计."""
    data = _get("/api/stats")
    return (
        f"总节点: {data['total_nodes']} | 活跃: {data['active_nodes']} | 边: {data['total_edges']}\n"
        f"最后更新: {data.get('last_updated', 'N/A')}"
    )


@mcp.tool()
def briefing(
    agent_id: str = "mcp-client",
    max_nodes: int = 6,
    project_id: str = "",
    project_namespace: str = "",
) -> str:
    """获取Agent冷启动简报 — 高频节点+待处理handoff+近期活动."""
    data = _get(
        "/api/briefing",
        {
            "agent_id": agent_id,
            "max_nodes": max_nodes,
            **(
                {
                    "project_id": project_id,
                    "project_namespace": project_namespace,
                }
                if project_id and project_namespace
                else {}
            ),
        },
    )
    lines = [f"Agent: {data.get('agent_id', agent_id)} | role: {data.get('role', 'N/A')}"]
    owner_line = _owner_defaults_line(data)
    if owner_line:
        lines.append(owner_line)
    lines.append(f"活跃节点: {data.get('total_active', '?')} | 休眠: {data.get('total_dormant', '?')}\n")

    hot = data.get("hot_nodes", [])
    if hot:
        lines.append("## 高频节点")
        for n in hot[:6]:
            lines.append(f"- [{n['id']}] {n['name'][:60]} (activated: {n.get('activation_count', 0)})")

    pending = data.get("pending_handoffs", [])
    if pending:
        lines.append(f"\n## 待接收Handoff ({len(pending)})")
        for h in pending:
            lines.append(f"- [{h['id']}] {h.get('description', '')[:80]}")

    return "\n".join(lines)


@mcp.tool()
def search_nodes(keyword: str, limit: int = 10) -> str:
    """按关键词搜索节点列表. 返回ID+名称+cluster, 用于定位."""
    data = _get("/api/nodes", {"limit": 2000})
    nodes = data if isinstance(data, list) else data.get("nodes", [])
    kw_lower = keyword.lower()
    matched = []
    for n in nodes:
        text = f"{n.get('id', '')} {n.get('name', '')} {n.get('cluster', '')}".lower()
        kws = " ".join(n.get("activation_keywords", [])).lower()
        if kw_lower in text or kw_lower in kws:
            matched.append(n)
    matched = matched[:limit]
    if not matched:
        return f"未找到包含 '{keyword}' 的节点"
    lines = [f"找到 {len(matched)} 个匹配:"]
    for n in matched:
        lines.append(f"- [{n['id']}] {n.get('name', '')[:60]} ({n.get('cluster', '')})")
    return "\n".join(lines)


@mcp.tool()
def create_node(
    name: str,
    cluster: str,
    description: str,
    keywords: str,
    node_type: str = "knowledge",
    agent_id: str = "mcp-client",
) -> str:
    """创建新节点. keywords用逗号分隔, 至少5个(含中英文/同义词).

    用于: 发现新概念/错误/决策时持久化到知识图谱.
    """
    kw_list = [k.strip() for k in keywords.split(",") if k.strip()]
    data = _post("/api/nodes", {
        "name": name,
        "cluster": cluster,
        "type": node_type,
        "content": {"description": description},
        "activation_keywords": kw_list,
    })
    nid = data.get("id", "unknown")
    warnings = data.get("warnings", [])
    result = f"节点已创建: {nid}"
    if warnings:
        result += f"\n⚠ {'; '.join(warnings)}"
    return result


if __name__ == "__main__":
    mcp.run(transport="stdio")
