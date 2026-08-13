from __future__ import annotations

import asyncio
import concurrent.futures
import datetime as dt
import hashlib
import hmac
import importlib.util
import json
import multiprocessing
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"


def load_app():
    sys.path.insert(0, str(BACKEND))
    spec = importlib.util.spec_from_file_location("ticket_app_under_test", BACKEND / "app.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["ticket_app_under_test"] = module
    spec.loader.exec_module(module)
    return module


def _import_ledger(backend: str):
    if backend not in sys.path:
        sys.path.insert(0, backend)
    from ticket_ledger import TicketLedger, canonical_hash

    return TicketLedger, canonical_hash


def _process_issue(backend: str, database: str, suffix: str) -> str:
    TicketLedger, _canonical_hash = _import_ledger(backend)
    ledger = TicketLedger(database)
    ticket = {
        "ticket_id": f"rt_{suffix}",
        "lease_key": "shared-process-lease",
        "agent_id": "agent-process",
        "project_id": "project",
        "workspace_id": "workspace",
        "workorder_id": "workorder",
        "issued_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "ttl_sec": 900,
        "target_digest": "target",
        "scope_digest": "scope",
        "policy_version": "policy/v2",
        "allowed_error_ids": [],
    }
    return ledger.issue(ticket)[0]["ticket_id"]


def _process_consume(backend: str, database: str, ticket_id: str, suffix: str) -> int:
    TicketLedger, _canonical_hash = _import_ledger(backend)
    ledger = TicketLedger(database)
    ticket = ledger.consume(
        ticket_id,
        agent_id="agent-process",
        target_digest="target",
        scope_digest="scope",
        consumed={"tool_name": "worker", "tool_input_summary": suffix},
    )
    return int(ticket["consume_count"])


def _process_complete(backend: str, database: str, ticket_id: str) -> str:
    TicketLedger, canonical_hash = _import_ledger(backend)
    from ticket_ledger import LedgerError

    ledger = TicketLedger(database)
    request = {"ticket_id": ticket_id, "agent_id": "agent-process", "done": True}
    request_hash = canonical_hash(request)
    try:
        begun = ledger.begin_completion(
            ticket_id,
            agent_id="agent-process",
            request_hash=request_hash,
            request=request,
            requested_error_ids=[],
        )
        if begun["mode"] == "replay":
            return "replay"
        ledger.complete(
            ticket_id,
            request_hash=request_hash,
            owner_token=begun["owner_token"],
            response={"ok": True, "ticket_id": ticket_id},
        )
        return "completed"
    except LedgerError as exc:
        return exc.code


class FakeEngine:
    def __init__(self, app_module):
        self.app = app_module
        self.nodes = {}
        self.edges = []
        self.activity_log = []
        self.fail_create_edge_once = False

    @staticmethod
    def run_consistent(operation, /, *args, **kwargs):
        return operation(*args, **kwargs)

    def get_node(self, node_id):
        return self.nodes.get(node_id)

    def create_node(self, req, *, internal_owner=None):
        del internal_owner
        from models import Node

        now = dt.datetime.now(dt.timezone.utc).isoformat()
        node = Node(
            id=req.id,
            name=req.name,
            cluster=req.cluster,
            layer=req.layer,
            type=req.type,
            status=req.status,
            content=req.content,
            activation_keywords=req.activation_keywords,
            priority=req.priority,
            created_at=now,
            updated_at=now,
            updated_by=req.primary_author,
            primary_author=req.primary_author,
        )
        self.nodes[node.id] = node
        return node

    def update_node(self, node_id, req, *, internal_owner=None):
        del internal_owner
        node = self.nodes.get(node_id)
        if not node:
            return None
        update = req.model_dump(exclude_none=True)
        for key, value in update.items():
            if key == "content" and isinstance(value, dict):
                node.content = self.app.NodeContent(**value)
            else:
                setattr(node, key, value)
        return node

    def create_edge(self, req, *, internal_owner=None):
        del internal_owner
        from models import Edge

        if self.fail_create_edge_once:
            self.fail_create_edge_once = False
            raise OSError("injected edge-store failure")
        key = (req.source, req.target, str(getattr(req.type, "value", req.type)))
        for edge in self.edges:
            existing = (
                edge.source,
                edge.target,
                str(getattr(edge.type, "value", edge.type)),
            )
            if existing == key:
                return edge
        edge = Edge(
            source=req.source,
            target=req.target,
            type=req.type,
            weight=req.weight,
            description=req.description,
        )
        self.edges.append(edge)
        return edge

    def log_activity(self, agent_id, action, detail="", affected_nodes=None, meta=None):
        body = json.dumps(
            {
                "index": len(self.activity_log),
                "agent_id": agent_id,
                "action": action,
                "detail": detail,
                "affected": affected_nodes or [],
                "meta": meta or {},
            },
            sort_keys=True,
        )
        entry = SimpleNamespace(
            timestamp=dt.datetime.now(dt.timezone.utc).isoformat(),
            agent_id=agent_id,
            action=action,
            detail=detail,
            affected_nodes=affected_nodes or [],
            meta=meta or {},
            self_hash=hashlib.sha256(body.encode()).hexdigest(),
        )
        entry.model_copy = lambda *, deep=False: entry
        self.activity_log.append(entry)
        return entry


def _canonical_case(app, *, component="ticket-ledger", error_type="scope-mismatch"):
    fingerprint = app.deterministic_fingerprint(
        project_id="project",
        operation="edit",
        component=component,
        error_type=error_type,
    )
    case_id = f"ERR-case-{fingerprint.split(':', 1)[1][:24]}"
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    payload = {
        "schema_version": "3can.error-case/v1",
        "case_id": case_id,
        "fingerprint": fingerprint,
        "fingerprint_version": "ek2",
        "project_id": "project",
        "operation": "edit",
        "component": component,
        "error_type": error_type,
        "root_cause": "scope was not frozen",
        "applicability": {
            "project_id": "project",
            "operation": "edit",
            "component": component,
            "error_type": error_type,
            "fingerprint_version": "ek2",
        },
        "state": "observed",
        "blocking": True,
        "occurrence_count": 2,
        "first_seen_at": now,
        "last_seen_at": now,
        "promoted_at": now,
        "state_changed_at": now,
        "diagnosis": None,
        "diagnosed_by": None,
        "diagnosed_at": None,
        "mitigation": None,
        "mitigated_by": None,
        "mitigated_at": None,
        "active_resolution": None,
        "resolution_history": [],
        "regression_count": 0,
        "superseded_by": None,
        "metadata": {},
    }
    return payload


@pytest.fixture
def ticket_runtime(tmp_path, monkeypatch):
    app = load_app()
    from models import Node

    fake = FakeEngine(app)
    case = _canonical_case(app)
    error_node = Node(
        id=case["case_id"],
        name="Canonical repeated test error",
        cluster="ErrorKnowledge",
        layer="L1",
        type=app.NodeType.feedback,
        status=app.NodeStatus.blocked,
        content=app.NodeContent(
            description="A canonical ErrorCase",
            current_state="observed",
            blockers=["exact promoted ErrorCase"],
            extra={
                "error_case": case,
                "error_knowledge_schema_version": "3can.error-knowledge/v2",
                "case_status": "observed",
                "occurrence_count": 2,
                "fingerprint": case["fingerprint"],
            },
        ),
        activation_keywords=["ticket-ledger", "scope-mismatch"],
    )
    fake.nodes[error_node.id] = error_node
    monkeypatch.setattr(app, "engine", fake)
    monkeypatch.setattr(app, "_ROUTE_TICKETS_FILE", tmp_path / "route_tickets.json")
    monkeypatch.setattr(
        app,
        "_ROUTE_TICKET_RECEIPTS_FILE",
        tmp_path / "route_ticket_receipts.jsonl",
    )
    monkeypatch.setattr(app, "_TICKET_LEDGER_PATH", tmp_path / "ticket_ledger.sqlite3")
    monkeypatch.setattr(app, "_TICKET_LEDGER_INSTANCE", None)
    monkeypatch.setenv("THREECAN_EVIDENCE_ROOTS", str(tmp_path))
    monkeypatch.setenv("THREECAN_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("THREECAN_READINESS_MODE", "development")
    monkeypatch.setenv("THREECAN_ALLOW_UNTICKETED_ERROR_OCCURRENCES", "1")
    monkeypatch.setenv(
        "THREECAN_EVIDENCE_HMAC_KEY",
        "public-test-only-signing-key-32-bytes-minimum",
    )

    async def fake_route(_req):
        return SimpleNamespace(
            activated_nodes=[error_node],
            relevant_edges=[],
            scores={error_node.id: 1.0},
            total_nodes=1,
            total_edges=0,
            route_meta={},
        )

    monkeypatch.setattr(app, "_route_in_worker", fake_route)
    monkeypatch.setattr(
        app,
        "_verified_project_target_root",
        lambda _target, **_context: tmp_path,
    )
    return app, fake, error_node, tmp_path


def _issue(
    app,
    *,
    task="Fix deterministic ticket lifecycle",
    project_id="project",
    project_namespace="project",
):
    return asyncio.run(app.issue_route_ticket({
        "agent_id": "agent-a",
        "project_id": project_id,
        "project_namespace": project_namespace,
        "workspace_id": "workspace",
        "workorder_id": "workorder",
        "task_description": task,
        "target_files": [str(app._default_project_dir() / "backend" / "app.py")],
        "scope_keywords": ["ticket", "error"],
        "task_type": "Edit",
    }))


async def _asgi_post(
    app,
    path: str,
    payload: dict,
    *,
    drain: bool = True,
    headers: dict[str, str] | None = None,
):
    transport = httpx.ASGITransport(app=app.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://3can.test",
    ) as client:
        response = await client.post(path, json=payload, headers=headers)
    if drain:
        await app._drain_automatic_error_observer()
    return response


async def _asgi_get(app, path: str):
    transport = httpx.ASGITransport(app=app.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://3can.test",
    ) as client:
        response = await client.get(path)
    await app._drain_automatic_error_observer()
    return response


def test_target_manifest_accepts_only_capsule_bound_git_worktree(
    tmp_path,
    monkeypatch,
):
    app = load_app()
    authority_root = tmp_path / "authority"
    authority_root.mkdir()
    repo = tmp_path / "linked-worktree"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "remote",
            "add",
            "origin",
            "https://github.com/public/example.git",
        ],
        check=True,
        capture_output=True,
    )
    capsule_dir = repo / ".agents"
    capsule_dir.mkdir()
    (capsule_dir / "project.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "project_id": "public-demo",
                "project_namespace": "public-demo",
                "project_root": ".",
                "git_repository": "github.com/public/example",
            }
        ),
        encoding="utf-8",
    )
    target = repo / "backend" / "app.py"
    target.parent.mkdir()
    target.write_text("value = 1\n", encoding="utf-8")
    common_dir = Path(
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        ).stdout.strip()
    )
    workspace_id = (
        f"git-{app._local_path_sha256(common_dir)[:12]}-"
        f"{app._local_path_sha256(repo)[:12]}"
    )
    monkeypatch.setenv("THREECAN_PROJECT_DIR", str(authority_root))
    monkeypatch.delenv("THREECAN_TARGET_ROOTS", raising=False)

    manifest = app._target_state_manifest(
        [str(target)],
        project_id="public-demo",
        project_namespace="public-demo",
        workspace_id=workspace_id,
    )

    assert manifest[0]["kind"] == "file"
    assert str(repo) not in manifest[0]["path"]
    windows_path = "C:" + "/".join(("", "Project", "backend", "app.py"))
    wsl_path = "/" + "/".join(("mnt", "c", "Project", "backend", "app.py"))
    assert app._canonical_physical_path(windows_path) == app._canonical_physical_path(
        wsl_path
    )
    if sys.platform == "win32":
        target_wire = str(target).replace("\\", "/")
        target_drive, target_tail = target_wire.split(":/", 1)
        wsl_target = "/" + "/".join(
            ("mnt", target_drive.casefold(), target_tail.lstrip("/"))
        )
        assert app._target_state_manifest(
            [wsl_target],
            project_id="public-demo",
            project_namespace="public-demo",
            workspace_id=workspace_id,
        ) == manifest
    with pytest.raises(HTTPException) as relative:
        app._target_state_manifest(
            ["backend/app.py"],
            project_id="public-demo",
            project_namespace="public-demo",
            workspace_id=workspace_id,
        )
    assert relative.value.detail["error"] == "bound_target_path_must_be_absolute"
    with pytest.raises(HTTPException) as mismatch:
        app._target_state_manifest(
            [str(target)],
            project_id="public-demo",
            project_namespace="public-demo",
            workspace_id="git-wrong-worktree",
        )
    assert mismatch.value.status_code == 403
    assert mismatch.value.detail["error"] == "project_target_identity_unverified"


