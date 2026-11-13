"""Identity diagnostics must explain refusal without changing ticket semantics."""
from __future__ import annotations

import asyncio
import errno
import json
import logging
import re
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from test_ticket_lifecycle import FakeEngine, load_app


SECRET = "TEST_ONLY_PRIVATE_VALUE_73c9"
PREFIX = "project_target_identity_unverified "
GIT_OPS = {
    ("rev-parse", "--show-toplevel"): "worktree_root",
    ("remote", "get-url", "origin"): "origin_remote",
    ("rev-parse", "--path-format=absolute", "--git-common-dir"): "git_common_dir",
}
FAULTS = [
    ("project", "project_id_mismatch"),
    ("namespace", "project_namespace_mismatch"),
    ("workspace", "workspace_id_mismatch"),
    ("git_timeout", "git_timeout"),
    ("git_spawn", "git_os_error"),
    ("git_nonzero", "git_nonzero"),
    ("capsule_permission", "filesystem_unavailable"),
    ("capsule_json", "capsule_json_invalid"),
]


@pytest.fixture
def identity_runtime(tmp_path, monkeypatch):
    app = load_app()
    fake = FakeEngine(app)
    repo = tmp_path / SECRET
    (repo / ".agents").mkdir(parents=True)
    target = repo / "target.txt"
    target.write_text("public fixture\n", encoding="utf-8")
    capsule_path = repo / ".agents" / "project.json"
    capsule = {
        "project_id": "public-project",
        "project_namespace": "public-namespace",
        "project_root": ".",
        "git_repository": "example.invalid/public/repository",
    }
    capsule_path.write_text(json.dumps(capsule), encoding="utf-8")
    context = {
        "project_id": capsule["project_id"],
        "project_namespace": capsule["project_namespace"],
        "workspace_id": (
            f"git-{app._local_path_sha256(repo / '.git')[:12]}-"
            f"{app._local_path_sha256(repo)[:12]}"
        ),
    }
    state = {"fault": None, "operation": "worktree_root", "calls": []}

    def git_run(command, **kwargs):
        assert command[:2] == ["git", "-C"] and Path(command[2]) == repo
        assert kwargs == {
            "check": False, "capture_output": True, "text": True,
            "encoding": "utf-8", "errors": "replace", "timeout": 10,
        }
        operation = GIT_OPS[tuple(command[3:])]
        state["calls"].append(operation)
        if operation == state["operation"]:
            if state["fault"] == "git_timeout":
                raise subprocess.TimeoutExpired(
                    command, 10, output=SECRET, stderr=SECRET,
                )
            if state["fault"] == "git_spawn":
                raise FileNotFoundError(errno.ENOENT, SECRET, str(repo / SECRET))
            if state["fault"] == "git_nonzero":
                return subprocess.CompletedProcess(command, 128, SECRET, SECRET)
        values = {
            "worktree_root": str(repo),
            "origin_remote": f"https://user:{SECRET}@example.invalid/public/repository.git",
            "git_common_dir": str(repo / ("foreign.git" if state["fault"] == "workspace" else ".git")),
        }
        return subprocess.CompletedProcess(command, 0, values[operation] + "\n", "")

    read_text = Path.read_text

    def capsule_read(path, *args, **kwargs):
        if path == capsule_path:
            if state["fault"] == "capsule_permission":
                raise PermissionError(errno.EACCES, SECRET, str(path))
            if state["fault"] == "capsule_json":
                return '{"secret":"' + SECRET
            if state["fault"] in {"project", "namespace"}:
                changed = dict(capsule)
                key = "project_id" if state["fault"] == "project" else "project_namespace"
                changed[key] = "foreign-project"
                return json.dumps(changed)
        return read_text(path, *args, **kwargs)

    async def route(_request):
        return SimpleNamespace(activated_nodes=[], route_meta={})

    monkeypatch.setattr(app, "engine", fake)
    monkeypatch.setattr(app, "_route_in_worker", route)
    monkeypatch.setattr(app, "_TICKET_LEDGER_PATH", tmp_path / "tickets.sqlite3")
    monkeypatch.setattr(app, "_TICKET_LEDGER_INSTANCE", None)
    monkeypatch.setattr(app, "_ROUTE_TICKETS_FILE", tmp_path / "unused-legacy.json")
    monkeypatch.setattr(app, "_ROUTE_TICKET_RECEIPTS_FILE", tmp_path / "unused-receipts.jsonl")
    monkeypatch.setenv("THREECAN_PROJECT_DIR", str(tmp_path / "authority"))
    monkeypatch.delenv("THREECAN_TARGET_ROOTS", raising=False)
    monkeypatch.setattr(app.subprocess, "run", git_run)
    monkeypatch.setattr(Path, "read_text", capsule_read)
    return SimpleNamespace(
        app=app, fake=fake, repo=repo, target=target, context=context, state=state,
    )


def issue(runtime):
    return asyncio.run(runtime.app.issue_route_ticket({
        **runtime.context,
        "agent_id": "public-agent",
        "workorder_id": "public-workorder",
        "task_description": "Check isolated identity diagnostics",
        "task_type": "Edit",
        "target_files": [str(runtime.target)],
        "scope_keywords": ["identity"],
    }))


