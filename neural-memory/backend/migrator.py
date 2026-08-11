"""Neural Memory — 迁移器。

从现有 ~/.claude/projects/.../memory/ 目录导入节点和边。
保留原有记忆体系不动，仅读取并创建图节点。

用法: python migrator.py
"""
from __future__ import annotations

import io
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from graph_engine import GraphEngine
from models import (
    Edge, EdgeCreate, EdgeType, Node, NodeContent, NodeCreate,
    NodeStatus, NodeType, Priority,
)

MEMORY_DIR = Path(os.environ.get("THREECAN_MEMORY_DIR", Path.home() / ".claude" / "memory")).expanduser()

# Windows GBK console fix
if os.name == "nt":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")


# ── 文件分类规则 ──

def classify_memory_file(filename: str) -> tuple[str, NodeType, str, Priority]:
    """返回 (cluster, node_type, layer, priority)。"""
    name = filename.lower().replace(".md", "")

    if name.startswith("feedback-"):
        return "反馈与规则", NodeType.feedback, "L0", Priority.high
    elif name.startswith("session-"):
        return "会话记录", NodeType.session, "L0", Priority.low
    elif name.startswith("project-"):
        if "architecture" in name or "neural" in name:
            return "架构设计", NodeType.knowledge, "L3", Priority.critical
        elif "strategic" in name or "direction" in name:
            return "战略决策", NodeType.decision, "L4", Priority.high
        elif "product" in name or "philosophy" in name:
            return "产品与工艺", NodeType.knowledge, "L2", Priority.high
        elif "store" in name or "ops" in name:
            return "运营知识", NodeType.knowledge, "L2", Priority.high
        elif "data" in name:
            return "数据基础设施", NodeType.knowledge, "L1", Priority.medium
        else:
            return "业务逻辑", NodeType.knowledge, "L2", Priority.medium
    elif name.startswith("technical-"):
        return "工具链", NodeType.tool, "L1", Priority.medium
    elif name.startswith("reference-"):
        return "外部引用", NodeType.reference, "L0", Priority.medium
    elif name.startswith("decisions-"):
        return "战略决策", NodeType.decision, "L3", Priority.high
    elif name.startswith("dashscope") or name.startswith("chrome-"):
        return "密钥配置", NodeType.config, "L0", Priority.medium
    elif name.startswith("agent-"):
        return "Agent协作", NodeType.knowledge, "L2", Priority.medium
    elif name.startswith("training-") or name.startswith("data-quality"):
        return "训练与模型", NodeType.knowledge, "L1", Priority.medium
    elif name.startswith("ecommerce-") or name.startswith("consumer-"):
        return "运营知识", NodeType.knowledge, "L2", Priority.high
    elif name.startswith("toolchain-"):
        return "工具链", NodeType.tool, "L1", Priority.medium
    elif name.startswith("user-"):
        return "反馈与规则", NodeType.feedback, "L0", Priority.low
    else:
        return "业务逻辑", NodeType.knowledge, "L1", Priority.medium


def parse_frontmatter(content: str) -> tuple[dict, str]:
    """解析YAML frontmatter。"""
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            fm = {}
            for line in parts[1].strip().split("\n"):
                if ":" in line:
                    k, v = line.split(":", 1)
                    fm[k.strip()] = v.strip()
            return fm, parts[2].strip()
    return {}, content


def extract_keywords(filename: str, content: str, frontmatter: dict) -> list[str]:
    """从文件内容提取激活关键词。"""
    keywords = set()

    # 从文件名
    name = filename.replace(".md", "").replace("-", " ").replace("_", " ")
    for part in name.split():
        if len(part) >= 2 and part.lower() not in ("the", "and", "for", "from"):
            keywords.add(part)

    # 从frontmatter description
    desc = frontmatter.get("description", "")
    for word in re.findall(r'[\u4e00-\u9fff]{2,4}|[a-zA-Z]{3,}', desc):
        keywords.add(word)

    # 从内容中提取高频中文词和英文术语
    cn_words = re.findall(r'[\u4e00-\u9fff]{2,4}', content[:500])
    from collections import Counter
    for word, cnt in Counter(cn_words).most_common(8):
        if cnt >= 2:
            keywords.add(word)

    en_words = re.findall(r'\b[A-Z][a-zA-Z]{2,}\b', content[:500])
    for word in en_words[:5]:
        keywords.add(word)

    return list(keywords)[:20]


def extract_references(content: str) -> list[str]:
    """提取文件中引用的路径。"""
    paths = re.findall(r'`([^`]+\.(?:py|json|md|html|yaml|yml|txt))`', content)
    return list(set(paths))[:10]


