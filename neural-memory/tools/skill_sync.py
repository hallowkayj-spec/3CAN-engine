"""3CAN Skill Sync — Claude Code SKILL.md ↔ 3CAN 节点双向同步

目标: 让 SKILL.md (程序性记忆) 成为 3CAN 一等公民节点, 支持:
- 冷启: 新增 SKILL.md → 扫描 → 入 3CAN 节点 (type=skill)
- 路由: `route(task, kind="skill")` 返回可执行技能
- 使用日志: 调用成功/失败 → activity_log, 统计 success_rate

扫描路径 (并集):
- ~/.claude/skills/*/SKILL.md         (用户级)
- 项目根 .claude/skills/*/SKILL.md   (项目级)
- ~/.claude/plugins/*/skills/*/SKILL.md (插件级, 带 plugin: 前缀)

节点映射:
  SKILL.md YAML frontmatter {name, description, allowed-tools?}
  → 3CAN Node:
      id: SKILL-{name-slug}
      type: skill
      cluster: "skills"
      name: "{name}: {description 首 40 字}"
      content.description: YAML description (L2, skeleton 可见)
      content.notes: SKILL.md 正文 (L3, retrieve 才返)
      content.extra: {
        skill_source: user|project|plugin
        skill_path: 完整路径
        allowed_tools: [...]
        success_count: 0  (首建)
        fail_count: 0
        avg_duration_s: null
      }
      activation_keywords: name + 从 description 提取触发词
      primary_author: "skill-sync"

运行:
  python tools/skill_sync.py --dry-run          # 列出
  python tools/skill_sync.py                     # 真实同步
  python tools/skill_sync.py --project-only     # 只同步项目级
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import requests

THREE_CAN = "http://localhost:9700"


def find_skills() -> list[dict]:
    """返回列表, 每项 {source, path, priority}."""
    home = Path.home() / ".claude" / "skills"
    project = Path.cwd() / ".claude" / "skills"
    plugins = Path.home() / ".claude" / "plugins"

    out = []
    # 用户级
    if home.exists():
        for p in home.glob("*/SKILL.md"):
            out.append({"source": "user", "path": p, "priority": "medium"})
    # 项目级
    if project.exists():
        for p in project.glob("*/SKILL.md"):
            out.append({"source": "project", "path": p, "priority": "high"})
    # 插件级
    if plugins.exists():
        for p in plugins.glob("*/skills/*/SKILL.md"):
            out.append({"source": "plugin", "path": p, "priority": "medium"})
    return out


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """简单 YAML frontmatter 解析. 返回 (meta, body)."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    raw_meta = parts[1].strip()
    body = parts[2].strip()
    meta: dict[str, str | list[str]] = {}
    for line in raw_meta.splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k, v = k.strip(), v.strip()
        if v.startswith("[") and v.endswith("]"):
            # 简单 list
            items = [x.strip().strip('"').strip("'") for x in v[1:-1].split(",") if x.strip()]
            meta[k] = items
        else:
            v = v.strip('"').strip("'")
            meta[k] = v
    return meta, body


KEYWORD_TRIGGER_PATTERNS = [
    r"trigger phrases? include[s]? [\"\'\(]?([^\"\'\)\.]+)",
    r'use when [^\.]+',
    r'triggered? by [^\.]+',
]


def extract_keywords(name: str, description: str) -> list[str]:
    """从 SKILL.md frontmatter 提 8-12 keywords."""
    kws: list[str] = [name]
    # 分词 description (英文 + 中文)
    text = description.lower()
    # 删掉常见动词短语 stopwords
    stop = set("use when user users ask asks request requests that the a an is are for to with and or of in on "
               "at by from this these those it its their we us any all some".split())
    toks = re.findall(r"[a-z]{4,}|[\u4e00-\u9fa5]{2,}", text)
    freq: dict[str, int] = {}
    for t in toks:
        if t not in stop:
            freq[t] = freq.get(t, 0) + 1
    # 按频率取前 10
    sorted_toks = sorted(freq.items(), key=lambda x: -x[1])
    for t, _ in sorted_toks[:10]:
        if t not in kws:
            kws.append(t)
    # 加通用 "skill"
    kws.extend(["skill", "auto-invoke"])
    return kws[:15]


