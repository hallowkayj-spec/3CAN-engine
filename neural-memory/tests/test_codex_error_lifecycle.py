from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


STAGING_ROOT = Path(__file__).resolve().parents[2]
HELPER_PATH = (
    STAGING_ROOT
    / "examples"
    / "codex-cli-project-kit"
    / "scripts"
    / "3can_codex.py"
)
SPEC = importlib.util.spec_from_file_location("threecan_codex_error_lifecycle", HELPER_PATH)
assert SPEC and SPEC.loader
HELPER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HELPER)
CODEX_POWERSHELL_PATH = (
    STAGING_ROOT
    / "examples"
    / "codex-cli-project-kit"
    / "scripts"
    / "codex-3can.ps1"
)
CODEX_WRAPPER_PATH = (
    STAGING_ROOT
    / "examples"
    / "codex-cli-project-kit"
    / "scripts"
    / "3can_codex_wrapper.ps1"
)


def _runtime_stats(
    engine_root: Path,
    graph_root: Path,
    *,
    startup_nonce: str = "",
    healthy: bool = True,
    total_nodes: int = 120,
) -> dict:
    return {
        "healthy": healthy,
        "total_nodes": total_nodes,
        "total_edges": 20,
        "readiness": {"production_ready": healthy},
        "runtime_identity": HELPER._expected_runtime_identity(
            engine_root,
            graph_root,
            startup_nonce=startup_nonce,
        ),
    }


