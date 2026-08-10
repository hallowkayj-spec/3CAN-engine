"""Auto-Bootstrap Pipeline v2 — 全量扫描仓库构建完整知识图谱。

3CAN v1.0核心基础设施: `3can init . --full-scan` 时全量扫描,
一次性建立项目的完整认知——代码接口+文档知识+Git历史+关系推断。

五阶段:
  Phase 1: 代码层 — AST扫描Python → 方法签名/类/常量 → INTF节点
  Phase 2: 文档层 — handoffs/memory/rules/docs → 项目知识节点
  Phase 3: Git层  — recent commits → 活动节点
  Phase 4: 关系推断 — import/引用/同session → 边
  Phase 5: 写入+Embedding

用法:
  python auto_bootstrap.py --code-dir /path/to/repo --full-scan [--write] [--verbose]
"""
from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

GRAPH_DIR = Path(__file__).resolve().parent.parent / "graph"
NODES_DIR = GRAPH_DIR / "nodes"


@dataclass
class FunctionSignature:
    """提取的函数签名。"""
    name: str
    file: str               # 相对路径
    class_name: str = ""    # 所属class (如果有)
    params: list[str] = field(default_factory=list)  # ["self", "merchant_id: str", "db_path: Path"]
    return_type: str = ""   # "str" / "dict" / "None"
    docstring: str = ""     # 第一行
    is_async: bool = False
    line_number: int = 0


@dataclass
class TableSchema:
    """提取的DB表schema。"""
    table_name: str
    file: str
    columns: list[dict] = field(default_factory=list)  # [{"name": "id", "type": "TEXT", "constraints": "PRIMARY KEY"}]
    line_number: int = 0


@dataclass
class APIEndpoint:
    """提取的API端点。"""
    method: str             # GET/POST/PUT/DELETE
    path: str               # "/api/loop/close"
    handler: str = ""       # "_handle_close_loop"
    file: str = ""
    line_number: int = 0


@dataclass
class DocNode:
    """提取的文档节点。"""
    file: str               # 相对路径
    title: str
    doc_type: str           # "handoff" | "memory" | "rule" | "spec" | "readme"
    agent: str = ""         # 推断的agent
    session_id: str = ""    # S59, S56b, ...
    status: str = ""        # active/archived/...
    summary: str = ""       # 前200字
    keywords: list[str] = field(default_factory=list)


@dataclass
class BootstrapReport:
    """建图报告。"""
    files_scanned: int = 0
    functions_found: int = 0
    tables_found: int = 0
    endpoints_found: int = 0
    docs_found: int = 0
    git_commits: int = 0
    edges_inferred: int = 0
    nodes_created: int = 0
    nodes_updated: int = 0
    errors: list[str] = field(default_factory=list)


# ═══════════════════════════════════════════════
# Stage 1: AST扫描
# ═══════════════════════════════════════════════

def scan_python_files(code_dir: Path, patterns: list[str] | None = None) -> list[FunctionSignature]:
    """扫描Python文件, 提取所有public函数签名。"""
    if patterns is None:
        patterns = ["tools/**/*.py", "scripts/**/*.py"]

    signatures: list[FunctionSignature] = []
    seen_files: set[str] = set()

    for pattern in patterns:
        for pyfile in code_dir.glob(pattern):
            relpath = str(pyfile.relative_to(code_dir)).replace("\\", "/")
            if relpath in seen_files:
                continue
            if any(skip in relpath for skip in ("__pycache__", "venv", ".git", "node_modules")):
                continue
            if pyfile.name == "__init__.py":
                continue
            seen_files.add(relpath)

            try:
                source = pyfile.read_text(encoding="utf-8", errors="replace")
                tree = ast.parse(source, filename=relpath)
            except (SyntaxError, UnicodeDecodeError):
                continue

            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    # 跳过私有方法 (除了 __init__)
                    if node.name.startswith("_") and node.name != "__init__":
                        continue

                    # 判断所属class
                    class_name = ""
                    for parent in ast.walk(tree):
                        if isinstance(parent, ast.ClassDef):
                            for child in ast.iter_child_nodes(parent):
                                if child is node:
                                    class_name = parent.name
                                    break

                    # 提取参数
                    params = []
                    for arg in node.args.args:
                        param_str = arg.arg
                        if arg.annotation:
                            try:
                                param_str += f": {ast.unparse(arg.annotation)}"
                            except Exception:
                                pass
                        params.append(param_str)

                    # 提取返回类型
                    return_type = ""
                    if node.returns:
                        try:
                            return_type = ast.unparse(node.returns)
                        except Exception:
                            pass

                    # 提取docstring
                    docstring = ast.get_docstring(node) or ""
                    if docstring:
                        docstring = docstring.split("\n")[0][:120]

                    signatures.append(FunctionSignature(
                        name=node.name,
                        file=relpath,
                        class_name=class_name,
                        params=params,
                        return_type=return_type,
                        docstring=docstring,
                        is_async=isinstance(node, ast.AsyncFunctionDef),
                        line_number=node.lineno,
                    ))

    return signatures


