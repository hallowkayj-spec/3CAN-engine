"""3CAN Session-Level Aggregator — 基座#4

从 activity_log 按 agent + time-window 自动聚合出 SES-auto-* 节点.
弥补"SES-* 靠人工 handoff-import 才建"的架构缺陷.

time-window 规则: 同 agent 的连续 activity, 间隔 >2h 视为切窗口.

每窗口 → 生成 SES-auto-{agent}-{start_ymd-HM} 节点:
- name: "auto-session {agent} {start_time} - {end_time}"
- cluster: "会话记录"
- type: session
- content.description: 1-2 句摘要 (DeepSeek 生成, 基于 actions + affected_nodes)
- content.current_state: 窗口统计 (actions count, 时长)
- content.notes: 前 N 个 activity 详情
- content.key_files: 出现频率高的 file_path
- activation_keywords: agent_id + 日期 + 高频 action + 高频节点前缀

运行:
  python tools/session_aggregator.py --dry-run
  python tools/session_aggregator.py --window-hours 2 --apply
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
ACTIVITY_FILE = ROOT / "graph" / "activity_log.json"

THREE_CAN = "http://localhost:9700"
MIN_WINDOW_EVENTS = 3           # 少于 3 条不聚合
DEFAULT_GAP_HOURS = 2


def load_activity() -> list[dict]:
    if not ACTIVITY_FILE.exists():
        return []
    try:
        return json.loads(ACTIVITY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def split_windows(events: list[dict], gap_hours: float) -> list[list[dict]]:
    """按 agent + 时间间隔 >gap 切窗."""
    by_agent: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        agent = e.get("agent_id") or "unknown"
        by_agent[agent].append(e)

    all_windows = []
    gap_sec = gap_hours * 3600
    for agent, evs in by_agent.items():
        # 排时间
        def _parse(t):
            try:
                return dt.datetime.fromisoformat((t or "").replace("Z", "+00:00"))
            except Exception:
                return None
        evs_with_ts = [(e, _parse(e.get("timestamp", ""))) for e in evs]
        evs_with_ts = [(e, t) for e, t in evs_with_ts if t is not None]
        evs_with_ts.sort(key=lambda x: x[1])

        current: list[dict] = []
        prev_t = None
        for e, t in evs_with_ts:
            if prev_t is None or (t - prev_t).total_seconds() <= gap_sec:
                current.append(e)
            else:
                if len(current) >= MIN_WINDOW_EVENTS:
                    all_windows.append(current)
                current = [e]
            prev_t = t
        if len(current) >= MIN_WINDOW_EVENTS:
            all_windows.append(current)
    return all_windows


def summarize_window(window: list[dict]) -> dict:
    """从窗口内 events 抽摘要数据."""
    agent = window[0].get("agent_id", "unknown")
    start_t = window[0].get("timestamp", "")
    end_t = window[-1].get("timestamp", "")
    n = len(window)

    actions = Counter(e.get("action", "") for e in window)
    affected = Counter()
    for e in window:
        for nid in (e.get("affected_nodes") or []):
            affected[nid] += 1
    # 前缀分布
    prefix_dist = Counter()
    for nid in affected:
        prefix = nid.split("-", 1)[0] if "-" in nid else nid[:4]
        prefix_dist[prefix] += affected[nid]

    # key files from detail fields
    key_files: list[str] = []
    for e in window:
        detail = e.get("detail", "") or ""
        # 简单抽 "edited X" / "wrote X" 里的 X
        for prefix in ("edited ", "wrote ", "nb-edited "):
            if prefix in detail:
                name = detail.split(prefix, 1)[1].split()[0]
                if name and name not in key_files:
                    key_files.append(name)

    return {
        "agent": agent,
        "start": start_t,
        "end": end_t,
        "n_events": n,
        "actions": dict(actions.most_common(5)),
        "affected_top": [nid for nid, _ in affected.most_common(10)],
        "prefix_dist": dict(prefix_dist.most_common(5)),
        "key_files": key_files[:8],
        "duration_min": int((dt.datetime.fromisoformat(end_t.replace("Z", "+00:00"))
                             - dt.datetime.fromisoformat(start_t.replace("Z", "+00:00"))).total_seconds() / 60)
                         if start_t and end_t else 0,
    }


def build_node(summary: dict) -> dict:
    start_iso = summary["start"][:16].replace(":", "").replace("-", "")  # 2026041822
    agent_slug = summary["agent"].replace(".", "-").replace("_", "-")[:30]
    nid = f"SES-auto-{agent_slug}-{start_iso[:13]}"  # SES-auto-opus-brain-main-20260418T10
    nid = nid.replace("--", "-")[:60]

    top_actions = ", ".join(f"{k}={v}" for k, v in summary["actions"].items())
    top_prefix = ", ".join(f"{k}×{v}" for k, v in summary["prefix_dist"].items())
    desc = (
        f"自动聚合 session: {summary['agent']} 在 {summary['start'][:19]} - {summary['end'][:19]} "
        f"间 {summary['n_events']} 事件, {summary['duration_min']} 分钟. "
        f"actions: {top_actions}. "
        f"涉及前缀: {top_prefix}."
    )[:400]

    notes_parts = [
        f"duration: {summary['duration_min']} min",
        f"events: {summary['n_events']}",
        f"actions: {summary['actions']}",
        f"affected_top_nodes: {summary['affected_top'][:10]}",
    ]
    if summary["key_files"]:
        notes_parts.append(f"key_files: {summary['key_files']}")

    kws = [
        summary["agent"], start_iso[:8],
        "auto-session", "aggregated",
        *list(summary["actions"].keys())[:3],
        *[f"prefix-{p}" for p in list(summary["prefix_dist"].keys())[:3]],
    ]

    return {
        "id": nid,
        "name": f"[auto-ses] {summary['agent']} {summary['start'][:16]} ({summary['n_events']}事件)"[:80],
        "cluster": "会话记录",
        "type": "session",
        "status": "active",
        "priority": "medium",
        "content": {
            "description": desc,
            "current_state": f"auto-aggregated {summary['duration_min']}min {summary['n_events']}events",
            "notes": "\n".join(notes_parts)[:1500],
            "key_files": summary["key_files"],
            "extra": {
                "aggregated_agent": summary["agent"],
                "start": summary["start"],
                "end": summary["end"],
                "affected_nodes_top": summary["affected_top"],
            }
        },
        "activation_keywords": kws,
        "primary_author": "session-aggregator",
    }


def upsert(node: dict, dry_run: bool) -> dict:
    if dry_run:
        return {"id": node["id"], "dry_run": True}
    # GET 查重
    try:
        g = requests.get(f"{THREE_CAN}/api/nodes/{node['id']}", timeout=5)
        if g.status_code == 200:
            return {"id": node["id"], "skip": "exists"}
    except Exception:
        pass
    try:
        r = requests.post(f"{THREE_CAN}/api/nodes?force=true", json=node, timeout=15)
        return {"id": node["id"], "status": r.status_code}
    except Exception as e:
        return {"id": node["id"], "error": str(e)[:80]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window-hours", type=float, default=DEFAULT_GAP_HOURS)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    events = load_activity()
    print(f"[aggregator] 读 {len(events)} 活动事件")
    if not events:
        return 0

    windows = split_windows(events, args.window_hours)
    print(f"[aggregator] 切 {len(windows)} 窗口 (gap>{args.window_hours}h 切)")

    results = []
    for w in windows:
        s = summarize_window(w)
        node = build_node(s)
        r = upsert(node, args.dry_run)
        r["agent"] = s["agent"]
        r["n_events"] = s["n_events"]
        r["duration_min"] = s["duration_min"]
        results.append(r)

    created = sum(1 for r in results if r.get("status", 0) in (200, 201))
    skipped = sum(1 for r in results if r.get("skip") == "exists")
    errors = sum(1 for r in results if r.get("error"))

    print(f"\n[aggregator] create={created} skip={skipped} error={errors}")
    for r in results[:10]:
        tag = "DRY" if r.get("dry_run") else ("NEW" if r.get("status", 0) in (200, 201) else ("SKIP" if r.get("skip") else "ERR"))
        print(f"  [{tag:4s}] {r.get('id','?')[:60]:60s}  agent={r.get('agent','')[:15]:15s} n_events={r.get('n_events','?')} {r.get('duration_min','?')}min")
    return 0


if __name__ == "__main__":
    sys.exit(main())
