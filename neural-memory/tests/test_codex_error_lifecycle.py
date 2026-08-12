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


def _failure_args(**overrides):
    values = {
        "agent_id": "codex-test",
        "base_url": "http://127.0.0.1:9700",
        "command_summary": "pytest tests/test_ticket.py",
        "error_excerpt": "UnicodeDecodeError while parsing child output",
        "target_files": ["backend/app.py"],
        "scope_keywords": ["ticket-lifecycle"],
        "related_nodes": [],
        "diagnosis": "",
        "node_id": "",
        "operation_class": "test",
        "component": "ticket-lifecycle",
        "error_type": "unicode-error",
        "root_cause": "",
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_fingerprint_ignores_volatile_command_and_absolute_path():
    first = HELPER._loop_signature_key(
        "pytest run id=123",
        "UnicodeDecodeError byte 0x99 at offset 30",
        [r"C:\one\backend\app.py"],
        operation_class="test",
        component="ticket-lifecycle",
        error_type="unicode-error",
        project_identity="demo",
    )
    second = HELPER._loop_signature_key(
        "pytest run id=999",
        "UnicodeDecodeError byte 0x81 at offset 900",
        [r"D:\other\backend\app.py"],
        operation_class="test",
        component="ticket-lifecycle",
        error_type="unicode-error",
        project_identity="demo",
    )

    assert first == second


def test_private_user_paths_are_removed_from_public_refs_and_components():
    windows_user_root = "C:\\" + "Users" + "\\Alice Doe"
    posix_user_path = "/" + "home" + "/alice/private/app.py"
    unc_user_path = "\\\\" + "server\\share\\" + "Users" + "\\alice\\private.py"
    assert HELPER._public_target_ref(windows_user_root + "\\secret.py") == "secret.py"
    assert HELPER._public_target_ref(posix_user_path) == "private/app.py"
    assert HELPER._public_target_ref(unc_user_path) == "private.py"
    assert HELPER._failure_component(
        "pytest",
        [windows_user_root + "\\secret.py"],
        [],
    ) == "secret"
    assert "alice" not in HELPER._failure_component(
        "pytest",
        [],
        [windows_user_root + "\\private-component"],
    )


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
    ) < wrapper_done.index("Resolve-TicketContext")


def test_packaged_wrapper_partitions_state_and_compact_never_loads_it_implicitly():
    outer_source = CODEX_POWERSHELL_PATH.read_text(encoding="utf-8")
    wrapper_source = CODEX_WRAPPER_PATH.read_text(encoding="utf-8")

    assert "[string]$AgentId = 'codex-main'" not in outer_source
    assert "[string]$AgentId = 'codex-main'" not in wrapper_source
    assert "Generic AgentId 'codex-main' is not allowed" in outer_source
    assert "Generic AgentId 'codex-main' is not allowed" in wrapper_source
    assert "$AgentWorkspaceStateDir" in wrapper_source
    assert '"codex_wrapper_states\\{0}\\{1}"' in wrapper_source
    assert "Get-3CanSha256Text $AgentId" in wrapper_source
    assert "workspace_key = $WorkspaceKey" in wrapper_source
    assert "function Test-3CanStateFresh" in wrapper_source
    assert "$MaxInheritedStateAgeSec = 900" in wrapper_source
    assert "latest.json" not in wrapper_source
    assert "$StatePath" not in wrapper_source

    clear_block = outer_source.split("'clear' {", 1)[1].split("'pr-check' {", 1)[0]
    assert "& $Wrapper clear-state -AgentId $AgentId -BaseUrl $BaseUrl" in clear_block

    compact_block = wrapper_source.split("'before-compact' {", 1)[1].split(
        "'check-ticket' {", 1
    )[0]
    assert "Load-State" not in compact_block
    assert "-RequireLiveTicket" in compact_block
    assert "-RequireWorkspaceBinding" in compact_block
    assert "explicit_files_only" in compact_block
    assert compact_block.index("Resolve-TicketContext") < compact_block.index(
        "Invoke-HelperJson $args"
    )
    assert "$expectedPaths.Count -eq 0 -and -not $ExpectedTicketId" in wrapper_source


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


