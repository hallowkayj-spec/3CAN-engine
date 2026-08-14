from __future__ import annotations

import importlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import urllib.parse
from urllib.error import URLError
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
TOOLS = ROOT / "tools"
RELEASE_ROOT = ROOT.parent
PROJECT_KIT_HELPER = (
    RELEASE_ROOT
    / "examples"
    / "codex-cli-project-kit"
    / "scripts"
    / "3can_codex.py"
)
PROJECT_KIT_CAPSULE_TEMPLATE = (
    RELEASE_ROOT
    / "examples"
    / "codex-cli-project-kit"
    / ".agents"
    / "project.template.json"
)
ROUTE_BENCHMARK = ROOT / "benchmark" / "route_benchmark_v1.json"
ROUTE_BENCHMARK_RUNNER = ROOT / "benchmark" / "run_benchmark.py"
VERIFY_PROJECT = RELEASE_ROOT / "scripts" / "verify_project.py"
INIT_PROJECT = RELEASE_ROOT / "scripts" / "init-project.ps1"
CLAUDE_SUBAGENT_STOP_HOOK = (
    RELEASE_ROOT / "examples" / "claude-code-hooks" / "3can-subagent-stop.js"
)
MCP_SERVER = ROOT / "mcp_server.py"


def clear_3can_modules() -> None:
    for name in ("seed_nodes", "graph_engine", "models", "project_bootstrapper"):
        sys.modules.pop(name, None)