def test_ticket_namespace_is_part_of_scope_and_lease_identity():
    app = load_app()
    common = {
        "project_id": "public-demo",
        "workspace_id": "git-family-worktree",
        "workorder_id": "WO-public",
        "task_type": "Edit",
        "scope_keywords": ["ticket"],
        "target_digest": "a" * 64,
    }

    left = app._ticket_scope_digest(project_namespace="namespace-a", **common)
    right = app._ticket_scope_digest(project_namespace="namespace-b", **common)

    assert left != right


def _consume(app, ticket):
    return asyncio.run(app.consume_route_ticket(ticket["ticket_id"], {
        "agent_id": "agent-a",
        "target_digest": ticket["target_digest"],
        "scope_digest": ticket["scope_digest"],
        "tool_name": "apply_patch",
        "tool_input_summary": "update ticket lifecycle",
    }))


def _artifact_evidence(path: Path, ticket):
    attestation = {
        "schema_version": "3can.verification-attestation/v1",
        "kind": "test_result",
        "verifier": "pytest",
        "ticket_id": ticket["ticket_id"],
        "target_digest": ticket["target_digest"],
        "scope_digest": ticket["scope_digest"],
        "command": "pytest neural-memory/tests -q",
        "exit_code": 0,
        "outcome": "passed",
    }
    attestation["signature"] = "hmac-sha256:" + hmac.new(
        b"public-test-only-signing-key-32-bytes-minimum",
        json.dumps(
            attestation,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    path.write_text(
        json.dumps(attestation, sort_keys=True),
        encoding="utf-8",
    )
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return [{
        "kind": "test_result",
        "ref": str(path),
        "digest": f"sha256:{digest}",
        "verified": True,
        "verifier": "pytest",
        "summary": "focused test artifact",
    }]


def _completion_payload(ticket, error_id, evidence, **overrides):
    payload = {
        "agent_id": "agent-a",
        "action": "done",
        "detail": "resolved after focused tests",
        "affected_nodes": [error_id],
        "meta": {"test": True},
        "ticket_id": ticket["ticket_id"],
        "resolved_errors": [error_id],
        "root_cause": "scope was not frozen",
        "solution_summary": "persist scoped authorization and verified evidence",
        "verification_evidence": evidence,
        "fixed_in": "backend/app.py",
    }
    payload.update(overrides)
    return payload


def test_issue_reuses_lease_and_receipts_freeze_authorization(ticket_runtime):
    app, fake, error_node, _tmp_path = ticket_runtime

    first = _issue(app)
    second = _issue(app)

    assert first["ticket_id"] == second["ticket_id"]
    assert first["reused"] is False
    assert second["reused"] is True
    assert first["allowed_error_ids"] == [error_node.id]
    # The routed node is similar/advisory because route_meta did not prove an
    # exact identity. It must not become a completion blocker.
    assert first["required_error_disposition_ids"] == []
    events = app._ticket_ledger().events(first["ticket_id"])
    assert [event["event"] for event in events] == ["issued"]
    receipt = events[0]
    assert receipt["project_id"] == "project"
    assert receipt["workspace_id"] == "workspace"
    assert receipt["workorder_id"] == "workorder"
    assert receipt["target_digest"] == first["target_digest"]
    assert receipt["policy_version"] == "3can.ticket-policy/v2"
    assert receipt["allowed_error_ids"] == [error_node.id]
    assert [entry.action for entry in fake.activity_log] == ["ticket_issued"]


def test_only_exact_unresolved_error_requires_completion_disposition(
    ticket_runtime,
    monkeypatch,
):
    app, fake, error_node, _tmp_path = ticket_runtime

    async def exact_route(_req):
        return SimpleNamespace(
            activated_nodes=[error_node],
            relevant_edges=[],
            scores={error_node.id: 1.0},
            total_nodes=1,
            total_edges=0,
            route_meta={
                "error_route_policy": {
                    "exact_error_case_ranking": {
                        "match_kinds": {
                            error_node.id: "canonical_identity",
                        }
                    }
                }
            },
        )

    monkeypatch.setattr(app, "_route_in_worker", exact_route)
    ticket = _issue(
        app,
        task=(
            "[project_id=project][operation=edit]"
            "[component=ticket-ledger][error_type=scope-mismatch]"
        ),
    )
    assert ticket["allowed_error_ids"] == [error_node.id]
    assert ticket["required_error_disposition_ids"] == [error_node.id]
    assert ticket["err_warnings"][0]["disposition_required"] is True
    _consume(app, ticket)

    completion = {
        "agent_id": "agent-a",
        "action": "done",
        "detail": "safe independent work completed",
        "affected_nodes": [],
        "meta": {"test": True},
        "ticket_id": ticket["ticket_id"],
        "resolved_errors": [],
    }
    with pytest.raises(HTTPException) as missing:
        asyncio.run(app.complete_activity_endpoint(completion))
    assert missing.value.status_code == 409
    assert (
        missing.value.detail["error"]
        == "ticket_error_disposition_incomplete"
    )
    assert app._ticket_ledger().get(
        ticket["ticket_id"],
        active_only=False,
    )["state"] == "consumed"

    result = asyncio.run(app.complete_activity_endpoint({
        **completion,
        "error_dispositions": [{
            "error_id": error_node.id,
            "disposition": "still_open",
            "reason": "root cause isolated but remediation belongs to a new scope",
        }],
    }))
    assert result["resolution_outcome"] == "disposition_recorded"
    assert result["error_dispositions"] == [{
        "error_id": error_node.id,
        "disposition": "still_open",
        "reason": "root cause isolated but remediation belongs to a new scope",
    }]
    assert fake.nodes[error_node.id].content.extra["case_status"] == "observed"
    assert fake.activity_log[-1].meta["error_dispositions"] == result[
        "error_dispositions"
    ]


def test_error_disposition_requires_reason_and_matches_resolution(
    ticket_runtime,
    monkeypatch,
    tmp_path,
):
    app, _fake, error_node, _runtime_path = ticket_runtime

    async def exact_route(_req):
        return SimpleNamespace(
            activated_nodes=[error_node],
            relevant_edges=[],
            scores={error_node.id: 1.0},
            total_nodes=1,
            total_edges=0,
            route_meta={
                "error_route_policy": {
                    "exact_error_case_ranking": {
                        "match_kinds": {error_node.id: "case_id"},
                    }
                }
            },
        )

    monkeypatch.setattr(app, "_route_in_worker", exact_route)
    ticket = _issue(app, task=f"Resolve {error_node.id}")
    _consume(app, ticket)

    with pytest.raises(HTTPException) as no_reason:
        asyncio.run(app.complete_activity_endpoint({
            "agent_id": "agent-a",
            "ticket_id": ticket["ticket_id"],
            "detail": "not applicable",
            "error_dispositions": [{
                "error_id": error_node.id,
                "disposition": "not_applicable",
            }],
        }))
    assert no_reason.value.status_code == 400
    assert (
        no_reason.value.detail["error"]
        == "error_disposition_reason_required"
    )

    proof = tmp_path / "exact-resolution-proof.json"
    evidence = _artifact_evidence(proof, ticket)
    payload = _completion_payload(ticket, error_node.id, evidence)
    payload["error_dispositions"] = [{
        "error_id": error_node.id,
        "disposition": "still_open",
        "reason": "incorrectly claims unresolved while requesting resolution",
    }]
    with pytest.raises(HTTPException) as mismatch:
        asyncio.run(app.complete_activity_endpoint(payload))
    assert mismatch.value.status_code == 409
    assert (
        mismatch.value.detail["error"]
        == "ticket_error_disposition_resolution_mismatch"
    )

    payload["error_dispositions"] = [{
        "error_id": error_node.id,
        "disposition": "resolved",
        "reason": "",
    }]
    resolved = asyncio.run(app.complete_activity_endpoint(payload))
    assert resolved["resolution_outcome"] == "resolved"
    assert resolved["resolved_errors"][0]["case_status"] == "resolved"


def test_target_content_change_invalidates_reuse_and_old_consume(ticket_runtime):
    app, _fake, _error_node, tmp_path = ticket_runtime
    target = tmp_path / "backend" / "app.py"
    target.parent.mkdir(parents=True)
    target.write_text("version = 1\n", encoding="utf-8")

    first = _issue(app)
    target.write_text("version = 2\n", encoding="utf-8")
    second = _issue(app)

    assert second["ticket_id"] != first["ticket_id"]
    assert second["target_digest"] != first["target_digest"]
    with pytest.raises(HTTPException) as stale:
        _consume(app, first)
    assert stale.value.status_code == 409
    assert stale.value.detail["error"] == "ticket_target_state_changed"


def test_consume_requires_agent_and_exact_target_scope(ticket_runtime):
    app, _fake, _error_node, _tmp_path = ticket_runtime
    ticket = _issue(app)

    with pytest.raises(HTTPException) as missing:
        asyncio.run(app.consume_route_ticket(ticket["ticket_id"], {
            "target_digest": ticket["target_digest"],
            "scope_digest": ticket["scope_digest"],
        }))
    assert missing.value.status_code == 400
    assert missing.value.detail["error"] == "agent_id_required"

    with pytest.raises(HTTPException) as mismatch:
        asyncio.run(app.consume_route_ticket(ticket["ticket_id"], {
            "agent_id": "agent-b",
            "target_digest": ticket["target_digest"],
            "scope_digest": ticket["scope_digest"],
            "tool_name": "apply_patch",
            "tool_input_summary": "update ticket lifecycle",
        }))
    assert mismatch.value.status_code == 403
    assert mismatch.value.detail["error"] == "ticket_agent_mismatch"

    with pytest.raises(HTTPException) as target_mismatch:
        asyncio.run(app.consume_route_ticket(ticket["ticket_id"], {
            "agent_id": "agent-a",
            "target_digest": "wrong",
            "scope_digest": ticket["scope_digest"],
            "tool_name": "apply_patch",
            "tool_input_summary": "update ticket lifecycle",
        }))
    assert target_mismatch.value.status_code == 403
    assert target_mismatch.value.detail["error"] == "ticket_target_digest_mismatch"

    result = _consume(app, ticket)
    assert result["consume_count"] == 1
    deadline = dt.datetime.fromisoformat(result["completion_deadline"])
    assert dt.timedelta(seconds=3590) < (
        deadline - dt.datetime.now(dt.timezone.utc)
    ) <= dt.timedelta(seconds=3600)
    consumed = app._ticket_ledger().events(ticket["ticket_id"])[1]
    assert consumed["event"] == "consumed"
    assert consumed["scope_digest"] == ticket["scope_digest"]
    assert consumed["details"]["completion_deadline"] == result["completion_deadline"]


def test_completion_grace_requires_consumed_state_expires_and_allows_replay(
    tmp_path,
    monkeypatch,
):
    sys.path.insert(0, str(BACKEND))
    import ticket_ledger as ledger_module
    from ticket_ledger import LedgerError, TicketLedger, canonical_hash

    clock = [dt.datetime(2026, 7, 29, 12, 0, tzinfo=dt.timezone.utc)]
    monkeypatch.setattr(ledger_module, "_utc_now", lambda: clock[0])
    assert TicketLedger(
        tmp_path / "default.sqlite3"
    ).completion_grace_sec == 3600
    ledger = TicketLedger(
        tmp_path / "grace.sqlite3",
        completion_grace_sec=120,
    )

    def ticket(ticket_id, lease_key):
        return {
            "ticket_id": ticket_id,
            "lease_key": lease_key,
            "agent_id": "agent-a",
            "project_id": "project",
            "workspace_id": "workspace",
            "workorder_id": "workorder",
            "issued_at": clock[0].isoformat(),
            "ttl_sec": 900,
            "target_digest": "target",
            "scope_digest": "scope",
            "policy_version": "policy/v2",
            "allowed_error_ids": [],
        }

    issued, _ = ledger.issue(ticket("rt_issued", "lease-issued"))
    issued_request = {"ticket_id": issued["ticket_id"], "done": True}
    with pytest.raises(LedgerError) as not_consumed:
        ledger.begin_completion(
            issued["ticket_id"],
            agent_id="agent-a",
            request_hash=canonical_hash(issued_request),
            request=issued_request,
            requested_error_ids=[],
        )
    assert not_consumed.value.code == "ticket_not_consumed"

    active, _ = ledger.issue(ticket("rt_active", "lease-active"))
    consumed = ledger.consume(
        active["ticket_id"],
        agent_id="agent-a",
        target_digest="target",
        scope_digest="scope",
        consumed={"tool_name": "apply_patch"},
    )
    expected_deadline = clock[0] + dt.timedelta(seconds=120)
    assert consumed["completion_deadline"] == expected_deadline.isoformat()
    with sqlite3.connect(ledger.path) as connection:
        stored_deadline = connection.execute(
            "SELECT expires_at FROM tickets WHERE ticket_id=?",
            (active["ticket_id"],),
        ).fetchone()[0]
    assert stored_deadline == pytest.approx(expected_deadline.timestamp())

    active_request = {"ticket_id": active["ticket_id"], "done": True}
    active_hash = canonical_hash(active_request)
    begun = ledger.begin_completion(
        active["ticket_id"],
        agent_id="agent-a",
        request_hash=active_hash,
        request=active_request,
        requested_error_ids=[],
        owner_token="owner-a",
    )
    assert begun["mode"] == "new"
    resumed = ledger.begin_completion(
        active["ticket_id"],
        agent_id="agent-a",
        request_hash=active_hash,
        request=active_request,
        requested_error_ids=[],
        owner_token="owner-a",
    )
    assert resumed["mode"] == "resume"
    ledger.complete(
        active["ticket_id"],
        request_hash=active_hash,
        owner_token="owner-a",
        response={"ok": True},
    )
    clock[0] = expected_deadline + dt.timedelta(seconds=1)
    replay = ledger.begin_completion(
        active["ticket_id"],
        agent_id="agent-a",
        request_hash=active_hash,
        request=active_request,
        requested_error_ids=[],
    )
    assert replay == {
        "mode": "replay",
        "response": {"ok": True},
        "ticket": replay["ticket"],
    }

    expired, _ = ledger.issue(ticket("rt_expired", "lease-expired"))
    expired_consumed = ledger.consume(
        expired["ticket_id"],
        agent_id="agent-a",
        target_digest="target",
        scope_digest="scope",
        consumed={"tool_name": "apply_patch"},
    )
    clock[0] = (
        dt.datetime.fromisoformat(expired_consumed["completion_deadline"])
        + dt.timedelta(seconds=1)
    )
    expired_request = {"ticket_id": expired["ticket_id"], "done": True}
    with pytest.raises(LedgerError) as deadline_expired:
        ledger.begin_completion(
            expired["ticket_id"],
            agent_id="agent-a",
            request_hash=canonical_hash(expired_request),
            request=expired_request,
            requested_error_ids=[],
        )
    assert deadline_expired.value.code == "ticket_completion_deadline_expired"


def test_activity_log_returns_full_hash_for_verifiable_evidence(ticket_runtime):
    app, fake, _error_node, _tmp_path = ticket_runtime

    result = asyncio.run(app.log_activity_endpoint({
        "agent_id": "agent-a",
        "action": "focused_test",
        "detail": "ticket lifecycle passed",
    }))

    assert result["self_hash"] == fake.activity_log[-1].self_hash
    assert len(result["self_hash"]) == 64


def test_compact_error_case_rejects_untyped_evidence_claim(ticket_runtime):
    app, _fake, error_node, _tmp_path = ticket_runtime
    error_node.content.extra.update({
        "case_status": "resolved",
        "solution_summary": "claimed fix",
        "verification_evidence": ["pytest passed"],
    })

    untyped = app._compact_error_case(error_node)

    assert untyped["verification_evidence_count"] == 0
    assert untyped["verified_evidence_count"] == 0
    assert untyped["verified"] is False

    error_node.content.extra["verification_evidence"] = [{
        "kind": "activity",
        "ref": "activity://focused-test",
        "verifier": "3can-server",
        "verified": True,
        "self_hash": "a" * 64,
        "verification_status": "activity_self_hash_verified",
    }]
    activity_only = app._compact_error_case(error_node)

    assert activity_only["verified_evidence_count"] == 0
    assert activity_only["verified"] is False

    error_node.content.extra["verification_evidence"] = [{
        "kind": "test_result",
        "ref": "proof.txt",
        "verifier": "3can-server",
        "verified": True,
        "digest": "sha256:" + ("b" * 64),
        "verification_status": "signed_attestation_verified",
    }]
    verified = app._compact_error_case(error_node)
    assert verified["verified_evidence_count"] == 1
    assert verified["verified"] is True


def test_canonical_error_case_recomputes_identity_and_case_id(ticket_runtime):
    app, _fake, error_node, _tmp_path = ticket_runtime
    assert app._canonical_error_case_payload(error_node) is not None

    error_node.content.extra["error_case"]["fingerprint"] = "ek2:" + ("f" * 64)
    assert app._canonical_error_case_payload(error_node) is None


def test_graph_reconcile_imports_identity_but_not_forged_resolution(
    ticket_runtime,
):
    app, fake, error_node, _tmp_path = ticket_runtime
    canonical = error_node.content.extra["error_case"]
    canonical["state"] = "resolved"
    error_node.content.extra["case_status"] = "resolved"
    error_node.content.extra["current_resolution_id"] = "FIX-forged"
    error_node.content.extra["resolution_ids"] = ["FIX-forged"]

    result = app._reconcile_error_ledger_from_graph()
    stored = app._ticket_ledger().error_case(case_id=error_node.id)

    assert result["imported"] == [error_node.id]
    assert result["untrusted_state_ignored"] == [error_node.id]
    assert stored["state"] == "observed"
    assert stored["occurrence_count"] == 2
    assert stored["resolution"] is None
    assert stored["resolution_refs"] == []
    assert not any(node_id.startswith("FIX-") for node_id in fake.nodes)

    canonical["state"] = "regressed"
    app._ticket_ledger().resolve_error_cases([{
        "case_id": error_node.id,
        "resolution_id": "FIX-authorized",
        "resolved_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "resolved_by": "authorized-agent",
        "solution_summary": "authorized resolution",
        "evidence": [{"verification_status": "artifact_digest_verified"}],
    }])
    app._reconcile_error_ledger_from_graph()
    preserved = app._ticket_ledger().error_case(case_id=error_node.id)
    assert preserved["state"] == "resolved"
    assert preserved["resolution"]["resolution_id"] == "FIX-authorized"


def test_public_crud_cannot_mutate_reserved_error_knowledge(ticket_runtime):
    app, _fake, error_node, _tmp_path = ticket_runtime

    with pytest.raises(HTTPException) as update_denied:
        asyncio.run(app.update_node(
            error_node.id,
            app.NodeUpdate(name="forged resolved case"),
        ))
    assert update_denied.value.status_code == 403
    assert (
        update_denied.value.detail["error"]
        == "error_knowledge_write_requires_lifecycle_endpoint"
    )

    with pytest.raises(HTTPException) as edge_denied:
        asyncio.run(app.create_edge(app.EdgeCreate(
            source=error_node.id,
            target="FIX-forged",
            type=app.EdgeType.resolves,
        )))
    assert edge_denied.value.status_code == 403


def test_verified_completion_replays_exact_response_and_rejects_conflict(
    ticket_runtime,
):
    app, fake, error_node, tmp_path = ticket_runtime
    ticket = _issue(app)
    _consume(app, ticket)
    proof = tmp_path / "proof.txt"
    proof.write_text("5 passed", encoding="utf-8")
    payload = _completion_payload(
        ticket,
        error_node.id,
        _artifact_evidence(proof, ticket),
    )

    first = asyncio.run(app.complete_activity_endpoint(payload))
    replay = asyncio.run(app.complete_activity_endpoint(payload))

    assert first == replay
    assert first["resolution_outcome"] == "resolved"
    assert fake.nodes[error_node.id].content.extra["case_status"] == "resolved"
    assert app._ticket_ledger().error_case(case_id=error_node.id)["state"] == "resolved"
    assert len([entry for entry in fake.activity_log if entry.action == "done"]) == 1
    events = app._ticket_ledger().events(ticket["ticket_id"])
    assert [event["event"] for event in events] == [
        "issued", "consumed", "completed"
    ]

    with pytest.raises(HTTPException) as conflict:
        asyncio.run(app.complete_activity_endpoint({
            **payload,
            "detail": "different canonical request",
        }))
    assert conflict.value.status_code == 409
    assert conflict.value.detail["error"] == "completion_request_conflict"


def test_ticket_cannot_resolve_an_error_outside_allowed_set(ticket_runtime):
    app, fake, allowed_node, tmp_path = ticket_runtime
    ticket = _issue(app)
    _consume(app, ticket)
    second_case = _canonical_case(
        app,
        component="other-component",
        error_type="other-error",
    )
    from models import Node

    second_node = Node(
        id=second_case["case_id"],
        name="Unauthorized case",
        cluster="ErrorKnowledge",
        layer="L1",
        type=app.NodeType.feedback,
        content=app.NodeContent(
            description="other",
            extra={"error_case": second_case},
        ),
    )
    fake.nodes[second_node.id] = second_node
    ledger = app._ticket_ledger()
    before = {
        "ticket": ledger.get(ticket["ticket_id"], active_only=False),
        "events": ledger.events(ticket["ticket_id"]),
        "journal": ledger.journal(ticket["ticket_id"]),
        "unauthorized_case": ledger.error_case(
            fingerprint=second_case["fingerprint"]
        ),
        "nodes": {
            node_id: node.model_dump(mode="json")
            for node_id, node in fake.nodes.items()
        },
    }
    proof = tmp_path / "proof.txt"
    proof.write_text("pass", encoding="utf-8")

    with pytest.raises(HTTPException) as denied:
        asyncio.run(app.complete_activity_endpoint(
            _completion_payload(
                ticket,
                second_node.id,
                _artifact_evidence(proof, ticket),
            )
        ))
    assert denied.value.status_code == 403
    assert denied.value.detail["error"] == "ticket_error_not_allowed"
    assert fake.nodes[allowed_node.id].content.extra["case_status"] == "observed"
    after = {
        "ticket": ledger.get(ticket["ticket_id"], active_only=False),
        "events": ledger.events(ticket["ticket_id"]),
        "journal": ledger.journal(ticket["ticket_id"]),
        "unauthorized_case": ledger.error_case(
            fingerprint=second_case["fingerprint"]
        ),
        "nodes": {
            node_id: node.model_dump(mode="json")
            for node_id, node in fake.nodes.items()
        },
    }
    assert after == before


def test_unverifiable_typed_evidence_sets_review_required_not_resolved(
    ticket_runtime,
):
    app, fake, error_node, tmp_path = ticket_runtime
    ticket = _issue(app)
    _consume(app, ticket)
    proof = tmp_path / "proof.txt"
    proof.write_text("actual", encoding="utf-8")
    evidence = [{
        "kind": "test_result",
        "ref": str(proof),
        "digest": "sha256:" + "0" * 64,
        "verified": True,
        "verifier": "claimant",
    }]

    result = asyncio.run(app.complete_activity_endpoint(
        _completion_payload(ticket, error_node.id, evidence)
    ))

    assert result["resolution_outcome"] == "review_required"
    assert result["resolved_errors"][0]["case_status"] == "review_required"
    assert fake.nodes[error_node.id].content.extra["case_status"] == "review_required"
    assert fake.nodes[error_node.id].content.extra["error_case"]["state"] == "observed"
    assert not any(node_id.startswith("FIX-") for node_id in fake.nodes)


def test_activity_self_hash_is_audit_reference_not_resolution_proof(
    ticket_runtime,
):
    app, fake, error_node, _tmp_path = ticket_runtime
    ticket = _issue(app)
    _consume(app, ticket)
    activity = asyncio.run(app.log_activity_endpoint({
        "agent_id": "agent-a",
        "action": "claimed_test",
        "detail": "caller supplied claim",
    }))
    evidence = [{
        "kind": "test_result",
        "ref": "activity://claimed-test",
        "self_hash": activity["self_hash"],
        "verified": True,
        "verifier": "claimant",
    }]

    result = asyncio.run(app.complete_activity_endpoint(
        _completion_payload(ticket, error_node.id, evidence)
    ))

    assert result["resolution_outcome"] == "review_required"
    assert result["resolved_errors"][0]["case_status"] == "review_required"
    stored = fake.nodes[error_node.id].content.extra["verification_evidence"][0]
    assert stored["verified"] is False
    assert (
        stored["verification_status"]
        == "activity_self_hash_untrusted_for_resolution"
    )
    assert not any(node_id.startswith("FIX-") for node_id in fake.nodes)


def test_fault_injection_recovers_from_journal_without_duplicate_activity(
    ticket_runtime,
):
    app, fake, error_node, tmp_path = ticket_runtime
    ticket = _issue(app)
    _consume(app, ticket)
    proof = tmp_path / "proof.txt"
    proof.write_text("pass", encoding="utf-8")
    payload = _completion_payload(
        ticket,
        error_node.id,
        _artifact_evidence(proof, ticket),
    )
    fake.fail_create_edge_once = True

    with pytest.raises(HTTPException) as failed:
        asyncio.run(app.complete_activity_endpoint(payload))
    assert failed.value.status_code == 500
    journal = app._ticket_ledger().journal(ticket["ticket_id"])
    assert journal["stage"] == "solution_upserted"
    assert journal["last_error"]

    recovered = asyncio.run(app.complete_activity_endpoint(payload))
    assert recovered["resolution_outcome"] == "resolved"
    assert app._ticket_ledger().journal(ticket["ticket_id"])["stage"] == "completed"
    assert len([entry for entry in fake.activity_log if entry.action == "done"]) == 1


def test_server_automatically_records_and_promotes_ticket_identity_rejections(
    ticket_runtime,
):
    app, fake, _error_node, _tmp_path = ticket_runtime
    first_ticket = _issue(app, task="automatic observer first identity rejection")
    payload = {
        "agent_id": "wrong-agent",
        "tool_name": "Edit",
        "tool_input_summary": "automatic observer test",
        "target_digest": first_ticket["target_digest"],
        "scope_digest": first_ticket["scope_digest"],
        "authorization": "Bearer must-not-be-recorded",
    }

    first = asyncio.run(_asgi_post(
        app,
        f"/api/route/ticket/{first_ticket['ticket_id']}/consume",
        payload,
    ))
    assert first.status_code == 403
    assert first.json() == {
        "detail": {
            "error": "ticket_agent_mismatch",
            "ticket_id": first_ticket["ticket_id"],
        }
    }
    fingerprint = app.deterministic_fingerprint(
        project_id="project",
        operation="POST consume_route_ticket",
        component="3can-http-api",
        error_type="ticket_agent_mismatch",
    )
    first_case = app._ticket_ledger().error_case(fingerprint=fingerprint)
    assert first_case["occurrence_count"] == 1
    assert first_case["case_id"] is None

    async def concurrent_replays():
        responses = await asyncio.gather(*[
            _asgi_post(
                app,
                f"/api/route/ticket/{first_ticket['ticket_id']}/consume",
                payload,
                drain=False,
            )
            for _ in range(2)
        ])
        await app._drain_automatic_error_observer()
        return responses

    replays = asyncio.run(concurrent_replays())
    assert [response.status_code for response in replays] == [403, 403]
    assert app._ticket_ledger().error_case(
        fingerprint=fingerprint,
    )["occurrence_count"] == 1

    second_ticket = _issue(app, task="automatic observer second identity rejection")
    payload["target_digest"] = second_ticket["target_digest"]
    payload["scope_digest"] = second_ticket["scope_digest"]
    second = asyncio.run(_asgi_post(
        app,
        f"/api/route/ticket/{second_ticket['ticket_id']}/consume",
        payload,
    ))
    assert second.status_code == 403
    promoted = app._ticket_ledger().error_case(fingerprint=fingerprint)
    assert promoted["occurrence_count"] == 2
    assert promoted["case_id"].startswith("ERR-case-")

    assert promoted["case_id"] in fake.nodes

    assert not any(
        entry.action == "3can_issue_observed"
        for entry in fake.activity_log
    )
    with sqlite3.connect(app._TICKET_LEDGER_PATH) as connection:
        stored_payloads = [
            json.loads(row[0])
            for row in connection.execute(
                "SELECT payload_json FROM error_occurrences ORDER BY occurred_at"
            )
        ]
    first_context = next(
        payload["context"]
        for payload in stored_payloads
        if payload["context"]["evidence_ref"]
        == f"route-ticket:{first_ticket['ticket_id']}"
    )
    assert first_context == {
        "schema": "3can.issue-observation/v1",
        "source": "server_http_response",
        "recording_tier": "error_knowledge",
        "category": "error",
        "severity": "P2",
        "project_id": "project",
        "project_namespace": "project",
        "workspace_id": "workspace",
        "endpoint": "/api/route/ticket/{ticket_id}/consume",
        "operation": "POST consume_route_ticket",
        "status_code": 403,
        "error_code": "ticket_agent_mismatch",
        "evidence_ref": f"route-ticket:{first_ticket['ticket_id']}",
        "retryable": False,
        "workorder_id": "workorder",
        "identity_source": "route_ticket",
    }
    stored = "\n".join(
        json.dumps(payload, sort_keys=True) for payload in stored_payloads
    )
    assert "must-not-be-recorded" not in stored
    assert "wrong-agent" not in stored


def test_server_error_observer_preserves_response_and_fails_open(
    ticket_runtime,
    monkeypatch,
):
    app, fake, _error_node, _tmp_path = ticket_runtime
    ticket = _issue(app, task="automatic observer injected failure")
    before_drops = app._AUTO_ERROR_OBSERVER_DROPS

    async def fail_record(_payload):
        raise OSError("injected observer failure")

    monkeypatch.setattr(app, "_record_error_occurrence_core", fail_record)
    response = asyncio.run(_asgi_post(
        app,
        f"/api/route/ticket/{ticket['ticket_id']}/consume",
        {
            "agent_id": "wrong-agent",
            "tool_name": "Edit",
            "tool_input_summary": "observer failure must not mask rejection",
            "target_digest": ticket["target_digest"],
            "scope_digest": ticket["scope_digest"],
        },
    ))

    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "ticket_agent_mismatch"
    assert app._AUTO_ERROR_OBSERVER_DROPS == before_drops + 1
    assert not any(
        entry.action == "3can_issue_observed"
        for entry in fake.activity_log
    )

    async def fail_observer(*_args, **_kwargs):
        raise RuntimeError("injected observer boundary failure")

    monkeypatch.setattr(app, "_observe_automatic_server_failure", fail_observer)
    boundary_failure = asyncio.run(_asgi_post(
        app,
        f"/api/route/ticket/{ticket['ticket_id']}/consume",
        {
            "agent_id": "wrong-agent",
            "tool_name": "Edit",
            "tool_input_summary": "observer boundary failure",
            "target_digest": ticket["target_digest"],
            "scope_digest": ticket["scope_digest"],
        },
    ))
    assert boundary_failure.status_code == 403
    assert boundary_failure.json()["detail"]["error"] == "ticket_agent_mismatch"
    assert app._AUTO_ERROR_OBSERVER_DROPS == before_drops + 2

    monkeypatch.setattr(
        app,
        "_automatic_failure_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(BrokenPipeError()),
    )
    scheduling_failure = asyncio.run(_asgi_post(
        app,
        f"/api/route/ticket/{ticket['ticket_id']}/consume",
        {
            "agent_id": "wrong-agent",
            "tool_name": "Edit",
            "tool_input_summary": "observer scheduling failure",
            "target_digest": ticket["target_digest"],
            "scope_digest": ticket["scope_digest"],
        },
    ))
    assert scheduling_failure.status_code == 403
    assert scheduling_failure.json()["detail"]["error"] == "ticket_agent_mismatch"
    assert app._AUTO_ERROR_OBSERVER_DROPS == before_drops + 3


def test_server_error_observer_uses_authoritative_ticket_project_scope(
    ticket_runtime,
):
    app, _fake, _error_node, _tmp_path = ticket_runtime
    ticket = _issue(
        app,
        task="automatic observer other project",
        project_id="other-project",
        project_namespace="other-namespace",
    )
    response = asyncio.run(_asgi_post(
        app,
        f"/api/route/ticket/{ticket['ticket_id']}/consume",
        {
            "agent_id": "wrong-agent",
            "project_id": "forged-project",
            "tool_name": "Edit",
            "tool_input_summary": "scope must come from the ticket",
            "target_digest": ticket["target_digest"],
            "scope_digest": ticket["scope_digest"],
        },
    ))
    assert response.status_code == 403

    authoritative = app.deterministic_fingerprint(
        project_id="other-project",
        operation="POST consume_route_ticket",
        component="3can-http-api",
        error_type="ticket_agent_mismatch",
    )
    forged = app.deterministic_fingerprint(
        project_id="forged-project",
        operation="POST consume_route_ticket",
        component="3can-http-api",
        error_type="ticket_agent_mismatch",
    )
    assert app._ticket_ledger().error_case(fingerprint=authoritative) is not None
    assert app._ticket_ledger().error_case(fingerprint=forged) is None


def test_server_error_observer_does_not_trust_unverified_ticket_project(
    ticket_runtime,
):
    app, fake, _error_node, _tmp_path = ticket_runtime

    def issue_unverified(suffix):
        return asyncio.run(app.issue_route_ticket({
            "agent_id": "agent-a",
            "project_id": "victim-project",
            "project_namespace": "victim-namespace",
            "workspace_id": "fabricated-workspace",
            "workorder_id": f"workorder-{suffix}",
            "task_description": f"unverified project ticket {suffix}",
            "target_files": [],
            "scope_keywords": ["identity"],
            "task_type": "Edit",
        }))

    for suffix in ("one", "two"):
        ticket = issue_unverified(suffix)
        assert ticket["project_identity_verified"] is False
        response = asyncio.run(_asgi_post(
            app,
            f"/api/route/ticket/{ticket['ticket_id']}/consume",
            {
                "agent_id": "wrong-agent",
                "tool_name": "Edit",
                "tool_input_summary": "must not claim victim project",
                "target_digest": ticket["target_digest"],
                "scope_digest": ticket["scope_digest"],
            },
        ))
        assert response.status_code == 403

    victim_fingerprint = app.deterministic_fingerprint(
        project_id="victim-project",
        operation="POST consume_route_ticket",
        component="3can-http-api",
        error_type="ticket_agent_mismatch",
    )
    runtime_fingerprint = app.deterministic_fingerprint(
        project_id="3can-runtime",
        operation="POST consume_route_ticket",
        component="3can-http-api",
        error_type="ticket_agent_mismatch",
    )
    assert app._ticket_ledger().error_case(fingerprint=victim_fingerprint) is None
    assert app._ticket_ledger().error_case(fingerprint=runtime_fingerprint) is None
    observations = [
        entry for entry in fake.activity_log
        if entry.action == "3can_issue_observed"
    ]
    assert len(observations) == 1
    assert observations[0].meta["project_id"] == "3can-runtime"
    assert observations[0].meta["recording_tier"] == "activity"
    assert observations[0].affected_nodes == ["DOC-3can-issue-intake-v1"]

    unverified_500 = {
        "event_id": "unverified-500",
        "method": "POST",
        "route_path": "/api/route/ticket/{ticket_id}/consume",
        "route_name": "consume_route_ticket",
        "ticket_id": ticket["ticket_id"],
        "request_correlation_digest": "",
        "status_code": 500,
        "error_code": "unhandled_os_error",
    }
    asyncio.run(app._observe_automatic_server_failure(unverified_500))
    runtime_500 = app._ticket_ledger().error_case(
        fingerprint=app.deterministic_fingerprint(
            project_id="3can-runtime",
            operation="POST consume_route_ticket",
            component="3can-http-api",
            error_type="unhandled_os_error",
        )
    )
    assert runtime_500["project_id"] == "3can-runtime"
    with sqlite3.connect(app._TICKET_LEDGER_PATH) as connection:
        stored_context = json.loads(connection.execute(
            "SELECT payload_json FROM error_occurrences "
            "WHERE fingerprint=? ORDER BY occurred_at DESC LIMIT 1",
            (runtime_500["fingerprint"],),
        ).fetchone()[0])["context"]
    assert stored_context["schema"] == "3can.issue-observation/v1"
    assert stored_context["category"] == "runtime"
    assert stored_context["severity"] == "P2"
    assert stored_context["project_namespace"] == "3can-runtime"
    assert stored_context["workspace_id"] == "3can-runtime"
    assert "workorder_id" not in stored_context
    assert stored_context["evidence_ref"] == "server-event:unverified-500"


def test_server_error_observer_uses_ticket_id_from_error_response_detail(
    ticket_runtime,
):
    app, _fake, _error_node, _tmp_path = ticket_runtime
    ticket = _issue(
        app,
        task="automatic observer completion identity rejection",
        project_id="completion-project",
        project_namespace="completion-namespace",
    )
    _consume(app, ticket)

    response = asyncio.run(_asgi_post(
        app,
        "/api/activity/done",
        {
            "agent_id": "wrong-agent",
            "ticket_id": ticket["ticket_id"],
            "detail": "must remain rejected",
            "affected_nodes": [],
            "meta": {},
        },
    ))

    assert response.status_code == 403
    assert response.json()["detail"] == {
        "error": "ticket_agent_mismatch",
        "ticket_id": ticket["ticket_id"],
    }
    fingerprint = app.deterministic_fingerprint(
        project_id="completion-project",
        operation="POST complete_activity_endpoint",
        component="3can-http-api",
        error_type="ticket_agent_mismatch",
    )
    case = app._ticket_ledger().error_case(fingerprint=fingerprint)
    assert case["occurrence_count"] == 1
    assert case["project_id"] == "completion-project"


def test_ticket_activity_integrity_mismatch_is_error_knowledge_candidate(
    ticket_runtime,
):
    app, _fake, _error_node, _tmp_path = ticket_runtime
    assert "ticket_consumption_activity_mismatch" in (
        app._TICKET_INTEGRITY_ERROR_CODES
    )
    assert app._automatic_failure_tier({
        "route_path": "/api/activity/done",
        "status_code": 403,
        "error_code": "ticket_consumption_activity_mismatch",
    }) == "error_knowledge_candidate"


def test_error_recorder_failures_do_not_recurse_into_the_observer(
    ticket_runtime,
):
    app, fake, _error_node, _tmp_path = ticket_runtime
    before = len([
        entry for entry in fake.activity_log
        if entry.action == "3can_issue_observed"
    ])
    response = asyncio.run(_asgi_post(
        app,
        "/api/errors/occurrences",
        {"secret": "not-an-occurrence"},
    ))

    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "occurrence_fields_missing"
    after = len([
        entry for entry in fake.activity_log
        if entry.action == "3can_issue_observed"
    ])
    assert after == before


def test_server_observer_keeps_validation_as_activity_and_ignores_read_miss(
    ticket_runtime,
):
    app, fake, _error_node, _tmp_path = ticket_runtime
    validation = asyncio.run(_asgi_post(app, "/api/route", {}))
    assert validation.status_code == 422
    observations = [
        entry for entry in fake.activity_log
        if entry.action == "3can_issue_observed"
    ]
    assert len(observations) == 1
    assert observations[0].meta["recording_tier"] == "activity"
    assert observations[0].meta["error_code"] == "request_validation_error"

    repeated = asyncio.run(_asgi_post(app, "/api/route", {}))
    assert repeated.status_code == 422
    assert app._AUTO_ERROR_OBSERVER_SUPPRESSED == 1
    assert len([
        entry for entry in fake.activity_log
        if entry.action == "3can_issue_observed"
    ]) == 1

    missing = asyncio.run(_asgi_get(app, "/api/nodes/DOC-not-present"))
    assert missing.status_code == 404
    assert len([
        entry for entry in fake.activity_log
        if entry.action == "3can_issue_observed"
    ]) == 1


def test_server_observer_records_unhandled_500_without_exposing_exception(
    ticket_runtime,
    monkeypatch,
):
    app, _fake, _error_node, _tmp_path = ticket_runtime

    async def fail_route(_req):
        raise OSError("private path C:\\Users\\secret and token=do-not-return")

    monkeypatch.setattr(app, "_route_in_worker", fail_route)
    response = asyncio.run(_asgi_post(
        app,
        "/api/route",
        {
            "task": "trigger bounded automatic observer test",
            "confirm_low_confidence": True,
        },
        headers={"x-request-id": "stable-retry-correlation"},
    ))
    assert response.status_code == 500
    assert "do-not-return" not in response.text

    fingerprint = app.deterministic_fingerprint(
        project_id="3can-runtime",
        operation="POST route_task",
        component="3can-http-api",
        error_type="unhandled_os_error",
    )
    case = app._ticket_ledger().error_case(fingerprint=fingerprint)
    assert case["occurrence_count"] == 1
    with sqlite3.connect(app._TICKET_LEDGER_PATH) as connection:
        stored = "\n".join(
            row[0]
            for row in connection.execute(
                "SELECT payload_json FROM error_occurrences ORDER BY occurred_at"
            )
        )
    assert "do-not-return" not in stored
    assert "C:\\Users\\secret" not in stored

    retry = asyncio.run(_asgi_post(
        app,
        "/api/route",
        {
            "task": "retry the same request correlation",
            "confirm_low_confidence": True,
        },
        headers={"x-request-id": "stable-retry-correlation"},
    ))
    assert retry.status_code == 500
    assert app._ticket_ledger().error_case(
        fingerprint=fingerprint,
    )["occurrence_count"] == 1

    repeated = asyncio.run(_asgi_post(
        app,
        "/api/route",
        {
            "task": "trigger a second independent bounded observer test",
            "confirm_low_confidence": True,
        },
        headers={"x-request-id": "independent-correlation-2"},
    ))
    assert repeated.status_code == 500
    promoted = app._ticket_ledger().error_case(fingerprint=fingerprint)
    assert promoted["occurrence_count"] == 2
    assert promoted["case_id"].startswith("ERR-case-")

    third = asyncio.run(_asgi_post(
        app,
        "/api/route",
        {
            "task": "record a third independent server failure",
            "confirm_low_confidence": True,
        },
        headers={"x-request-id": "independent-correlation-3"},
    ))
    assert third.status_code == 500
    assert app._ticket_ledger().error_case(
        fingerprint=fingerprint,
    )["occurrence_count"] == 3

    long_request_id = "long-correlation-" + ("x" * 240)
    long_first = asyncio.run(_asgi_post(
        app,
        "/api/route",
        {"task": "long correlation first", "confirm_low_confidence": True},
        headers={"x-request-id": long_request_id},
    ))
    long_retry = asyncio.run(_asgi_post(
        app,
        "/api/route",
        {"task": "long correlation retry", "confirm_low_confidence": True},
        headers={"x-request-id": long_request_id},
    ))
    assert long_first.status_code == long_retry.status_code == 500
    assert app._ticket_ledger().error_case(
        fingerprint=fingerprint,
    )["occurrence_count"] == 4

    same_prefix_different_suffix = (
        "long-correlation-" + ("x" * 240) + "-different"
    )
    distinct_long = asyncio.run(_asgi_post(
        app,
        "/api/route",
        {"task": "distinct long correlation", "confirm_low_confidence": True},
        headers={"x-request-id": same_prefix_different_suffix},
    ))
    assert distinct_long.status_code == 500
    assert app._ticket_ledger().error_case(
        fingerprint=fingerprint,
    )["occurrence_count"] == 5

def test_server_observer_records_missing_agent_id_as_bounded_activity(
    ticket_runtime,
):
    app, fake, _error_node, _tmp_path = ticket_runtime
    response = asyncio.run(_asgi_post(app, "/api/agents/checkin", {}))
    assert response.status_code == 400
    assert response.json() == {"detail": {"error": "agent_id_required"}}
    observed = [
        entry for entry in fake.activity_log
        if entry.action == "3can_issue_observed"
    ]
    assert len(observed) == 1
    entry = observed[0]
    assert entry.agent_id == "3can-server-error-observer"
    assert entry.affected_nodes == ["DOC-3can-issue-intake-v1"]
    assert entry.meta == {
        "schema": "3can.issue-observation/v1",
        "source": "server_http_response",
        "recording_tier": "activity",
        "category": "gate",
        "severity": "info",
        "project_id": "3can-runtime",
        "project_namespace": "3can-runtime",
        "workspace_id": "3can-runtime",
        "endpoint": "/api/agents/checkin",
        "operation": "POST agent_checkin",
        "status_code": 400,
        "error_code": "agent_id_required",
        "evidence_ref": f"server-event:{entry.meta['observation_id']}",
        "retryable": False,
        "observation_id": entry.meta["observation_id"],
    }
    assert re.fullmatch(r"[0-9a-f]{32}", entry.meta["observation_id"])


@pytest.mark.parametrize(
    ("missing_field", "error_code"),
    [
        ("agent_id", "agent_id_required"),
        ("target_digest", "target_digest_required"),
        ("scope_digest", "scope_digest_required"),
    ],
)
def test_server_observer_records_valid_ticket_consume_binding_failures(
    ticket_runtime,
    missing_field,
    error_code,
):
    app, fake, _error_node, _tmp_path = ticket_runtime
    ticket = _issue(app, task=f"missing consume binding {missing_field}")
    payload = {
        "agent_id": ticket["agent_id"],
        "tool_name": "Edit",
        "tool_input_summary": "binding failure must remain attributable",
        "target_digest": ticket["target_digest"],
        "scope_digest": ticket["scope_digest"],
    }
    payload.pop(missing_field)

    response = asyncio.run(_asgi_post(
        app,
        f"/api/route/ticket/{ticket['ticket_id']}/consume",
        payload,
    ))

    assert response.status_code == 400
    assert response.json()["detail"]["error"] == error_code
    fingerprint = app.deterministic_fingerprint(
        project_id="project",
        operation="POST consume_route_ticket",
        component="3can-http-api",
        error_type=error_code,
    )
    occurrence = app._ticket_ledger().error_case(fingerprint=fingerprint)
    assert occurrence["occurrence_count"] == 1
    assert occurrence["case_id"] is None
    assert not any(
        entry.action == "3can_issue_observed"
        and entry.meta.get("error_code") == error_code
        for entry in fake.activity_log
    )


def test_server_observer_promotes_second_independent_consume_agent_binding_failure(
    ticket_runtime,
):
    app, _fake, _error_node, _tmp_path = ticket_runtime
    for index in (1, 2):
        ticket = _issue(app, task=f"independent missing consume agent {index}")
        response = asyncio.run(_asgi_post(
            app,
            f"/api/route/ticket/{ticket['ticket_id']}/consume",
            {
                "tool_name": "Edit",
                "tool_input_summary": f"missing agent occurrence {index}",
                "target_digest": ticket["target_digest"],
                "scope_digest": ticket["scope_digest"],
            },
        ))
        assert response.status_code == 400

    fingerprint = app.deterministic_fingerprint(
        project_id="project",
        operation="POST consume_route_ticket",
        component="3can-http-api",
        error_type="agent_id_required",
    )
    promoted = app._ticket_ledger().error_case(fingerprint=fingerprint)
    assert promoted["occurrence_count"] == 2
    assert promoted["case_id"].startswith("ERR-case-")


def test_server_observer_keeps_inactive_ticket_binding_failure_as_activity(
    ticket_runtime,
):
    app, fake, _error_node, _tmp_path = ticket_runtime
    ticket = _issue(app, task="inactive ticket missing consume agent")
    with sqlite3.connect(app._TICKET_LEDGER_PATH) as connection:
        connection.execute(
            "UPDATE tickets SET expires_at=0 WHERE ticket_id=?",
            (ticket["ticket_id"],),
        )

    response = asyncio.run(_asgi_post(
        app,
        f"/api/route/ticket/{ticket['ticket_id']}/consume",
        {
            "tool_name": "Edit",
            "tool_input_summary": "inactive ticket must not become durable",
            "target_digest": ticket["target_digest"],
            "scope_digest": ticket["scope_digest"],
        },
    ))

    assert response.status_code == 400
    project_fingerprint = app.deterministic_fingerprint(
        project_id="project",
        operation="POST consume_route_ticket",
        component="3can-http-api",
        error_type="agent_id_required",
    )
    assert app._ticket_ledger().error_case(fingerprint=project_fingerprint) is None
    observed = [
        entry for entry in fake.activity_log
        if entry.action == "3can_issue_observed"
        and entry.meta.get("error_code") == "agent_id_required"
    ]
    assert len(observed) == 1
    assert observed[0].meta["recording_tier"] == "activity"
    assert observed[0].meta["project_id"] == "project"
    assert observed[0].agent_id == "agent-a"
    assert observed[0].meta["project_namespace"] == "project"
    assert observed[0].meta["workspace_id"] == "workspace"
    assert observed[0].meta["workorder_id"] == "workorder"
    assert observed[0].meta["evidence_ref"] == (
        f"route-ticket:{ticket['ticket_id']}"
    )


def test_server_observer_does_not_delay_original_rejection(
    ticket_runtime,
    monkeypatch,
):
    app, _fake, _error_node, _tmp_path = ticket_runtime
    ticket = _issue(app, task="observer response independence")

    async def exercise():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def hanging_observer(_snapshot):
            entered.set()
            await release.wait()

        monkeypatch.setattr(app, "_observe_automatic_server_failure", hanging_observer)
        response = await asyncio.wait_for(
            _asgi_post(
                app,
                f"/api/route/ticket/{ticket['ticket_id']}/consume",
                {
                    "agent_id": "wrong-agent",
                    "tool_name": "Edit",
                    "tool_input_summary": "response must not wait for observer",
                    "target_digest": ticket["target_digest"],
                    "scope_digest": ticket["scope_digest"],
                },
                drain=False,
            ),
            timeout=1,
        )
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert response.status_code == 403
        assert response.json()["detail"]["error"] == "ticket_agent_mismatch"
        release.set()
        await app._drain_automatic_error_observer()

    asyncio.run(exercise())


def test_server_observer_bounds_pending_work_and_cleans_finished_tasks(
    ticket_runtime,
    monkeypatch,
):
    app, _fake, _error_node, _tmp_path = ticket_runtime

    async def exercise():
        release = asyncio.Event()

        async def wait_for_release(_snapshot):
            await release.wait()

        monkeypatch.setattr(
            app,
            "_observe_automatic_server_failure",
            wait_for_release,
        )
        before_drops = app._AUTO_ERROR_OBSERVER_DROPS
        for index in range(app._AUTO_ERROR_OBSERVER_MAX_TASKS + 1):
            app._schedule_automatic_server_failure({
                "event_id": f"event-{index}",
                "method": "POST",
                "route_path": "/api/route",
                "route_name": "route_task",
                "ticket_id": "",
                "request_correlation_digest": "",
                "status_code": 422,
                "error_code": "request_validation_error",
            })
        assert len(app._AUTO_ERROR_OBSERVER_TASKS) == (
            app._AUTO_ERROR_OBSERVER_MAX_TASKS
        )
        assert app._AUTO_ERROR_OBSERVER_DROPS == before_drops + 1
        await asyncio.sleep(0)
        release.set()
        await app._drain_automatic_error_observer()
        assert not app._AUTO_ERROR_OBSERVER_TASKS

    asyncio.run(exercise())


def test_server_observer_keeps_route_names_distinct_for_same_500_code(
    ticket_runtime,
    monkeypatch,
):
    app, fake, _error_node, _tmp_path = ticket_runtime

    async def fail_route(_req):
        raise OSError("route failed")

    monkeypatch.setattr(app, "_route_in_worker", fail_route)

    def fail_checkin(**_kwargs):
        raise OSError("checkin failed")

    monkeypatch.setattr(fake, "agent_checkin", fail_checkin, raising=False)
    route_response = asyncio.run(_asgi_post(
        app,
        "/api/route",
        {"task": "route failure", "confirm_low_confidence": True},
    ))
    assert route_response.status_code == 500

    checkin_response = asyncio.run(_asgi_post(
        app,
        "/api/agents/checkin",
        {"agent_id": "agent-a"},
    ))
    assert checkin_response.status_code == 500

    route_fingerprint = app.deterministic_fingerprint(
        project_id="3can-runtime",
        operation="POST route_task",
        component="3can-http-api",
        error_type="unhandled_os_error",
    )
    checkin_fingerprint = app.deterministic_fingerprint(
        project_id="3can-runtime",
        operation="POST agent_checkin",
        component="3can-http-api",
        error_type="unhandled_os_error",
    )
    route_case = app._ticket_ledger().error_case(fingerprint=route_fingerprint)
    checkin_case = app._ticket_ledger().error_case(fingerprint=checkin_fingerprint)
    assert route_case["operation"] == "post route_task"
    assert checkin_case["operation"] == "post agent_checkin"
    assert route_case["fingerprint"] != checkin_case["fingerprint"]


def test_server_observer_keeps_incompatible_500_types_separate(
    ticket_runtime,
    monkeypatch,
):
    app, _fake, _error_node, _tmp_path = ticket_runtime

    async def fail_with_os_error(_req):
        raise OSError("private details")

    monkeypatch.setattr(app, "_route_in_worker", fail_with_os_error)
    first = asyncio.run(_asgi_post(
        app,
        "/api/route",
        {"task": "first failure", "confirm_low_confidence": True},
    ))
    assert first.status_code == 500

    async def fail_with_value_error(_req):
        raise ValueError("different private details")

    monkeypatch.setattr(app, "_route_in_worker", fail_with_value_error)
    second = asyncio.run(_asgi_post(
        app,
        "/api/route",
        {"task": "second failure", "confirm_low_confidence": True},
    ))
    assert second.status_code == 500

    os_fingerprint = app.deterministic_fingerprint(
        project_id="3can-runtime",
        operation="POST route_task",
        component="3can-http-api",
        error_type="unhandled_os_error",
    )
    value_fingerprint = app.deterministic_fingerprint(
        project_id="3can-runtime",
        operation="POST route_task",
        component="3can-http-api",
        error_type="unhandled_value_error",
    )
    os_case = app._ticket_ledger().error_case(fingerprint=os_fingerprint)
    value_case = app._ticket_ledger().error_case(fingerprint=value_fingerprint)
    assert os_case["occurrence_count"] == 1
    assert value_case["occurrence_count"] == 1
    assert os_case["fingerprint"] != value_case["fingerprint"]


def test_server_observer_preserves_safe_structured_500_code(
    ticket_runtime,
    monkeypatch,
):
    app, _fake, _error_node, _tmp_path = ticket_runtime

    async def fail_route(_req):
        raise HTTPException(
            503,
            detail={
                "error": "embedding_backend_unavailable",
                "diagnostic": "token=must-not-be-stored",
            },
        )

    monkeypatch.setattr(app, "_route_in_worker", fail_route)
    response = asyncio.run(_asgi_post(
        app,
        "/api/route",
        {"task": "structured failure", "confirm_low_confidence": True},
    ))
    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "embedding_backend_unavailable"

    fingerprint = app.deterministic_fingerprint(
        project_id="3can-runtime",
        operation="POST route_task",
        component="3can-http-api",
        error_type="embedding_backend_unavailable",
    )
    case = app._ticket_ledger().error_case(fingerprint=fingerprint)
    assert case["occurrence_count"] == 1
    with sqlite3.connect(app._TICKET_LEDGER_PATH) as connection:
        stored = "\n".join(
            row[0]
            for row in connection.execute(
                "SELECT payload_json FROM error_occurrences ORDER BY occurred_at"
            )
        )
    assert "must-not-be-stored" not in stored


def test_occurrence_ledger_promotes_only_second_and_uses_core_24_hex_id(
    ticket_runtime,
):
    app, fake, _error_node, _tmp_path = ticket_runtime
    identity = {
        "project_id": "project",
        "operation": "test",
        "component": "occurrence-ledger",
        "error_type": "unicode-decode",
    }
    fingerprint = app.deterministic_fingerprint(**identity)
    base = {
        **identity,
        "fingerprint": fingerprint,
        "error": "UnicodeDecodeError",
        "root_cause": "encoding implicit",
        "occurred_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    first = asyncio.run(app.record_error_occurrence({
        **base, "occurrence_id": "occ-1",
    }))
    replay = asyncio.run(app.record_error_occurrence({
        **base, "occurrence_id": "occ-1",
    }))
    second = asyncio.run(app.record_error_occurrence({
        **base, "occurrence_id": "occ-2",
    }))

    assert first["case"]["occurrence_count"] == 1
    assert first["case"]["case_id"] is None
    assert first["case"]["blocking"] is False
    assert replay["idempotent"] is True
    assert replay["case"]["occurrence_count"] == 1
    assert replay["case"]["blocking"] is False
    assert second["status"] == "PROMOTED"
    expected_id = f"ERR-case-{fingerprint.split(':', 1)[1][:24]}"
    assert second["case"]["case_id"] == expected_id
    assert second["case"]["blocking"] is True
    assert expected_id in fake.nodes
    projected_identity = fake.nodes[expected_id].content.extra
    assert {
        field: projected_identity[field]
        for field in ("project_id", "operation", "component", "error_type")
    } == identity
    queried = asyncio.run(app.get_error_case(fingerprint=fingerprint))
    assert queried["occurrence_count"] == 2
    assert queried["blocking"] is True

    app._ticket_ledger().resolve_error_cases([{
        "case_id": expected_id,
        "resolution_id": "FIX-occurrence-ledger",
    }])
    resolved = asyncio.run(app.get_error_case(fingerprint=fingerprint))
    assert resolved["state"] == "resolved"
    assert resolved["blocking"] is False
    regressed = asyncio.run(app.record_error_occurrence({
        **base, "occurrence_id": "occ-3",
    }))
    assert regressed["case"]["state"] == "regressed"
    assert regressed["case"]["blocking"] is True


