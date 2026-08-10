"""3CAN Project Bootstrapper — 基座#27 冷启动种子生成器

新用户部署 3CAN 后运行此工具, 扫用户项目 → 抽初始 20-50 节点种子.
让 "下载 + 部署" 到 "第一天开始真实可用" 的距离 ≤ 30 分钟.

扫描来源:
- README.md / *.md (项目说明 → DOC-*)
- git log --oneline (最近 30 次提交 → SES-auto-*)
- package.json / pyproject.toml / requirements.txt (技术栈 → tool-*)
- scripts/*.py / tools/*.py (工具接口 → INTF-*)
- 目录结构 (主要模块 → MOD-*)

运行:
  python tools/project_bootstrapper.py --project /path/to/project --dry-run
  python tools/project_bootstrapper.py --project /path/to/project --apply
  python tools/project_bootstrapper.py --project /path/to/project --with-llm  # 用 LLM 生成 description, 不用 LLM 就用 Heuristic

the maintainer 原则: 不自作主张, 用户可审阅 dry-run → 决定是否 apply.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import requests

THREE_CAN = os.environ.get("THREECAN_BASE_URL", "http://localhost:9700")
MAX_SEEDS = 50


def ensure_windows_stdio() -> None:
    if os.name != "nt":
        return
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "buffer"):
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")


def slugify(s: str, n: int = 30) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s[:n] or "x"


def read_first_title(fp: Path, max_chars: int = 200) -> tuple[str, str]:
    """读 markdown 文件, 返 (title, head_snippet)."""
    try:
        txt = fp.read_text(encoding="utf-8", errors="ignore")
        lines = [line for line in txt.split("\n") if line.strip()]
        title = ""
        for line in lines[:5]:
            if line.startswith("#"):
                title = line.lstrip("\ufeff").lstrip("#").strip()
                break
        if not title and lines:
            title = lines[0].lstrip("\ufeff")[:60].strip()
        head = txt[:max_chars]
        return title, head
    except Exception:
        return "", ""


def scan_markdown(project: Path) -> list[dict]:
    """扫 README 和 docs/ 下 md 文件 → DOC 种子."""
    out = []
    candidates = []
    # README 优先
    for name in ["README.md", "README", "readme.md"]:
        p = project / name
        if p.exists():
            candidates.append(p)
            break
    # docs/
    for sub in ["docs", "doc", "specs"]:
        d = project / sub
        if d.exists():
            for p in d.rglob("*.md"):
                if "node_modules" in str(p) or "venv" in str(p):
                    continue
                candidates.append(p)
                if len(candidates) >= 20:
                    break
    # 独立 md
    for p in project.glob("*.md"):
        if p not in candidates:
            candidates.append(p)

    for p in candidates[:15]:
        title, head = read_first_title(p)
        if not title:
            continue
        rel = str(p.relative_to(project)).replace("\\", "/")
        slug = slugify(p.stem)
        out.append({
            "id": f"DOC-seed-{slug}",
            "name": f"[seed] {title[:60]}",
            "cluster": "项目文档",
            "type": "reference",
            "content": {
                "description": f"项目文档: {rel}. 标题: {title[:80]}",
                "notes": head[:500],
                "key_files": [rel],
            },
            "activation_keywords": [slug, "seed", "doc", p.stem, "readme" if "readme" in p.name.lower() else "docs"],
        })
    return out


def scan_git_log(project: Path, n: int = 30) -> list[dict]:
    """git log 最近 N 次提交 → SES-seed-* 节点."""
    try:
        r = subprocess.run(
            ["git", "-C", str(project), "log", "--oneline", "-n", str(n), "--no-merges"],
            capture_output=True, text=True, timeout=10
        )
        if r.returncode != 0:
            return []
        lines = r.stdout.strip().split("\n")
    except Exception:
        return []

    out = []
    for line in lines[:10]:  # 只要最近 10 次
        parts = line.split(maxsplit=1)
        if len(parts) < 2:
            continue
        sha, msg = parts
        slug = slugify(msg, 40)
        out.append({
            "id": f"SES-seed-git-{sha[:8]}",
            "name": f"[seed] git {sha[:8]}: {msg[:50]}",
            "cluster": "会话记录",
            "type": "session",
            "content": {
                "description": f"git 提交种子: {msg[:150]}",
                "notes": f"sha={sha}\nmessage={msg}",
            },
            "activation_keywords": [slug, "seed", "git-commit", sha[:8]],
        })
    return out


def scan_tech_stack(project: Path) -> list[dict]:
    """package.json / pyproject.toml / requirements.txt → tool 种子."""
    out = []
    # pyproject
    pyp = project / "pyproject.toml"
    req = project / "requirements.txt"
    pkg = project / "package.json"

    if pyp.exists() or req.exists():
        deps = []
        if req.exists():
            try:
                for line in req.read_text(
                    encoding="utf-8",
                    errors="ignore",
                ).split("\n"):
                    line = line.strip()
                    if line and not line.startswith("#"):
                        deps.append(
                            line.split("=")[0].split(">")[0].split("<")[0].strip()
                        )
            except Exception:
                pass
        out.append({
            "id": "MOD-seed-python-stack",
            "name": "[seed] Python 技术栈",
            "cluster": "工具链",
            "type": "tool",
            "content": {
                "description": f"Python 项目依赖. {len(deps)} 个包",
                "notes": "\n".join(deps[:40]),
                "tech_stack": deps[:20],
            },
            "activation_keywords": ["python stack", "seed", "dependencies", "pip", *deps[:5]],
        })

    if pkg.exists():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8", errors="ignore"))
            deps = list((data.get("dependencies") or {}).keys()) + list((data.get("devDependencies") or {}).keys())
            out.append({
                "id": "MOD-seed-node-stack",
                "name": f"[seed] Node.js {data.get('name','project')}",
                "cluster": "工具链",
                "type": "tool",
                "content": {
                    "description": f"Node.js 项目 {data.get('name','')} v{data.get('version','')}. {len(deps)} 依赖",
                    "notes": json.dumps(deps[:30], indent=2),
                    "tech_stack": deps[:20],
                },
                "activation_keywords": ["node stack", "seed", "npm", "package.json", *deps[:5]],
            })
        except Exception:
            pass
    return out


def scan_modules(project: Path) -> list[dict]:
    """项目下主要目录 → MOD 种子."""
    out = []
    skip = {"node_modules", "venv", "__pycache__", ".git", "dist", "build", ".pytest_cache",
            ".ruff_cache", "cache", "archive", "_refs", ".claude"}
    for d in project.iterdir():
        if not d.is_dir() or d.name.startswith(".") or d.name in skip:
            continue
        # 统计文件数
        try:
            n_files = sum(1 for _ in d.rglob("*") if _.is_file())
        except Exception:
            n_files = 0
        if n_files < 3:
            continue
        slug = slugify(d.name, 30)
        out.append({
            "id": f"MOD-seed-{slug}",
            "name": f"[seed] 模块 {d.name} ({n_files} 文件)",
            "cluster": "项目模块",
            "type": "knowledge",
            "content": {
                "description": f"项目主目录 {d.name}, 含 {n_files} 文件",
                "key_files": [str(d.name)],
            },
            "activation_keywords": [slug, "seed", "module", d.name],
        })
    return out[:10]


def generate_seeds(project: Path) -> list[dict]:
    all_seeds = []
    all_seeds += scan_markdown(project)
    all_seeds += scan_git_log(project)
    all_seeds += scan_tech_stack(project)
    all_seeds += scan_modules(project)
    # 去重 (按 id)
    seen = set()
    unique = []
    for s in all_seeds:
        if s["id"] in seen:
            continue
        seen.add(s["id"])
        unique.append(s)
    return unique[:MAX_SEEDS]


def apply_seeds(seeds: list[dict], primary_author: str = "project-bootstrapper") -> dict:
    stats = {"created": 0, "skipped": 0, "errors": []}
    for s in seeds:
        s["primary_author"] = primary_author
        s.setdefault("status", "active")
        s.setdefault("priority", "medium")
        # R1 查重
        try:
            g = requests.get(f"{THREE_CAN}/api/nodes/{s['id']}", timeout=5)
            if g.status_code == 200:
                stats["skipped"] += 1
                continue
        except Exception:
            pass
        try:
            r = requests.post(f"{THREE_CAN}/api/nodes?force=true", json=s, timeout=15)
            if r.status_code in (200, 201):
                stats["created"] += 1
            else:
                stats["errors"].append(f"{s['id']}: http {r.status_code}")
        except Exception as e:
            stats["errors"].append(f"{s['id']}: {str(e)[:80]}")
    return stats


def main() -> int:
    global THREE_CAN
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True, help="项目根路径")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--with-llm", action="store_true", help="用 LLM 生成 description (暂未实现, 降级 Heuristic)")
    ap.add_argument("--author", default="project-bootstrapper")
    ap.add_argument("--base-url", default=THREE_CAN, help="3CAN base URL; defaults to THREECAN_BASE_URL or localhost:9700")
    args = ap.parse_args()
    THREE_CAN = args.base_url.rstrip("/")

    project = Path(args.project).resolve()
    if not project.exists():
        print(f"[FAIL] 项目路径不存在: {project}")
        return 1

    print(f"[bootstrap] 扫描 {project}...")
    seeds = generate_seeds(project)
    print(f"[bootstrap] 生成 {len(seeds)} 个初始种子节点:")
    for s in seeds[:10]:
        print(f"  {s['id']:50s}  {s['name'][:60]}")
    if len(seeds) > 10:
        print(f"  ... (共 {len(seeds)})")

    if args.dry_run or (not args.apply):
        print("\n[bootstrap] dry-run. 用 --apply 真实入库.")
        return 0

    if args.with_llm:
        print("[bootstrap] --with-llm 尚未实现, 用 Heuristic description")

    print("\n[bootstrap] 入库...")
    stats = apply_seeds(seeds, args.author)
    print(f"[bootstrap] done: created={stats['created']} skipped={stats['skipped']} errors={len(stats['errors'])}")
    if stats["errors"]:
        for e in stats["errors"][:5]:
            print(f"  error: {e}")
    print("\n[bootstrap] 建议下一步:")
    print("  1. python tools/skill_sync.py              # 同步 SKILL.md")
    print("  2. python tools/node_gdi_scorer.py          # 打分评估")
    print("  3. python tools/leiden_community.py         # 聚类")
    print("  4. python tools/bootstrap_check.py          # 最终体检")
    return 0


if __name__ == "__main__":
    ensure_windows_stdio()
    sys.exit(main())
