#!/usr/bin/env python3
"""Verify a running 3CAN sidecar for a project.

This is intentionally dependency-light and uses only the Python standard
library so it can run before the full optional semantic stack is installed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any


BACKEND_DIR = Path(__file__).resolve().parents[1] / "neural-memory" / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from error_knowledge import deterministic_fingerprint  # noqa: E402


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
    parser.add_argument("--project-id", default="")
    parser.add_argument("--project-namespace", default="")
    parser.add_argument(
        "--exercise-writes",
        action="store_true",
        help="exercise disposable writeback, ErrorKnowledge, and project isolation",
    )
    parser.add_argument(
        "--require-production-ready",
        action="store_true",
        help="fail unless the pinned production readiness profile passes",
    )
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON only")
    args = parser.parse_args()
    project_scoped = bool(args.project_id or args.project_namespace)
    if bool(args.project_id) != bool(args.project_namespace):
        parser.error("--project-id and --project-namespace must be supplied together")
    if args.exercise_writes and not project_scoped:
        parser.error("--exercise-writes requires an explicit project identity pair")

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

    route_payload: dict[str, Any] = {
        "task": args.route_task,
        "max_nodes": 4,
        "mode": "skeleton",
        "budget_tokens": 1400,
        "agent_id": "verify-project",
        "confirm_low_confidence": True,
        "allow_degraded": True,
    }
    if project_scoped:
        route_payload.update({
            "project_id": args.project_id,
            "project_namespace": args.project_namespace,
        })
    route_ok, route = request_json("POST", f"{base_url}/api/route", route_payload, timeout=args.route_timeout)
    route_nodes = route.get("nodes") if isinstance(route, dict) else []
    summary["checks"]["route"] = {
        "ok": route_ok and bool(route_nodes),
        "node_count": len(route_nodes) if isinstance(route_nodes, list) else 0,
        "first_node": route_nodes[0].get("id") if isinstance(route_nodes, list) and route_nodes else None,
        "confidence": route.get("confidence") if isinstance(route, dict) else None,
        "error": None if route_ok else route,
    }
    route_meta = route.get("route_meta") if isinstance(route, dict) else {}
    route_meta = route_meta if isinstance(route_meta, dict) else {}
    owner_defaults = route_meta.get("owner_defaults")
    summary["checks"]["owner_intent"] = (
        {
            "ok": (
                isinstance(owner_defaults, dict)
                and owner_defaults.get("status") == "applied"
                and owner_defaults.get("source") == "3CAN.md"
            ),
            "status": "APPLIED" if isinstance(owner_defaults, dict) else "MISSING",
            "digest": (
                owner_defaults.get("digest")
                if isinstance(owner_defaults, dict)
                else None
            ),
        }
        if project_scoped
        else {"ok": True, "status": "NOT_REQUESTED", "digest": None}
    )

    first_node = (
        route_nodes[0].get("id")
        if isinstance(route_nodes, list) and route_nodes
        else None
    )
    exact_ok, exact = (
        request_json(
            "GET",
            f"{base_url}/api/nodes/{urllib.parse.quote(str(first_node), safe='')}",
            timeout=10,
        )
        if first_node
        else (False, {"error": "route_returned_no_node"})
    )
    summary["checks"]["exact_read"] = {
        "ok": exact_ok and isinstance(exact, dict) and exact.get("id") == first_node,
        "node_id": first_node,
        "error": None if exact_ok else exact,
    }

    if args.exercise_writes:
        suffix = uuid.uuid4().hex[:10]
        node_a = f"DOC-public-rc-a-{suffix}"
        node_b = f"DOC-public-rc-b-{suffix}"
        node_c = f"DOC-public-rc-c-{suffix}"
        common_keyword = f"publicrcisolation{suffix}"

        def create_fixture(
            node_id: str,
            project_id: str,
            project_namespace: str,
        ) -> tuple[bool, Any]:
            return request_json(
                "POST",
                f"{base_url}/api/nodes?force=true",
                {
                    "id": node_id,
                    "name": f"Public RC fixture {node_id}",
                    "cluster": "public-rc",
                    "type": "reference",
                    "content": {
                        "description": f"{common_keyword} project isolation fixture",
                        "extra": {
                            "project_id": project_id,
                            "project_namespace": project_namespace,
                        },
                    },
                    "activation_keywords": [
                        common_keyword,
                        "public",
                        "release",
                        "isolation",
                        project_id,
                    ],
                },
                timeout=10,
            )

        create_a_ok, create_a = create_fixture(
            node_a,
            args.project_id,
            args.project_namespace,
        )
        other_project = f"{args.project_id}-other"
        other_namespace = f"{args.project_namespace}-other"
        create_b_ok, create_b = create_fixture(
            node_b,
            other_project,
            args.project_namespace,
        )
        create_c_ok, create_c = create_fixture(
            node_c,
            args.project_id,
            other_namespace,
        )
        isolated_ok, isolated = request_json(
            "POST",
            f"{base_url}/api/route",
            {
                "task": common_keyword,
                "max_nodes": 10,
                "mode": "skeleton",
                "agent_id": "verify-project-isolation",
                "project_id": args.project_id,
                "project_namespace": args.project_namespace,
                "confirm_low_confidence": True,
                "allow_degraded": True,
            },
            timeout=args.route_timeout,
        )
        isolated_ids = {
            str(item.get("id"))
            for item in (isolated.get("nodes") or [])
            if isinstance(item, dict)
        } if isinstance(isolated, dict) else set()
        summary["checks"]["project_isolation"] = {
            "ok": (
                create_a_ok
                and create_b_ok
                and create_c_ok
                and isolated_ok
                and node_a in isolated_ids
                and node_b not in isolated_ids
                and node_c not in isolated_ids
            ),
            "project_node": node_a,
            "excluded_nodes": [node_b, node_c],
            "returned_node_ids": sorted(isolated_ids),
            "errors": [
                value
                for ok, value in (
                    (create_a_ok, create_a),
                    (create_b_ok, create_b),
                    (create_c_ok, create_c),
                    (isolated_ok, isolated),
                )
                if not ok
            ],
        }

        write_ok, writeback = request_json(
            "POST",
            f"{base_url}/api/writeback",
            {
                "agent_id": "verify-project-writeback",
                "project_id": args.project_id,
                "project_namespace": args.project_namespace,
                "changes": [
                    {
                        "node_id": node_a,
                        "field": "notes",
                        "value": "public clean-clone writeback verified",
                    }
                ],
            },
            timeout=10,
        )
        readback_ok, readback = request_json(
            "GET",
            f"{base_url}/api/nodes/{urllib.parse.quote(node_a, safe='')}",
            timeout=10,
        )
        readback_notes = (
            (readback.get("content") or {}).get("notes")
            if isinstance(readback, dict)
            else None
        )
        summary["checks"]["writeback"] = {
            "ok": (
                write_ok
                and node_a in (writeback.get("updated") or [])
                and readback_ok
                and readback_notes == "public clean-clone writeback verified"
            ),
            "error": None if write_ok and readback_ok else [writeback, readback],
        }

        identity = {
            "project_id": args.project_id,
            "operation": "verify-project",
            "component": "verify-project",
            "error_type": "public-rc-failure",
        }
        fingerprint = deterministic_fingerprint(**identity)
        occurred_at = dt.datetime.now(dt.timezone.utc).isoformat()
        statuses = []
        error_results = []
        for ordinal in (1, 2):
            error_ok, error_result = request_json(
                "POST",
                f"{base_url}/api/errors/occurrences",
                {
                    "occurrence_id": f"public-rc-{suffix}-{ordinal}",
                    "fingerprint": fingerprint,
                    **identity,
                    "error": "public rc deterministic fixture failure",
                    "root_cause": "public rc fixture",
                    "occurred_at": occurred_at,
                    "agent_id": "verify-project-errors",
                },
                timeout=10,
            )
            error_results.append(error_result)
            statuses.append(
                str(error_result.get("status") or "")
                if error_ok and isinstance(error_result, dict)
                else ""
            )
        summary["checks"]["error_knowledge"] = {
            "ok": statuses == ["RECORDED", "PROMOTED"],
            "statuses": statuses,
            "errors": [item for item in error_results if isinstance(item, dict) and item.get("error")],
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
        and summary["checks"]["owner_intent"]["ok"]
        and summary["checks"]["exact_read"]["ok"]
        and (
            not args.exercise_writes
            or all(
                summary["checks"][name]["ok"]
                for name in ("project_isolation", "writeback", "error_knowledge")
            )
        )
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
        print(f"  owner_intent: ok={summary['checks']['owner_intent']['ok']}")
        print(f"  exact_read: ok={summary['checks']['exact_read']['ok']}")
        if args.exercise_writes:
            print(f"  project_isolation: ok={summary['checks']['project_isolation']['ok']}")
            print(f"  writeback: ok={summary['checks']['writeback']['ok']}")
            print(f"  error_knowledge: ok={summary['checks']['error_knowledge']['ok']}")
        print(f"  token_usage: ok={summary['checks']['token_usage']['ok']}")
        print(f"  result: {'PASS' if summary['ok'] else 'FAIL'}")

    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
