from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HOOK_PATH = (
    ROOT.parent
    / "examples"
    / "codex-cli-project-kit"
    / "scripts"
    / "3can_error_lifecycle_hook.py"
)


def _load_hook():
    spec = importlib.util.spec_from_file_location(
        "threecan_error_lifecycle_hook_under_test",
        HOOK_PATH,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


HOOK = _load_hook()


def test_posttooluse_nonzero_is_recorded_but_never_blocked(
    monkeypatch,
    capsys,
):
    captured = {}

    def fake_fail(args):
        captured["args"] = args
        print(json.dumps({
            "status": "PARTIAL",
            "occurrence_id": "OCC-hook",
            "outbox_path": "pending/error-occurrence-OCC-hook.json",
        }))
        return 1

    monkeypatch.setattr(HOOK.HELPER, "fail", fake_fail)
    monkeypatch.setattr(
        HOOK.HELPER,
        "_flush_one_error_occurrence_outbox",
        lambda *args, **kwargs: {"attempted": False, "posted": False},
    )
    code, result = HOOK._hook_json({
        "hook_event_name": "PostToolUse",
        "session_id": "session-a",
        "turn_id": "turn-a",
        "tool_name": "Bash",
        "tool_input": {"command": "pytest -q"},
        "tool_response": {
            "exit_code": 1,
            "stderr": "one focused assertion failed",
        },
    })

    assert code == 0
    assert result["continue"] is True
    assert "not blocked" in result["systemMessage"]
    assert captured["args"].timeout_seconds <= 3.0
    assert captured["args"].command_summary == "pytest -q"
    assert capsys.readouterr().out == ""


def test_posttooluse_success_replays_at_most_one_outbox_without_failure_record(
    monkeypatch,
):
    calls = []
    monkeypatch.setattr(
        HOOK.HELPER,
        "_flush_one_error_occurrence_outbox",
        lambda *args, **kwargs: (
            calls.append((args, kwargs))
            or {"attempted": True, "posted": True}
        ),
    )
    monkeypatch.setattr(
        HOOK,
        "_record_failed_tool",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("successful tools must not be failure occurrences")
        ),
    )

    code, result = HOOK._hook_json({
        "hook_event_name": "PostToolUse",
        "session_id": "session-a",
        "turn_id": "turn-a",
        "tool_name": "Bash",
        "tool_input": {"command": "pytest -q"},
        "tool_response": {"result": {"exitCode": 0}},
    })

    assert code == 0
    assert result["continue"] is True
    assert len(calls) == 1


def test_stop_blocks_only_pending_exact_ticket_dispositions(monkeypatch):
    monkeypatch.setenv("THREECAN_AGENT_ID", "codex-exact")
    monkeypatch.setattr(
        HOOK.HELPER,
        "_pending_error_disposition_tickets",
        lambda **kwargs: [{
            "ticket_id": "rt_exact",
            "required_error_disposition_ids": ["ERR-case-exact"],
        }],
    )
    code, result = HOOK._hook_json({
        "hook_event_name": "Stop",
        "session_id": "session-a",
        "turn_id": "turn-a",
        "stop_hook_active": False,
        "last_assistant_message": "ready to finish",
    })

    assert code == 0
    assert result["decision"] == "block"
    assert "ERR-case-exact" in result["reason"]
    assert "error-disposition" in result["reason"]

    monkeypatch.setattr(
        HOOK.HELPER,
        "_pending_error_disposition_tickets",
        lambda **kwargs: [],
    )
    code, result = HOOK._hook_json({
        "hook_event_name": "Stop",
        "session_id": "session-a",
        "turn_id": "turn-a",
        "stop_hook_active": True,
        "last_assistant_message": "disposition accepted",
    })
    assert code == 0
    assert result == {"continue": True}


def test_local_stop_state_is_created_only_for_server_required_exact_errors(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(HOOK.HELPER, "LOCAL_RUNTIME_DIR", tmp_path)
    monkeypatch.setenv("CODEX_SESSION_ID", "session-a")
    approximate = {
        "ticket_id": "rt_similar",
        "allowed_error_ids": ["ERR-case-similar"],
        "required_error_disposition_ids": [],
    }
    assert HOOK.HELPER._record_error_disposition_ticket(
        approximate,
        agent_id="codex-a",
        base_url="http://127.0.0.1:9700",
    ) is None

    exact = {
        "ticket_id": "rt_exact",
        "allowed_error_ids": ["ERR-case-exact", "ERR-case-similar"],
        "required_error_disposition_ids": ["ERR-case-exact"],
    }
    state_path = HOOK.HELPER._record_error_disposition_ticket(
        exact,
        agent_id="codex-a",
        base_url="http://127.0.0.1:9700",
    )
    assert state_path
    pending = HOOK.HELPER._pending_error_disposition_tickets(
        session_id="session-a",
        agent_id="codex-a",
        cwd=str(Path.cwd()),
    )
    assert [item["ticket_id"] for item in pending] == ["rt_exact"]

    HOOK.HELPER._complete_error_disposition_ticket(
        "rt_exact",
        response={
            "completion_request_hash": "abc",
            "error_dispositions": [{
                "error_id": "ERR-case-exact",
                "disposition": "still_open",
                "reason": "tracked",
            }],
        },
    )
    assert HOOK.HELPER._pending_error_disposition_tickets(
        session_id="session-a",
        agent_id="codex-a",
        cwd=str(Path.cwd()),
    ) == []


def test_stop_uses_hook_cwd_when_session_and_agent_env_are_absent(
    monkeypatch,
    tmp_path,
):
    runtime = tmp_path / "runtime"
    worktree = tmp_path / "worktree"
    other = tmp_path / "other"
    worktree.mkdir()
    other.mkdir()
    monkeypatch.setattr(HOOK.HELPER, "LOCAL_RUNTIME_DIR", runtime)
    monkeypatch.delenv("CODEX_SESSION_ID", raising=False)
    monkeypatch.delenv("THREECAN_SESSION_ID", raising=False)
    monkeypatch.delenv("THREECAN_AGENT_ID", raising=False)
    monkeypatch.chdir(worktree)
    HOOK.HELPER._record_error_disposition_ticket(
        {
            "ticket_id": "rt_cwd",
            "required_error_disposition_ids": ["ERR-case-cwd"],
        },
        agent_id="codex-worktree",
        base_url="http://127.0.0.1:9700",
    )

    code, same_cwd = HOOK._hook_json({
        "hook_event_name": "Stop",
        "session_id": "stdin-session-not-in-env",
        "turn_id": "turn-a",
        "cwd": str(worktree),
        "stop_hook_active": False,
        "last_assistant_message": "ready",
    })
    assert code == 0
    assert same_cwd["decision"] == "block"
    assert "ERR-case-cwd" in same_cwd["reason"]

    code, different_cwd = HOOK._hook_json({
        "hook_event_name": "Stop",
        "session_id": "another-session",
        "turn_id": "turn-b",
        "cwd": str(other),
        "stop_hook_active": False,
        "last_assistant_message": "unrelated worktree",
    })
    assert code == 0
    assert different_cwd == {"continue": True}
