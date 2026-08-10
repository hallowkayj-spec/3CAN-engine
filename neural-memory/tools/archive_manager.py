"""3CAN Archive Manager — 基座#8 物理归档隔离

the maintainer 明确: 归档不删除, 但要"物理隔离" — status=archived 的节点从 graph/nodes/
移到 graph/archive/nodes/, 引擎 _load 时跳过 archive 目录, 但用户想看可 restore.

符合 the maintainer 原则: 节省的数据/文件不直接删, 归档隔离 (和之前做过的一样)

命令:
  python tools/archive_manager.py --list              # 列 status=archived 节点
  python tools/archive_manager.py --migrate           # 把 status=archived 的物理移到 archive/
  python tools/archive_manager.py --restore <id>      # 把一个 archived 节点搬回 nodes/, status=active
  python tools/archive_manager.py --restore-all       # 全部搬回 (慎用)
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NODES_DIR = ROOT / "graph" / "nodes"
ARCHIVE_DIR = ROOT / "graph" / "archive" / "nodes"


def ensure_archive_dir():
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)


def list_archived() -> list[dict]:
    """列当前 nodes/ 里 status=archived + archive/ 里所有."""
    out = []
    for p in NODES_DIR.glob("*.json"):
        try:
            n = json.loads(p.read_text(encoding="utf-8"))
            if n.get("status") == "archived":
                out.append({"location": "nodes/", "id": n["id"], "name": n.get("name", "")[:50],
                            "updated_at": n.get("updated_at", "")})
        except Exception:
            continue
    if ARCHIVE_DIR.exists():
        for p in ARCHIVE_DIR.glob("*.json"):
            try:
                n = json.loads(p.read_text(encoding="utf-8"))
                out.append({"location": "archive/", "id": n["id"], "name": n.get("name", "")[:50],
                            "updated_at": n.get("updated_at", "")})
            except Exception:
                continue
    return out


def migrate_to_archive() -> dict:
    """把 nodes/ 里 status=archived 的移到 archive/."""
    ensure_archive_dir()
    moved = 0
    errors = []
    for p in NODES_DIR.glob("*.json"):
        try:
            n = json.loads(p.read_text(encoding="utf-8"))
            if n.get("status") != "archived":
                continue
            target = ARCHIVE_DIR / p.name
            shutil.move(str(p), str(target))
            moved += 1
        except Exception as e:
            errors.append(f"{p.name}: {str(e)[:60]}")
    return {"moved": moved, "errors": errors}


def restore_one(node_id: str) -> dict:
    """从 archive/ 搬回 nodes/ 并设 status=active."""
    src = ARCHIVE_DIR / f"{node_id}.json"
    if not src.exists():
        return {"ok": False, "error": f"{src} 不存在"}
    try:
        n = json.loads(src.read_text(encoding="utf-8"))
        n["status"] = "active"
        import datetime as dt
        n["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        n["updated_by"] = "archive-manager-restore"
        target = NODES_DIR / src.name
        target.write_text(json.dumps(n, ensure_ascii=False, indent=2), encoding="utf-8")
        src.unlink()
        return {"ok": True, "id": node_id, "from": "archive/", "to": "nodes/"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:100]}


def restore_all() -> dict:
    if not ARCHIVE_DIR.exists():
        return {"restored": 0}
    count = 0
    errors = []
    for p in ARCHIVE_DIR.glob("*.json"):
        r = restore_one(p.stem)
        if r.get("ok"):
            count += 1
        else:
            errors.append(r.get("error"))
    return {"restored": count, "errors": errors}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--migrate", action="store_true")
    ap.add_argument("--restore", type=str, help="节点 id")
    ap.add_argument("--restore-all", action="store_true")
    args = ap.parse_args()

    if args.list:
        items = list_archived()
        print(f"[archive] 共 {len(items)} archived 节点:")
        for it in items[:30]:
            print(f"  [{it['location']:8s}] {it['id']:55s} {it['name']}")
        if len(items) > 30:
            print(f"  ... (共 {len(items)})")
        return 0

    if args.migrate:
        stats = migrate_to_archive()
        print(f"[archive] 物理迁移: {stats['moved']} 节点 → graph/archive/nodes/")
        if stats.get("errors"):
            print(f"[archive] 错误: {stats['errors'][:5]}")
        print("\n[archive] 提示: 调 /api/reload 让引擎忘掉这些节点 (它们不再参与 route)")
        return 0

    if args.restore:
        r = restore_one(args.restore)
        print(f"[archive] restore: {r}")
        return 0 if r.get("ok") else 1

    if args.restore_all:
        stats = restore_all()
        print(f"[archive] 全量 restore: {stats}")
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