def test_packaged_workspace_key_uses_host_case_semantics_and_non_git_fallback(request):
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

    def workspace_key(wrapper: Path, project: Path) -> str:
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
        state_path = Path(json.loads(result.stdout)["wrapper_state_path"])
        assert state_path.resolve().is_relative_to(project.resolve())
        return json.loads(state_path.read_text(encoding="utf-8-sig"))["workspace_key"]

    if os.name == "nt":
        project = tmp_path / "CaseProject"
        wrapper = install(project)
        assert workspace_key(wrapper, project) == workspace_key(
            Path(str(wrapper).swapcase()), project
        )
    else:
        upper_project = tmp_path / "CaseProject"
        lower_project = tmp_path / "caseproject"
        assert workspace_key(install(upper_project), upper_project) != workspace_key(
            install(lower_project), lower_project
        )


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


def test_fail_blocks_when_canonical_v2_contract_is_unavailable(
    monkeypatch, capsys
):
    monkeypatch.setattr(HELPER, "ERROR_KNOWLEDGE_CONTRACT", None)
    monkeypatch.setattr(
        HELPER,
        "ERROR_KNOWLEDGE_CONTRACT_STATUS",
        {
            "status": "blocked",
            "kind": "error_knowledge_contract_unavailable",
            "failures": [{"error": "incompatible canonical contract"}],
        },
    )
    monkeypatch.setattr(
        HELPER,
        "_try_json_request",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("network must not run without the canonical contract")
        ),
    )

    assert HELPER.fail(_failure_args()) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "BLOCKED"
    assert result["error"]["kind"] == "error_knowledge_contract_unavailable"
    assert result["client_graph_projection_attempted"] is False


