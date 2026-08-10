from __future__ import annotations

import importlib.util
import io
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


STAGING_ROOT = Path(__file__).resolve().parents[2]
HUB_PATH = (
    STAGING_ROOT
    / "examples"
    / "plugins"
    / "3can-resource-governor"
    / "scripts"
    / "3can_resource_hub.py"
)


def load_hub():
    spec = importlib.util.spec_from_file_location(
        "threecan_resource_hub_under_test",
        HUB_PATH,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _last_json(capsys) -> dict:
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def _base_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("THREECAN_RESOURCE_HUB_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("THREECAN_PROJECT_ID", "PUBLIC-project")
    monkeypatch.setenv("CODEX_THREAD_ID", "thr-public")
    monkeypatch.setenv("THREECAN_AGENT_ID", "PUBLIC-main")
    monkeypatch.setenv("THREECAN_WORKORDER_ID", "PUBLIC-workorder")


def test_performance_profile_advises_while_constrained_profile_blocks(
    monkeypatch,
    tmp_path,
    capsys,
):
    hub = load_hub()
    _base_env(monkeypatch, tmp_path)
    assert hub.main([
        "acquire",
        "--resource-key",
        "port:9700",
    ]) == 0
    first = _last_json(capsys)
    assert first["status"] == "ACQUIRED"

    monkeypatch.setenv("CODEX_THREAD_ID", "thr-other")
    monkeypatch.setenv("THREECAN_AGENT_ID", "PUBLIC-other")
    monkeypatch.setenv("THREECAN_PROJECT_ID", "PUBLIC-other-project")
    assert hub.main([
        "acquire",
        "--resource-key",
        "port:9700",
    ]) == 0
    advisory = _last_json(capsys)
    assert advisory["status"] == "ADVISORY"
    assert advisory["profile"] == "performance"
    assert advisory["owner"]["project_key"] == "PUBLIC-project"

    assert hub.main([
        "--profile",
        "constrained",
        "acquire",
        "--resource-key",
        "port:9700",
    ]) == 3
    blocked = _last_json(capsys)
    assert blocked["status"] == "BLOCKED"
    assert blocked["owner"]["session_key"] == "thr-public"


def test_finish_marks_only_current_session_pending_until_verified_release(
    monkeypatch,
    tmp_path,
    capsys,
):
    hub = load_hub()
    _base_env(monkeypatch, tmp_path)
    assert hub.main([
        "acquire",
        "--resource-key",
        "docker-build:public-image",
    ]) == 0
    first = _last_json(capsys)

    monkeypatch.setenv("CODEX_THREAD_ID", "thr-sibling")
    assert hub.main([
        "acquire",
        "--resource-key",
        "compose-project:public-sibling",
    ]) == 0
    _last_json(capsys)

    monkeypatch.setenv("CODEX_THREAD_ID", "thr-public")
    assert hub.main(["finish"]) == 0
    finished = _last_json(capsys)
    assert finished["status"] == "CLEANUP_PENDING"
    assert finished["cleanup_resource_keys"] == [
        "docker-build:public-image"
    ]
    manifest = json.loads(
        Path(finished["cleanup_manifest"]).read_text(encoding="utf-8")
    )
    assert manifest["cleanup_pending_leases"][0]["state"] == (
        "cleanup_pending"
    )
    assert manifest["policy"]["owner_scoped"] is True
    assert manifest["policy"]["docker_commands_executed"] is False
    assert manifest["policy"]["codex_session_files_deleted"] is False
    assert manifest["policy"]["hook_must_not_run_docker"] is True
    assert manifest["policy"]["docker_system_prune_prohibited"] is True
    assert manifest["policy"]["docker_volume_prune_prohibited"] is True
    assert manifest["docker_cleanup_candidates"][0]["candidate_only"] is True
    assert manifest["docker_cleanup_candidates"][0]["ownership"][
        "identifier"
    ] == "public-image"
    assert manifest["docker_cleanup_candidates"][0]["ownership"][
        "hook_execution_allowed"
    ] is False

    assert hub.main(["status"]) == 0
    status = _last_json(capsys)
    assert {
        item["resource_key"]: item["state"]
        for item in status["leases"]
    } == {
        "compose-project:public-sibling": "active",
        "docker-build:public-image": "cleanup_pending",
    }

    assert hub.main([
        "release",
        "--lease-id",
        first["lease_id"],
    ]) == 2
    assert _last_json(capsys)["error"].startswith(
        "cleanup_verification_required"
    )

    assert hub.main([
        "release",
        "--lease-id",
        first["lease_id"],
        "--cleanup-verified",
        "--reason",
        "owner_cleanup_verified",
    ]) == 0
    assert _last_json(capsys)["status"] == "RELEASED"

    assert hub.main(["status"]) == 0
    status = _last_json(capsys)
    assert [item["resource_key"] for item in status["leases"]] == [
        "compose-project:public-sibling"
    ]


def test_subagent_stop_hook_releases_only_that_actor(
    monkeypatch,
    tmp_path,
    capsys,
):
    hub = load_hub()
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("THREECAN_AGENT_ID", "subagent-a")
    assert hub.main([
        "acquire",
        "--resource-key",
        "port:3001",
    ]) == 0
    _last_json(capsys)

    monkeypatch.setenv("THREECAN_AGENT_ID", "subagent-b")
    assert hub.main([
        "acquire",
        "--resource-key",
        "port:3002",
    ]) == 0
    _last_json(capsys)

    payload = {
        "session_id": "thr-public",
        "turn_id": "turn-public",
        "cwd": str(tmp_path),
        "hook_event_name": "SubagentStop",
        "agent_id": "subagent-a",
        "agent_type": "worker",
        "agent_transcript_path": "",
        "stop_hook_active": False,
        "last_assistant_message": "not persisted by the hub",
    }
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(json.dumps(payload)),
    )
    assert hub.main(["hook"]) == 0
    assert _last_json(capsys) == {}

    assert hub.main(["status"]) == 0
    status = _last_json(capsys)
    assert {
        item["actor_id"]: item["state"]
        for item in status["leases"]
    } == {
        "subagent-a": "cleanup_pending",
        "subagent-b": "active",
    }


def test_acquire_requires_stable_actor_identity(
    monkeypatch,
    tmp_path,
    capsys,
):
    hub = load_hub()
    _base_env(monkeypatch, tmp_path)
    monkeypatch.delenv("THREECAN_AGENT_ID")
    monkeypatch.delenv("CODEX_AGENT_ID", raising=False)

    assert hub.main([
        "acquire",
        "--resource-key",
        "port:9700",
    ]) == 2
    result = _last_json(capsys)
    assert result["status"] == "ERROR"
    assert result["error"].startswith("actor_id_required")


def test_expired_lease_stays_blocking_until_cleanup_is_verified(
    monkeypatch,
    tmp_path,
    capsys,
):
    hub = load_hub()
    _base_env(monkeypatch, tmp_path)
    current = datetime(2026, 7, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(hub, "_now", lambda: current)
    assert hub.main([
        "acquire",
        "--resource-key",
        "port:9700",
        "--ttl-sec",
        "30",
    ]) == 0
    _last_json(capsys)

    current += timedelta(seconds=31)
    monkeypatch.setenv("THREECAN_PROJECT_ID", "PUBLIC-other-project")
    monkeypatch.setenv("CODEX_THREAD_ID", "thr-other")
    monkeypatch.setenv("THREECAN_AGENT_ID", "PUBLIC-other")
    assert hub.main([
        "--profile",
        "constrained",
        "acquire",
        "--resource-key",
        "port:9700",
    ]) == 3
    blocked = _last_json(capsys)
    assert blocked["status"] == "BLOCKED"
    assert blocked["reason"] == "resource_cleanup_pending"
    assert blocked["owner"]["state"] == "cleanup_pending"


def test_generic_agent_or_session_keys_are_not_leasable(
    monkeypatch,
    tmp_path,
    capsys,
):
    hub = load_hub()
    _base_env(monkeypatch, tmp_path)
    for resource_key in ("agent:all", "session:all", "task:all"):
        assert hub.main([
            "acquire",
            "--resource-key",
            resource_key,
        ]) == 2
        result = _last_json(capsys)
        assert result["status"] == "ERROR"
        assert result["error"].startswith("resource_kind_not_leasable")


def test_session_audit_is_read_only_and_emits_review_candidates(
    monkeypatch,
    tmp_path,
    capsys,
):
    hub = load_hub()
    _base_env(monkeypatch, tmp_path)
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    parent_rollout = sessions_dir / "rollout-parent.jsonl"
    child_rollout = sessions_dir / "rollout-child.jsonl"
    unreferenced_rollout = sessions_dir / "rollout-unreferenced.jsonl"
    parent_rollout.write_text('{"parent":true}\n', encoding="utf-8")
    child_rollout.write_text(
        '{"forked_history":"large enough for threshold"}\n',
        encoding="utf-8",
    )
    unreferenced_rollout.write_text(
        '{"candidate_only":true}\n',
        encoding="utf-8",
    )

    child_db_path = str(child_rollout)
    if os.name == "nt":
        child_db_path = "\\\\?\\" + child_db_path
    state_db = tmp_path / "state_5.sqlite"
    connection = sqlite3.connect(state_db)
    try:
        connection.executescript(
            """
            CREATE TABLE threads (
                id TEXT PRIMARY KEY,
                rollout_path TEXT NOT NULL
            );
            CREATE TABLE thread_spawn_edges (
                parent_thread_id TEXT NOT NULL,
                child_thread_id TEXT NOT NULL PRIMARY KEY,
                status TEXT NOT NULL
            );
            """
        )
        connection.executemany(
            "INSERT INTO threads (id, rollout_path) VALUES (?, ?)",
            [
                ("parent", str(parent_rollout)),
                ("child", child_db_path),
            ],
        )
        connection.execute(
            """
            INSERT INTO thread_spawn_edges (
                parent_thread_id, child_thread_id, status
            ) VALUES ('parent', 'child', 'open')
            """
        )
        connection.commit()
    finally:
        connection.close()

    assert hub.main([
        "audit-sessions",
        "--state-db",
        str(state_db),
        "--sessions-dir",
        str(sessions_dir),
        "--large-rollout-bytes",
        "1",
    ]) == 0
    report = _last_json(capsys)
    assert report["status"] == "CANDIDATES_ONLY"
    assert report["metrics"]["thread_count"] == 2
    assert report["metrics"]["open_spawn_edge_count"] == 1
    assert report["metrics"]["possible_full_history_fork_count"] == 1
    assert report["metrics"]["unreferenced_rollout_candidate_count"] == 1
    assert report["metrics"]["normalized_long_path_count"] == (
        1 if os.name == "nt" else 0
    )
    assert report["open_spawn_edge_candidates"][0][
        "possible_full_history_fork"
    ] is True
    assert report["open_spawn_edge_candidates"][0]["delete_allowed"] is False
    assert report["unreferenced_rollout_candidates"][0][
        "delete_allowed"
    ] is False
    assert report["policy"]["database_mutations_executed"] is False
    assert report["policy"]["rollout_files_deleted"] is False
    assert Path(report["audit_manifest"]).is_file()

    connection = sqlite3.connect(state_db)
    try:
        assert connection.execute(
            "SELECT status FROM thread_spawn_edges"
        ).fetchone()[0] == "open"
    finally:
        connection.close()
    assert parent_rollout.is_file()
    assert child_rollout.is_file()
    assert unreferenced_rollout.is_file()


def test_empty_transcript_path_does_not_resolve_to_working_directory():
    hub = load_hub()
    normalized = hub._normalize_path_text("")
    assert normalized == {
        "raw": "",
        "normalized": "",
        "comparison_key": "",
        "had_long_path_prefix": False,
    }


def test_hub_contains_no_docker_or_rollout_deletion_executor():
    source = HUB_PATH.read_text(encoding="utf-8").casefold()
    assert "import subprocess" not in source
    assert "docker system prune" not in source
    assert "docker volume prune" not in source
    assert "unlink(" not in source
    assert "delete from threads" not in source
    assert "delete from thread_spawn_edges" not in source