def build_node(sk: dict, meta: dict, body: str) -> dict:
    name = meta.get("name", sk["path"].parent.name)
    desc = meta.get("description", "")
    allowed_tools = meta.get("allowed-tools", [])
    if isinstance(allowed_tools, str):
        allowed_tools = [allowed_tools]

    # ID: SKILL-{name-slug}
    slug = re.sub(r"[^a-zA-Z0-9-]", "-", str(name)).strip("-").lower()
    if sk["source"] == "plugin":
        plugin_name = sk["path"].parents[2].name  # plugins/{pname}/skills/{skill}/SKILL.md
        slug = f"{plugin_name}-{slug}"
        node_id = f"SKILL-plugin-{slug[:40]}"
    else:
        node_id = f"SKILL-{sk['source']}-{slug[:40]}"

    # name: 40-60 字
    node_name = f"[skill:{sk['source']}] {name}: {desc[:40]}" if desc else f"[skill:{sk['source']}] {name}"
    node_name = node_name[:80]

    kws = extract_keywords(str(name), str(desc))

    return {
        "id": node_id,
        "name": node_name,
        "cluster": "skills",
        "type": "skill",
        "status": "active",
        "content": {
            "description": str(desc)[:300] if desc else f"Skill '{name}' from {sk['source']}",
            "current_state": "synced",
            "notes": body[:2000],
            "tools": list(allowed_tools) if allowed_tools else [],
            "extra": {
                "skill_source": sk["source"],
                "skill_path": str(sk["path"]),
                "skill_name": str(name),
                "allowed_tools": allowed_tools,
                "success_count": 0,
                "fail_count": 0,
                "avg_duration_s": None,
                "last_invoked_at": None,
            },
        },
        "activation_keywords": kws,
        "priority": sk["priority"],
        "primary_author": "skill-sync",
    }


def upsert_node(node: dict, dry_run: bool) -> dict:
    nid = node["id"]
    if dry_run:
        return {"nid": nid, "dry_run": True}
    # 先 GET 看在不在
    try:
        g = requests.get(f"{THREE_CAN}/api/nodes/{nid}", timeout=5)
        if g.status_code == 200:
            existing = g.json()
            # 保留 extra.success_count / fail_count / avg_duration_s / last_invoked_at
            e_extra = existing.get("content", {}).get("extra", {}) or {}
            n_extra = node["content"]["extra"]
            for k in ("success_count", "fail_count", "avg_duration_s", "last_invoked_at"):
                if k in e_extra and e_extra[k] not in (None, 0):
                    n_extra[k] = e_extra[k]
            # PUT 更新
            r = requests.put(f"{THREE_CAN}/api/nodes/{nid}", json={
                "content": node["content"],
                "activation_keywords": node["activation_keywords"],
                "priority": node["priority"],
            }, timeout=15)
            return {"nid": nid, "action": "update", "status": r.status_code}
    except Exception as e:
        return {"nid": nid, "error": f"get failed: {str(e)[:80]}"}

    # POST 创建 (force=true 跳过 R1 查重, skill 节点明确唯一)
    try:
        r = requests.post(f"{THREE_CAN}/api/nodes?force=true", json=node, timeout=15)
        return {"nid": nid, "action": "create", "status": r.status_code, "body": r.text[:120] if r.status_code >= 400 else None}
    except Exception as e:
        return {"nid": nid, "error": f"post failed: {str(e)[:80]}"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--project-only", action="store_true")
    ap.add_argument("--user-only", action="store_true")
    args = ap.parse_args()

    skills = find_skills()
    if args.project_only:
        skills = [s for s in skills if s["source"] == "project"]
    if args.user_only:
        skills = [s for s in skills if s["source"] == "user"]

    print(f"[skill-sync] 扫到 {len(skills)} 个 SKILL.md")
    sources = {}
    for s in skills:
        sources[s["source"]] = sources.get(s["source"], 0) + 1
    print(f"            来源: {sources}")

    results = []
    for s in skills:
        try:
            text = s["path"].read_text(encoding="utf-8")
            meta, body = parse_frontmatter(text)
            if not meta.get("name") and not meta.get("description"):
                results.append({"path": str(s["path"]), "skip": "no frontmatter"})
                continue
            node = build_node(s, meta, body)
            res = upsert_node(node, args.dry_run)
            res["source"] = s["source"]
            res["name"] = meta.get("name", "?")
            results.append(res)
            if args.dry_run:
                print(f"  [DRY] {node['id']:50s} source={s['source']} kws={len(node['activation_keywords'])}")
            else:
                action = res.get("action", "?")
                status = res.get("status", "?")
                print(f"  [{action:6s}] {res['nid']:50s} http={status}")
        except Exception as e:
            results.append({"path": str(s["path"]), "error": str(e)[:100]})
            print(f"  [error ] {s['path']}: {e}")

    create_ct = sum(1 for r in results if r.get("action") == "create" and r.get("status", 0) < 400)
    update_ct = sum(1 for r in results if r.get("action") == "update" and r.get("status", 0) < 400)
    err_ct = sum(1 for r in results if r.get("error") or r.get("status", 0) >= 400)
    print(f"\n[skill-sync] create={create_ct} update={update_ct} error={err_ct}")
    return 0 if err_ct == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
