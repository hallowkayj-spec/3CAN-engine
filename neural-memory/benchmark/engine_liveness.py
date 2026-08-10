"""3CAN Engine Liveness Harness (S67 硬规则 §0, 2026-04-20).

一个 3-5 秒的引擎存活探针, 用途:
  1. session 开场自检 / UAT 前置门禁
  2. CI health check
  3. behavioral-gate 离线判定的独立复核 (gate 用 node http, 这里用 Python httpx)

就位标准 (HARD):
  - HTTP 200 on /api/stats
  - total_nodes >= THRESHOLD_NODES (默认 1000)
  - total_edges >= THRESHOLD_EDGES (默认 500)
  - /api/route POST 能在 10s 内返回 ≥1 个节点 (活路由验证, 防只起 FastAPI 空壳)

exit 0 全通, exit 1 任何一项 fail.
带 --json 打印 JSON 报告供 harness 链路消费.

用法:
  python neural-memory/benchmark/engine_liveness.py
  python neural-memory/benchmark/engine_liveness.py --json
  python neural-memory/benchmark/engine_liveness.py --strict-route  # 要 route 能跑才算 ok
"""
from __future__ import annotations

import argparse
import json
import sys
import time

import httpx

ENGINE_URL = "http://127.0.0.1:9700"
THRESHOLD_NODES = 1000
THRESHOLD_EDGES = 500
STATS_TIMEOUT = 5.0
ROUTE_TIMEOUT = 10.0


def check_stats() -> tuple[bool, dict, str]:
    try:
        r = httpx.get(f"{ENGINE_URL}/api/stats", timeout=STATS_TIMEOUT)
    except httpx.ConnectError:
        return False, {}, "connect_refused: port 9700 no listener"
    except httpx.ReadTimeout:
        return False, {}, f"stats_timeout: no response within {STATS_TIMEOUT}s"
    except Exception as e:
        return False, {}, f"stats_error: {type(e).__name__}: {e}"
    if r.status_code != 200:
        return False, {}, f"stats_http_{r.status_code}"
    try:
        data = r.json()
    except Exception:
        return False, {}, "stats_not_json"
    n, e = data.get("total_nodes", 0), data.get("total_edges", 0)
    if n < THRESHOLD_NODES:
        return False, data, f"nodes_too_few: {n} < {THRESHOLD_NODES} (empty graph = bug)"
    if e < THRESHOLD_EDGES:
        return False, data, f"edges_too_few: {e} < {THRESHOLD_EDGES}"
    return True, data, "ok"


def check_route() -> tuple[bool, dict, str]:
    payload = {"task": "liveness probe", "max_nodes": 1, "agent_id": "engine_liveness"}
    try:
        r = httpx.post(f"{ENGINE_URL}/api/route",
                       json=payload, timeout=ROUTE_TIMEOUT)
    except httpx.ConnectError:
        return False, {}, "route_connect_refused"
    except httpx.ReadTimeout:
        return False, {}, f"route_timeout: >{ROUTE_TIMEOUT}s (engine alive but route subsystem hung)"
    except Exception as e:
        return False, {}, f"route_error: {type(e).__name__}: {e}"
    if r.status_code != 200:
        return False, {}, f"route_http_{r.status_code}"
    try:
        data = r.json()
    except Exception:
        return False, {}, "route_not_json"
    nodes = data.get("nodes", [])
    if not nodes:
        return False, data, "route_zero_results"
    return True, {"top1_id": nodes[0].get("id"), "count": len(nodes)}, "ok"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="emit JSON report")
    ap.add_argument("--strict-route", action="store_true",
                    help="require /api/route to work (not just /api/stats)")
    args = ap.parse_args()

    t0 = time.time()
    stats_ok, stats_data, stats_reason = check_stats()
    route_ok, route_data, route_reason = (None, {}, "skipped")
    if stats_ok and args.strict_route:
        route_ok, route_data, route_reason = check_route()

    elapsed = round(time.time() - t0, 3)
    overall_ok = stats_ok and (route_ok is not False)
    report = {
        "ok": overall_ok,
        "elapsed_sec": elapsed,
        "stats": {"ok": stats_ok, "reason": stats_reason,
                  "nodes": stats_data.get("total_nodes", 0),
                  "edges": stats_data.get("total_edges", 0),
                  "active_nodes": stats_data.get("active_nodes", 0)},
        "route": {"ok": route_ok, "reason": route_reason, **route_data} if args.strict_route
                 else {"skipped": True},
        "threshold": {"nodes": THRESHOLD_NODES, "edges": THRESHOLD_EDGES},
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        status = "OK" if overall_ok else "FAIL"
        print(f"[3CAN liveness] {status} in {elapsed}s")
        print(f"  stats: {stats_reason} (nodes={report['stats']['nodes']} "
              f"edges={report['stats']['edges']})")
        if args.strict_route:
            print(f"  route: {route_reason}")
        if not overall_ok:
            print("  启动: cd neural-memory && python backend/app.py --port 9700 > logs/backend_9700.log 2>&1 &")
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
