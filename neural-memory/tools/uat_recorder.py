"""UAT Recorder v0.1 (pilot) — Real Task Evaluation for 3CAN.

Per LLM_POLICY.md §8 and REAL_UAT_PLAN.md §5.

Commands: start / route / err-check / file-change / note / close.

Typical flow:
    python tools/uat_recorder.py start --task-id cross-session-bug-trace --scenario A \\
        --description "Test if agent finds historical ERR before action"

    # agent does route calls during the task:
    python tools/uat_recorder.py route --task-id cross-session-bug-trace \\
        --query "slim mode bug history" --tokens-before 45000 --tokens-after 500

    # mark an ERR hit for proactive-check:
    python tools/uat_recorder.py err-check --task-id cross-session-bug-trace \\
        --err-id ERR-longmemeval-runner-slim-mode-wrong-api-use-2026-04-18 --position 1

    # optional: note observations:
    python tools/uat_recorder.py note --task-id cross-session-bug-trace \\
        --text "Agent read ERR current_state before editing"

    # close with perceived satisfaction 1-5:
    python tools/uat_recorder.py close --task-id cross-session-bug-trace \\
        --satisfaction 4 --notes "Zero rework, ERR surfaced top1"

State is kept in ~/.claude/logs/uat_state/{task_id}.json across calls.
On close, writes SES-uat-{task_id}-{YYYYMMDD} node to 3CAN via /api/nodes.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.request
from pathlib import Path

ENGINE_URL = "http://localhost:9700"
STATE_DIR = Path(os.path.expanduser("~")) / ".claude" / "logs" / "uat_state"


def _post(path: str, body: dict) -> dict:
    url = ENGINE_URL + path
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        return json.loads(urllib.request.urlopen(req, timeout=30).read().decode("utf-8"))
    except Exception as e:
        return {"_error": str(e)[:200]}


def _state_file(task_id: str) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / f"{task_id}.json"


def _load(task_id: str) -> dict:
    p = _state_file(task_id)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {}


def _save(task_id: str, state: dict) -> None:
    _state_file(task_id).write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def cmd_start(args) -> int:
    state = {
        "task_id": args.task_id,
        "scenario": args.scenario,
        "description": args.description or "",
        "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "route_calls": [],
        "err_checks": [],
        "file_changes": [],
        "notes": [],
    }
    _save(args.task_id, state)
    print(f"[UAT] started task={args.task_id} scenario={args.scenario}")
    print(f"  state: {_state_file(args.task_id)}")
    return 0


def cmd_route(args) -> int:
    s = _load(args.task_id)
    if not s:
        print(f"[UAT] ERROR: task {args.task_id} not started")
        return 1
    body = {
        "task": args.query,
        "max_nodes": args.max_nodes,
        "agent_id": args.agent_id,
        "mode": args.mode,
        "confirm_low_confidence": True,
        "allow_degraded": True,
    }
    resp = _post("/api/route", body)
    nodes = resp.get("nodes") or resp.get("activated_nodes") or []
    top5 = [n.get("id") for n in nodes][:5]
    conf = resp.get("confidence", "unknown")
    entry = {
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        "query": args.query,
        "agent_id": args.agent_id,
        "mode": args.mode,
        "tokens_before": args.tokens_before,
        "tokens_after": args.tokens_after,
        "top5_ids": top5,
        "confidence": conf,
    }
    s["route_calls"].append(entry)
    _save(args.task_id, s)
    delta = (args.tokens_before or 0) - (args.tokens_after or 0)
    print(f"[UAT] route top5={top5} conf={conf} token_delta={delta}")
    return 0


def cmd_err(args) -> int:
    s = _load(args.task_id)
    if not s:
        return 1
    s["err_checks"].append({
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        "err_id": args.err_id,
        "position_in_top_k": args.position,
        "surfaced_before_action": (args.position is not None and args.position <= 3),
    })
    _save(args.task_id, s)
    print(f"[UAT] err-check {args.err_id} pos={args.position}")
    return 0


def cmd_file(args) -> int:
    s = _load(args.task_id)
    if not s:
        return 1
    s["file_changes"].append({
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        "file_path": args.file_path,
        "tool_name": args.tool_name,
        "ticket_id": args.ticket_id,
    })
    _save(args.task_id, s)
    print(f"[UAT] file_change {args.file_path}")
    return 0


def cmd_note(args) -> int:
    s = _load(args.task_id)
    if not s:
        return 1
    s["notes"].append({
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        "text": args.text[:500],
    })
    _save(args.task_id, s)
    print("[UAT] note saved")
    return 0


def cmd_close(args) -> int:
    s = _load(args.task_id)
    if not s:
        print(f"[UAT] ERROR: task {args.task_id} not started")
        return 1
    s["closed_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    s["satisfaction_1_5"] = args.satisfaction
    s["close_notes"] = args.notes or ""

    routes = s.get("route_calls", [])
    savings = [
        (c.get("tokens_before") or 0) - (c.get("tokens_after") or 0)
        for c in routes
        if c.get("tokens_before") and c.get("tokens_after")
    ]
    errs = s.get("err_checks", [])
    err_surfaced = sum(1 for e in errs if e.get("surfaced_before_action"))
    duration = int(
        (dt.datetime.fromisoformat(s["closed_at"]) -
         dt.datetime.fromisoformat(s["started_at"])).total_seconds()
    )
    s["aggregate"] = {
        "n_route_calls": len(routes),
        "avg_token_save": int(sum(savings) / len(savings)) if savings else 0,
        "err_surfaced_rate": round(err_surfaced / len(errs), 3) if errs else None,
        "n_file_changes": len(s.get("file_changes", [])),
        "duration_sec": duration,
    }
    _save(args.task_id, s)

    today = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d")
    node_id = f"SES-uat-{args.task_id}-{today}"
    body = {
        "id": node_id,
        "name": f"[UAT] {args.task_id} scenario {s.get('scenario', '?')}",
        "cluster": "会话记录",
        "type": "session",
        "content": {
            "description": s.get("description") or f"UAT {args.task_id}",
            "current_state": f"closed. satisfaction={args.satisfaction}/5. "
                             f"agg={json.dumps(s['aggregate'], ensure_ascii=False)}",
            "notes": json.dumps(s, ensure_ascii=False, indent=2)[:4000],
        },
        "activation_keywords": [
            "UAT", "真实任务测试", "dogfood", args.task_id,
            f"scenario-{s.get('scenario', 'x')}",
            "satisfaction", "user-experience",
        ],
        "priority": "medium",
        "primary_author": "uat-recorder",
    }
    r = _post("/api/nodes?force=true", body)
    if "_error" in r:
        print(f"[UAT] writeback FAILED: {r['_error']}")
    else:
        print(f"[UAT] closed. node={node_id}")
    print(f"  aggregate: {s['aggregate']}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="3CAN UAT Recorder v0.1")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s_start = sub.add_parser("start")
    s_start.add_argument("--task-id", required=True)
    s_start.add_argument("--scenario", default="custom",
                         help="A|B|C or custom label (see REAL_UAT_PLAN.md §2)")
    s_start.add_argument("--description", default="")
    s_start.set_defaults(fn=cmd_start)

    s_route = sub.add_parser("route")
    s_route.add_argument("--task-id", required=True)
    s_route.add_argument("--query", required=True)
    s_route.add_argument("--agent-id", default="uat-agent")
    s_route.add_argument("--mode", default="slim")
    s_route.add_argument("--max-nodes", type=int, default=5)
    s_route.add_argument("--tokens-before", type=int, default=None)
    s_route.add_argument("--tokens-after", type=int, default=None)
    s_route.set_defaults(fn=cmd_route)

    s_err = sub.add_parser("err-check")
    s_err.add_argument("--task-id", required=True)
    s_err.add_argument("--err-id", required=True)
    s_err.add_argument("--position", type=int, default=None,
                       help="1-based position in top-K")
    s_err.set_defaults(fn=cmd_err)

    s_file = sub.add_parser("file-change")
    s_file.add_argument("--task-id", required=True)
    s_file.add_argument("--file-path", required=True)
    s_file.add_argument("--tool-name", required=True)
    s_file.add_argument("--ticket-id", default=None)
    s_file.set_defaults(fn=cmd_file)

    s_note = sub.add_parser("note")
    s_note.add_argument("--task-id", required=True)
    s_note.add_argument("--text", required=True)
    s_note.set_defaults(fn=cmd_note)

    s_close = sub.add_parser("close")
    s_close.add_argument("--task-id", required=True)
    s_close.add_argument("--satisfaction", type=int, required=True,
                         choices=[1, 2, 3, 4, 5])
    s_close.add_argument("--notes", default="")
    s_close.set_defaults(fn=cmd_close)

    args = ap.parse_args()
    return args.fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())