def test_projection_failure_is_partial_but_sqlite_occurrence_survives(
    ticket_runtime,
    monkeypatch,
):
    app, _fake, _error_node, _tmp_path = ticket_runtime
    identity = {
        "project_id": "project",
        "operation": "test",
        "component": "projection",
        "error_type": "disk-write",
    }
    fingerprint = app.deterministic_fingerprint(**identity)
    payload = {
        **identity,
        "fingerprint": fingerprint,
        "error": "projection failed",
        "root_cause": "injected",
        "occurred_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    asyncio.run(app.record_error_occurrence({**payload, "occurrence_id": "p-1"}))
    monkeypatch.setattr(
        app,
        "_project_error_case",
        lambda _case: (_ for _ in ()).throw(OSError("projection unavailable")),
    )
    second = asyncio.run(app.record_error_occurrence({
        **payload, "occurrence_id": "p-2",
    }))

    assert second["status"] == "PARTIAL"
    assert second["case"]["occurrence_count"] == 2
    assert second["case"]["graph_projection_state"] == "partial"
    assert app._ticket_ledger().error_occurrence("p-2")["fingerprint"] == fingerprint


def test_sqlite_ledger_handles_two_processes_and_more_than_500_active(tmp_path):
    sys.path.insert(0, str(BACKEND))
    from ticket_ledger import SCHEMA_VERSION, TicketLedger

    database = tmp_path / "concurrent.sqlite3"
    ledger = TicketLedger(database)
    assert SCHEMA_VERSION == 3
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3

    context = multiprocessing.get_context("spawn")
    with concurrent.futures.ProcessPoolExecutor(
        max_workers=2,
        mp_context=context,
    ) as pool:
        issued = list(pool.map(
            _process_issue,
            [str(BACKEND)] * 2,
            [str(database)] * 2,
            ["a", "b"],
        ))
    assert len(set(issued)) == 1
    ticket_id = issued[0]

    with concurrent.futures.ProcessPoolExecutor(
        max_workers=2,
        mp_context=context,
    ) as pool:
        consume_counts = list(pool.map(
            _process_consume,
            [str(BACKEND)] * 2,
            [str(database)] * 2,
            [ticket_id] * 2,
            ["a", "b"],
        ))
    assert sorted(consume_counts) == [1, 2]

    with concurrent.futures.ProcessPoolExecutor(
        max_workers=2,
        mp_context=context,
    ) as pool:
        completions = list(pool.map(
            _process_complete,
            [str(BACKEND)] * 2,
            [str(database)] * 2,
            [ticket_id] * 2,
        ))
    assert "completed" in completions
    assert set(completions) <= {"completed", "replay", "completion_in_progress"}
    assert ledger.get(ticket_id, active_only=False)["state"] == "completed"

    now = dt.datetime.now(dt.timezone.utc).isoformat()
    for index in range(501):
        ledger.issue({
            "ticket_id": f"rt_bulk_{index}",
            "lease_key": f"lease_bulk_{index}",
            "agent_id": "bulk",
            "project_id": "project",
            "workspace_id": "workspace",
            "workorder_id": "workorder",
            "issued_at": now,
            "ttl_sec": 900,
            "target_digest": f"target-{index}",
            "scope_digest": f"scope-{index}",
            "policy_version": "policy/v2",
            "allowed_error_ids": [],
        })
    assert ledger.active_count() == 501


def test_legacy_receipts_are_imported_once_without_fake_completion(tmp_path):
    sys.path.insert(0, str(BACKEND))
    from ticket_ledger import TicketLedger

    issued_at = dt.datetime.now(dt.timezone.utc).isoformat()
    legacy_ticket = {
        "ticket_id": "rt_legacy",
        "lease_key": "legacy-lease",
        "agent_id": "legacy-agent",
        "issued_at": issued_at,
        "ttl_sec": 900,
        "scope": {"target_files": ["backend/app.py"]},
        "consumed_by_tools": [{"tool_name": "apply_patch"}],
    }
    tickets_path = tmp_path / "route_tickets.json"
    receipts_path = tmp_path / "route_ticket_receipts.jsonl"
    tickets_path.write_text(
        json.dumps({"rt_legacy": legacy_ticket}),
        encoding="utf-8",
    )
    receipts_path.write_text(
        json.dumps({
            "event": "issued",
            "ticket_id": "rt_legacy",
            "agent_id": "legacy-agent",
            "timestamp": issued_at,
            "details": {},
        }) + "\n",
        encoding="utf-8",
    )
    database = tmp_path / "ledger.sqlite3"

    first = TicketLedger(
        database,
        legacy_tickets_path=tickets_path,
        legacy_receipts_path=receipts_path,
    )
    second = TicketLedger(
        database,
        legacy_tickets_path=tickets_path,
        legacy_receipts_path=receipts_path,
    )

    assert first.migration_status()["status"] == "imported"
    assert second.get("rt_legacy", active_only=False)["state"] == "consumed"
    assert [event["event"] for event in second.events("rt_legacy")] == ["issued"]


def test_strict_budget_uses_summary_reference_and_never_exceeds(ticket_runtime):
    app, _fake, _error_node, _tmp_path = ticket_runtime
    packed = [
        {"id": "ERR-required", "summary": "x" * 1000},
        {"id": "DOC-optional", "summary": "y" * 1000},
    ]

    kept, truncated = app._enforce_budget(
        packed,
        20,
        protected_ids={"ERR-required"},
    )

    assert truncated is True
    assert kept == [{"id": "ERR-required", "summary_ref": True}]
    assert app._estimate_packed_tokens(kept) <= 20
