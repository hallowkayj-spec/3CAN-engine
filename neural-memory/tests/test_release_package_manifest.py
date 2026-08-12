from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
SCANNER_PATH = PACKAGE_ROOT / "scripts" / "prerelease_scan.py"
SPEC = importlib.util.spec_from_file_location("threecan_prerelease_scan", SCANNER_PATH)
assert SPEC and SPEC.loader
SCANNER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SCANNER
SPEC.loader.exec_module(SCANNER)


def _write_manifest(root: Path, required_paths: list[str]) -> None:
    (root / SCANNER.PACKAGE_MANIFEST).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "package": "fixture",
                "required_paths": required_paths,
            }
        ),
        encoding="utf-8",
    )


def test_real_release_package_manifest_is_complete() -> None:
    # Structural completeness is testable before the final explicit-path
    # staging step.  Git tracking is exercised independently below and by the
    # pre-release scanner in CI.
    assert SCANNER.scan_package_manifest(
        PACKAGE_ROOT,
        verify_git_tracking=False,
    ) == []


def test_manifest_rejects_missing_duplicate_and_escaping_paths(tmp_path: Path) -> None:
    required = tmp_path / "present.txt"
    required.write_text("ok", encoding="utf-8")
    _write_manifest(
        tmp_path,
        ["present.txt", "present.txt", "missing.txt", "../outside.txt"],
    )

    findings = SCANNER.scan_package_manifest(tmp_path)

    assert any("duplicate" in finding for finding in findings)
    assert any("missing" in finding for finding in findings)
    assert any("escapes package" in finding for finding in findings)


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def test_manifest_rejects_required_files_not_tracked_by_git(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    present = tmp_path / "present.txt"
    present.write_text("ok", encoding="utf-8")
    _write_manifest(tmp_path, ["present.txt"])
    _git(tmp_path, "add", SCANNER.PACKAGE_MANIFEST)

    findings = SCANNER.scan_package_manifest(tmp_path)

    assert any("required file is not tracked by Git: present.txt" in item for item in findings)

    _git(tmp_path, "add", "present.txt")
    assert SCANNER.scan_package_manifest(tmp_path) == []


def test_manifest_fails_closed_outside_a_git_checkout(tmp_path: Path) -> None:
    (tmp_path / "present.txt").write_text("ok", encoding="utf-8")
    _write_manifest(tmp_path, ["present.txt"])

    findings = SCANNER.scan_package_manifest(tmp_path)

    assert any("not inside a readable Git checkout" in item for item in findings)


def test_runtime_scan_rejects_databases_outside_graph(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "route_ticket_ledger.sqlite3").write_bytes(b"runtime")
    (state / "route_ticket_ledger.sqlite3-wal").write_bytes(b"runtime")
    (state / "route_ticket_ledger.sqlite3-shm").write_bytes(b"runtime")
    (state / "route_ticket_ledger.sqlite3-journal").write_bytes(b"runtime")

    assert SCANNER.scan_runtime_artifacts(tmp_path) == [
        "state/route_ticket_ledger.sqlite3",
        "state/route_ticket_ledger.sqlite3-journal",
        "state/route_ticket_ledger.sqlite3-shm",
        "state/route_ticket_ledger.sqlite3-wal",
    ]


def test_runtime_scan_allows_only_documentation_in_graph(tmp_path: Path) -> None:
    graph = tmp_path / "neural-memory" / "graph"
    graph.mkdir(parents=True)
    (graph / "README.md").write_text("runtime state is local", encoding="utf-8")

    assert SCANNER.scan_runtime_artifacts(tmp_path) == []

    (graph / "activity_log.json").write_text("[]", encoding="utf-8")
    assert SCANNER.scan_runtime_artifacts(tmp_path) == [
        "neural-memory/graph/activity_log.json"
    ]


def test_runtime_scan_rejects_shared_lock_and_project_kit_state(
    tmp_path: Path,
) -> None:
    runtime_files = [
        "neural-memory/.3can-locks/graph-demo.lock",
        "examples/codex-cli-project-kit/data/_3can_runtime/session-demo.json",
        "examples/codex-cli-project-kit/data/_3can_pending_writeback/pending.json",
        "examples/codex-cli-project-kit/test-results/3can/receipt.json",
    ]
    for rel in runtime_files:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("runtime state", encoding="utf-8")

    assert SCANNER.scan_runtime_artifacts(tmp_path) == sorted(runtime_files)


def test_public_benchmark_fixtures_are_synthetic_and_private_graph_free() -> None:
    fixture_path = (
        PACKAGE_ROOT
        / "neural-memory"
        / "benchmark"
        / "route_benchmark_v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    evidence = fixture["release_evidence"]

    assert len(fixture["queries"]) == 46
    assert fixture["fixture_scope"] == "synthetic_public"
    assert evidence["status"] == "synthetic_public_fixture"
    assert evidence["reproducible_from_release_package"] is True
    assert evidence["requires_excluded_runtime_graph"] is False
    assert evidence["raw_result_receipt_included"] is True
    receipt = PACKAGE_ROOT / evidence["result_receipt"]
    assert receipt.is_file()
    receipt_payload = json.loads(receipt.read_text(encoding="utf-8"))
    assert receipt_payload["status"] == "VERIFIED_CANDIDATE"
    assert receipt_payload["route_benchmark"]["queries"] == 46
    for relative_path, expected_sha256 in receipt_payload["source_sha256"].items():
        source_path = PACKAGE_ROOT / relative_path
        assert source_path.is_file()
        git_bytes = source_path.read_bytes().replace(b"\r\n", b"\n")
        assert hashlib.sha256(git_bytes).hexdigest() == expected_sha256
    assert evidence["fixture_changed_after_recorded_2026_04_15_run"] is True
    assert evidence["historical_2026_04_15_score_reproducible"] is False

    substrate = json.loads(
        (
            PACKAGE_ROOT
            / "neural-memory"
            / "benchmark"
            / "substrate_bench_v1.json"
        ).read_text(encoding="utf-8")
    )
    assert len(substrate["cases"]) == 10
    assert substrate["fixture_scope"] == "synthetic_public"
    assert substrate["release_evidence"]["status"] == "synthetic_public_fixture"

    seed_tree = ast.parse(
        (
            PACKAGE_ROOT / "neural-memory" / "backend" / "seed_nodes.py"
        ).read_text(encoding="utf-8")
    )
    seed_ids = {
        keyword.value.value
        for call in ast.walk(seed_tree)
        if isinstance(call, ast.Call)
        and getattr(call.func, "id", None) == "NodeCreate"
        for keyword in call.keywords
        if keyword.arg == "id"
        and isinstance(keyword.value, ast.Constant)
        and isinstance(keyword.value.value, str)
    }
    fixture_ids = {
        node_id
        for query in fixture["queries"]
        for node_id in [query["expected_top1"], *query["expected_any3"]]
    }
    substrate_ids = {
        node_id
        for case in substrate["cases"]
        for node_id in [case["expected_top1"], *case["expected_top3"]]
    }
    assert fixture_ids <= seed_ids
    assert substrate_ids <= seed_ids
    assert SCANNER.scan_public_benchmark_fixtures(PACKAGE_ROOT) == []


def test_public_benchmark_scan_rejects_private_derived_content(
    tmp_path: Path,
) -> None:
    benchmark = tmp_path / "neural-memory" / "benchmark"
    benchmark.mkdir(parents=True)
    safe_payload = {
        "fixture_scope": "synthetic_public",
        "release_evidence": {
            "status": "synthetic_public_fixture",
            "requires_excluded_runtime_graph": False,
        },
        "queries": [],
    }
    for rel in SCANNER.PUBLIC_BENCHMARK_FIXTURES:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(safe_payload), encoding="utf-8")

    private_payload = dict(safe_payload)
    private_payload["queries"] = [
        {"query": "DeepSeek API key", "expected_top1": "SES-20260419-private"}
    ]
    (tmp_path / SCANNER.PUBLIC_BENCHMARK_FIXTURES[0]).write_text(
        json.dumps(private_payload),
        encoding="utf-8",
    )

    findings = SCANNER.scan_public_benchmark_fixtures(tmp_path)
    assert any("private-derived benchmark content" in item for item in findings)


def test_real_numpy_load_consumers_are_reviewed_and_pickle_disabled() -> None:
    assert SCANNER.scan_numpy_load_policy(PACKAGE_ROOT) == []


def test_numpy_load_policy_rejects_pickle_and_unreviewed_consumers(
    tmp_path: Path,
) -> None:
    for rel in SCANNER.TRUSTED_NUMPY_LOAD_CONSUMERS:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "import numpy as np\nnp.load('cache.npz', allow_pickle=False)\n",
            encoding="utf-8",
        )

    unsafe = tmp_path / sorted(SCANNER.TRUSTED_NUMPY_LOAD_CONSUMERS)[0]
    unsafe.write_text(
        "import numpy as np\nnp.load('cache.npz', allow_pickle=True)\n",
        encoding="utf-8",
    )
    rogue = tmp_path / "neural-memory" / "tools" / "unreviewed_loader.py"
    rogue.write_text(
        "import numpy as numeric\nnumeric.load('cache.npz', allow_pickle=False)\n",
        encoding="utf-8",
    )

    findings = SCANNER.scan_numpy_load_policy(tmp_path)
    assert any("must set allow_pickle=False" in item for item in findings)
    assert any("not in the reviewed allowlist" in item for item in findings)