def load_verify_project():
    spec = importlib.util.spec_from_file_location(
        "release_verify_project",
        VERIFY_PROJECT,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_mcp_server():
    spec = importlib.util.spec_from_file_location("release_mcp_server", MCP_SERVER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_seed_nodes_are_current_schema_and_idempotent(tmp_path, monkeypatch):
    graph_dir = tmp_path / "graph"
    monkeypatch.setenv("THREECAN_GRAPH_DIR", str(graph_dir))
    monkeypatch.syspath_prepend(str(BACKEND))
    clear_3can_modules()

    graph_engine = importlib.import_module("graph_engine")
    graph_engine._embed_model = graph_engine._HashingEmbeddingModel()
    seed_nodes = importlib.import_module("seed_nodes")

    assert seed_nodes._seed_internal_owner("ERR-policy") == "error-migration"
    assert seed_nodes._seed_internal_owner("FIX-solution") == "error-migration"
    assert seed_nodes._seed_internal_owner("EVD-receipt") == "error-migration"
    assert seed_nodes._seed_internal_owner("INTF-route") == "durable-seed"
    assert seed_nodes._seed_internal_owner("PROC-bootstrap") == "durable-seed"
    assert seed_nodes._seed_internal_owner("DEC-owner") == "durable-seed"
    assert seed_nodes._seed_internal_owner("DOC-quickstart") is None

    assert seed_nodes.main() == 0
    assert seed_nodes.main() == 0

    node_ids = {path.stem for path in (graph_dir / "nodes").glob("*.json")}
    assert node_ids == {node.id for node in seed_nodes.SEED_NODES}
    assert {
        "DOC-3can-quickstart",
        "DEC-3can-project-graph-isolation",
        "PROC-3can-standing-orders",
        "INTF-3can-token-usage-api",
        "ERR-3can-local-path-rebinding",
        "SEC-3can-secret-reference-policy",
    }.issubset(node_ids)

    edges = json.loads((graph_dir / "edges.json").read_text(encoding="utf-8"))
    edge_keys = {(edge["source"], edge["target"], edge["type"]) for edge in edges}
    assert len(edges) == len(edge_keys)
    assert all("source_id" not in edge and "target_id" not in edge for edge in edges)


def test_project_bootstrapper_uses_env_and_cli_base_url(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / "README.md").write_text("# Demo Project\n\nA small smoke target.\n", encoding="utf-8")

    monkeypatch.setenv("THREECAN_BASE_URL", "http://127.0.0.1:9719")
    monkeypatch.syspath_prepend(str(TOOLS))
    clear_3can_modules()
    bootstrapper = importlib.import_module("project_bootstrapper")
    assert bootstrapper.THREE_CAN == "http://127.0.0.1:9719"

    result = subprocess.run(
        [
            sys.executable,
            str(TOOLS / "project_bootstrapper.py"),
            "--project",
            str(project),
            "--base-url",
            "http://127.0.0.1:9720",
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        env={**os.environ, "THREECAN_BASE_URL": "http://127.0.0.1:9719"},
    )
    assert result.returncode == 0, result.stderr
    assert "DOC-seed-readme" in result.stdout


def test_prerelease_scan_is_project_root_relative(tmp_path):
    script = RELEASE_ROOT / "scripts" / "prerelease_scan.py"
    result = subprocess.run(
        [sys.executable, str(script), str(RELEASE_ROOT)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "high-confidence secret" not in result.stdout


def test_prerelease_scan_accepts_complete_extracted_package(tmp_path):
    package = tmp_path / "3can-engine"
    shutil.copytree(
        RELEASE_ROOT,
        package,
        ignore=shutil.ignore_patterns(".git", ".pytest_cache", "__pycache__"),
    )
    script = package / "scripts" / "prerelease_scan.py"
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            str(package),
            "--strict",
            "--extracted-package",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "[pre-release scan] clean" in result.stdout


def test_windows_sidecar_launcher_is_identity_bound_and_receipted():
    source = INIT_PROJECT.read_text(encoding="utf-8")

    for required in (
        "Get-NetTCPConnection -State Listen -ErrorAction Stop",
        "THREECAN_SIDECAR_START_BUSY",
        "runtime_identity.engine_root_sha256",
        "runtime_identity.graph_root_sha256",
        "readiness.development_ready",
        "-PassThru",
        ".sidecar-owner.json",
        "[System.IO.File]::Replace",
    ):
        assert required in source
    assert 'Write-Host "[3CAN] started $BaseUrl"' not in source


def test_prerelease_scan_blocks_runtime_graph_artifacts(tmp_path):
    root = tmp_path / "release"
    graph = root / "neural-memory" / "graph"
    graph.mkdir(parents=True)
    (graph / "README.md").write_text("runtime graph docs\n", encoding="utf-8")
    (graph / "token_usage.sqlite3").write_bytes(b"sqlite placeholder")

    script = RELEASE_ROOT / "scripts" / "prerelease_scan.py"
    result = subprocess.run(
        [sys.executable, str(script), str(root), "--strict"],
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 1
    assert "runtime graph artifacts" in result.stdout
    assert "token_usage.sqlite3" in result.stdout


def test_subagent_stop_hook_persists_only_content_free_metadata():
    source = CLAUDE_SUBAGENT_STOP_HOOK.read_text(encoding="utf-8")

    assert "response_length_bytes" in source
    assert "response_sha256" in source
    assert "detail: summary" not in source
    assert ".slice(0, 300)" not in source


def test_mcp_route_returns_exact_correlation_and_read_node_forwards_it(monkeypatch):
    server = load_mcp_server()
    posted = {}
    fetched = {}

    def fake_post(path, body):
        posted.update({"path": path, "body": body})
        return {
            "total_nodes": 1,
            "nodes": [{"id": "DOC-PUBLIC", "name": "Public node"}],
            "route_meta": {"route_id": "route-public-1"},
        }

    def fake_get(path, params=None):
        fetched.update({"path": path, "params": params})
        return {"id": "DOC-PUBLIC", "name": "Public node", "content": {}}

    monkeypatch.setattr(server, "_post", fake_post)
    monkeypatch.setattr(server, "_get", fake_get)

    route_text = server.route(
        "public query",
        session_instance_id="session-public-1",
    )
    server.read_node(
        "DOC-PUBLIC",
        session_instance_id="session-public-1",
        route_id="route-public-1",
    )

    assert posted["body"]["session_instance_id"] == "session-public-1"
    assert "route_id=route-public-1" in route_text
    assert "session_instance_id=session-public-1" in route_text
    assert fetched == {
        "path": "/api/nodes/DOC-PUBLIC",
        "params": {
            "agent_id": "mcp-client",
            "route_id": "route-public-1",
            "session_instance_id": "session-public-1",
        },
    }


def test_mcp_uses_configured_sidecar_endpoint(monkeypatch):
    monkeypatch.setenv("THREECAN_BASE_URL", "http://127.0.0.1:9711/")
    monkeypatch.delenv("THREECAN_URL", raising=False)

    server = load_mcp_server()

    assert server.BASE == "http://127.0.0.1:9711"


def test_mcp_read_without_correlation_is_read_only(monkeypatch):
    server = load_mcp_server()
    calls = []
    monkeypatch.setattr(
        server,
        "_get",
        lambda path, params=None: (
            calls.append((path, params))
            or {"id": "DOC-PUBLIC", "name": "Public node", "content": {}}
        ),
    )

    server.read_node("DOC-PUBLIC")

    assert calls == [("/api/nodes/DOC-PUBLIC", None)]


def load_project_kit_helper():
    spec = importlib.util.spec_from_file_location(
        "release_project_kit_helper",
        PROJECT_KIT_HELPER,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_project_kit_documents_durable_capsule_setup_without_runtime_state():
    capsule = json.loads(PROJECT_KIT_CAPSULE_TEMPLATE.read_text(encoding="utf-8"))
    assert set(capsule) == {
        "schema_version",
        "project_id",
        "project_namespace",
        "project_name",
        "project_root",
        "git_repository",
    }
    assert capsule["schema_version"] == 1
    assert capsule["project_root"] == "."
    assert not {
        "threecan_base_url",
        "threecan_engine_root",
        "agent_id_prefixes",
        "frontend_ports",
    }.intersection(capsule)

    readme = (RELEASE_ROOT / "README.md").read_text(encoding="utf-8")
    kit_doc = (RELEASE_ROOT / "docs" / "PROJECT_KIT.md").read_text(
        encoding="utf-8"
    )
    for document in (readme, kit_doc):
        assert ".agents/project.template.json" in document
        assert ".agents/project.json" in document
        assert "project_identity.status=pass" in document


def make_project_kit_identity_worktrees(tmp_path: Path) -> tuple[Path, Path]:
    repository = tmp_path / "repository"
    linked = tmp_path / "linked-worktree"
    repository.mkdir()

    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(repository), *args],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return result.stdout.strip()

    git("init")
    git("config", "user.email", "project-kit@example.invalid")
    git("config", "user.name", "Project Kit Test")
    git("remote", "add", "origin", "git@github.com:Example/Project-Kit.git")
    capsule = repository / ".agents" / "project.json"
    capsule.parent.mkdir()
    capsule.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "project_id": "project-kit",
                "project_namespace": "project-kit",
                "project_name": "Project Kit",
                "project_root": ".",
                "git_repository": "github.com/example/project-kit",
                # These legacy environment fields must not govern identity.
                "threecan_base_url": "http://127.0.0.1:1",
                "agent_id_prefixes": ["some-other-agent"],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (repository / "root-target.txt").write_text("root target\n", encoding="utf-8")
    git("add", ".agents/project.json", "root-target.txt")
    git("commit", "-m", "test fixture")
    git("worktree", "add", "-b", "test/linked", str(linked))
    return repository, linked


def project_kit_ticket_args(target_file: str) -> SimpleNamespace:
    return SimpleNamespace(
        agent_id="codex-project-kit-test",
        base_url="http://3can.test",
        scope_keywords=["identity"],
        target_files=[target_file],
        task_description="Verify linked worktree identity",
        task_type="Edit",
    )


def assert_project_execution_payload(
    helper,
    payload: dict[str, object],
    linked: Path,
) -> None:
    context = helper._execution_context()
    assert payload["project_id"] == "project-kit"
    assert payload["project_namespace"] == "project-kit"
    assert payload["workspace_id"] == context["workspace_id"]
    assert payload["target_files"] == [
        helper._canonical_physical_path(linked / "root-target.txt")
    ]


def test_project_kit_binds_linked_worktree_identity_and_rejects_outside_target(
    tmp_path,
    monkeypatch,
):
    helper = load_project_kit_helper()
    _, linked = make_project_kit_identity_worktrees(tmp_path)
    monkeypatch.setattr(helper, "PROJECT_ROOT", linked)

    context = helper._execution_context()
    common_dir = Path(
        subprocess.run(
            [
                "git",
                "-C",
                str(linked),
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
    )
    expected_workspace = (
        f"git-{helper._local_path_sha256(common_dir)[:12]}-"
        f"{helper._local_path_sha256(linked)[:12]}"
    )
    assert context == {
        "project_id": "project-kit",
        "project_namespace": "project-kit",
        "workspace_id": expected_workspace,
    }
    windows_path = "\\".join(("C:", "Users", "Example", "Project", ".git"))
    wsl_path = "/" + "/".join(("mnt", "c", "Users", "Example", "Project", ".git"))
    assert helper._canonical_physical_path(
        windows_path
    ) == helper._canonical_physical_path(wsl_path)

    root_target = helper._resolved_target_files(["root-target.txt"])
    assert root_target == [helper._canonical_physical_path(linked / "root-target.txt")]
    assert helper._resolved_target_files([str(linked / "root-target.txt")]) == root_target
    with pytest.raises(ValueError, match="target_path_outside_project_root"):
        helper._resolved_target_files([str(tmp_path / "outside.txt")])

    gate = helper._project_identity_gate(
        "http://127.0.0.1:9701",
        {"selected": "", "source": "test"},
        agent_id="unrelated-agent",
        command="route",
    )
    assert gate["status"] == "pass"
    assert {check["name"] for check in gate["checks"]}.isdisjoint(
        {"base_url", "agent_id"}
    )


@pytest.mark.parametrize(
    "repository_value",
    [
        "https://github.com/Example/Project-Kit.git",
        "git@github.com:Example/Project-Kit.git",
        "github.com/example/project-kit.git",
        "github.com/example/project-kit/",
    ],
)
def test_project_kit_rejects_noncanonical_capsule_repository(
    tmp_path,
    monkeypatch,
    repository_value,
):
    helper = load_project_kit_helper()
    _, linked = make_project_kit_identity_worktrees(tmp_path)
    capsule_path = linked / ".agents" / "project.json"
    capsule = json.loads(capsule_path.read_text(encoding="utf-8"))
    capsule["git_repository"] = repository_value
    capsule_path.write_text(json.dumps(capsule), encoding="utf-8")
    monkeypatch.setattr(helper, "PROJECT_ROOT", linked)

    gate = helper._project_identity_gate(
        "http://127.0.0.1:9701",
        {"selected": "", "source": "test"},
        command="doctor",
    )

    repository_check = next(
        check for check in gate["checks"] if check["name"] == "git_repository"
    )
    assert gate["status"] == "block"
    assert repository_check["error"] == "git_repository_not_normalized"


def test_project_kit_rejects_missing_capsule_repository_and_origin(
    tmp_path,
    monkeypatch,
):
    helper = load_project_kit_helper()
    repository, linked = make_project_kit_identity_worktrees(tmp_path)
    subprocess.run(
        ["git", "-C", str(repository), "remote", "remove", "origin"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    capsule_path = linked / ".agents" / "project.json"
    capsule = json.loads(capsule_path.read_text(encoding="utf-8"))
    capsule["git_repository"] = ""
    capsule_path.write_text(json.dumps(capsule), encoding="utf-8")
    monkeypatch.setattr(helper, "PROJECT_ROOT", linked)

    gate = helper._project_identity_gate(
        "http://127.0.0.1:9701",
        {"selected": "", "source": "test"},
        command="doctor",
    )

    repository_check = next(
        check for check in gate["checks"] if check["name"] == "git_repository"
    )
    assert gate["status"] == "block"
    assert repository_check["error"] == "git_repository_missing"


def test_project_kit_blocks_missing_capsule_mutation_before_http(
    tmp_path,
    monkeypatch,
):
    helper = load_project_kit_helper()
    _, linked = make_project_kit_identity_worktrees(tmp_path)
    (linked / ".agents" / "project.json").unlink()
    output: list[dict[str, object]] = []
    monkeypatch.setattr(helper, "PROJECT_ROOT", linked)
    monkeypatch.setattr(helper, "_print_json", output.append)
    monkeypatch.setattr(
        helper,
        "resolve_engine_root",
        lambda _override=None: {"selected": "", "source": "test"},
    )
    monkeypatch.setattr(
        helper,
        "ticket",
        lambda _args: pytest.fail("missing capsule mutation must not run"),
    )

    result = helper.main(
        [
            "ticket",
            "--agent-id",
            "codex-project-kit-test",
            "--task-description",
            "blocked unbound mutation",
            "--target-file",
            "root-target.txt",
        ]
    )

    assert result == 1
    assert output[0]["error"]["kind"] == "project_identity_gate_blocked"
    assert output[0]["project_identity"]["reason"] == (
        "project_capsule_required_for_mutation"
    )


def test_project_kit_ticket_and_prepare_send_bound_identity(
    tmp_path,
    monkeypatch,
):
    helper = load_project_kit_helper()
    _, linked = make_project_kit_identity_worktrees(tmp_path)
    monkeypatch.setattr(helper, "PROJECT_ROOT", linked)
    monkeypatch.setattr(helper, "_print_json", lambda _payload: None)
    monkeypatch.setattr(helper, "_record_local_token_estimate", lambda *args, **kwargs: None)

    captured: list[tuple[str, dict[str, object] | None]] = []
    expected_target = helper._canonical_physical_path(linked / "root-target.txt")

    def request(_base_url, path, *, method="GET", payload=None, timeout=8.0):
        assert timeout > 0
        captured.append((path, payload))
        if path == "/api/stats":
            return True, {"total_nodes": 100, "healthy": True}
        if path == "/api/route":
            return True, {"confidence": "high", "nodes": [], "total_nodes": 100}
        if path == "/api/route/ticket":
            return True, {
                "ticket_id": "TKT-project-kit",
                "agent_id": "codex-project-kit-test",
                "task_description": "Verify linked worktree identity",
                "target_digest": "a" * 64,
                "scope_digest": "b" * 64,
                "scope": {
                    "target_files": [expected_target],
                    "scope_keywords": ["identity"],
                },
            }
        if path.endswith("/consume"):
            return True, {"ok": True}
        raise AssertionError(path)

    monkeypatch.setattr(helper, "_try_json_request", request)
    args = project_kit_ticket_args("root-target.txt")
    assert helper.ticket(args) == 0
    ticket_payload = next(payload for path, payload in captured if path == "/api/route/ticket")
    assert ticket_payload is not None
    assert_project_execution_payload(helper, ticket_payload, linked)

    captured.clear()
    prepare_args = SimpleNamespace(
        **vars(args),
        tool_name="apply_patch",
        tool_input_summary="Update root target",
    )
    assert helper.prepare(prepare_args) == 0
    prepare_payload = next(payload for path, payload in captured if path == "/api/route/ticket")
    assert prepare_payload is not None
    assert_project_execution_payload(helper, prepare_payload, linked)


def test_project_kit_rejects_mutation_project_mismatch_bypass(monkeypatch):
    helper = load_project_kit_helper()
    output: list[dict[str, object]] = []
    monkeypatch.setattr(helper, "_print_json", output.append)
    monkeypatch.setattr(
        helper,
        "ticket",
        lambda _args: pytest.fail("mutation command must not run"),
    )

    result = helper.main(
        [
            "--allow-project-mismatch",
            "ticket",
            "--agent-id",
            "codex-project-kit-test",
            "--task-description",
            "blocked bypass",
            "--target-file",
            "root-target.txt",
        ]
    )

    assert result == 2
    assert output[0]["error"]["kind"] == (
        "project_mismatch_bypass_not_allowed_for_mutation"
    )

    def mismatched_context():
        raise RuntimeError("git_repository_mismatch")

    monkeypatch.setattr(helper, "_execution_context", mismatched_context)
    assert helper._with_execution_context(
        {"task": "read-only diagnosis"},
        allow_project_mismatch=True,
    ) == {"task": "read-only diagnosis"}
    with pytest.raises(RuntimeError, match="git_repository_mismatch"):
        helper._with_execution_context({"task": "mutation"})

    assert helper._project_mismatch_bypass_is_read_only(
        SimpleNamespace(command="route")
    )


def test_project_kit_has_no_production_runtime_lifecycle_authority():
    scripts = [
        PROJECT_KIT_HELPER,
        PROJECT_KIT_HELPER.parent / "codex-3can.ps1",
        PROJECT_KIT_HELPER.parent / "3can_codex_wrapper.ps1",
        ROOT / "init.py",
    ]
    source = "\n".join(path.read_text(encoding="utf-8") for path in scripts)
    for forbidden in (
        "subprocess.Popen",
        "DETACHED_PROCESS",
        "schtasks.exe",
        "THREECAN_SUPERVISOR_TASK_NAME",
        "--start-if-offline",
        "StartIfOffline",
    ):
        assert forbidden not in source


def test_project_kit_derives_stable_agent_identity_without_user_ceremony(
    monkeypatch,
):
    helper = load_project_kit_helper()
    identity_env = (
        "THREECAN_AGENT_ID",
        "CODEX_AGENT_ID",
        "CODEX_THREAD_ID",
        "CODEX_SESSION_ID",
        "THREECAN_SESSION_ID",
        "THREECAN_WORKORDER_ID",
        "WORKORDER_ID",
    )
    for name in identity_env:
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(
        ValueError,
        match="agent_id_stable_execution_identity_required",
    ):
        helper._resolve_agent_id()

    monkeypatch.setenv("CODEX_THREAD_ID", "thread-alpha")
    alpha = helper._resolve_agent_id()
    assert alpha.startswith("codex-thread-alpha-")
    assert helper._resolve_agent_id() == alpha
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-beta")
    assert helper._resolve_agent_id() != alpha
    monkeypatch.setenv("CODEX_THREAD_ID", "thread:collision")
    colon = helper._resolve_agent_id()
    monkeypatch.setenv("CODEX_THREAD_ID", "thread/collision")
    assert helper._resolve_agent_id() != colon

    long_prefix = "codex-" + ("x" * 200)
    assert helper._resolve_agent_id(long_prefix + "-one") != helper._resolve_agent_id(
        long_prefix + "-two"
    )

    monkeypatch.setenv("THREECAN_AGENT_ID", "codex-configured")
    assert helper._resolve_agent_id() == "codex-configured"
    assert helper._resolve_agent_id("codex-explicit") == "codex-explicit"
    with pytest.raises(ValueError, match="generic_agent_id_forbidden"):
        helper._resolve_agent_id("codex-main")

    monkeypatch.delenv("THREECAN_AGENT_ID")
    monkeypatch.delenv("CODEX_THREAD_ID")
    monkeypatch.setenv("THREECAN_WORKORDER_ID", "workorder-final-fallback")
    assert helper._resolve_agent_id().startswith(
        "codex-workorder-final-fallback-"
    )


def test_project_kit_cli_reuses_derived_agent_across_independent_commands(
    monkeypatch,
):
    helper = load_project_kit_helper()
    monkeypatch.delenv("THREECAN_AGENT_ID", raising=False)
    monkeypatch.delenv("CODEX_AGENT_ID", raising=False)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-shared-flow")
    parser = helper.build_parser()
    omitted_value = parser.parse_args(
        ["route", "--agent-id", "--task", "read current project reality"]
    )
    assert omitted_value.agent_id == ""
    commands = (
        ["route", "--task", "read current project reality"],
        [
            "prepare",
            "--task-description",
            "bounded mutation",
            "--target-file",
            "README.md",
            "--tool-name",
            "apply_patch",
            "--tool-input-summary",
            "edit README",
        ],
        [
            "done",
            "--ticket-id",
            "TKT-exact",
            "--detail",
            "bounded mutation complete",
        ],
    )
    resolved = []
    for argv in commands:
        args = parser.parse_args(argv)
        assert args.agent_id == ""
        resolved.append(helper._resolve_agent_id(args.agent_id))
    assert len(set(resolved)) == 1
    assert resolved[0].startswith("codex-thread-shared-flow-")


def test_project_kit_writeback_overrides_file_identity_with_current_execution(
    monkeypatch,
    tmp_path,
):
    helper = load_project_kit_helper()
    payload_path = tmp_path / "writeback.json"
    payload_path.write_text(
        json.dumps({"agent_id": "stale-agent", "changes": []}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-current-writeback")
    monkeypatch.setattr(
        helper,
        "_with_execution_context",
        lambda payload: {**payload, "project_id": "public-demo"},
    )
    captured = {}
    monkeypatch.setattr(
        helper,
        "_try_json_request",
        lambda _base, _path, **kwargs: (
            captured.update(kwargs["payload"]) is None,
            {"ok": True},
        ),
    )
    monkeypatch.setattr(helper, "_print_json", lambda _payload: None)
    monkeypatch.setattr(helper, "_record_local_token_estimate", lambda *args, **kwargs: None)
    args = helper.build_parser().parse_args(
        ["writeback", "--file", str(payload_path)]
    )
    args.agent_id = helper._resolve_agent_id(args.agent_id)

    assert helper.writeback(args) == 0
    assert captured["agent_id"].startswith("codex-thread-current-writeback-")
    assert captured["agent_id"] != "stale-agent"


def test_project_kit_session_correlation_is_derived_without_local_state():
    helper = load_project_kit_helper()
    session_id = helper._session_id_for_agent("codex-thread-one")
    assert helper._session_id_for_agent("codex-thread-one") == session_id
    assert helper._session_id_for_agent("codex-thread-two") != session_id
    assert not hasattr(helper, "LOCAL_RUNTIME_DIR")


def test_project_kit_wrapper_has_no_local_ticket_truth():
    scripts = PROJECT_KIT_HELPER.parent
    helper_source = PROJECT_KIT_HELPER.read_text(encoding="utf-8")
    wrapper = (scripts / "3can_codex_wrapper.ps1").read_text(encoding="utf-8")
    outer = (scripts / "codex-3can.ps1").read_text(encoding="utf-8")
    for forbidden in (
        "codex_wrapper_states",
        "Load-State",
        "Save-State",
        "THREECAN_TICKET_ID",
        "show-state",
        "clear-state",
        "wrapper_state_",
    ):
        assert forbidden not in wrapper
    assert "'state'" not in outer
    assert "'clear'" not in outer
    assert "'supervise'" not in outer
    assert "'supervise-status'" not in outer
    assert "_record_supervise_state" not in helper_source
    assert "route-freshness" not in helper_source
    assert "last_route" not in helper_source
    assert "session_{_safe_id_part" not in helper_source
    for forbidden_policy in (
        "memory-preflight",
        "failure-gate-sync",
        "flush-pending",
        "loop_signatures",
        "error_disposition_tickets",
    ):
        assert forbidden_policy not in helper_source
        assert forbidden_policy not in outer
    assert "done requires explicit -TicketId" in wrapper


def test_project_kit_hook_commands_resolve_to_shipped_scripts():
    project_kit = PROJECT_KIT_HELPER.parents[1]
    hook_config = json.loads(
        (project_kit / ".codex" / "hooks.json").read_text(encoding="utf-8")
    )
    commands = [
        hook["command"]
        for groups in hook_config["hooks"].values()
        for group in groups
        for hook in group.get("hooks", [])
        if hook.get("type") == "command"
    ]
    for command in commands:
        parts = command.split()
        assert parts[0] == "python"
        assert (project_kit / parts[1]).is_file(), command


def test_active_client_guidance_keeps_runtime_machine_owned():
    active_paths = (
        RELEASE_ROOT / "3CAN.md",
        RELEASE_ROOT / "README.md",
        RELEASE_ROOT / "README.en.md",
        RELEASE_ROOT / "docs" / "PROJECT_KIT.md",
        RELEASE_ROOT / "docs" / "USER_GUIDE.md",
        RELEASE_ROOT / "docs" / "specs" / "3CAN_ENGINE" / "AGENT_BINDING.md",
        RELEASE_ROOT
        / "docs"
        / "specs"
        / "3CAN_ENGINE"
        / "recipes"
        / "CODEX_CLI_INTEGRATION.md",
        RELEASE_ROOT / "examples" / "codex-cli-project-kit" / "AGENTS.template.md",
    )
    source = "\n".join(path.read_text(encoding="utf-8") for path in active_paths)
    for forbidden in (
        "starts backend/proxy when allowed and offline",
        "checks or starts 3CAN",
        "request Supervisor recovery",
        "bootstrap the correct project-local 3CAN endpoint",
        "Resolve the blocked supervisor gates",
    ):
        assert forbidden not in source


def test_project_kit_offline_probe_is_typed_and_read_only(monkeypatch, tmp_path):
    helper = load_project_kit_helper()
    engine_root = tmp_path / "neural-memory"
    graph_root = engine_root / "graph"
    monkeypatch.setattr(
        helper,
        "resolve_engine_root",
        lambda override=None: {
            "selected": str(engine_root),
            "valid_engine_root": True,
        },
    )
    monkeypatch.setattr(helper, "_selected_graph_root", lambda root: graph_root)
    monkeypatch.setattr(
        helper,
        "_project_identity_gate",
        lambda *args, **kwargs: {"status": "pass"},
    )
    unavailable = {
        "status": "UNAVAILABLE",
        "kind": "threecan_runtime_unavailable",
        "reason": "connection refused",
    }
    monkeypatch.setattr(
        helper,
        "_probe_stats",
        lambda *args, **kwargs: (
            False,
            unavailable,
            False,
            {"kind": "offline"},
        ),
    )

    result = helper.ensure_online(
        "http://3can.test",
        engine_root_override=None,
        min_nodes=10,
    )

    assert result["online"] is False
    assert result["started"] is False
    assert result["healthy"] is False
    assert result["code"] == "THREECAN_RUNTIME_UNAVAILABLE"
    assert result["error"] == unavailable


@pytest.mark.parametrize(
    ("path", "method", "payload"),
    [
        ("/api/route", "POST", {"task": "route"}),
        ("/api/route/ticket", "POST", {"task_description": "ticket"}),
        ("/api/writeback", "POST", {"node_id": "DOC-test"}),
    ],
)
def test_project_kit_transport_failure_is_typed_unavailable(
    monkeypatch, path, method, payload
):
    helper = load_project_kit_helper()

    def unavailable(*_args, **_kwargs):
        raise URLError("connection refused")

    monkeypatch.setattr(helper, "urlopen", unavailable)
    ok, result = helper._try_json_request(
        "http://3can.test", path, method=method, payload=payload
    )

    assert ok is False
    assert result == {
        "status": "UNAVAILABLE",
        "kind": "threecan_runtime_unavailable",
        "reason": "connection refused",
    }


def test_offline_hooks_do_not_claim_production_runtime_lifecycle():
    hooks = RELEASE_ROOT / "examples" / "claude-code-hooks"
    behavioral = (hooks / "3can-behavioral-gate.js").read_text(encoding="utf-8")
    cold_start = (hooks / "3can-cold-start.js").read_text(encoding="utf-8")
    source = behavioral + "\n" + cold_start

    for forbidden in (
        "ENGINE_BOOTSTRAP_OK",
        "schtasks",
        "backend/app.py --port 9700",
        "engine_offline_mutating",
    ):
        assert forbidden not in source
    assert "runtime_unavailable_local_work_continues" in behavioral
    assert "OFFLINE_HARD_DENY" in behavioral
    assert "THREECAN_URL" in source


def test_offline_behavioral_gate_allows_local_work_and_keeps_independent_safety(
    tmp_path,
):
    hook = RELEASE_ROOT / "examples" / "claude-code-hooks" / "3can-behavioral-gate.js"
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "USERPROFILE": str(tmp_path),
        "THREECAN_URL": "http://127.0.0.1:1",
    }

    def decision(tool_name: str, tool_input: dict[str, str]) -> str | None:
        completed = subprocess.run(
            ["node", str(hook)],
            input=json.dumps({"tool_name": tool_name, "tool_input": tool_input}),
            text=True,
            encoding="utf-8",
            capture_output=True,
            env=env,
            timeout=10,
            check=True,
        )
        if not completed.stdout.strip():
            return None
        return json.loads(completed.stdout)["hookSpecificOutput"].get(
            "permissionDecision"
        )

    assert decision(
        "Bash", {"command": "curl -X POST http://127.0.0.1:5900/api/test"}
    ) is None
    assert decision(
        "Bash", {"command": "curl -X POST http://127.0.0.1:9700/api/test"}
    ) == "deny"
    assert decision(
        "Bash", {"command": "curl -X POST http://127.0.0.1:17890/api/test"}
    ) == "deny"
    assert decision(
        "Bash", {"command": "curl -X POST https://external.example/api/test"}
    ) == "deny"
    assert decision(
        "Bash",
        {"command": "curl -X POST http://127.0.0.1:5900/api/test && echo done"},
    ) == "deny"
    assert decision(
        "Bash",
        {
            "command": (
                "curl -X POST http://127.0.0.1:5900/api/test "
                "--url external.example/api/test"
            )
        },
    ) == "deny"
    assert decision(
        "Bash",
        {
            "command": (
                'curl -X POST http://127.0.0.1:5900/api/test "$(echo unsafe)"'
            )
        },
    ) == "deny"
    assert decision(
        "Bash", {"command": "curl -X POST http://127.0.0.1:5900/api;whoami"}
    ) == "deny"
    assert decision("Bash", {"command": "pytest -q"}) is None
    assert decision("Edit", {"file_path": "local.txt", "new_string": "safe"}) is None
    assert decision("Bash", {"command": "rm -rf ./tmp"}) == "deny"
    assert decision("Bash", {"command": "npm publish"}) == "deny"


def test_verifier_types_development_readiness_and_can_require_production(
    monkeypatch,
):
    verifier = load_verify_project()

    def request(_method, url, _payload=None, timeout=10):
        assert timeout > 0
        if url.endswith("/api/health/live"):
            return True, {"alive": True}
        if url.endswith("/api/stats?deep=true"):
            return True, {
                "total_nodes": 14,
                "total_edges": 12,
                "readiness": {
                    "mode": "development",
                    "development_ready": True,
                    "production_ready": False,
                    "reasons": [{"code": "development_mode_not_production"}],
                },
            }
        if url.endswith("/api/route"):
            assert "project_id" not in (_payload or {})
            assert "project_namespace" not in (_payload or {})
            return True, {
                "nodes": [{"id": "DOC-3can-quickstart"}],
                "route_meta": {
                    "owner_defaults": {
                        "status": "applied",
                        "source": "3CAN.md",
                        "digest": "sha256:" + "a" * 64,
                    }
                },
            }
        if url.endswith("/api/nodes/DOC-3can-quickstart"):
            return True, {"id": "DOC-3can-quickstart"}
        if url.endswith("/api/token-usage/health"):
            return True, {}
        raise AssertionError(url)

    monkeypatch.setattr(verifier, "request_json", request)
    monkeypatch.setattr(
        sys,
        "argv",
        ["verify_project.py", "--base-url", "http://3can.test"],
    )
    assert verifier.main() == 0

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "verify_project.py",
            "--base-url",
            "http://3can.test",
            "--require-production-ready",
        ],
    )
    assert verifier.main() == 1


def test_verifier_exercises_project_isolation_writeback_and_error_lifecycle(
    monkeypatch,
):
    verifier = load_verify_project()
    created: dict[str, dict] = {}
    error_calls = 0

    def request(method, url, payload=None, timeout=10):
        nonlocal error_calls
        assert timeout > 0
        if url.endswith("/api/health/live"):
            return True, {"alive": True}
        if url.endswith("/api/stats?deep=true"):
            return True, {
                "total_nodes": 14,
                "total_edges": 12,
                "readiness": {
                    "mode": "development",
                    "development_ready": True,
                    "production_ready": False,
                },
            }
        if method == "POST" and url.endswith("/api/nodes?force=true"):
            created[payload["id"]] = payload
            return True, {"id": payload["id"]}
        if method == "POST" and url.endswith("/api/route"):
            if payload["task"].startswith("publicrcisolation"):
                visible = [
                    {"id": node_id}
                    for node_id, node in created.items()
                    if node["content"]["extra"]["project_id"]
                    == payload["project_id"]
                    and node["content"]["extra"]["project_namespace"]
                    == payload["project_namespace"]
                ]
                return True, {"nodes": visible, "route_meta": {}}
            return True, {
                "nodes": [{"id": "DOC-3can-quickstart"}],
                "route_meta": {
                    "owner_defaults": {
                        "status": "applied",
                        "source": "3CAN.md",
                        "digest": "sha256:" + "b" * 64,
                    }
                },
            }
        if method == "POST" and url.endswith("/api/writeback"):
            node_id = payload["changes"][0]["node_id"]
            created[node_id]["content"]["notes"] = payload["changes"][0][
                "value"
            ]
            return True, {"updated": [node_id]}
        if method == "POST" and url.endswith("/api/errors/occurrences"):
            error_calls += 1
            return True, {
                "status": "RECORDED" if error_calls == 1 else "PROMOTED"
            }
        if method == "GET" and "/api/nodes/" in url:
            node_id = urllib.parse.unquote(url.rsplit("/", 1)[-1])
            if node_id == "DOC-3can-quickstart":
                return True, {"id": node_id}
            return True, {"id": node_id, "content": created[node_id]["content"]}
        if url.endswith("/api/token-usage/health"):
            return True, {}
        raise AssertionError((method, url, payload))

    monkeypatch.setattr(verifier, "request_json", request)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "verify_project.py",
            "--base-url",
            "http://3can.test",
            "--exercise-writes",
            "--project-id",
            "public-rc",
            "--project-namespace",
            "public-scope",
        ],
    )

    assert verifier.main() == 0
    assert len(created) == 3
    created_scopes = {
        (
            node["content"]["extra"]["project_id"],
            node["content"]["extra"]["project_namespace"],
        )
        for node in created.values()
    }
    assert created_scopes == {
        ("public-rc", "public-scope"),
        ("public-rc-other", "public-scope"),
        ("public-rc", "public-scope-other"),
    }
    assert error_calls == 2


def test_route_benchmark_rejects_the_wrong_graph_fixture():
    suite = json.loads(ROUTE_BENCHMARK.read_text(encoding="utf-8"))
    spec = importlib.util.spec_from_file_location(
        "release_route_benchmark",
        ROUTE_BENCHMARK_RUNNER,
    )
    assert spec and spec.loader
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)

    required = set(suite["graph_binding"]["required_node_ids"])
    invalid = runner.validate_graph_binding(
        suite,
        node_exists=lambda node_id: node_id == "DOC-3can-quickstart",
    )
    valid = runner.validate_graph_binding(
        suite,
        node_exists=lambda node_id: node_id in required,
    )

    assert invalid["ok"] is False
    assert len(invalid["missing"]) == len(required) - 1
    assert valid["ok"] is True
    assert runner.any_at_k(["DOC-other", "DOC-expected"], ["DOC-expected"], 3) == 1.0
    assert runner.any_at_k(["DOC-other"], ["DOC-expected"], 3) == 0.0