def generate_node_id(filename: str) -> str:
    """根据文件名生成稳定的节点ID。"""
    name = filename.replace(".md", "")
    # 取前缀
    parts = name.split("-")
    if parts[0] in ("feedback", "project", "session", "technical", "reference", "decisions"):
        prefix = parts[0][:3].upper()
        suffix = "-".join(parts[1:3])[:12]
    else:
        prefix = "MEM"
        suffix = name[:12]
    return f"{prefix}-{suffix}".replace(" ", "")


def build_edges(nodes: dict[str, Node]) -> list[Edge]:
    """基于内容分析自动推断边。"""
    edges = []
    node_list = list(nodes.values())

    for i, n1 in enumerate(node_list):
        text1 = n1.content.description + " " + n1.content.notes
        for j, n2 in enumerate(node_list):
            if i == j:
                continue
            # 如果n1的内容引用了n2的ID或名称
            if n2.id in text1 or n2.name in text1:
                edges.append(Edge(
                    source=n1.id,
                    target=n2.id,
                    type=EdgeType.informs,
                    weight=0.5,
                    description=f"{n1.name} references {n2.name}",
                ))

    # 同cluster内的节点互相关联
    clusters: dict[str, list[str]] = {}
    for n in node_list:
        clusters.setdefault(n.cluster, []).append(n.id)

    for cluster, nids in clusters.items():
        if len(nids) <= 1 or cluster == "会话记录":
            continue
        # 链式连接同cluster节点
        for k in range(len(nids) - 1):
            edges.append(Edge(
                source=nids[k],
                target=nids[k + 1],
                type=EdgeType.informs,
                weight=0.3,
                description=f"same cluster: {cluster}",
            ))

    # 反馈→所有其他类型节点的规则约束
    feedback_ids = [n.id for n in node_list if n.type == NodeType.feedback and n.priority in (Priority.high, Priority.critical)]
    architecture_ids = [n.id for n in node_list if n.cluster == "架构设计"]
    for fid in feedback_ids[:5]:
        for aid in architecture_ids:
            edges.append(Edge(
                source=fid,
                target=aid,
                type=EdgeType.validates,
                weight=0.4,
                description="feedback constrains architecture",
            ))

    return edges


def migrate() -> None:
    """执行迁移。"""
    if not MEMORY_DIR.exists():
        print(f"[ERROR] Memory目录不存在: {MEMORY_DIR}")
        return

    engine = GraphEngine()
    created = 0
    skipped = 0

    # 读取所有 .md 文件（排除 MEMORY.md 索引）
    md_files = sorted(MEMORY_DIR.glob("*.md"))
    md_files = [f for f in md_files if f.name != "MEMORY.md"]

    print(f"[INFO] 发现 {len(md_files)} 个memory文件")

    nodes: dict[str, Node] = {}

    for f in md_files:
        node_id = generate_node_id(f.name)

        # 跳过已存在的
        if engine.get_node(node_id):
            skipped += 1
            continue

        content = f.read_text(encoding="utf-8")
        frontmatter, body = parse_frontmatter(content)
        cluster, node_type, layer, priority = classify_memory_file(f.name)
        keywords = extract_keywords(f.name, body, frontmatter)
        key_files = extract_references(body)

        # 提取名称
        name = frontmatter.get("name", "")
        if not name:
            name = f.stem.replace("-", " ").replace("_", " ").title()

        # 提取描述
        description = frontmatter.get("description", "")
        if not description:
            # 取第一个非空行
            for line in body.split("\n"):
                line = line.strip().lstrip("#").strip()
                if line and len(line) > 5:
                    description = line[:200]
                    break

        req = NodeCreate(
            id=node_id,
            name=name,
            cluster=cluster,
            layer=layer,
            type=node_type,
            status=NodeStatus.active,
            content=NodeContent(
                description=description,
                key_files=key_files,
                notes=body[:500],  # 保留前500字作为notes
                last_session=frontmatter.get("type", ""),
            ),
            activation_keywords=keywords,
            priority=priority,
        )

        try:
            node = engine.create_node(req)
        except PermissionError as exc:
            skipped += 1
            print(f"  [SKIP] {node_id}: {exc}")
            continue
        nodes[node.id] = node
        created += 1
        print(f"  [+] {node.id}: {node.name} ({cluster})")

    # 自动推断边
    if created > 0:
        all_nodes = {**engine.nodes}  # 包含之前已有的
        edges = build_edges(all_nodes)
        for edge in edges:
            engine.create_edge(EdgeCreate(
                source=edge.source,
                target=edge.target,
                type=edge.type,
                weight=edge.weight,
                description=edge.description,
            ))
        print(f"\n[INFO] 自动生成 {len(edges)} 条边")

    print(f"\n[DONE] 迁移完成: {created} 新建, {skipped} 跳过, 总计 {len(engine.nodes)} 节点, {len(engine.edges)} 边")


if __name__ == "__main__":
    migrate()