# ═══════════════════════════════════════════════
# Stage 2: SQL扫描
# ═══════════════════════════════════════════════

def scan_sql_schemas(code_dir: Path, patterns: list[str] | None = None) -> list[TableSchema]:
    """扫描Python文件中的CREATE TABLE语句, 提取表schema。"""
    if patterns is None:
        patterns = ["tools/**/*.py", "scripts/**/*.py"]

    tables: list[TableSchema] = []
    seen_tables: set[str] = set()

    # 正则: CREATE TABLE IF NOT EXISTS table_name (...)
    create_re = re.compile(
        r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s*\((.*?)\)',
        re.DOTALL | re.IGNORECASE,
    )
    # 列定义: column_name TYPE CONSTRAINTS
    col_re = re.compile(r'^\s*(\w+)\s+(TEXT|INTEGER|REAL|BLOB|NUMERIC|VARCHAR|BOOLEAN)(.*)$', re.IGNORECASE)

    for pattern in patterns:
        for pyfile in code_dir.glob(pattern):
            relpath = str(pyfile.relative_to(code_dir)).replace("\\", "/")
            if "__pycache__" in relpath or "venv" in relpath:
                continue

            try:
                source = pyfile.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            for match in create_re.finditer(source):
                table_name = match.group(1)
                if table_name in seen_tables:
                    continue
                if table_name.endswith("_fts"):  # skip FTS virtual tables
                    continue
                seen_tables.add(table_name)

                columns = []
                body = match.group(2)
                for line in body.split(","):
                    line = line.strip()
                    col_match = col_re.match(line)
                    if col_match:
                        columns.append({
                            "name": col_match.group(1),
                            "type": col_match.group(2).upper(),
                            "constraints": col_match.group(3).strip(),
                        })

                if columns:
                    # 计算行号
                    line_number = source[:match.start()].count("\n") + 1
                    tables.append(TableSchema(
                        table_name=table_name,
                        file=relpath,
                        columns=columns,
                        line_number=line_number,
                    ))

    return tables


# ═══════════════════════════════════════════════
# Stage 2b: API端点扫描
# ═══════════════════════════════════════════════

def scan_api_endpoints(code_dir: Path) -> list[APIEndpoint]:
    """扫描API端点定义 (支持 if path == 和 @app.get/post 模式)。"""
    endpoints: list[APIEndpoint] = []

    for pyfile in code_dir.glob("tools/**/*.py"):
        relpath = str(pyfile.relative_to(code_dir)).replace("\\", "/")
        if "__pycache__" in relpath:
            continue

        try:
            source = pyfile.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        # Pattern 1: if path == "/api/xxx"
        for i, line in enumerate(source.split("\n"), 1):
            m = re.search(r'if\s+path\s*==\s*["\'](/api/[^"\']+)["\']', line)
            if m:
                endpoints.append(APIEndpoint(
                    method="POST",  # 大多数在do_POST里
                    path=m.group(1),
                    file=relpath,
                    line_number=i,
                ))

        # Pattern 2: @app.get("/api/xxx") / @app.post("/api/xxx")
        for i, line in enumerate(source.split("\n"), 1):
            m = re.search(r'@app\.(get|post|put|delete)\(["\'](/api/[^"\']+)["\']', line)
            if m:
                endpoints.append(APIEndpoint(
                    method=m.group(1).upper(),
                    path=m.group(2),
                    file=relpath,
                    line_number=i,
                ))

    return endpoints


# ═══════════════════════════════════════════════
# Phase 2: 文档层全量扫描
# ═══════════════════════════════════════════════

