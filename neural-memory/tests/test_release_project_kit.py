from __future__ import annotations

import importlib
import importlib.util
import json
import os
import subprocess
import sys
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


def test_project_kit_ticket_prepare_and_supervise_send_bound_identity(
    tmp_path,
    monkeypatch,
):
    helper = load_project_kit_helper()
    _, linked = make_project_kit_identity_worktrees(tmp_path)
    monkeypatch.setattr(helper, "PROJECT_ROOT", linked)
    monkeypatch.setattr(helper, "_print_json", lambda _payload: None)
    monkeypatch.setattr(helper, "_record_local_token_estimate", lambda *args, **kwargs: None)
    monkeypatch.setattr(helper, "_record_error_disposition_ticket", lambda *args, **kwargs: None)
    monkeypatch.setattr(helper, "_record_route_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(helper, "_record_supervise_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        helper,
        "build_memory_preflight",
        lambda *args, **kwargs: {
            "status": "pass",
            "memory_quality": {},
            "policy_hits": [],
            "must_do_next": [],
            "must_not_do": [],
        },
    )

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

    captured.clear()
    engine_root = tmp_path / "engine"
    monkeypatch.setattr(
        helper,
        "resolve_engine_root",
        lambda override=None: {"selected": str(engine_root), "source": "test"},
    )
    monkeypatch.setattr(helper, "_selected_graph_root", lambda _root: engine_root / "graph")
    monkeypatch.setattr(helper, "_validate_stats", lambda *args, **kwargs: (True, None))
    supervise_args = SimpleNamespace(
        **vars(prepare_args),
        engine_root=None,
        min_nodes=10,
        max_nodes=8,
        mode="slim",
        budget_tokens=1200,
        timeout_seconds=10.0,
        ticket_id="",
        no_consume_ticket=True,
        skip_ticket=False,
    )
    assert helper.supervise(supervise_args) == 0
    route_payload = next(payload for path, payload in captured if path == "/api/route")
    supervise_payload = next(
        payload for path, payload in captured if path == "/api/route/ticket"
    )
    assert route_payload is not None
    assert route_payload["project_id"] == "project-kit"
    assert route_payload["project_namespace"] == "project-kit"
    assert route_payload["workspace_id"] == helper._execution_context()["workspace_id"]
    assert supervise_payload is not None
    assert_project_execution_payload(helper, supervise_payload, linked)


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
        SimpleNamespace(command="failure-gate-sync", apply=False)
    )
    assert not helper._project_mismatch_bypass_is_read_only(
        SimpleNamespace(command="failure-gate-sync", apply=True)
    )


def test_project_kit_has_no_direct_runtime_launcher():
    source = PROJECT_KIT_HELPER.read_text(encoding="utf-8")
    assert "subprocess.Popen" not in source
    assert "DETACHED_PROCESS" not in source
    assert "_stop_threecan_port_processes" not in source
    assert "_terminate_verified_threecan_process" not in source
    assert "_cleanup_launched_runtime" not in source
    assert "THREECAN_SUPERVISOR_TASK_NAME" in source


def test_project_kit_offline_recovery_uses_supervisor(monkeypatch, tmp_path):
    helper = load_project_kit_helper()
    engine_root = tmp_path / "neural-memory"
    graph_root = engine_root / "graph"
    probes = iter([
        (False, {"error": "offline"}, False, {"kind": "offline"}),
        (
            True,
            {
                "total_nodes": 100,
                "total_edges": 10,
                "healthy": True,
                "readiness": {"production_ready": True},
            },
            True,
            None,
        ),
    ])
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
    monkeypatch.setattr(helper, "_probe_stats", lambda *args, **kwargs: next(probes))
    monkeypatch.setattr(
        helper,
        "_request_runtime_supervisor",
        lambda: (True, {"kind": "supervisor_requested"}),
    )
    monkeypatch.setattr(helper, "_proxy_state", lambda root: None)

    result = helper.ensure_online(
        "http://3can.test",
        engine_root_override=None,
        start_if_offline=True,
        wait_seconds=1,
        min_nodes=10,
    )

    assert result["online"] is True
    assert result["healthy"] is True
    assert result["code"] == "THREECAN_SUPERVISOR_RECOVERED"


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
            return True, {"nodes": [{"id": "DOC-3can-quickstart"}]}
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
