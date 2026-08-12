#!/usr/bin/env python3
"""Verify a running 3CAN sidecar for a project.

This is intentionally dependency-light and uses only the Python standard
library so it can run before the full optional semantic stack is installed.
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from typing import Any


DEFAULT_ROUTE_TASK = "quickstart route api graph memory token usage"


def request_json(method: str, url: str, payload: dict[str, Any] | None = None, timeout: int = 10) -> tuple[bool, Any]:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return True, json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return False, {"error": f"http {exc.code}", "body": body[:500]}
    except Exception as exc:
        return False, {"error": str(exc)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=os.environ.get("THREECAN_BASE_URL", "http://127.0.0.1:9700"))
    parser.add_argument("--min-nodes", type=int, default=int(os.environ.get("THREECAN_MIN_NODES", "10")))
    parser.add_argument("--route-task", default=DEFAULT_ROUTE_TASK)
    parser.add_argument("--route-timeout", type=int, default=30)
    parser.add_argument("--readiness-timeout", type=int, default=60)
    parser.add_argument(
        "--require-production-ready",
        action="store_true",
        help="fail unless the pinned production readiness profile passes",
    )
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON only")
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    summary: dict[str, Any] = {
        "base_url": base_url,
        "min_nodes": args.min_nodes,
        "checks": {},
        "ok": False,
    }

    live_ok, live = request_json("GET", f"{base_url}/api/health/live", timeout=10)
    summary["checks"]["liveness"] = {
        "ok": live_ok and isinstance(live, dict) and live.get("alive") is True,
        "error": None if live_ok else live,
    }

    stats_ok, stats = request_json(
        "GET",
        f"{base_url}/api/stats?deep=true",
        timeout=args.readiness_timeout,
    )
    total_nodes = int(stats.get("total_nodes") or 0) if isinstance(stats, dict) else 0
    summary["checks"]["stats"] = {
        "ok": stats_ok and total_nodes >= args.min_nodes,
        "total_nodes": total_nodes,
        "total_edges": stats.get("total_edges") if isinstance(stats, dict) else None,
        "error": None if stats_ok else stats,
    }
    readiness = stats.get("readiness") if isinstance(stats, dict) else None
    readiness = readiness if isinstance(readiness, dict) else {}
    production_ready = readiness.get("production_ready") is True
    development_ready = (
        readiness.get("mode") == "development"
        and readiness.get("development_ready") is True
    )
    readiness_ok = production_ready or (
        development_ready and not args.require_production_ready
    )
    summary["checks"]["readiness"] = {
        "ok": readiness_ok,
        "status": (
            "VERIFIED_PRODUCTION"
            if production_ready
            else "DEVELOPMENT_ONLY"
            if development_ready
            else "NOT_READY"
        ),
        "mode": readiness.get("mode"),
        "production_ready": production_ready,
        "development_ready": development_ready,
        "verification_state": (readiness.get("cache") or {}).get(
            "verification_state"
        ),
        "reasons": readiness.get("reasons") or [],
    }

    route_payload = {
        "task": args.route_task,
        "max_nodes": 4,
        "mode": "skeleton",
        "budget_tokens": 400,
        "agent_id": "verify-project",
        "confirm_low_confidence": True,
        "allow_degraded": True,
    }
    route_ok, route = request_json("POST", f"{base_url}/api/route", route_payload, timeout=args.route_timeout)
    route_nodes = route.get("nodes") if isinstance(route, dict) else []
    summary["checks"]["route"] = {
        "ok": route_ok and bool(route_nodes),
        "node_count": len(route_nodes) if isinstance(route_nodes, list) else 0,
        "first_node": route_nodes[0].get("id") if isinstance(route_nodes, list) and route_nodes else None,
        "confidence": route.get("confidence") if isinstance(route, dict) else None,
        "error": None if route_ok else route,
    }

    token_ok, token_health = request_json("GET", f"{base_url}/api/token-usage/health", timeout=10)
    summary["checks"]["token_usage"] = {
        "ok": token_ok,
        "auto_importer": token_health.get("auto_importer") if isinstance(token_health, dict) else None,
        "error": None if token_ok else token_health,
    }

    summary["ok"] = bool(
        summary["checks"]["liveness"]["ok"]
        and summary["checks"]["stats"]["ok"]
        and summary["checks"]["readiness"]["ok"]
        and summary["checks"]["route"]["ok"]
    )

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"[3CAN verify] {base_url}")
        print(f"  liveness: ok={summary['checks']['liveness']['ok']}")
        print(f"  stats: ok={summary['checks']['stats']['ok']} nodes={total_nodes} min={args.min_nodes}")
        print(
            "  readiness: ok={ok} status={status} mode={mode}".format(
                **summary["checks"]["readiness"]
            )
        )
        print(
            "  route: ok={ok} nodes={node_count} first={first_node} confidence={confidence}".format(
                **summary["checks"]["route"]
            )
        )
        print(f"  token_usage: ok={summary['checks']['token_usage']['ok']}")
        print(f"  result: {'PASS' if summary['ok'] else 'FAIL'}")

    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