# 排除的目录（训练数据/模型输出/旧数据等）
_SKIP_DIRS = {
    "__pycache__", "venv", ".git", "node_modules", "archive", "old_data",
    "seed", "unified_dataset", "images", "comfyui", "sdxl_lora", "LLaVA",
    "flux_train", "model_output", "faiss_index", "_backups", "runninghub",
    "AiToEarn", "claude-code-source", "dist-info", "site-packages",
    "kohya_ss", "bitsandbytes", "gradio", "sentence_transformers",
    "licenses", "templates", "_quarantine", "cross_encoder", "sparse_encoder",
    "httpcore", "gradio_client", ".egg-info",
}


def _infer_doc_type(filepath: Path, relpath: str) -> str:
    """推断文档类型。"""
    if "handoff" in relpath.lower():
        return "handoff"
    if "memory" in relpath.lower() or relpath.startswith("memory/"):
        return "memory"
    if "rules" in relpath.lower() or ".claude/rules" in relpath:
        return "rule"
    if filepath.name in ("CLAUDE.md", "AGENTS.md", "README.md"):
        return "spec"
    if "spec" in relpath.lower() or "docs" in relpath.lower():
        return "spec"
    return "doc"


def _infer_agent_from_doc(filename: str, content: str) -> str:
    """从文档内容推断关联的agent。"""
    name_lower = filename.lower()
    content_lower = content[:500].lower()
    if "codex" in name_lower or "codex" in content_lower:
        return "codex-cli"
    if "opus" in name_lower and ("video" in name_lower or "视频" in content_lower):
        return "opus2-video"
    if "opus" in name_lower and ("3can" in name_lower or "neural" in name_lower):
        return "opus3"
    if "sonnet" in name_lower:
        return "sonnet"
    if "opus" in name_lower or "opus" in content_lower:
        return "opus-main"
    return ""


def _extract_session_id_from_doc(filename: str) -> str:
    """从文件名提取session号。"""
    m = re.search(r"S(\d+[a-d]?)", filename, re.IGNORECASE)
    return f"S{m.group(1)}" if m else ""


def scan_documents(
    code_dir: Path,
    memory_dir: Path | None = None,
) -> list[DocNode]:
    """Phase 2: 扫描所有文档文件。"""
    docs: list[DocNode] = []
    seen: set[str] = set()

    # 扫描仓库内的docs
    scan_dirs = [
        (code_dir / "docs", "docs"),
        (code_dir / ".claude" / "rules", ".claude/rules"),
        (code_dir / "frontend", "frontend"),
    ]
    # 仓库根目录的md文件
    for f in code_dir.glob("*.md"):
        scan_dirs.append((f.parent, ""))

    if memory_dir and memory_dir.exists():
        scan_dirs.append((memory_dir, "memory"))

    for scan_path, prefix in scan_dirs:
        if not scan_path.exists():
            continue

        files = [scan_path] if scan_path.is_file() else scan_path.rglob("*.md")

        for f in files:
            if not f.is_file():
                continue
            relpath = str(f.relative_to(f.parent.parent if not prefix else scan_path.parent)).replace("\\", "/")
            if relpath in seen:
                continue
            if any(skip in relpath for skip in _SKIP_DIRS):
                continue
            seen.add(relpath)

            try:
                content = f.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            # 提取标题
            title = ""
            for line in content.split("\n")[:10]:
                if line.startswith("# "):
                    title = line[2:].strip()[:100]
                    break
            if not title:
                title = f.stem.replace("-", " ").replace("_", " ")[:80]

            # frontmatter
            status = ""
            if content.startswith("---"):
                end = content.find("---", 3)
                if end > 0:
                    for fmline in content[3:end].split("\n"):
                        if "status" in fmline.lower() and ":" in fmline:
                            status = fmline.split(":", 1)[-1].strip()

            # 提取关键词
            keywords = re.findall(r"S\d+[a-d]?", f.name)
            words = re.findall(r"[A-Z][a-z]+|[a-z]{4,}", title)
            keywords.extend(w for w in words[:5] if len(w) > 3)

            doc_type = _infer_doc_type(f, relpath)
            agent = _infer_agent_from_doc(f.name, content[:500])
            session_id = _extract_session_id_from_doc(f.name)

            docs.append(DocNode(
                file=relpath,
                title=title,
                doc_type=doc_type,
                agent=agent,
                session_id=session_id,
                status=status,
                summary=content[:300].replace("\n", " "),
                keywords=keywords[:10],
            ))

    return docs