def consume(runtime, ticket):
    return asyncio.run(runtime.app.consume_route_ticket(ticket["ticket_id"], {
        "agent_id": ticket["agent_id"],
        "tool_name": "Edit",
        "tool_input_summary": "public fixture only",
        "target_digest": ticket["target_digest"],
        "scope_digest": ticket["scope_digest"],
    }))


def assert_diagnostic(caplog, runtime, reason):
    records = [record for record in caplog.records if record.getMessage().startswith(PREFIX)]
    assert len(records) == 1
    record = records[0]
    data = json.loads(record.getMessage()[len(PREFIX):])
    assert set(data) == {
        "reason", "git_operation", "git_elapsed_ms", "git_exit_code", "correlation",
    }
    assert data["reason"] == reason
    assert re.fullmatch("[0-9a-f]{64}", data["correlation"])
    if reason.startswith("git_") and reason in {"git_timeout", "git_os_error", "git_nonzero"}:
        assert data["git_operation"] == runtime.state["operation"]
        assert isinstance(data["git_elapsed_ms"], (int, float))
        assert data["git_elapsed_ms"] >= 0
        assert data["git_exit_code"] == (128 if reason == "git_nonzero" else None)
    else:
        assert data["git_operation"] == "none"
        assert data["git_elapsed_ms"] is None
        assert data["git_exit_code"] is None
    assert record.exc_info is None
    rendered = record.getMessage() + str(record.args)
    for private in [SECRET, str(runtime.repo), str(runtime.target), *runtime.context.values()]:
        assert private not in rendered
    return data


@pytest.mark.parametrize("phase", ["issue", "consume"])
@pytest.mark.parametrize("fault,reason", FAULTS)
def test_identity_refusal_preserves_ledger_and_explains_failure(
    identity_runtime, caplog, phase, fault, reason,
):
    runtime = identity_runtime
    ticket = issue(runtime)
    ledger = runtime.app._ticket_ledger()
    before = ledger.get(ticket["ticket_id"])
    activities = len(runtime.fake.activity_log)
    runtime.state.update(fault=fault, calls=[])
    caplog.set_level(logging.WARNING)
    with pytest.raises(HTTPException) as denied:
        issue(runtime) if phase == "issue" else consume(runtime, ticket)
    assert denied.value.status_code == 403
    assert denied.value.detail == {"error": "project_target_identity_unverified"}
    assert ledger.get(ticket["ticket_id"]) == before
    assert before["consume_count"] == 0
    assert len(runtime.fake.activity_log) == activities
    with sqlite3.connect(ledger.path) as db:
        assert db.execute("SELECT count(*) FROM tickets").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM ticket_events").fetchone()[0] == 1
    assert len(runtime.state["calls"]) == (3 if fault == "workspace" else 1)
    assert_diagnostic(caplog, runtime, reason)


def test_valid_identity_issue_consume_and_replay_are_unchanged(identity_runtime, caplog):
    runtime = identity_runtime
    ticket = issue(runtime)
    assert ticket["project_identity_verified"] is True
    assert ticket["target_digest_source"] == "server_verified_snapshot_v1"
    reused = issue(runtime)
    assert reused["ticket_id"] == ticket["ticket_id"] and reused["reused"] is True
    first = consume(runtime, ticket)
    replay = consume(runtime, ticket)
    assert first["ok"] and first["consume_count"] == 1 and not first["idempotent"]
    assert replay["ok"] and replay["consume_count"] == 1 and replay["idempotent"]
    assert first["activity_hash"] == replay["activity_hash"]
    stored = runtime.app._ticket_ledger().get(ticket["ticket_id"])
    assert stored["state"] == "consumed" and stored["consume_count"] == 1
    assert stored["target_digest"] == ticket["target_digest"]
    assert stored["scope_digest"] == ticket["scope_digest"]
    assert [entry.action for entry in runtime.fake.activity_log] == ["ticket_issued", "ticket_consumed"]
    assert runtime.state["calls"] == list(GIT_OPS.values()) * 4
    assert not [record for record in caplog.records if record.getMessage().startswith(PREFIX)]


@pytest.mark.parametrize("operation", list(GIT_OPS.values()))
def test_git_failure_identifies_exact_operation(identity_runtime, caplog, operation):
    runtime = identity_runtime
    ticket = issue(runtime)
    runtime.state.update(fault="git_timeout", operation=operation, calls=[])
    caplog.set_level(logging.WARNING)
    with pytest.raises(HTTPException) as denied:
        consume(runtime, ticket)
    assert denied.value.status_code == 403
    assert runtime.app._ticket_ledger().get(ticket["ticket_id"])["consume_count"] == 0
    assert runtime.state["calls"] == list(GIT_OPS.values())[:list(GIT_OPS.values()).index(operation) + 1]
    assert_diagnostic(caplog, runtime, "git_timeout")


def test_unrecognized_exception_text_is_not_logged(identity_runtime, monkeypatch, caplog):
    runtime = identity_runtime

    def invalid_remote(_remote):
        raise ValueError(SECRET)

    monkeypatch.setattr(runtime.app, "_repository_key", invalid_remote)
    caplog.set_level(logging.WARNING)
    with pytest.raises(HTTPException) as denied:
        issue(runtime)
    assert denied.value.status_code == 403
    assert_diagnostic(caplog, runtime, "identity_validation_failed")
