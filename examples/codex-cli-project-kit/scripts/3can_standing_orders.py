#!/usr/bin/env python3
"""3CAN standing-orders wrapper.

This is a Codex-side harness helper. It does not replace 3CAN engine state and
does not enforce edits by itself; it produces explicit pass/warn/block decisions
that agents can record before mutating files.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOOP_STATE = PROJECT_ROOT / "test-results" / "3can" / "loop_signatures.json"
DEFAULT_ENGINE_ROOT = Path(os.environ.get("THREECAN_ENGINE_ROOT", PROJECT_ROOT / "docs" / "specs" / "3CAN_ENGINE" / "_release_staging_3CAN-engine" / "neural-memory")).expanduser()
MAX_HISTORICAL_DOC_BYTES = 200_000


@dataclass(frozen=True)
class RiskRule:
    risk: str
    reason: str
    pattern: re.Pattern[str]
    applies_to: str = "both"  # path | text | both


RISK_RULES: tuple[RiskRule, ...] = (
    RiskRule(
        "secret_or_private_runtime",
        "Secrets, cookies, credentials, recovery codes, .env files, and private runtime logs require approval.",
        re.compile(r"(^|[/\\])(\.env($|\.)|.*(secret|cookie|credential|api[-_]?key|access[-_]?token|refresh[-_]?token|recovery[-_ ]?code|private[-_ ]?runtime[-_ ]?log).*)", re.I),
    ),
    RiskRule(
        "destructive_delete",
        "File deletion or destructive cleanup requires explicit approval.",
        re.compile(r"\b(delete|remove|rm\s+-|unlink|clean|reset\s+--hard|git\s+clean)\b", re.I),
        "text",
    ),
    RiskRule(
        "db_schema_or_migration",
        "Database schema changes and migrations require approval.",
        re.compile(r"(migrations?|alembic|schema\.sql|db_schema|database schema|\bALTER\s+TABLE\b|\bCREATE\s+TABLE\b|\bDROP\s+TABLE\b)", re.I),
    ),
    RiskRule(
        "interface_contract",
        "Interface/API contracts require approval before mutation.",
        re.compile(r"(api[_-]?contract|openapi|swagger|interface contract|鎺ュ彛濂戠害|INTF-|contract\.py|contracts?/)", re.I),
    ),
    RiskRule(
        "real_api_cost",
        "Real or paid API consumption requires approval.",
        re.compile(r"(real api|鐪熷疄\s*api|paid api|浠樿垂|apimart|runninghub|volcengine|seedance|veo|kling|happyhorse|openai|doubao|deepseek)", re.I),
        "text",
    ),
    RiskRule(
        "public_publish",
        "Public posting, publishing, or external release requires approval.",
        re.compile(r"(public post|public publish|鍏紑鍙戝竷|鍙戝竷鍒皘publish|posting|social post|github pr|push to github)", re.I),
        "text",
    ),
    RiskRule(
        "real_store_data_write",
        "Writing real merchant/store/account data requires approval.",
        re.compile(r"(鐪熷疄搴楅摵|鐪熷疄鍟嗗|搴楅摵鏁版嵁鍐欏叆|real store|merchant data write|account data write|production shop)", re.I),
    ),
)


ALLOWED_LOOP_STATUSES = {"recorded_failure", "stop_and_diagnose"}

PR_SCOPE_RULES: tuple[RiskRule, ...] = (
    RiskRule(
        "env_or_secret_file",
        "Do not include .env, secret, cookie, credential, API-key, access-token, refresh-token, or recovery-code paths in a harness PR.",
        re.compile(r"(^|[/\\])(\.env($|\.)|.*(secret|cookie|credential|api[-_]?key|access[-_]?token|refresh[-_]?token|recovery[-_ ]?code).*)", re.I),
        "path",
    ),
    RiskRule(
        "database_artifact",
        "Do not include database artifacts such as data/*.db or SQLite runtime files in a harness PR.",
        re.compile(r"(^|[/\\])data[/\\].*\.(db|sqlite|sqlite3)$", re.I),
        "path",
    ),
    RiskRule(
        "runtime_log",
        "Do not include runtime logs in a harness PR.",
        re.compile(r"(^|[/\\])(logs?|test-results[/\\]3can)[/\\].*|.*\.(log|jsonl)$", re.I),
        "path",
    ),
    RiskRule(
        "huge_historical_doc",
        "Huge historical docs should stay out of a focused harness PR unless explicitly selected.",
        re.compile(r"(^|[/\\])docs[/\\]specs[/\\](handoffs|_archive|.*history.*|.*historical.*)[/\\].*|(^|[/\\])docs[/\\]specs[/\\].*\.(pdf|pptx)$", re.I),
        "path",
    ),
)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _print_json(data: Any) -> None:
    sys.stdout.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _safe_load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _safe_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _norm_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def evaluate_standing_orders(
    *,
    action: str,
    target_files: list[str],
    summary: str = "",
    approval_id: str = "",
) -> dict[str, Any]:
    """Return pass/warn/block for a planned action."""
    target_blob = "\n".join(target_files)
    text_blob = f"{action}\n{summary}"
    risks: list[dict[str, str]] = []

    for rule in RISK_RULES:
        hit = False
        if rule.applies_to in ("path", "both") and rule.pattern.search(target_blob):
            hit = True
        if rule.applies_to in ("text", "both") and rule.pattern.search(text_blob):
            hit = True
        if hit:
            risks.append({"risk": rule.risk, "reason": rule.reason})

    missing_targets = not target_files and action in {"edit-start", "edit-done"}
    if missing_targets:
        risks.append({
            "risk": "missing_target_files",
            "reason": "Edit actions should name the target files for ticket/activity traceability.",
        })

    approval_required = any(r["risk"] != "missing_target_files" for r in risks)
    if approval_required and not approval_id:
        status = "block"
    elif risks:
        status = "warn"
    else:
        status = "pass"

    return {
        "ok": status != "block",
        "status": status,
        "action": action,
        "approval_required": approval_required,
        "approval_id": approval_id,
        "risks": risks,
        "standing_orders": [
            "route_before_code_change",
            "check_ERR_on_failure",
            "check_INTF_before_contract_change",
            "writeback_after_stage",
            "tests_and_uat_note_for_new_module",
        ],
    }


def _normalize_pr_path(path: str) -> str:
    return path.strip().replace("\\", "/")


def evaluate_pr_scope(paths: list[str], *, project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    """Return a focused PR file-list recommendation and risky path findings."""
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        path = _normalize_pr_path(raw)
        if not path or path in seen:
            continue
        seen.add(path)
        normalized.append(path)

    findings: list[dict[str, str]] = []
    suggested: list[str] = []
    rejected: list[str] = []

    for path in normalized:
        path_findings: list[dict[str, str]] = []
        for rule in PR_SCOPE_RULES:
            if rule.pattern.search(path):
                path_findings.append({"risk": rule.risk, "reason": rule.reason})

        candidate = project_root / path
        if path.lower().startswith("docs/") and candidate.exists():
            try:
                if candidate.is_file() and candidate.stat().st_size > MAX_HISTORICAL_DOC_BYTES:
                    path_findings.append({
                        "risk": "huge_historical_doc",
                        "reason": f"Doc is larger than {MAX_HISTORICAL_DOC_BYTES} bytes and should not ride along in a focused harness PR.",
                    })
            except OSError:
                pass

        if path_findings:
            rejected.append(path)
            for finding in path_findings:
                findings.append({"path": path, **finding})
        else:
            suggested.append(path)

    status = "block" if findings else ("warn" if not suggested else "pass")
    return {
        "ok": status != "block",
        "status": status,
        "checked_count": len(normalized),
        "suggested_pr_files": suggested,
        "rejected_files": rejected,
        "risks": findings,
        "notes": [
            "Keep the PR scoped to wrapper, tests, and concise docs for this harness hardening pass.",
            "Secrets, DB artifacts, runtime logs, and huge historical docs should be excluded from the PR.",
        ],
    }


def make_error_signature(command: str, target_files: list[str], error_text: str) -> dict[str, str]:
    normalized_error = _norm_text(error_text)[:2000]
    normalized_command = _norm_text(command)
    normalized_files = sorted(_norm_text(p) for p in target_files)
    payload = json.dumps(
        {"command": normalized_command, "target_files": normalized_files, "error": normalized_error},
        ensure_ascii=False,
        sort_keys=True,
    )
    return {
        "hash": hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16],
        "command": normalized_command,
        "target_files": normalized_files,
        "error_excerpt": normalized_error[:300],
    }


def record_loop_failure(
    *,
    state_file: Path,
    command: str,
    target_files: list[str],
    error_text: str,
    threshold: int = 2,
) -> dict[str, Any]:
    sig = make_error_signature(command, target_files, error_text)
    state = _safe_load_json(state_file, {"signatures": {}})
    signatures = state.setdefault("signatures", {})
    item = signatures.setdefault(sig["hash"], {
        "count": 0,
        "command": sig["command"],
        "target_files": sig["target_files"],
        "error_excerpt": sig["error_excerpt"],
        "first_failed_at": _now(),
    })
    item["count"] = int(item.get("count") or 0) + 1
    item["last_failed_at"] = _now()
    item["error_excerpt"] = sig["error_excerpt"]
    _safe_write_json(state_file, state)

    loop_status = "stop_and_diagnose" if item["count"] >= threshold else "recorded_failure"
    return {
        "ok": loop_status != "stop_and_diagnose",
        "status": "block" if loop_status == "stop_and_diagnose" else "warn",
        "loop_status": loop_status,
        "signature": sig,
        "count": item["count"],
        "threshold": threshold,
        "diagnosis_required": loop_status == "stop_and_diagnose",
        "state_file": str(state_file),
    }


def _json_request(base_url: str, path: str, timeout: float = 4.0) -> tuple[bool, Any]:
    try:
        req = Request(f"{base_url}{path}", headers={"Accept": "application/json"})
        with urlopen(req, timeout=timeout) as response:
            return True, json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        return False, {"error": str(exc)}


def health_summary(base_url: str, engine_root: Path = DEFAULT_ENGINE_ROOT) -> dict[str, Any]:
    stats_ok, stats = _json_request(base_url, "/api/stats")
    agents_ok, agents = _json_request(base_url, "/api/agents")
    skills_ok, skills = _json_request(base_url, "/api/skills")
    activity_ok, _activity = _json_request(base_url, "/api/activity?limit=1")
    health_ok, health = _json_request(base_url, "/api/health/scan")

    node_count = None
    valid_engine_root = (engine_root / "backend" / "app.py").exists() and (engine_root / "proxy" / "server.py").exists()
    nodes_dir = engine_root / "graph" / "nodes"
    if nodes_dir.exists():
        node_count = sum(1 for _ in nodes_dir.glob("*.json"))

    return {
        "ok": stats_ok and bool(stats.get("total_nodes", 0) >= 100 if stats_ok else False),
        "base_url": base_url,
        "engine_root": str(engine_root),
        "engine_root_valid": valid_engine_root,
        "engine_root_node_count": node_count,
        "total_nodes": stats.get("total_nodes") if stats_ok else None,
        "agents_available": agents_ok,
        "agent_count": len(agents) if isinstance(agents, list) else None,
        "route_available": stats_ok,
        "writeback_available": activity_ok,
        "task_ledger_available": True,
        "approval_gate_status": "wrapper_available",
        "skills_available": skills_ok,
        "skill_count": skills.get("total") if isinstance(skills, dict) else None,
        "graph_health_available": health_ok,
        "graph_health_score": health.get("health_score") if isinstance(health, dict) else None,
        "notes": [
            "route_available is inferred from live stats; this command avoids POST /api/route side effects.",
            "writeback_available is inferred from readable activity endpoint; this command avoids mutating writeback.",
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="3CAN standing-orders, approval, loop, and health helper.")
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ("check", "edit-start", "edit-done"):
        p = sub.add_parser(name)
        p.add_argument("--target-file", dest="target_files", action="append", default=[])
        p.add_argument("--summary", default="")
        p.add_argument("--approval-id", default="")

    fail = sub.add_parser("edit-fail")
    fail.add_argument("--command", dest="failed_command", required=True)
    fail.add_argument("--target-file", dest="target_files", action="append", default=[])
    fail.add_argument("--error-text", required=True)
    fail.add_argument("--state-file", default=str(DEFAULT_LOOP_STATE))
    fail.add_argument("--threshold", type=int, default=2)

    health = sub.add_parser("health")
    health.add_argument("--base-url", default="http://127.0.0.1:9700")
    health.add_argument("--engine-root", default=str(DEFAULT_ENGINE_ROOT))

    pr_scope = sub.add_parser("pr-scope")
    pr_scope.add_argument("--file", dest="files", action="append", default=[])
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command in {"check", "edit-start", "edit-done"}:
        result = evaluate_standing_orders(
            action=args.command,
            target_files=args.target_files,
            summary=args.summary,
            approval_id=args.approval_id,
        )
        _print_json(result)
        return 0 if result["status"] != "block" else 2

    if args.command == "edit-fail":
        result = record_loop_failure(
            state_file=Path(args.state_file),
            command=args.failed_command,
            target_files=args.target_files,
            error_text=args.error_text,
            threshold=args.threshold,
        )
        _print_json(result)
        return 0 if result["loop_status"] in ALLOWED_LOOP_STATUSES else 1

    if args.command == "health":
        result = health_summary(args.base_url, Path(args.engine_root))
        _print_json(result)
        return 0 if result["ok"] else 1

    if args.command == "pr-scope":
        result = evaluate_pr_scope(args.files)
        _print_json(result)
        return 0 if result["status"] != "block" else 2

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