def test_first_occurrence_stays_local_and_second_promotes(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(HELPER, "LOG_DIR", tmp_path)
    monkeypatch.setattr(HELPER, "PENDING_WRITEBACK_DIR", tmp_path / "pending")
    calls = []
    counts: dict[str, int] = {}

    def fake_request(base_url, path, **kwargs):
        payload = kwargs.get("payload") or {}
        calls.append((path, payload))
        fingerprint = str(payload["fingerprint"])
        counts[fingerprint] = counts.get(fingerprint, 0) + 1
        count = counts[fingerprint]
        return True, {
            "ok": True,
            "status": "RECORDED" if count == 1 else "PROMOTED",
            "idempotent": False,
            "case": {
                "case_id": None if count == 1 else f"ERR-case-{fingerprint.split(':', 1)[1][:24]}",
                "fingerprint": fingerprint,
                "occurrence_count": count,
                "promoted": count >= 2,
                "state": "observed" if count == 1 else "open",
            },
        }

    monkeypatch.setattr(HELPER, "_try_json_request", fake_request)

    assert HELPER.fail(_failure_args()) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["count"] == 1
    assert first["promoted"] is False
    assert first["node_id"] is None
    assert first["server_status"] == "RECORDED"
    assert [path for path, _ in calls] == ["/api/errors/occurrences"]

    assert HELPER.fail(_failure_args()) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["count"] == 2
    assert second["promoted"] is True
    assert second["case_status"] == "open"
    assert second["case_id"].startswith("ERR-case-")
    assert len(second["case_id"]) == len("ERR-case-") + 24
    assert [path for path, _ in calls] == [
        "/api/errors/occurrences",
        "/api/errors/occurrences",
    ]
    assert all("force=true" not in path for path, _ in calls)


def test_recorded_occurrence_seals_legacy_store_without_stopping_caller(
    monkeypatch,
    tmp_path,
    capsys,
):
    monkeypatch.setattr(HELPER, "LOG_DIR", tmp_path)
    monkeypatch.setattr(HELPER, "PENDING_WRITEBACK_DIR", tmp_path / "pending")
    legacy = tmp_path / "loop_signatures.json"
    legacy.write_text(
        json.dumps({"version": 2, "signatures": {}}),
        encoding="utf-8",
    )

    def recorded(base_url, path, **kwargs):
        payload = kwargs["payload"]
        return True, {
            "ok": True,
            "status": "RECORDED",
            "idempotent": False,
            "case": {
                "case_id": None,
                "fingerprint": payload["fingerprint"],
                "occurrence_count": 1,
                "promoted": False,
                "state": "observed",
            },
        }

    monkeypatch.setattr(HELPER, "_try_json_request", recorded)

    assert HELPER.fail(_failure_args()) == 0
    result = json.loads(capsys.readouterr().out)
    checksum = (tmp_path / "loop_signatures.sha256").read_text(
        encoding="ascii"
    ).strip()

    assert result["status"] == "OK"
    assert result["local_store_status"] == "READY"
    assert result["server_status"] == "RECORDED"
    assert hashlib.sha256(legacy.read_bytes()).hexdigest() == checksum


def test_server_promoted_observed_case_is_locally_blocking(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(HELPER, "LOG_DIR", tmp_path)
    monkeypatch.setattr(HELPER, "PENDING_WRITEBACK_DIR", tmp_path / "pending")

    def fake_request(base_url, path, **kwargs):
        payload = kwargs["payload"]
        return True, {
            "ok": True,
            "status": "PROMOTED",
            "case": {
                "case_id": f"ERR-case-{payload['fingerprint'].split(':', 1)[1][:24]}",
                "fingerprint": payload["fingerprint"],
                "occurrence_count": 2,
                "promoted": True,
                "blocking": True,
                "state": "observed",
            },
        }

    monkeypatch.setattr(HELPER, "_try_json_request", fake_request)
    assert HELPER.fail(_failure_args()) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["case_status"] == "open"
    assert result["block_next_blind_retry"] is True
    saved = next(iter(HELPER._load_loop_signatures()["signatures"].values()))
    assert saved["case_status"] == "open"
    assert saved["blocking"] is True


def test_unsupported_occurrence_endpoint_queues_partial_outbox(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(HELPER, "LOG_DIR", tmp_path / "log")
    monkeypatch.setattr(HELPER, "PENDING_WRITEBACK_DIR", tmp_path / "pending")
    calls = []

    def unavailable(base_url, path, **kwargs):
        calls.append((path, kwargs.get("payload")))
        return False, {"http_status": 404, "body": "not found"}

    monkeypatch.setattr(HELPER, "_try_json_request", unavailable)
    assert HELPER.fail(_failure_args()) == 1
    result = json.loads(capsys.readouterr().out)

    assert result["status"] == "PARTIAL"
    assert result["client_graph_projection_attempted"] is False
    assert [path for path, _ in calls] == ["/api/errors/occurrences"]
    outbox = Path(result["outbox_path"])
    queued = json.loads(outbox.read_text(encoding="utf-8"))
    assert queued["kind"] == "error_occurrence"
    assert queued["payload"]["occurrence_id"] == result["occurrence_id"]
    assert queued["payload"]["fingerprint"].startswith("ek2:")


def test_error_occurrence_outbox_replay_is_idempotent_and_removes_file(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(HELPER, "LOG_DIR", tmp_path / "log")
    monkeypatch.setattr(HELPER, "PENDING_WRITEBACK_DIR", tmp_path / "pending")
    monkeypatch.setattr(
        HELPER,
        "_try_json_request",
        lambda *args, **kwargs: (False, {"http_status": 405}),
    )
    assert HELPER.fail(_failure_args()) == 1
    first = json.loads(capsys.readouterr().out)
    occurrence_id = first["occurrence_id"]
    queued_path = Path(first["outbox_path"])

    replay_payloads = []

    def replay(base_url, path, **kwargs):
        payload = kwargs["payload"]
        replay_payloads.append(payload)
        return True, {
            "ok": True,
            "status": "RECORDED",
            "idempotent": True,
            "case": {
                "case_id": None,
                "fingerprint": payload["fingerprint"],
                "occurrence_count": 1,
                "promoted": False,
                "state": "observed",
            },
        }

    monkeypatch.setattr(HELPER, "_try_json_request", replay)
    args = argparse.Namespace(
        base_url="http://127.0.0.1:9700",
        dry_run=False,
        no_sleep=True,
    )
    assert HELPER.flush_pending(args) == 0
    json.loads(capsys.readouterr().out)
    assert [item["occurrence_id"] for item in replay_payloads] == [occurrence_id]
    assert not queued_path.exists()


def test_hook_fast_replay_processes_at_most_one_occurrence_outbox(
    monkeypatch,
    tmp_path,
    capsys,
):
    monkeypatch.setattr(HELPER, "LOG_DIR", tmp_path / "log")
    monkeypatch.setattr(
        HELPER,
        "PENDING_WRITEBACK_DIR",
        tmp_path / "pending",
    )
    monkeypatch.setattr(
        HELPER,
        "_try_json_request",
        lambda *args, **kwargs: (False, {"http_status": 503}),
    )
    assert HELPER.fail(_failure_args()) == 1
    capsys.readouterr()
    assert HELPER.fail(_failure_args()) == 1
    capsys.readouterr()
    queued = sorted((tmp_path / "pending").glob("error-occurrence-*.json"))
    assert len(queued) == 2

    replayed = []

    def replay(base_url, path, **kwargs):
        payload = kwargs["payload"]
        replayed.append(payload["occurrence_id"])
        return True, {
            "ok": True,
            "status": "RECORDED",
            "idempotent": True,
            "case": {
                "case_id": None,
                "fingerprint": payload["fingerprint"],
                "occurrence_count": 1,
                "promoted": False,
                "state": "observed",
            },
        }

    monkeypatch.setattr(HELPER, "_try_json_request", replay)
    result = HELPER._flush_one_error_occurrence_outbox(
        "http://127.0.0.1:9700",
        timeout_seconds=0.2,
    )
    assert result["attempted"] is True
    assert result["posted"] is True
    assert len(replayed) == 1
    assert len(
        list((tmp_path / "pending").glob("error-occurrence-*.json"))
    ) == 1


def test_repeated_case_requires_exact_recent_unresolved_identity(monkeypatch, tmp_path):
    monkeypatch.setattr(HELPER, "LOG_DIR", tmp_path)
    now = datetime.now(timezone.utc).isoformat()
    state = {
        "version": 2,
        "signatures": {
            "abc123": {
                "signature": "abc123",
                "count": 2,
                "case_status": "open",
                "node_id": "ERR-repeated-ticket-unicode-abc123",
                "operation_class": "test",
                "component": "ticket-lifecycle",
                "error_type": "unicode-error",
                "target_files": ["backend/app.py"],
                "last_seen_at": now,
            }
        },
    }
    HELPER._save_loop_signatures(state)

    assert HELPER._repeated_error_policy_hits("generic runtime work", []) == []
    hits = HELPER._repeated_error_policy_hits("investigate abc123", [])
    assert len(hits) == 1
    assert hits[0]["block"] is True

    state["signatures"]["abc123"]["case_status"] = "resolved"
    HELPER._save_loop_signatures(state)
    assert HELPER._repeated_error_policy_hits("investigate abc123", []) == []


def test_same_basename_and_partial_tuple_are_advisory_not_blocking(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(HELPER, "LOG_DIR", tmp_path)
    now = datetime.now(timezone.utc).isoformat()
    fingerprint = HELPER._loop_signature_key(
        "pytest tests/test_ticket.py",
        "UnicodeDecodeError",
        ["backend/app.py"],
        operation_class="test",
        component="ticket-lifecycle",
        error_type="unicode-error",
        project_identity="public-demo",
    )
    HELPER._save_loop_signatures(
        {
            "version": 2,
            "signatures": {
                fingerprint: {
                    "signature": fingerprint,
                    "count": 2,
                    "case_status": "open",
                    "case_id": f"ERR-case-{fingerprint.split(':', 1)[1][:24]}",
                    "node_id": f"ERR-case-{fingerprint.split(':', 1)[1][:24]}",
                    "project_identity": "public-demo",
                    "operation_class": "test",
                    "component": "ticket-lifecycle",
                    "error_type": "unicode-error",
                    "target_files": ["backend/app.py"],
                    "last_seen_at": now,
                }
            },
        }
    )

    basename_hits = HELPER._repeated_error_policy_hits(
        "ordinary application work",
        ["frontend/app.py"],
    )
    assert len(basename_hits) == 1
    assert basename_hits[0]["match_kind"] == "heuristic_warning"
    assert basename_hits[0]["block"] is False

    exact_hits = HELPER._repeated_error_policy_hits(
        "[project_id=public-demo][operation=test]"
        "[component=ticket-lifecycle][error_type=unicode-error]",
        [],
    )
    assert len(exact_hits) == 1
    assert exact_hits[0]["match_kind"] == "exact_identity"
    assert exact_hits[0]["block"] is True

    different_error_hits = HELPER._repeated_error_policy_hits(
        "[project_id=public-demo][operation=test]"
        "[component=ticket-lifecycle][error_type=timeout]",
        [],
    )
    assert different_error_hits
    assert all(hit["block"] is False for hit in different_error_hits)


def test_supervise_preserves_memory_warning_and_fails_closed_on_unknown(
    monkeypatch,
    tmp_path,
    capsys,
):
    monkeypatch.setattr(
        HELPER,
        "resolve_engine_root",
        lambda _override: {"selected": str(tmp_path)},
    )
    monkeypatch.setattr(
        HELPER,
        "_validate_stats",
        lambda *args, **kwargs: (True, ""),
    )
    monkeypatch.setattr(
        HELPER,
        "_try_json_request",
        lambda _base_url, path, **kwargs: (
            True,
            (
                {"total_nodes": 120, "total_edges": 20}
                if path == "/api/stats"
                else {
                    "confidence": "high",
                    "nodes": [],
                    "route_meta": {},
                    "total_nodes": 120,
                    "total_edges": 20,
                }
            ),
        ),
    )
    monkeypatch.setattr(HELPER, "_record_route_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(HELPER, "_record_supervise_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        HELPER,
        "_record_local_token_estimate",
        lambda *args, **kwargs: None,
    )
    args = argparse.Namespace(
        engine_root=str(tmp_path),
        base_url="http://127.0.0.1:9700",
        agent_id="PUBLIC-supervisor",
        task_description="inspect advisory warning",
        target_files=[],
        scope_keywords=[],
        task_type="Edit",
        tool_name="codex-mutate",
        tool_input_summary="",
        ticket_id="",
        mode="slim",
        max_nodes=4,
        budget_tokens=800,
        timeout_seconds=1.0,
        min_nodes=100,
        no_consume_ticket=False,
        skip_ticket=False,
    )

    for raw_status, expected_return, expected_status in (
        ("warn", 0, "warn"),
        ("block", 1, "block"),
        ("unexpected", 1, "block"),
    ):
        monkeypatch.setattr(
            HELPER,
            "build_memory_preflight",
            lambda *args, _status=raw_status, **kwargs: {
                "status": _status,
                "memory_quality": "bounded",
            },
        )
        assert HELPER.supervise(args) == expected_return
        result = json.loads(capsys.readouterr().out)
        memory_gate = next(
            gate for gate in result["gates"]
            if gate["name"] == "memory_preflight"
        )
        assert memory_gate["status"] == expected_status
        assert result["supervision_status"] == expected_status


def test_occurrence_store_recovers_last_good_and_blocks_double_corruption(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(HELPER, "LOG_DIR", tmp_path)
    first = {"version": 2, "signatures": {"one": {"signature": "one", "count": 1}}}
    second = {"version": 2, "signatures": {"two": {"signature": "two", "count": 2}}}
    HELPER._save_loop_signatures(first)
    HELPER._save_loop_signatures(second)

    HELPER._loop_signatures_path().write_bytes(b"{corrupt")
    recovered = HELPER._load_loop_signatures()
    assert recovered["_store_status"] == "PARTIAL"
    assert recovered["signatures"] == first["signatures"]

    HELPER._loop_signatures_last_good_path().write_bytes(b"{also-corrupt")
    try:
        HELPER._load_loop_signatures()
    except HELPER.LoopSignatureStoreError as exc:
        assert "BLOCKED" in str(exc)
    else:
        raise AssertionError("double corruption must not be treated as empty history")


def test_occurrence_store_cross_process_lock_prevents_lost_updates(tmp_path):
    process_count = 4
    log_dir = tmp_path / "log"
    pending_dir = tmp_path / "pending"
    runtime_dir = tmp_path / "runtime"
    env = os.environ.copy()
    env.update(
        {
            "THREECAN_ENGINE_ROOT": str(STAGING_ROOT / "neural-memory"),
            "THREECAN_LOG_DIR": str(log_dir),
            "THREECAN_PENDING_WRITEBACK_DIR": str(pending_dir),
            "THREECAN_LOCAL_RUNTIME_DIR": str(runtime_dir),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
    )
    command = [
        sys.executable,
        str(HELPER_PATH),
        "--base-url",
        "http://127.0.0.1:1",
        "fail",
        "--agent-id",
        "codex-lock-test",
        "--command-summary",
        "pytest tests/test_ticket.py",
        "--error-excerpt",
        "UnicodeDecodeError while parsing child output",
        "--target-file",
        "backend/app.py",
        "--scope-keyword",
        "ticket-lifecycle",
        "--operation-class",
        "test",
        "--component",
        "ticket-lifecycle",
        "--error-type",
        "unicode-error",
    ]
    processes = [
        subprocess.Popen(
            command,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        for _ in range(process_count)
    ]
    outputs = [process.communicate(timeout=30) for process in processes]
    assert all(process.returncode == 1 for process in processes), outputs

    data = (log_dir / "loop_signatures.json").read_bytes()
    checksum = (log_dir / "loop_signatures.sha256").read_text(
        encoding="ascii"
    ).strip()
    assert hashlib.sha256(data).hexdigest() == checksum
    payload = json.loads(data.decode("utf-8"))
    entries = list(payload["signatures"].values())
    assert len(entries) == 1
    assert entries[0]["count"] == process_count
    assert len(entries[0]["occurrence_ids"]) == process_count


def test_done_requires_evidence_and_updates_local_resolution(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(HELPER, "LOG_DIR", tmp_path)
    node_id = "ERR-repeated-ticket-unicode-abc123"
    HELPER._save_loop_signatures(
        {
            "version": 2,
            "signatures": {
                "abc123": {
                    "signature": "abc123",
                    "count": 2,
                    "case_status": "diagnosed",
                    "node_id": node_id,
                    "last_seen_at": datetime.now(timezone.utc).isoformat(),
                }
            },
        }
    )
    base = {
        "agent_id": "codex-test",
        "base_url": "http://127.0.0.1:9700",
        "detail": "fixed",
        "affected_nodes": [],
        "ticket_id": "rt_test",
        "meta": "",
        "resolved_errors": [node_id],
        "root_cause": "child process encoding was implicit",
        "solution_summary": "",
        "verification_evidence": [],
        "fixed_in": "abcde12",
    }
    assert HELPER.done(argparse.Namespace(**base)) == 2
    assert json.loads(capsys.readouterr().out)["error"]["kind"] == "resolution_evidence_required"

    calls = []

    server_response = {"ok": True, "ticket_state": "completed"}

    def fake_request(base_url, path, **kwargs):
        calls.append((path, kwargs["payload"]))
        return True, dict(server_response)

    monkeypatch.setattr(HELPER, "_try_json_request", fake_request)
    monkeypatch.setattr(HELPER, "_record_local_token_estimate", lambda *args, **kwargs: None)
    base["solution_summary"] = "force UTF-8 for the Python child process"
    evidence = {
        "kind": "test_result",
        "ref": "README.md",
        "digest": "sha256:" + ("a" * 64),
        "verified": True,
        "verifier": "pytest",
    }
    base["verification_evidence"] = [json.dumps(evidence)]

    assert HELPER.done(argparse.Namespace(**base)) == 0
    result = json.loads(capsys.readouterr().out)
    assert calls[0][0] == "/api/activity/done"
    assert result["resolved_errors"] == []
    assert result["local_resolution_state_updated"] == []
    saved = HELPER._load_loop_signatures()["signatures"]["abc123"]
    assert saved["case_status"] == "diagnosed"

    server_response["resolved_errors"] = [{
        "error_id": node_id,
        "resolution_id": None,
        "evidence_id": None,
        "case_status": "review_required",
    }]
    server_response["resolution_outcome"] = "review_required"
    assert HELPER.done(argparse.Namespace(**base)) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["resolved_errors"] == []
    assert result["review_required_errors"] == [node_id]
    assert result["local_resolution_state_updated"] == []
    assert result["local_review_required_state_updated"] == [node_id]
    saved = HELPER._load_loop_signatures()["signatures"]["abc123"]
    assert saved["case_status"] == "review_required"

    server_response["resolved_errors"] = [
        {
            "error_id": node_id,
            "resolution_id": "FIX-PUBLIC",
            "evidence_id": "EVD-PUBLIC",
            "case_status": "resolved",
        }
    ]
    server_response["resolution_outcome"] = "resolved"
    assert HELPER.done(argparse.Namespace(**base)) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["resolved_errors"] == [node_id]
    assert result["local_resolution_state_updated"] == [node_id]
    saved = HELPER._load_loop_signatures()["signatures"]["abc123"]
    assert saved["case_status"] == "resolved"
    assert saved["verification_evidence"] == [evidence]