def test_project_kit_ignore_template_covers_local_runtime_state() -> None:
    assert SCANNER.scan_project_kit_runtime_ignores(PACKAGE_ROOT) == []


def test_secure_evidence_configuration_is_documented_without_a_secret() -> None:
    env_values = {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in (PACKAGE_ROOT / ".env.example").read_text(
            encoding="utf-8"
        ).splitlines()
        if line and not line.startswith("#") and "=" in line
    }
    assert env_values["THREECAN_TARGET_ROOTS"] == ""
    assert env_values["THREECAN_EVIDENCE_ROOTS"] == ""
    assert env_values["THREECAN_EVIDENCE_HMAC_KEY"] == ""
    assert env_values["THREECAN_EVIDENCE_MAX_BYTES"] == "4194304"

    documentation = "\n".join(
        [
            (PACKAGE_ROOT / "README.md").read_text(encoding="utf-8"),
            (PACKAGE_ROOT / "README.en.md").read_text(encoding="utf-8"),
            (
                PACKAGE_ROOT
                / "docs"
                / "specs"
                / "3CAN_ENGINE"
                / "EVIDENCE.md"
            ).read_text(encoding="utf-8"),
        ]
    )
    for required in (
        "THREECAN_TARGET_ROOTS",
        "THREECAN_EVIDENCE_ROOTS",
        "THREECAN_EVIDENCE_HMAC_KEY",
        "3can.verification-attestation/v1",
        "hmac-sha256:",
        "review_required",
    ):
        assert required in documentation