def _build_doc_node(doc: DocNode) -> dict:
    """构建一个文档知识节点。"""
    # 根据类型决定cluster和prefix
    type_map = {
        "handoff": ("项目交接", "HO"),
        "memory": ("会话记录", "MEM"),
        "rule": ("反馈与规则", "RUL"),
        "spec": ("项目文档", "DOC"),
        "doc": ("项目文档", "DOC"),
    }
    cluster, prefix = type_map.get(doc.doc_type, ("项目文档", "DOC"))

    # 生成稳定ID
    stem = doc.file.split("/")[-1].replace(".md", "").replace("_", "-")[:30]
    node_id = f"{prefix}-{stem}"

    # 优先级
    priority = "medium"
    if doc.doc_type == "rule":
        priority = "high"
    if doc.doc_type == "handoff" and doc.agent:
        priority = "high"

    keywords = list(doc.keywords)
    if doc.agent:
        keywords.append(doc.agent)
    if doc.session_id:
        keywords.append(doc.session_id)
    keywords.append(doc.doc_type)

    return {
        "id": node_id,
        "name": doc.title[:60],
        "cluster": cluster,
        "layer": "L2" if doc.doc_type in ("handoff", "memory") else "L1",
        "type": "knowledge" if doc.doc_type == "spec" else "session" if doc.doc_type in ("handoff", "memory") else "feedback",
        "status": "active",
        "content": {
            "description": f"{doc.doc_type}: {doc.title}",
            "current_state": doc.status or "",
            "tech_stack": [],
            "key_files": [doc.file],
            "decisions": [],
            "api_refs": [],
            "tools": [],
            "blockers": [],
            "last_session": doc.session_id or "auto-bootstrap",
            "notes": doc.summary,
            "extra": {
                "doc_type": doc.doc_type,
                "agent": doc.agent,
                "session_id": doc.session_id,
                "source_file": doc.file,
            },
        },
        "activation_keywords": keywords[:15],
        "priority": priority,
        "activation_count": 0,
        "updated_by": "auto-bootstrap",
    }


# ═══════════════════════════════════════════════
# Phase 3: Git层
# ═══════════════════════════════════════════════

def scan_git_log(code_dir: Path, max_commits: int = 50) -> list[dict]:
    """提取最近的git commit。"""
    import subprocess
    try:
        result = subprocess.run(
            ["git", "log", f"--max-count={max_commits}",
             "--format=%H|%ai|%an|%s"],
            capture_output=True, text=True, cwd=str(code_dir),
            timeout=10, encoding="utf-8", errors="replace",
        )
        commits = []
        for line in result.stdout.strip().split("\n"):
            parts = line.split("|", 3)
            if len(parts) == 4:
                commits.append({
                    "hash": parts[0][:8],
                    "date": parts[1].strip()[:10],
                    "author": parts[2].strip(),
                    "message": parts[3].strip(),
                })
        return commits
    except Exception:
        return []


# ═══════════════════════════════════════════════
# Phase 4: 关系推断
# ═══════════════════════════════════════════════

def infer_edges(all_nodes: list[dict]) -> list[dict]:
    """从节点内容推断关系边。"""
    edges = []
    # 同session的节点 → informs边
    session_groups: dict[str, list[str]] = {}
    for n in all_nodes:
        sid = n["content"].get("extra", {}).get("session_id", "")
        if sid:
            session_groups.setdefault(sid, []).append(n["id"])

    for sid, nids in session_groups.items():
        if len(nids) < 2:
            continue
        # 同session的节点两两连接 (限制最多5条)
        for i, a in enumerate(nids[:5]):
            for b in nids[i+1:6]:
                edges.append({
                    "source": a, "target": b,
                    "type": "informs", "weight": 0.3,
                    "description": f"same session: {sid}",
                })

    # 同agent的节点 → updates边
    agent_groups: dict[str, list[str]] = {}
    for n in all_nodes:
        agent = n["content"].get("extra", {}).get("agent", "")
        if agent:
            agent_groups.setdefault(agent, []).append(n["id"])

    for agent, nids in agent_groups.items():
        if len(nids) < 2:
            continue
        # 时间序列连接 (前→后 = updates)
        for i in range(min(len(nids) - 1, 10)):
            edges.append({
                "source": nids[i], "target": nids[i+1],
                "type": "updates", "weight": 0.2,
                "description": f"agent {agent} sequence",
            })

    return edges