def _load_backend_app():
    backend_dir = STAGING_ROOT / "neural-memory" / "backend"
    backend_dir_text = str(backend_dir)
    if backend_dir_text not in sys.path:
        sys.path.insert(0, backend_dir_text)
    spec = importlib.util.spec_from_file_location(
        "threecan_backend_runtime_identity_test",
        backend_dir / "app.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_private_user_paths_are_removed_from_public_refs():
    windows_user_root = "C:\\" + "Users" + "\\Alice Doe"
    posix_user_path = "/" + "home" + "/alice/private/app.py"
    unc_user_path = "\\\\" + "server\\share\\" + "Users" + "\\alice\\private.py"
    assert HELPER._public_target_ref(windows_user_root + "\\secret.py") == "secret.py"
    assert HELPER._public_target_ref(posix_user_path) == "private/app.py"
    assert HELPER._public_target_ref(unc_user_path) == "private.py"


def test_ticket_consume_payload_binds_server_digests_and_agent():
    ticket = {
        "agent_id": "codex-test",
        "target_digest": "target-sha256",
        "scope_digest": "scope-sha256",
    }
    assert HELPER._ticket_consume_payload(
        ticket,
        agent_id="codex-test",
        tool_name="apply_patch",
        tool_input_summary="focused edit",
    ) == {
        "agent_id": "codex-test",
        "tool_name": "apply_patch",
        "tool_input_summary": "focused edit",
        "target_digest": "target-sha256",
        "scope_digest": "scope-sha256",
    }
    with pytest.raises(ValueError, match="ticket_agent_id_mismatch"):
        HELPER._ticket_consume_payload(
            ticket,
            agent_id="",
            tool_name="apply_patch",
            tool_input_summary="focused edit",
        )
    for broken, expected in (
        ({"agent_id": "codex-test", "scope_digest": "scope"}, "ticket_target_digest_missing"),
        ({"agent_id": "codex-test", "target_digest": "target"}, "ticket_scope_digest_missing"),
        (
            {
                "agent_id": "other",
                "target_digest": "target",
                "scope_digest": "scope",
            },
            "ticket_agent_id_mismatch",
        ),
    ):
        try:
            HELPER._ticket_consume_payload(
                broken,
                agent_id="codex-test",
                tool_name="apply_patch",
                tool_input_summary="focused edit",
            )
        except ValueError as exc:
            assert str(exc) == expected
        else:
            raise AssertionError(f"{expected} must fail closed")


def test_ticket_consume_fetches_authoritative_digest_snapshot(monkeypatch, capsys):
    calls = []

    def fake_request(base_url, path, **kwargs):
        calls.append((path, kwargs))
        if kwargs.get("method") != "POST":
            return True, {
                "agent_id": "codex-test",
                "target_digest": "target-sha256",
                "scope_digest": "scope-sha256",
            }
        return True, {"ok": True, "agent_id": "codex-test"}

    monkeypatch.setattr(HELPER, "_try_json_request", fake_request)
    monkeypatch.setattr(HELPER, "_record_local_token_estimate", lambda *args, **kwargs: None)
    args = argparse.Namespace(
        base_url="http://127.0.0.1:9700",
        ticket_id="rt_test",
        agent_id="codex-test",
        tool_name="apply_patch",
        tool_input_summary="focused edit",
    )
    assert HELPER.ticket_consume(args) == 0
    json.loads(capsys.readouterr().out)
    assert [path for path, _ in calls] == [
        "/api/route/ticket/rt_test",
        "/api/route/ticket/rt_test/consume",
    ]
    assert calls[1][1]["payload"]["target_digest"] == "target-sha256"
    assert calls[1][1]["payload"]["scope_digest"] == "scope-sha256"
    assert calls[1][1]["payload"]["agent_id"] == "codex-test"


def test_verification_evidence_parser_preserves_structured_json():
    artifact = {
        "kind": "artifact_digest",
        "ref": "README.md",
        "sha256": "a" * 64,
    }
    activity = {"kind": "activity_self_hash", "self_hash": "b" * 64}
    assert HELPER._parse_verification_evidence(
        [json.dumps(artifact), json.dumps([activity])]
    ) == [artifact, activity]
    for invalid in ("plain text", json.dumps(["not-an-object"])):
        try:
            HELPER._parse_verification_evidence([invalid])
        except ValueError:
            pass
        else:
            raise AssertionError("non-object evidence must fail closed")


def test_done_requires_explicit_ticket_id_before_any_request(
    monkeypatch, capsys
):
    def unexpected_call(*args, **kwargs):
        raise AssertionError("missing ticket id must fail before HTTP or metadata lookup")

    monkeypatch.setattr(HELPER, "_try_json_request", unexpected_call)
    monkeypatch.setattr(HELPER, "_current_project_metadata", unexpected_call)
    args = argparse.Namespace(
        agent_id="codex-test",
        base_url="http://127.0.0.1:9700",
        detail="focused change",
        affected_nodes=[],
        ticket_id="",
        meta="",
        resolved_errors=[],
        root_cause="",
        solution_summary="",
        verification_evidence=[],
        fixed_in="",
    )

    assert HELPER.done(args) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is False
    assert result["error"]["kind"] == "ticket_id_required"


def test_done_passes_explicit_ticket_id_to_authoritative_endpoint(
    monkeypatch, capsys
):
    calls = []

    def request(base_url, path, **kwargs):
        calls.append((base_url, path, kwargs))
        return True, {
            "ok": True,
            "ticket_state": "completed",
            "resolved_errors": [],
        }

    monkeypatch.setattr(HELPER, "_try_json_request", request)
    monkeypatch.setattr(
        HELPER,
        "_current_project_metadata",
        lambda **kwargs: {"project_id": "public-demo"},
    )
    monkeypatch.setattr(
        HELPER,
        "_record_local_token_estimate",
        lambda *args, **kwargs: None,
    )
    args = argparse.Namespace(
        agent_id="codex-test",
        base_url="http://127.0.0.1:9700",
        detail="focused change",
        affected_nodes=[],
        ticket_id="rt_explicit",
        meta="",
        resolved_errors=[],
        root_cause="",
        solution_summary="",
        verification_evidence=[],
        fixed_in="",
    )

    assert HELPER.done(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is True
    assert [(path, call["payload"]["ticket_id"]) for _, path, call in calls] == [
        ("/api/activity/done", "rt_explicit")
    ]


def test_packaged_powershell_done_never_infers_shared_ticket_state():
    required_guard = (
        "done requires explicit -TicketId; shared wrapper state is never inferred."
    )
    outer_source = CODEX_POWERSHELL_PATH.read_text(encoding="utf-8")
    wrapper_source = CODEX_WRAPPER_PATH.read_text(encoding="utf-8")

    assert required_guard in outer_source
    assert required_guard in wrapper_source
    outer_done = outer_source.split("'done' {", 1)[1].split("'compact' {", 1)[0]
    wrapper_done = wrapper_source.split("'after-edit' {", 1)[1].split(
        "'before-compact' {", 1
    )[0]
    assert outer_done.index("if (-not $TicketId)") < outer_done.index(
        "Invoke-Codex3CanWrapperObject"
    )
    assert wrapper_done.index(
        "if ($Action -eq 'done' -and -not $TicketId)"
    ) < wrapper_done.index("Invoke-HelperJson $args")


def test_packaged_wrapper_has_no_ticket_state_and_compact_is_explicit():
    outer_source = CODEX_POWERSHELL_PATH.read_text(encoding="utf-8")
    wrapper_source = CODEX_WRAPPER_PATH.read_text(encoding="utf-8")

    assert "[string]$AgentId = 'codex-main'" not in outer_source
    assert "[string]$AgentId = 'codex-main'" not in wrapper_source
    assert "Generic AgentId 'codex-main' is not allowed" in outer_source
    assert "Generic AgentId 'codex-main' is not allowed" in wrapper_source
    for forbidden in (
        "codex_wrapper_states",
        "Load-State",
        "Save-State",
        "THREECAN_TICKET_ID",
        "show-state",
        "clear-state",
        "wrapper_state_",
    ):
        assert forbidden not in wrapper_source
    assert "'state'" not in outer_source
    assert "'clear'" not in outer_source
    assert "'supervise'" not in outer_source
    assert "'supervise-status'" not in outer_source

    mutate_block = wrapper_source.split("'before-mutate' {", 1)[1].split(
        "'after-edit' {", 1
    )[0]
    compact_block = wrapper_source.split("'before-compact' {", 1)[1].split(
        "'check-ticket' {", 1
    )[0]
    assert "Resolve-TicketContext" not in mutate_block
    assert "Resolve-TicketContext" not in compact_block
    assert "explicit_files_only" in compact_block
    assert "ticketScope" not in compact_block


def test_packaged_outer_prepare_is_one_thin_prepare_transport(tmp_path):
    shell = shutil.which("pwsh") or shutil.which("powershell")
    assert shell, "PowerShell is required to test the packaged wrapper contract"

    scripts_dir = tmp_path / "project" / "scripts"
    scripts_dir.mkdir(parents=True)
    shutil.copy2(CODEX_POWERSHELL_PATH, scripts_dir / "codex-3can.ps1")
    shutil.copy2(CODEX_WRAPPER_PATH, scripts_dir / "3can_codex_wrapper.ps1")
    call_log = tmp_path / "calls.jsonl"
    (scripts_dir / "3can_codex.py").write_text(
        """import json, os, pathlib, sys
path = pathlib.Path(os.environ['THREECAN_TEST_CALL_LOG'])
with path.open('a', encoding='utf-8') as handle:
    handle.write(json.dumps(sys.argv[1:]) + '\\n')
print(json.dumps({
    'ticket': {
        'ticket_id': 'TKT-thin',
        'state': 'consumed',
        'workorder_id': os.environ.get('THREECAN_WORKORDER_ID'),
        'ttl_sec': 900,
    },
    'consume': {'ok': True, 'consume_count': 1},
    'workorder_id': os.environ.get('THREECAN_WORKORDER_ID'),
}))
""",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["THREECAN_TEST_CALL_LOG"] = str(call_log)
    env["CODEX_THREAD_ID"] = "thread-thin-prepare"
    result = subprocess.run(
        [
            shell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(scripts_dir / "codex-3can.ps1"),
            "prepare",
            "-WorkorderId",
            "workorder-thin-prepare",
            "-TaskDescription",
            "bounded update",
            "-TargetFiles",
            "README.md",
            "-ToolName",
            "apply_patch",
            "-ToolInputSummary",
            "edit README",
        ],
        cwd=tmp_path / "project",
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in call_log.read_text(encoding="utf-8").splitlines()]
    assert len(calls) == 1
    assert "prepare" in calls[0]
    assert "supervise" not in calls[0]
    assert json.loads(result.stdout)["prepare"]["workorder_id"] == (
        "workorder-thin-prepare"
    )


def test_packaged_outer_compact_preserves_explicit_target_files(tmp_path):
    shell = shutil.which("pwsh") or shutil.which("powershell")
    assert shell, "PowerShell is required to test the packaged wrapper contract"

    scripts_dir = tmp_path / "project" / "scripts"
    scripts_dir.mkdir(parents=True)
    shutil.copy2(CODEX_POWERSHELL_PATH, scripts_dir / "codex-3can.ps1")
    shutil.copy2(CODEX_WRAPPER_PATH, scripts_dir / "3can_codex_wrapper.ps1")
    (scripts_dir / "3can_codex.py").write_text(
        "import json, sys\nprint(json.dumps({'argv': sys.argv[1:]}))\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            shell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(scripts_dir / "codex-3can.ps1"),
            "compact",
            "-AgentId",
            "codex-test-compact-W1",
            "-TaskSummary",
            "durable continuation delta",
            "-TargetFiles",
            "README.md,scripts/tool.py,README.md",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    argv = payload["argv"]
    compact_files = [argv[index + 1] for index, item in enumerate(argv) if item == "--file"]

    assert compact_files == ["README.md", "scripts/tool.py"]
    assert payload["compact_scope_files"] == compact_files
    assert payload["compact_scope_selection"] == "explicit_files_only"


def test_packaged_before_edit_returns_server_ticket_without_local_state(request):
    shell = shutil.which("pwsh") or shutil.which("powershell")
    assert shell, "PowerShell is required to test the packaged wrapper contract"
    tmp_path = Path(tempfile.mkdtemp(prefix="3can-workspace-case-"))
    request.addfinalizer(lambda: shutil.rmtree(tmp_path, ignore_errors=True))

    def install(project: Path) -> Path:
        scripts_dir = project / "scripts"
        scripts_dir.mkdir(parents=True)
        wrapper = scripts_dir / "3can_codex_wrapper.ps1"
        shutil.copy2(CODEX_WRAPPER_PATH, wrapper)
        (scripts_dir / "3can_codex.py").write_text(
            "import json\n"
            "print(json.dumps({'ticket_id': 'rt_case', 'issued_at': "
            "'2026-08-10T00:00:00+00:00', 'ttl_sec': 900}))\n",
            encoding="utf-8",
        )
        return wrapper

    project = tmp_path / "Project"
    wrapper = install(project)
    result = subprocess.run(
        [
            shell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(wrapper),
            "before-edit",
            "-AgentId",
            "codex-workspace-case-W1",
            "-TaskDescription",
            "verify worktree identity",
            "-TargetFiles",
            "README.md",
            "-ScopeKeywords",
            "workspace-identity",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout)["ticket_id"] == "rt_case"
    assert not (project / "test-results").exists()


def test_agent_api_projects_stale_heartbeats_without_mutating_registry():
    backend_app = _load_backend_app()
    from models import AgentInfo, AgentStatus

    now = datetime.now(timezone.utc)
    old_checkin = (now - timedelta(seconds=601)).isoformat()
    fresh_checkin = (now - timedelta(seconds=10)).isoformat()
    stale_registered = AgentInfo(
        agent_id="codex-old-session",
        status=AgentStatus.busy,
        last_checkin=old_checkin,
    )
    fresh_registered = AgentInfo(
        agent_id="codex-current-session",
        status=AgentStatus.online,
        last_checkin=fresh_checkin,
    )
    projected = {
        agent.agent_id: agent
        for agent in backend_app._agents_with_heartbeat_presence(
            [stale_registered, fresh_registered],
            status_filter=None,
            heartbeat_ttl_sec=300,
            now=now,
        )
    }

    assert projected["codex-old-session"].status == AgentStatus.offline
    assert projected["codex-old-session"].meta["heartbeat_presence"] == {
        "stale": True,
        "age_sec": 601,
        "ttl_sec": 300,
        "registered_status": "busy",
    }
    assert projected["codex-current-session"].status == AgentStatus.online
    assert projected["codex-current-session"].meta["heartbeat_presence"]["stale"] is False
    assert stale_registered.status == AgentStatus.busy
    assert "heartbeat_presence" not in stale_registered.meta
    assert [
        agent.agent_id
        for agent in backend_app._agents_with_heartbeat_presence(
            [stale_registered, fresh_registered],
            status_filter="stale",
            heartbeat_ttl_sec=300,
            now=now,
        )
    ] == ["codex-old-session"]
    assert [
        agent.agent_id
        for agent in backend_app._agents_with_heartbeat_presence(
            [stale_registered, fresh_registered],
            status_filter="online",
            heartbeat_ttl_sec=300,
            now=now,
        )
    ] == ["codex-current-session"]


def test_stats_runtime_identity_is_public_safe(monkeypatch, tmp_path):
    backend_app = _load_backend_app()
    engine_root = tmp_path / "private" / "engine"
    graph_root = tmp_path / "private" / "graph"
    startup_nonce = "never-return-this-startup-secret"

    identity = backend_app._public_runtime_identity(
        engine_root=engine_root,
        graph_root=graph_root,
        startup_nonce=startup_nonce,
    )

    assert identity == {
        "schema": "3can.runtime-identity/v1",
        "engine_root_sha256": hashlib.sha256(
            os.path.normcase(str(engine_root.resolve())).encode("utf-8")
        ).hexdigest(),
        "graph_root_sha256": hashlib.sha256(
            os.path.normcase(str(graph_root.resolve())).encode("utf-8")
        ).hexdigest(),
        "startup_nonce_sha256": hashlib.sha256(
            startup_nonce.encode("utf-8")
        ).hexdigest(),
    }
    serialized = json.dumps(identity)
    assert str(engine_root) not in serialized
    assert str(graph_root) not in serialized
    assert startup_nonce not in serialized

    class FakeStats:
        @staticmethod
        def model_dump():
            return {"total_nodes": 120, "total_edges": 20}

    class FakeEngine:
        @staticmethod
        def run_consistent(operation, *args, **kwargs):
            return operation(*args, **kwargs)

        @staticmethod
        def stats():
            return FakeStats()

        @staticmethod
        def embedding_status(*, deep=False):
            return {"backend_id": "test", "deep_verified": bool(deep)}

    monkeypatch.setattr(backend_app, "engine", FakeEngine())
    monkeypatch.setattr(backend_app, "GRAPH_DIR", graph_root)
    monkeypatch.setattr(
        backend_app._READINESS_CACHE,
        "snapshot",
        lambda *args, **kwargs: {"production_ready": True},
    )
    monkeypatch.setenv("THREECAN_STARTUP_NONCE", startup_nonce)
    response = asyncio.run(backend_app.get_stats())
    assert response["healthy"] is True
    assert response["runtime_identity"]["schema"] == identity["schema"]
    assert (
        response["runtime_identity"]["startup_nonce_sha256"]
        == identity["startup_nonce_sha256"]
    )
    assert startup_nonce not in json.dumps(response)
    assert str(graph_root) not in json.dumps(response)


def test_stats_validation_requires_health_and_exact_runtime_identity(tmp_path):
    engine_root = tmp_path / "selected" / "neural-memory"
    graph_root = engine_root / "graph"
    nonce = "launch-secret"
    good = _runtime_stats(
        engine_root,
        graph_root,
        startup_nonce=nonce,
        total_nodes=9999,
    )

    healthy, warning = HELPER._validate_stats(
        good,
        min_nodes=100,
        expected_engine_root=engine_root,
        expected_graph_root=graph_root,
        expected_startup_nonce=nonce,
    )
    assert healthy is True
    assert warning is None

    arbitrary_high_node_stats = {
        "healthy": True,
        "total_nodes": 999999,
        "total_edges": 999999,
    }
    healthy, warning = HELPER._validate_stats(
        arbitrary_high_node_stats,
        min_nodes=100,
        expected_engine_root=engine_root,
        expected_graph_root=graph_root,
    )
    assert healthy is False
    assert warning["kind"] == "runtime_identity_missing"

    unhealthy = dict(good)
    unhealthy["healthy"] = False
    healthy, warning = HELPER._validate_stats(
        unhealthy,
        min_nodes=100,
        expected_engine_root=engine_root,
        expected_graph_root=graph_root,
        expected_startup_nonce=nonce,
    )
    assert healthy is False
    assert warning["kind"] == "compatibility_health_mismatch"

    wrong_graph = dict(good)
    wrong_graph["runtime_identity"] = dict(good["runtime_identity"])
    wrong_graph["runtime_identity"]["graph_root_sha256"] = "0" * 64
    healthy, warning = HELPER._validate_stats(
        wrong_graph,
        min_nodes=100,
        expected_engine_root=engine_root,
        expected_graph_root=graph_root,
        expected_startup_nonce=nonce,
    )
    assert healthy is False
    assert warning["kind"] == "runtime_identity_mismatch"




def test_packaged_cli_rejects_unshipped_actions_without_fake_dispatch():
    source = CODEX_POWERSHELL_PATH.read_text(encoding="utf-8")
    for action in ("deploy", "maintenance", "autoloop"):
        assert f"'{action}' {{" not in source
        assert action in source
    assert "Action '$_' is unsupported in this release package" in source
    for missing_harness in (
        "3can_bluegreen_deploy.py",
        "3can_graph_maintenance.py",
        "3can_autoloop.py",
    ):
        assert missing_harness not in source
