#!/usr/bin/env python3
"""Wrapper-layer 3CAN task ledger MVP.

The real 3CAN engine remains the project memory substrate. This file provides a
local, testable ledger for background work when the engine has no task schema.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LEDGER = PROJECT_ROOT / "test-results" / "3can" / "task_ledger.json"
TASK_STATUSES = {"queued", "running", "blocked", "needs_review", "succeeded", "failed", "lost"}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _epoch() -> float:
    return time.time()


def _print_json(data: Any) -> None:
    sys.stdout.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def load_ledger(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"tasks": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"tasks": []}
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        return {"tasks": []}
    return data


def save_ledger(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _find_task(data: dict[str, Any], task_id: str) -> dict[str, Any] | None:
    return next((task for task in data.get("tasks", []) if task.get("task_id") == task_id), None)


def add_task(path: Path, *, title: str, status: str = "queued", owner: str = "codex-main", task_id: str = "", note: str = "") -> dict[str, Any]:
    if status not in TASK_STATUSES:
        raise ValueError(f"invalid status: {status}")
    data = load_ledger(path)
    tid = task_id or f"task_{uuid.uuid4().hex[:10]}"
    if _find_task(data, tid):
        raise ValueError(f"task already exists: {tid}")
    now = _now()
    task = {
        "task_id": tid,
        "title": title,
        "status": status,
        "owner": owner,
        "created_at": now,
        "updated_at": now,
        "updated_epoch": _epoch(),
        "notes": [note] if note else [],
        "history": [{"status": status, "at": now, "note": note}],
    }
    data["tasks"].append(task)
    save_ledger(path, data)
    return task


def update_task(path: Path, *, task_id: str, status: str, note: str = "") -> dict[str, Any]:
    if status not in TASK_STATUSES:
        raise ValueError(f"invalid status: {status}")
    data = load_ledger(path)
    task = _find_task(data, task_id)
    if not task:
        raise KeyError(f"task not found: {task_id}")
    now = _now()
    task["status"] = status
    task["updated_at"] = now
    task["updated_epoch"] = _epoch()
    if note:
        task.setdefault("notes", []).append(note)
    task.setdefault("history", []).append({"status": status, "at": now, "note": note})
    save_ledger(path, data)
    return task


def list_tasks(path: Path, *, status: str = "") -> dict[str, Any]:
    data = load_ledger(path)
    tasks = data.get("tasks", [])
    if status:
        tasks = [task for task in tasks if task.get("status") == status]
    counts = {state: 0 for state in sorted(TASK_STATUSES)}
    for task in data.get("tasks", []):
        state = task.get("status")
        if state in counts:
            counts[state] += 1
    return {"count": len(tasks), "status_counts": counts, "tasks": tasks}


def mark_lost(path: Path, *, max_age_sec: float) -> dict[str, Any]:
    data = load_ledger(path)
    now = _epoch()
    changed = []
    for task in data.get("tasks", []):
        if task.get("status") != "running":
            continue
        age = now - float(task.get("updated_epoch") or now)
        if age < max_age_sec:
            continue
        task["status"] = "lost"
        task["updated_at"] = _now()
        task["updated_epoch"] = now
        task.setdefault("history", []).append({"status": "lost", "at": task["updated_at"], "note": f"running older than {max_age_sec}s"})
        changed.append(task["task_id"])
    save_ledger(path, data)
    return {"lost_count": len(changed), "lost_task_ids": changed}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="3CAN wrapper task ledger MVP.")
    parser.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add")
    add.add_argument("--title", required=True)
    add.add_argument("--status", default="queued", choices=sorted(TASK_STATUSES))
    add.add_argument("--owner", default="codex-main")
    add.add_argument("--task-id", default="")
    add.add_argument("--note", default="")

    upd = sub.add_parser("update")
    upd.add_argument("--task-id", required=True)
    upd.add_argument("--status", required=True, choices=sorted(TASK_STATUSES))
    upd.add_argument("--note", default="")

    lst = sub.add_parser("list")
    lst.add_argument("--status", default="", choices=["", *sorted(TASK_STATUSES)])

    lost = sub.add_parser("mark-lost")
    lost.add_argument("--max-age-sec", type=float, default=3600)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    ledger = Path(args.ledger)
    try:
        if args.command == "add":
            _print_json({"ok": True, "task": add_task(ledger, title=args.title, status=args.status, owner=args.owner, task_id=args.task_id, note=args.note)})
        elif args.command == "update":
            _print_json({"ok": True, "task": update_task(ledger, task_id=args.task_id, status=args.status, note=args.note)})
        elif args.command == "list":
            _print_json({"ok": True, **list_tasks(ledger, status=args.status)})
        elif args.command == "mark-lost":
            _print_json({"ok": True, **mark_lost(ledger, max_age_sec=args.max_age_sec)})
        else:
            parser.error(f"unknown command: {args.command}")
    except Exception as exc:
        _print_json({"ok": False, "error": str(exc)})
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