# ═══════════════════════════════════════════════
# Stage 3: 写入3CAN图
# ═══════════════════════════════════════════════

def _build_intf_node(file: str, class_name: str, functions: list[FunctionSignature]) -> dict:
    """构建一个INTF节点的JSON。"""
    # 生成节点ID
    base = file.replace("/", "-").replace(".py", "").replace("_", "-")
    if class_name:
        base = f"{base}-{class_name}"
    node_id = f"INTF-{base[:30]}"

    # 构建方法列表文本
    methods_text = []
    for fn in functions[:20]:  # 限制20个方法
        sig = f"{fn.name}({', '.join(fn.params[:5])})"
        if fn.return_type:
            sig += f" -> {fn.return_type}"
        methods_text.append(sig)

    # 提取关键词
    keywords = [fn.name for fn in functions[:10]]
    if class_name:
        keywords.insert(0, class_name)
    keywords.extend(["INTF", file.split("/")[-1].replace(".py", "")])

    return {
        "id": node_id,
        "name": f"INTF: {class_name or file.split('/')[-1]}",
        "cluster": "接口契约",
        "layer": "L1",
        "type": "knowledge",
        "status": "active",
        "content": {
            "description": f"Auto-extracted interface from {file}" + (f" class {class_name}" if class_name else ""),
            "current_state": f"{len(functions)} public methods",
            "tech_stack": list({fn.file.split("/")[0] for fn in functions}),
            "key_files": [file],
            "decisions": [],
            "api_refs": [],
            "tools": [],
            "blockers": [],
            "last_session": "auto-bootstrap",
            "notes": "\n".join(methods_text),
            "extra": {
                "methods": [
                    {
                        "name": fn.name,
                        "params": fn.params,
                        "return_type": fn.return_type,
                        "docstring": fn.docstring,
                        "line": fn.line_number,
                    }
                    for fn in functions[:20]
                ],
                "source_file": file,
                "class_name": class_name,
            },
        },
        "activation_keywords": keywords[:15],
        "priority": "medium",
        "activation_count": 0,
        "updated_by": "auto-bootstrap",
    }


def _build_table_node(table: TableSchema) -> dict:
    """构建一个DB表schema节点。"""
    node_id = f"INTF-db-{table.table_name[:25]}"

    col_text = "\n".join(
        f"  {c['name']:25} {c['type']:10} {c['constraints']}"
        for c in table.columns
    )

    keywords = [table.table_name, "database", "schema", "SQL"]
    keywords.extend(c["name"] for c in table.columns[:8])

    return {
        "id": node_id,
        "name": f"DB: {table.table_name}",
        "cluster": "接口契约",
        "layer": "L1",
        "type": "knowledge",
        "status": "active",
        "content": {
            "description": f"Database table schema: {table.table_name} ({len(table.columns)} columns)",
            "current_state": f"{len(table.columns)} columns",
            "tech_stack": ["SQLite"],
            "key_files": [table.file],
            "decisions": [],
            "api_refs": [],
            "tools": [],
            "blockers": [],
            "last_session": "auto-bootstrap",
            "notes": f"CREATE TABLE {table.table_name}\n{col_text}",
            "extra": {
                "table_name": table.table_name,
                "columns": table.columns,
                "source_file": table.file,
                "line_number": table.line_number,
            },
        },
        "activation_keywords": keywords[:15],
        "priority": "medium",
        "activation_count": 0,
        "updated_by": "auto-bootstrap",
    }


def write_nodes(nodes: list[dict], dry_run: bool = True) -> int:
    """写入节点到graph/nodes/目录。"""
    if dry_run:
        return len(nodes)

    NODES_DIR.mkdir(parents=True, exist_ok=True)
    written = 0
    for node in nodes:
        node_id = node["id"]
        path = NODES_DIR / f"{node_id}.json"

        # 添加时间戳
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        node["created_at"] = node.get("created_at", now)
        node["updated_at"] = now

        path.write_text(json.dumps(node, ensure_ascii=False, indent=2), encoding="utf-8")
        written += 1

    return written


# ═══════════════════════════════════════════════
# Main Pipeline
# ═══════════════════════════════════════════════

