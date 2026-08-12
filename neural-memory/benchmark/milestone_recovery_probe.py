#!/usr/bin/env python3
"""Probe whether a clean Agent can recover one serious durable milestone.

This is a bounded acceptance client over the existing route and exact-node
surfaces.  It creates no new server-side knowledge owner or background job.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


SCHEMA = "3can.milestone-recovery-probe/v1"
_DIGEST = re.compile(r"^(?:sha256:)?([0-9a-f]{64})$", re.IGNORECASE)
_NON_DISCRIMINATIVE_FACTS = frozenset(
    {
        "active",
        "current",
        "done",
        "latest",
        "pass",
        "passed",
        "success",
        "true",
        "verified",
        "当前",
        "成功",
        "已验证",
    }
)


def _trusted_leaf_values(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [
            leaf
            for child in value.values()
            for leaf in _trusted_leaf_values(child)
        ]
    if isinstance(value, list):
        return [
            leaf
            for child in value
            for leaf in _trusted_leaf_values(child)
        ]
    if value is None:
        return []
    return [str(value)]


def _fact_matches_leaf(alternative: str, leaf: str) -> bool:
    expected = alternative.strip().casefold()
    actual = leaf.strip().casefold()
    if len(expected) < 3 or not actual:
        return False
    expected_digest = _DIGEST.fullmatch(expected)
    actual_digest = _DIGEST.fullmatch(actual)
    if expected_digest or actual_digest:
        return bool(
            expected_digest
            and actual_digest
            and expected_digest.group(1).casefold() == actual_digest.group(1).casefold()
        )
    if expected.isascii():
        return bool(
            re.search(
                rf"(?<![\w]){re.escape(expected)}(?![\w])",
                actual,
                flags=re.IGNORECASE,
            )
        )
    return expected in actual


def _request_json(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> tuple[bool, Any]:
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(
        base_url.rstrip("/") + path,
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - explicit local/user URL
            return True, json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            detail = {"error": str(exc)}
        return False, {"http_status": exc.code, "detail": detail}
    except (OSError, URLError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return False, {"error": type(exc).__name__, "message": str(exc)}


def _validate_fact_specs(
    facts: list[dict[str, Any]],
    expected_node_ids: set[str],
) -> None:
    for fact in facts:
        if not isinstance(fact, dict):
            raise ValueError("probe_fact_must_be_object")
        fact_id = str(fact.get("fact_id") or "").strip()
        node_id = str(fact.get("node_id") or "").strip()
        alternatives = fact.get("any_of")
        if (
            not fact_id
            or node_id not in expected_node_ids
            or not isinstance(alternatives, list)
            or not alternatives
        ):
            raise ValueError("probe_fact_requires_fact_id_node_id_and_any_of")
        if not all(
            isinstance(item, str) and len(item.strip()) >= 3
            for item in alternatives
        ):
            raise ValueError(f"probe_fact_alternative_invalid:{fact_id}")
        if any(
            item.strip().casefold() in _NON_DISCRIMINATIVE_FACTS
            for item in alternatives
        ):
            raise ValueError(f"probe_fact_alternative_not_discriminative:{fact_id}")


def _fact_results(
    facts: list[dict[str, Any]],
    node_payloads: dict[str, dict[str, Any]],
    expected_node_ids: set[str],
    *,
    fact_class: str,
) -> list[dict[str, Any]]:
    _validate_fact_specs(facts, expected_node_ids)
    results: list[dict[str, Any]] = []
    for fact in facts:
        fact_id = str(fact["fact_id"]).strip()
        node_id = str(fact["node_id"]).strip()
        alternatives = fact["any_of"]
        payload = node_payloads.get(node_id)
        content = payload.get("content") if isinstance(payload, dict) else None
        content = content if isinstance(content, dict) else {}
        content_fields = (
            (
                "description",
                "current_state",
                "blockers",
                "tech_stack",
                "key_files",
                "extra",
            )
            if fact_class == "critical"
            else ("description", "current_state", "key_files", "extra")
        )
        trusted_content = {key: content.get(key) for key in content_fields}
        trusted_payload = (
            {
                key: payload.get(key)
                for key in ("id", "name", "cluster", "layer", "type", "status", "priority")
            }
            if isinstance(payload, dict)
            else {}
        )
        trusted_payload["content"] = trusted_content
        trusted_leaves = (
            _trusted_leaf_values(trusted_payload) if payload is not None else []
        )
        matched = next(
            (
                item
                for item in alternatives
                if any(_fact_matches_leaf(item, leaf) for leaf in trusted_leaves)
            ),
            None,
        )
        results.append(
            {
                "fact_id": fact_id,
                "node_id": node_id,
                "recovered": matched is not None,
                "matched": matched,
            }
        )
    return results


def run_probe(
    spec: dict[str, Any],
    *,
    base_url: str,
    request_json: Callable[..., tuple[bool, Any]] = _request_json,
) -> dict[str, Any]:
    if spec.get("schema") != SCHEMA:
        raise ValueError("probe_schema_unsupported")
    probe_id = str(spec.get("probe_id") or "").strip()
    task = str(spec.get("task") or "").strip()
    agent_id = str(spec.get("agent_id") or "").strip()
    expected_node_ids = spec.get("expected_node_ids")
    if not probe_id or not task or not agent_id:
        raise ValueError("probe_id_task_agent_required")
    if (
        not isinstance(expected_node_ids, list)
        or not expected_node_ids
        or not all(isinstance(item, str) and item for item in expected_node_ids)
    ):
        raise ValueError("probe_expected_node_ids_required")
    expected_graph_root_sha256 = str(
        spec.get("expected_graph_root_sha256") or ""
    )
    if (
        len(expected_graph_root_sha256) != 64
        or any(ch not in "0123456789abcdef" for ch in expected_graph_root_sha256)
    ):
        raise ValueError("probe_expected_graph_root_sha256_required")
    expected_readiness = str(spec.get("expected_readiness") or "").strip()
    if expected_readiness not in {"development", "production"}:
        raise ValueError("probe_expected_readiness_invalid")
    critical_facts = spec.get("critical_facts")
    evidence_facts = spec.get("evidence_facts")
    if not isinstance(critical_facts, list) or not critical_facts:
        raise ValueError("probe_critical_facts_required")
    if not isinstance(evidence_facts, list) or not evidence_facts:
        raise ValueError("probe_evidence_facts_required")
    expected_node_id_set = set(expected_node_ids)
    _validate_fact_specs(critical_facts, expected_node_id_set)
    _validate_fact_specs(evidence_facts, expected_node_id_set)

    stats_ok, stats = request_json(
        base_url,
        "/api/stats?deep=true",
        timeout=float(spec.get("timeout_seconds", 60.0)),
    )
    if not stats_ok or not isinstance(stats, dict):
        return {
            "schema": SCHEMA,
            "probe_id": probe_id,
            "status": "PARTIAL",
            "reason": "deep_readiness_unavailable",
            "stats_response": stats,
        }
    runtime_identity = stats.get("runtime_identity") or {}
    readiness = stats.get("readiness") or {}
    graph_identity_matches = (
        isinstance(runtime_identity, dict)
        and runtime_identity.get("schema") == "3can.runtime-identity/v1"
        and runtime_identity.get("graph_root_sha256")
        == expected_graph_root_sha256
    )
    readiness_matches = (
        isinstance(readiness, dict)
        and readiness.get("schema") == "3can.production-readiness/v1"
        and readiness.get("mode") == expected_readiness
        and readiness.get(
            (
                "production_ready"
                if expected_readiness == "production"
                else "development_ready"
            )
        ) is True
    )
    if not graph_identity_matches or not readiness_matches:
        return {
            "schema": SCHEMA,
            "probe_id": probe_id,
            "status": "PARTIAL",
            "reason": "runtime_binding_mismatch",
            "expected_graph_root_sha256": expected_graph_root_sha256,
            "runtime_identity": runtime_identity,
            "readiness": readiness,
        }

    route_payload: dict[str, Any] = {
        "task": task,
        "agent_id": agent_id,
        "mode": "skeleton",
        "max_nodes": int(spec.get("max_nodes", 10)),
        "include_edges": True,
    }
    for key in (
        "session_instance_id",
        "project_id",
        "project_namespace",
        "workspace_id",
        "workorder_id",
    ):
        value = spec.get(key)
        if value:
            route_payload[key] = value

    route_ok, route_response = request_json(
        base_url,
        "/api/route",
        method="POST",
        payload=route_payload,
        timeout=float(spec.get("timeout_seconds", 60.0)),
    )
    if not route_ok or not isinstance(route_response, dict):
        return {
            "schema": SCHEMA,
            "probe_id": probe_id,
            "status": "PARTIAL",
            "reason": "route_unavailable",
            "route_response": route_response,
        }

    if (
        route_response.get("route_response_schema") != "3can.route-response/v1"
        or route_response.get("mode") != "skeleton"
        or not isinstance(route_response.get("nodes"), list)
    ):
        return {
            "schema": SCHEMA,
            "probe_id": probe_id,
            "status": "PARTIAL",
            "reason": "route_contract_invalid",
            "route_response": route_response,
        }
    activated = route_response["nodes"]
    routed_ids = [
        str(item.get("id"))
        for item in activated
        if isinstance(item, dict) and item.get("id")
    ]
    missing_nodes = [node_id for node_id in expected_node_ids if node_id not in routed_ids]
    read_nodes: dict[str, dict[str, Any]] = {}
    read_failures: list[dict[str, Any]] = []
    for node_id in expected_node_ids:
        if node_id not in routed_ids:
            continue
        ok, response = request_json(
            base_url,
            f"/api/nodes/{quote(node_id, safe='')}",
            timeout=float(spec.get("timeout_seconds", 60.0)),
        )
        if (
            ok
            and isinstance(response, dict)
            and str(response.get("id") or "") == node_id
        ):
            read_nodes[node_id] = response
        else:
            read_failures.append({"node_id": node_id, "response": response})

    fact_results = _fact_results(
        critical_facts,
        read_nodes,
        expected_node_id_set,
        fact_class="critical",
    )
    evidence_results = _fact_results(
        evidence_facts,
        read_nodes,
        expected_node_id_set,
        fact_class="evidence",
    )
    recovered = (
        not missing_nodes
        and not read_failures
        and all(item["recovered"] for item in fact_results)
        and all(item["recovered"] for item in evidence_results)
    )
    return {
        "schema": SCHEMA,
        "probe_id": probe_id,
        "status": "PASS" if recovered else "PARTIAL",
        "route_id": (route_response.get("route_meta") or {}).get("route_id"),
        "runtime_identity": runtime_identity,
        "readiness": readiness,
        "routed_node_ids": routed_ids,
        "expected_node_ids": expected_node_ids,
        "missing_node_ids": missing_nodes,
        "read_node_ids": list(read_nodes),
        "read_failures": read_failures,
        "critical_facts": fact_results,
        "evidence_facts": evidence_results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify clean-Agent recovery of a serious 3CAN milestone."
    )
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:9700")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    try:
        spec = json.loads(args.spec.read_text(encoding="utf-8"))
        result = run_probe(spec, base_url=args.base_url)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        result = {
            "schema": SCHEMA,
            "status": "BLOCKED",
            "reason": type(exc).__name__,
            "message": str(exc),
        }

    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_name(
            f".{args.output.name}.{hashlib.sha256(rendered.encode('utf-8')).hexdigest()[:12]}.tmp"
        )
        temporary.write_text(rendered + "\n", encoding="utf-8")
        temporary.replace(args.output)
    print(rendered)
    return 0 if result.get("status") == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