def test_graph_ignore_is_deny_by_default_but_keeps_readme(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text(
        (PACKAGE_ROOT / ".gitignore").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    _git(tmp_path, "init", "-q")
    private_paths = [
        "neural-memory/graph/maintenance/legacy_error_migration/backups/run/nodes/ERR-x.json",
        "neural-memory/graph/maintenance/legacy_error_migration/archives/run.jsonl",
        "neural-memory/graph/.legacy_error_migration.lock",
        "neural-memory/graph/route_ticket_ledger.sqlite3-wal",
        "neural-memory/.3can-locks/graph-demo.lock",
        "examples/codex-cli-project-kit/data/_3can_pending_writeback/pending.json",
        "examples/codex-cli-project-kit/data/_3can_runtime/session-demo.json",
        "examples/codex-cli-project-kit/test-results/3can/receipt.json",
    ]
    for rel in private_paths:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("private", encoding="utf-8")
        result = subprocess.run(
            ["git", "-C", str(tmp_path), "check-ignore", "--quiet", "--", rel],
            check=False,
        )
        assert result.returncode == 0, rel

    readme = tmp_path / "neural-memory" / "graph" / "README.md"
    readme.write_text("runtime state is local", encoding="utf-8")
    result = subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "check-ignore",
            "--quiet",
            "--",
            "neural-memory/graph/README.md",
        ],
        check=False,
    )
    assert result.returncode == 1


def test_real_release_license_policy_is_coherent() -> None:
    assert SCANNER.scan_license_policy(PACKAGE_ROOT) == []


def test_license_policy_rejects_wrong_header_and_stale_candidates(
    tmp_path: Path,
) -> None:
    (tmp_path / "LICENSE").write_text("not the selected license", encoding="utf-8")
    for rel in SCANNER.LICENSE_POLICY_DOCS:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "PolyForm Noncommercial; source-available; not OSI open source.",
            encoding="utf-8",
        )
    stale_wording = "\n".join(
        [
            "PolyForm " + "Shield is the old license",
            "建议 " + "GPL-3.0 或 MPL-2.0，许可证" + "还没选",
            "开源 License 暂缓，等评测跑完再定",
            "请提供许可证选择建议",
        ]
    )
    (tmp_path / "stale.md").write_text(stale_wording, encoding="utf-8")

    findings = SCANNER.scan_license_policy(tmp_path)

    assert any("expected PolyForm Noncommercial" in item for item in findings)
    assert sum("conflicting current-license wording" in item for item in findings) >= 4
    assert sum("commercial-use boundary" in item for item in findings) == len(
        SCANNER.LICENSE_POLICY_DOCS
    )