def bootstrap(
    code_dir: Path,
    memory_dir: Path | None = None,
    patterns: list[str] | None = None,
    dry_run: bool = True,
    verbose: bool = False,
    min_methods: int = 2,
    full_scan: bool = False,
) -> BootstrapReport:
    """执行Auto-Bootstrap Pipeline。

    Args:
        code_dir: 代码仓库根目录
        memory_dir: memory文件目录 (可选)
        patterns: glob模式列表
        dry_run: True只报告不写入
        verbose: 详细输出
        min_methods: 一个文件至少N个public方法才建INTF节点
        full_scan: True则执行全部5个Phase (否则只Phase 1)
    """
    report = BootstrapReport()

    print(f"[Bootstrap v2] {'FULL SCAN' if full_scan else 'Code-only'} — {code_dir}")

    # ══ Phase 1: 代码层 ══
    print(f"\n{'─'*50}")
    print("[Phase 1] Code Layer — AST + SQL + API")
    print(f"{'─'*50}")

    signatures = scan_python_files(code_dir, patterns)
    report.functions_found = len(signatures)
    report.files_scanned = len({s.file for s in signatures})
    print(f"  AST: {len(signatures)} public functions in {report.files_scanned} files")

    groups: dict[str, list[FunctionSignature]] = {}
    for sig in signatures:
        key = f"{sig.file}::{sig.class_name}" if sig.class_name else sig.file
        groups.setdefault(key, []).append(sig)

    intf_nodes = []
    for key, funcs in groups.items():
        if len(funcs) < min_methods:
            continue
        node = _build_intf_node(funcs[0].file, funcs[0].class_name, funcs)
        intf_nodes.append(node)
    print(f"  INTF nodes: {len(intf_nodes)}")

    tables = scan_sql_schemas(code_dir, patterns)
    report.tables_found = len(tables)
    table_nodes = [_build_table_node(t) for t in tables]
    print(f"  SQL tables: {len(tables)}")

    endpoints = scan_api_endpoints(code_dir)
    report.endpoints_found = len(endpoints)
    print(f"  API endpoints: {len(endpoints)}")

    # ══ Phase 2: 文档层 (full_scan only) ══
    doc_nodes = []
    if full_scan:
        print(f"\n{'─'*50}")
        print("[Phase 2] Document Layer — handoffs + memory + rules + specs")
        print(f"{'─'*50}")

        docs = scan_documents(code_dir, memory_dir)
        report.docs_found = len(docs)
        print(f"  Documents found: {len(docs)}")

        # 按类型统计
        type_counts: dict[str, int] = {}
        agent_counts: dict[str, int] = {}
        for d in docs:
            type_counts[d.doc_type] = type_counts.get(d.doc_type, 0) + 1
            if d.agent:
                agent_counts[d.agent] = agent_counts.get(d.agent, 0) + 1

        for dt, cnt in sorted(type_counts.items(), key=lambda x: -x[1]):
            print(f"    {dt:12} {cnt:4}")
        if agent_counts:
            print("  Agent attribution:")
            for ag, cnt in sorted(agent_counts.items(), key=lambda x: -x[1]):
                print(f"    {ag:15} {cnt:4} docs")

        # 分层: 高价值文档(handoff/memory/rule)全量建节点
        # 低价值文档(spec/doc)只选有session_id或agent标注的
        high_value = [d for d in docs if d.doc_type in ("handoff", "memory", "rule")]
        low_value = [d for d in docs if d.doc_type in ("spec", "doc") and (d.session_id or d.agent)]

        filtered_docs = high_value + low_value
        print(f"  After filtering: {len(filtered_docs)} nodes (high={len(high_value)}, low={len(low_value)}, dropped={len(docs)-len(filtered_docs)})")

        doc_nodes = [_build_doc_node(d) for d in filtered_docs]

        if verbose:
            for d in docs[:20]:
                agent_tag = f" [{d.agent}]" if d.agent else ""
                print(f"  {d.session_id or 'n/a':6} | {d.doc_type:8} | {d.title[:45]}{agent_tag}")

    # ══ Phase 3: Git层 (full_scan only) ══
    git_nodes = []
    if full_scan:
        print(f"\n{'─'*50}")
        print("[Phase 3] Git Layer — recent commits")
        print(f"{'─'*50}")

        commits = scan_git_log(code_dir, max_commits=30)
        report.git_commits = len(commits)
        print(f"  Commits: {len(commits)}")

        if commits:
            # 创建一个汇总节点
            commit_summary = "\n".join(
                f"{c['hash']} [{c['date']}] {c['message'][:60]}"
                for c in commits[:20]
            )
            git_nodes.append({
                "id": "GIT-recent-commits",
                "name": f"Recent {len(commits)} commits",
                "cluster": "项目活动",
                "layer": "L2",
                "type": "session",
                "status": "active",
                "content": {
                    "description": f"Last {len(commits)} git commits",
                    "current_state": f"Latest: {commits[0]['message'][:60]}" if commits else "",
                    "tech_stack": [], "key_files": [], "decisions": [],
                    "api_refs": [], "tools": [], "blockers": [],
                    "last_session": "auto-bootstrap",
                    "notes": commit_summary,
                    "extra": {"commits": commits[:20]},
                },
                "activation_keywords": ["git", "commit", "recent", "history"] + [c["hash"] for c in commits[:5]],
                "priority": "low",
                "activation_count": 0,
                "updated_by": "auto-bootstrap",
            })

    # ══ Phase 4: 关系推断 (full_scan only) ══
    all_new_nodes = intf_nodes + table_nodes + doc_nodes + git_nodes
    edge_list = []
    if full_scan:
        print(f"\n{'─'*50}")
        print("[Phase 4] Relation Inference")
        print(f"{'─'*50}")

        edge_list = infer_edges(all_new_nodes)
        report.edges_inferred = len(edge_list)
        print(f"  Edges inferred: {len(edge_list)}")
    else:
        all_new_nodes = intf_nodes + table_nodes

    # ══ Phase 5: 写入 ══
    print(f"\n{'─'*50}")
    print("[Phase 5] Write to graph")
    print(f"{'─'*50}")

    if dry_run:
        report.nodes_created = len(all_new_nodes)
        print(f"  DRY RUN: would create {len(all_new_nodes)} nodes + {len(edge_list)} edges")
    else:
        written = write_nodes(all_new_nodes, dry_run=False)
        report.nodes_created = written
        print(f"  WRITTEN: {written} nodes to {NODES_DIR}")

        # 写入推断的边
        if edge_list:
            edges_file = GRAPH_DIR / "edges.json"
            existing_edges = []
            if edges_file.exists():
                try:
                    existing_edges = json.loads(edges_file.read_text(encoding="utf-8"))
                except Exception:
                    pass
            # 合并去重
            existing_pairs = {(e["source"], e["target"]) for e in existing_edges}
            new_edges = [e for e in edge_list if (e["source"], e["target"]) not in existing_pairs]
            all_edges = existing_edges + new_edges
            edges_file.write_text(json.dumps(all_edges, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"  EDGES: {len(new_edges)} new (total {len(all_edges)})")

    # ══ Summary ══
    print(f"\n{'='*60}")
    print(f"[Bootstrap v2] Complete — {'FULL SCAN' if full_scan else 'Code-only'}")
    print(f"  Phase 1 (Code):  {len(intf_nodes)} INTF + {len(table_nodes)} DB + {report.endpoints_found} API")
    print(f"  Phase 2 (Docs):  {len(doc_nodes)} document nodes")
    print(f"  Phase 3 (Git):   {len(git_nodes)} git nodes ({report.git_commits} commits)")
    print(f"  Phase 4 (Edges): {report.edges_inferred} inferred")
    print("  ─────────────────────────────")
    print(f"  TOTAL NODES:     {len(all_new_nodes)}")
    print(f"{'='*60}")

    return report


# ── CLI ──

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="3CAN Auto-Bootstrap v2: full repo scan → knowledge graph")
    parser.add_argument("--code-dir", type=str, required=True, help="Path to code repository")
    parser.add_argument("--memory-dir", type=str, default=None, help="Path to memory/ directory")
    parser.add_argument("--full-scan", action="store_true", help="Full scan: code + docs + git + edges")
    parser.add_argument("--write", action="store_true", help="Actually write nodes (default: dry-run)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    parser.add_argument("--min-methods", type=int, default=2, help="Min public methods per file/class")
    parser.add_argument("--patterns", type=str, nargs="*", default=None, help="Glob patterns")
    args = parser.parse_args()

    mem_dir = Path(args.memory_dir) if args.memory_dir else None

    bootstrap(
        code_dir=Path(args.code_dir),
        memory_dir=mem_dir,
        patterns=args.patterns,
        dry_run=not args.write,
        verbose=args.verbose,
        min_methods=args.min_methods,
        full_scan=args.full_scan,
    )
