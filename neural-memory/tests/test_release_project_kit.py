from __future__ import annotations

import importlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path


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
