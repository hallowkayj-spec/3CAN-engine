from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (
    PACKAGE_ROOT
    / "examples"
    / "codex-cli-project-kit"
    / "scripts"
    / "3can_convergence.py"
)
HOOKS = PACKAGE_ROOT / "examples" / "codex-cli-project-kit" / ".codex" / "hooks.json"
MANIFEST = PACKAGE_ROOT / "RELEASE_PACKAGE_MANIFEST.json"


def load_module():
    spec = importlib.util.spec_from_file_location("codex_convergence", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def convergence(tmp_path: Path) -> tuple[object, Path, Path, Path]:
    module = load_module()
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Convergence Test"],
        check=True,
    )
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    receipt = tmp_path / "test-results" / "3can" / "convergence" / "receipt.json"
    (tmp_path / ".gitignore").write_text("test-results/\n", encoding="utf-8")
    (tmp_path / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", ".gitignore", "tracked.txt"], check=True
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-qm", "fixture"], check=True
    )
    return module, tmp_path, codex_dir / "convergence.json", receipt


def write_contract(path: Path, *, owner_review: bool = True, guards=None) -> None:
    checks = [
        {
            "id": "diff-check",
            "type": "command",
            "argv": ["git", "diff", "--check"],
            "stages": ["episode", "final"],
            "timeout_seconds": 10,
        },
        {
            "id": "artifact",
            "type": "artifact",
            "path": "tracked.txt",
            "min_bytes": 1,
            "stages": ["final"],
        },
    ]
    if owner_review:
        checks.append(
            {"id": "owner-review", "type": "owner_review", "stages": ["final"]}
        )
    acceptance = [
        {
            "id": "mechanical-integrity",
            "text": "The declared automated evidence passes.",
            "evidence": ["diff-check", "artifact"],
        }
    ]
    if owner_review:
        acceptance.append(
            {
                "id": "owner-acceptance",
                "text": "The Owner accepts the final result.",
                "evidence": ["owner-review"],
            }
        )
    path.write_text(
        json.dumps(
            {
                "schema": "3can.convergence-contract/v1",
                "status": "active",
                "scope": "current_repository_only",
                "goal": "Deliver one reusable convergence foundation.",
                "acceptance": acceptance,
                "non_goals": ["Do not merge or deploy."],
                "checks": checks,
                "guards": guards or [],
            }
        ),
        encoding="utf-8",
    )


def hook(module, root: Path, contract: Path, receipt: Path, payload: dict) -> dict:
    old_stdin = sys.stdin
    old_stdout = sys.stdout
    sys.stdin = io.StringIO(json.dumps(payload))
    sys.stdout = io.StringIO()
    try:
        assert module.run_hook(root, contract, receipt) == 0
        return json.loads(sys.stdout.getvalue())
    finally:
        sys.stdin = old_stdin
        sys.stdout = old_stdout


def test_missing_contract_is_noop(convergence):
    module, root, contract, receipt = convergence

    output = hook(module, root, contract, receipt, {"hook_event_name": "Stop"})

    assert output == {}


def test_contract_rejects_path_traversal(convergence):
    module, root, contract, _ = convergence
    write_contract(contract)
    value = json.loads(contract.read_text(encoding="utf-8"))
    value["checks"][1]["path"] = "../outside.txt"

    with pytest.raises(module.ContractError, match="escapes the project root"):
        module.validate_contract(value, root)


def test_contract_requires_automated_final_evidence(convergence):
    module, root, contract, _ = convergence
    write_contract(contract)
    value = json.loads(contract.read_text(encoding="utf-8"))
    value["checks"] = [
        {"id": "owner-review", "type": "owner_review", "stages": ["final"]}
    ]

    with pytest.raises(module.ContractError, match="automated check"):
        module.validate_contract(value, root)


def test_visual_acceptance_without_bound_evidence_cannot_converge(convergence):
    module, root, contract, receipt = convergence
    write_contract(contract, owner_review=False)
    value = json.loads(contract.read_text(encoding="utf-8"))
    value["acceptance"] = [
        {
            "id": "visual-quality",
            "text": "The final video is visually correct.",
            "evidence": [],
        }
    ]
    contract.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(module.ContractError, match="must not be empty"):
        module.verify(
            root,
            contract,
            receipt,
            stage="final",
            next_objective="Obtain a visual review.",
        )


def test_legacy_string_acceptance_cannot_silently_converge(convergence):
    module, root, contract, _ = convergence
    write_contract(contract, owner_review=False)
    value = json.loads(contract.read_text(encoding="utf-8"))
    value["acceptance"] = ["The final video is visually correct."]

    with pytest.raises(module.ContractError, match="must bind text to evidence"):
        module.validate_contract(value, root)


def test_receipt_path_cannot_escape_project(convergence, tmp_path):
    module, root, contract, _ = convergence
    write_contract(contract)

    with pytest.raises(module.ContractError, match="receipt path escapes"):
        module.verify(
            root,
            contract,
            tmp_path.parent / "outside-receipt.json",
            stage="final",
            next_objective="",
        )


def test_compact_session_reinjects_only_contract_and_receipt(convergence):
    module, root, contract, receipt = convergence
    write_contract(contract)
    module.record_typed(
        root,
        contract,
        receipt,
        outcome="PARTIAL",
        reason="One validation remains.",
        next_objective="Run the final check.",
    )

    output = hook(
        module,
        root,
        contract,
        receipt,
        {"hook_event_name": "SessionStart", "source": "compact"},
    )
    context = output["hookSpecificOutput"]["additionalContext"]

    assert "Deliver one reusable convergence foundation." in context
    assert "mechanical-integrity: The declared automated evidence passes." in context
    assert "PARTIAL (current)" in context
    assert "Run the final check." in context
    assert "transcript" not in context.lower()
    assert len(context) <= module.MAX_CONTEXT_CHARS


@pytest.mark.parametrize("source", ["startup", "resume", "compact"])
def test_session_start_restores_contract_for_supported_sources(convergence, source):
    module, root, contract, receipt = convergence
    write_contract(contract)

    output = hook(
        module,
        root,
        contract,
        receipt,
        {"hook_event_name": "SessionStart", "source": source},
    )

    assert "additionalContext" in output["hookSpecificOutput"]
    assert "Deliver one reusable convergence foundation." in output[
        "hookSpecificOutput"
    ]["additionalContext"]


def test_stop_blocks_once_then_requires_honest_partial(convergence):
    module, root, contract, receipt = convergence
    write_contract(contract)

    first = hook(
        module,
        root,
        contract,
        receipt,
        {"hook_event_name": "Stop", "stop_hook_active": False},
    )
    second = hook(
        module,
        root,
        contract,
        receipt,
        {"hook_event_name": "Stop", "stop_hook_active": True},
    )

    assert first["decision"] == "block"
    assert "Run the declared checks" in first["reason"]
    assert "PARTIAL" in second["systemMessage"]
    assert "decision" not in second


def test_current_partial_receipt_cannot_stop_silently(convergence):
    module, root, contract, receipt = convergence
    write_contract(contract)
    module.record_typed(
        root,
        contract,
        receipt,
        outcome="PARTIAL",
        reason="Visual evidence is missing.",
        next_objective="Run visual review.",
    )

    first = hook(
        module,
        root,
        contract,
        receipt,
        {"hook_event_name": "Stop", "stop_hook_active": False},
    )
    second = hook(
        module,
        root,
        contract,
        receipt,
        {"hook_event_name": "Stop", "stop_hook_active": True},
    )

    assert first["decision"] == "block"
    assert "PARTIAL" in first["reason"]
    assert "do not claim completion" in first["reason"].lower()
    assert "Run visual review" in first["reason"]
    assert "PARTIAL" in second["systemMessage"]


def test_episode_and_final_receipts_keep_owner_acceptance_separate(convergence):
    module, root, contract, receipt = convergence
    write_contract(contract)

    episode = module.verify(
        root,
        contract,
        receipt,
        stage="episode",
        next_objective="Run final validation.",
    )
    final = module.verify(
        root,
        contract,
        receipt,
        stage="final",
        next_objective="Await owner review.",
    )

    assert episode["outcome"] == "PASS"
    assert episode["checkpoint_expectation"] == (
        "normal_git_checkpoint_before_next_destructive_episode"
    )
    assert final["outcome"] == "CANDIDATE_READY"
    assert final["threecan_writeback"] == {
        "eligible_trigger": "NONE",
        "performed": False,
    }
    assert "owner-review" in final["open_check_ids"]
    assert "owner-acceptance" in final["open_acceptance_ids"]
    first_stop = hook(
        module,
        root,
        contract,
        receipt,
        {"hook_event_name": "Stop", "stop_hook_active": False},
    )
    second_stop = hook(
        module,
        root,
        contract,
        receipt,
        {"hook_event_name": "Stop", "stop_hook_active": True},
    )
    assert first_stop["decision"] == "block"
    assert "CANDIDATE_READY" in first_stop["reason"]
    assert "owner-acceptance" in first_stop["reason"]
    assert "CANDIDATE_READY" in second_stop["systemMessage"]


def test_final_without_owner_review_is_converged(convergence):
    module, root, contract, receipt = convergence
    write_contract(contract, owner_review=False)

    final = module.verify(
        root,
        contract,
        receipt,
        stage="final",
        next_objective="",
    )

    assert final["outcome"] == "CONVERGED"
    assert final["threecan_writeback"]["eligible_trigger"] == "AUTO_CLOSEOUT"


def test_command_failure_is_evidence_not_completion(convergence):
    module, root, contract, receipt = convergence
    write_contract(contract, owner_review=False)
    value = json.loads(contract.read_text(encoding="utf-8"))
    value["checks"][0]["argv"] = [sys.executable, "-c", "raise SystemExit(7)"]
    contract.write_text(json.dumps(value), encoding="utf-8")

    result = module.verify(
        root,
        contract,
        receipt,
        stage="final",
        next_objective="Fix the failing check.",
    )

    assert result["outcome"] == "FAIL"
    failed = next(item for item in result["checks"] if item["id"] == "diff-check")
    assert failed["exit_code"] == 7
    assert set(failed["stdout"]) == {"bytes", "sha256"}


def test_declared_guard_requires_current_passed_check(convergence):
    module, root, contract, receipt = convergence
    guards = [
        {
            "tool_name_glob": "exec_command",
            "input_contains": "expensive-operation",
            "requires_check_ids": ["diff-check"],
        }
    ]
    write_contract(contract, guards=guards)
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "exec_command",
        "tool_input": {"cmd": "expensive-operation"},
    }

    denied = hook(module, root, contract, receipt, payload)
    module.verify(
        root,
        contract,
        receipt,
        stage="episode",
        next_objective="Run guarded operation.",
    )
    allowed = hook(module, root, contract, receipt, payload)

    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "diff-check" in denied["hookSpecificOutput"]["permissionDecisionReason"]
    assert allowed == {}


def test_pretool_without_matching_guard_skips_workspace_scan(convergence, monkeypatch):
    module, root, contract, receipt = convergence
    write_contract(contract)
    monkeypatch.setattr(
        module,
        "workspace_fingerprint",
        lambda _root: pytest.fail("unmatched PreToolUse must not scan Git"),
    )

    output = hook(
        module,
        root,
        contract,
        receipt,
        {"hook_event_name": "PreToolUse", "tool_name": "exec_command"},
    )

    assert output == {}


def test_workspace_change_stales_receipt_and_typed_state_allows_stop(convergence):
    module, root, contract, receipt = convergence
    write_contract(contract)
    module.verify(
        root,
        contract,
        receipt,
        stage="final",
        next_objective="Await owner review.",
    )
    (root / "tracked.txt").write_text("changed after receipt\n", encoding="utf-8")

    stale = hook(module, root, contract, receipt, {"hook_event_name": "Stop"})
    module.record_typed(
        root,
        contract,
        receipt,
        outcome="BLOCKED",
        reason="Owner input is required.",
        next_objective="Wait for owner review.",
    )
    typed_first = hook(
        module,
        root,
        contract,
        receipt,
        {"hook_event_name": "Stop", "stop_hook_active": False},
    )
    typed_second = hook(
        module,
        root,
        contract,
        receipt,
        {"hook_event_name": "Stop", "stop_hook_active": True},
    )

    assert stale["decision"] == "block"
    assert typed_first["decision"] == "block"
    assert "BLOCKED" in typed_first["reason"]
    assert "do not claim completion" in typed_first["reason"].lower()
    assert "BLOCKED" in typed_second["systemMessage"]


def test_ignored_artifact_change_stales_converged_receipt(convergence):
    module, root, contract, receipt = convergence
    (root / ".gitignore").write_text("test-results/\nartifacts/\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", ".gitignore"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "ignore artifacts"], check=True)
    artifact = root / "artifacts" / "candidate.bin"
    artifact.parent.mkdir()
    artifact.write_bytes(b"candidate-one")
    write_contract(contract, owner_review=False)
    value = json.loads(contract.read_text(encoding="utf-8"))
    value["checks"][1]["path"] = "artifacts/candidate.bin"
    contract.write_text(json.dumps(value), encoding="utf-8")

    final = module.verify(root, contract, receipt, stage="final", next_objective="")
    assert final["outcome"] == "CONVERGED"
    artifact.write_bytes(b"candidate-two")

    stopped = hook(module, root, contract, receipt, {"hook_event_name": "Stop"})
    assert stopped["decision"] == "block"
    assert "stale" in stopped["reason"].lower()


def test_command_generated_byproduct_is_fresh_without_false_race(convergence):
    module, root, contract, receipt = convergence
    write_contract(contract, owner_review=False)
    value = json.loads(contract.read_text(encoding="utf-8"))
    value["checks"][0]["argv"] = [
        sys.executable,
        "-c",
        "from pathlib import Path; p=Path('test-results/module/report.txt'); "
        "p.parent.mkdir(parents=True, exist_ok=True); p.write_text('current proof\\n')",
    ]
    value["checks"][1].update(
        {"path": "test-results/module/report.txt", "role": "byproduct"}
    )
    contract.write_text(json.dumps(value), encoding="utf-8")

    result = module.verify(root, contract, receipt, stage="final", next_objective="")

    assert result["outcome"] == "CONVERGED"
    (root / "test-results" / "module" / "report.txt").write_text(
        "replaced proof\n", encoding="utf-8"
    )
    stopped = hook(module, root, contract, receipt, {"hook_event_name": "Stop"})
    assert stopped["decision"] == "block"
    assert "stale" in stopped["reason"].lower()


def test_byproduct_is_checked_after_commands_regardless_of_json_order(convergence):
    module, root, contract, receipt = convergence
    write_contract(contract, owner_review=False)
    report = root / "test-results" / "module" / "report.txt"
    report.parent.mkdir(parents=True)
    report.write_text("old passing report\n", encoding="utf-8")
    value = json.loads(contract.read_text(encoding="utf-8"))
    command = value["checks"][0]
    artifact = value["checks"][1]
    command["argv"] = [
        sys.executable,
        "-c",
        "from pathlib import Path; Path('test-results/module/report.txt').write_bytes(b'')",
    ]
    artifact.update(
        {
            "path": "test-results/module/report.txt",
            "role": "byproduct",
            "min_bytes": 1,
        }
    )
    value["checks"] = [artifact, command]
    contract.write_text(json.dumps(value), encoding="utf-8")

    result = module.verify(
        root,
        contract,
        receipt,
        stage="final",
        next_objective="Repair the generated proof.",
    )

    assert result["outcome"] == "FAIL"
    artifact_result = next(item for item in result["checks"] if item["id"] == "artifact")
    assert artifact_result["status"] == "fail"
    assert result["proof_eligible"] is False


def test_contract_disappearance_after_activation_is_not_noop(convergence):
    module, root, contract, receipt = convergence
    write_contract(contract, owner_review=False)
    module.verify(root, contract, receipt, stage="final", next_objective="")
    contract.unlink()

    first = hook(
        module,
        root,
        contract,
        receipt,
        {"hook_event_name": "Stop", "stop_hook_active": False},
    )
    second = hook(
        module,
        root,
        contract,
        receipt,
        {"hook_event_name": "Stop", "stop_hook_active": True},
    )

    assert first["decision"] == "block"
    assert "missing after prior activation" in first["reason"]
    assert "UNAVAILABLE" in first["reason"]
    assert "PARTIAL" in second["systemMessage"]


def test_verification_race_records_conflict_and_cannot_unlock_guard(convergence):
    module, root, contract, receipt = convergence
    guards = [
        {
            "tool_name_glob": "exec_command",
            "input_contains": "expensive-operation",
            "requires_check_ids": ["diff-check"],
        }
    ]
    write_contract(contract, owner_review=False, guards=guards)
    value = json.loads(contract.read_text(encoding="utf-8"))
    value["checks"][0]["argv"] = [
        sys.executable,
        "-c",
        "from pathlib import Path; Path('tracked.txt').write_text('changed during check\\n')",
    ]
    contract.write_text(json.dumps(value), encoding="utf-8")

    result = module.verify(
        root,
        contract,
        receipt,
        stage="final",
        next_objective="Re-run against a stable candidate.",
    )

    assert result["outcome"] == "CONFLICT"
    assert result["proof_eligible"] is False
    assert "changed while checks were running" in result["reason"]
    stopped = hook(module, root, contract, receipt, {"hook_event_name": "Stop"})
    assert stopped["decision"] == "block"
    assert "CONFLICT" in stopped["reason"]
    guarded = hook(
        module,
        root,
        contract,
        receipt,
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "exec_command",
            "tool_input": {"cmd": "expensive-operation"},
        },
    )
    assert guarded["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "eligible evidence" in guarded["hookSpecificOutput"][
        "permissionDecisionReason"
    ]


def test_typed_receipt_cannot_launder_old_passed_checks(convergence):
    module, root, contract, receipt = convergence
    guards = [
        {
            "tool_name_glob": "exec_command",
            "input_contains": "expensive-operation",
            "requires_check_ids": ["diff-check"],
        }
    ]
    write_contract(contract, guards=guards)
    module.verify(
        root,
        contract,
        receipt,
        stage="episode",
        next_objective="Run the next operation.",
    )
    (root / "tracked.txt").write_text("new candidate\n", encoding="utf-8")

    typed = module.record_typed(
        root,
        contract,
        receipt,
        outcome="PARTIAL",
        reason="The candidate changed.",
        next_objective="Verify the new candidate.",
    )
    guarded = hook(
        module,
        root,
        contract,
        receipt,
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "exec_command",
            "tool_input": {"cmd": "expensive-operation"},
        },
    )

    assert typed["checks"] == []
    assert guarded["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "diff-check" in guarded["hookSpecificOutput"]["permissionDecisionReason"]


def test_compaction_marks_stale_objective_and_never_silently_truncates(convergence):
    module, root, contract, receipt = convergence
    write_contract(contract)
    module.verify(
        root,
        contract,
        receipt,
        stage="episode",
        next_objective="Run the previously selected next step.",
    )
    (root / "tracked.txt").write_text("stale\n", encoding="utf-8")

    stale = hook(
        module,
        root,
        contract,
        receipt,
        {"hook_event_name": "SessionStart", "source": "compact"},
    )
    context = stale["hookSpecificOutput"]["additionalContext"]
    assert "Previous next objective (stale; re-evaluate):" in context

    value = json.loads(contract.read_text(encoding="utf-8"))
    value["goal"] = "x" * (module.MAX_CONTEXT_CHARS + 1)
    contract.write_text(json.dumps(value), encoding="utf-8")
    too_large = hook(
        module,
        root,
        contract,
        receipt,
        {"hook_event_name": "SessionStart", "source": "compact"},
    )
    assert "UNAVAILABLE" in too_large["systemMessage"]
    assert "additionalContext" not in too_large


def test_invalid_contract_is_asymmetric_at_stop(convergence):
    module, root, contract, receipt = convergence
    contract.write_text("{not-json", encoding="utf-8")

    first = hook(
        module,
        root,
        contract,
        receipt,
        {"hook_event_name": "Stop", "stop_hook_active": False},
    )
    second = hook(
        module,
        root,
        contract,
        receipt,
        {"hook_event_name": "Stop", "stop_hook_active": True},
    )

    assert first["decision"] == "block"
    assert "UNAVAILABLE" in first["reason"]
    assert "UNAVAILABLE" in second["systemMessage"]
    assert "PARTIAL" in second["systemMessage"]


def test_invalid_contract_fails_open_on_development_path(convergence):
    module, root, contract, receipt = convergence
    contract.write_text("{not-json", encoding="utf-8")

    output = hook(
        module,
        root,
        contract,
        receipt,
        {"hook_event_name": "PreToolUse", "tool_name": "exec_command"},
    )

    assert "UNAVAILABLE" in output["systemMessage"]
    assert "failed open" in output["systemMessage"]
    assert "decision" not in output


def test_nested_project_workspace_paths_are_normalized(tmp_path):
    module = load_module()
    repository = tmp_path / "repository"
    project = repository / "packages" / "project"
    project.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "Nested Test"], check=True
    )
    target = project / "target.txt"
    target.write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-qm", "fixture"], check=True
    )
    target.write_text("after\n", encoding="utf-8")

    workspace = module.workspace_fingerprint(project)

    expected_path_hash = module._sha256_bytes(b"target.txt")
    expected_content = target.read_bytes()
    assert workspace["kind"] == "git"
    expected_files = [
        {
            "path_sha256": expected_path_hash,
            "state": "file",
            "size": len(expected_content),
            "content_sha256": module._sha256_bytes(expected_content),
        }
    ]
    assert workspace["changed_file_count"] == 1
    assert workspace["changed_files_sha256"] == module._sha256_bytes(
        module._json_bytes(expected_files)
    )


def test_dirty_submodule_is_rejected_by_current_repository_scope(tmp_path):
    module = load_module()
    child = tmp_path / "child"
    parent = tmp_path / "parent"
    for repository, name in ((child, "Child"), (parent, "Parent")):
        subprocess.run(["git", "init", "-q", str(repository)], check=True)
        subprocess.run(
            ["git", "-C", str(repository), "config", "user.email", "test@example.invalid"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(repository), "config", "user.name", name], check=True
        )
    (child / "child.txt").write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(child), "add", "child.txt"], check=True)
    subprocess.run(["git", "-C", str(child), "commit", "-qm", "child"], check=True)
    (parent / "parent.txt").write_text("parent\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(parent), "add", "parent.txt"], check=True)
    subprocess.run(["git", "-C", str(parent), "commit", "-qm", "parent"], check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "protocol.file.allow=always",
            "-C",
            str(parent),
            "submodule",
            "add",
            str(child),
            "modules/child",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "-C", str(parent), "commit", "-qam", "submodule"], check=True)
    (parent / "modules" / "child" / "child.txt").write_text(
        "dirty\n", encoding="utf-8"
    )

    with pytest.raises(module.ContractError, match="dirty submodules are unsupported"):
        module.workspace_fingerprint(parent)


@pytest.fixture
def installed_project_kit(tmp_path):
    project_kit = SCRIPT.parents[1]
    installed = tmp_path / "installed-project"
    shutil.copytree(project_kit, installed)
    shutil.copyfile(
        installed / ".codex" / "convergence.example.json",
        installed / ".codex" / "convergence.json",
    )
    shutil.copyfile(installed / ".gitignore.template", installed / ".gitignore")
    (installed / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(installed)], check=True)
    subprocess.run(
        ["git", "-C", str(installed), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(installed), "config", "user.name", "Installed Kit"],
        check=True,
    )
    subprocess.run(["git", "-C", str(installed), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(installed), "commit", "-qm", "installed kit"], check=True
    )
    hooks = json.loads(
        (installed / ".codex" / "hooks.json").read_text(encoding="utf-8")
    )["hooks"]

    def run_hook(event, payload):
        native_hook = hooks[event][0]["hooks"][0]
        command = native_hook["commandWindows" if os.name == "nt" else "command"]
        result = subprocess.run(
            command,
            cwd=installed / "scripts",
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return json.loads(result.stdout)

    return installed, run_hook


@pytest.mark.parametrize("source", ["startup", "resume", "compact"])
def test_installed_project_kit_executes_exact_native_hook_command(
    installed_project_kit, source
):
    _, run_hook = installed_project_kit

    output = run_hook(
        "SessionStart", {"hook_event_name": "SessionStart", "source": source}
    )
    context = output["hookSpecificOutput"]["additionalContext"]
    assert "Replace this with the exact observable outcome" in context
    assert "generic-delivery / v1" in context
    assert "owner-acceptance" in context


def test_installed_project_kit_exact_stop_blocks_stale_and_allows_current(
    installed_project_kit,
):
    installed, run_hook = installed_project_kit

    write_contract(installed / ".codex" / "convergence.json", owner_review=False)
    verified = subprocess.run(
        [
            sys.executable,
            str(installed / "scripts" / "3can_convergence.py"),
            "verify",
            "--stage",
            "final",
        ],
        cwd=installed,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert verified.returncode == 0, verified.stdout + verified.stderr
    assert json.loads(verified.stdout)["outcome"] == "CONVERGED"

    (installed / "tracked.txt").write_text("stale\n", encoding="utf-8")
    stale = run_hook("Stop", {"hook_event_name": "Stop"})
    assert stale["decision"] == "block"
    assert "stale" in stale["reason"]

    (installed / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    assert run_hook("Stop", {"hook_event_name": "Stop"}) == {}


def test_public_hook_configuration_and_package_surface_are_coherent():
    hooks = json.loads(HOOKS.read_text(encoding="utf-8"))["hooks"]
    manifest = set(json.loads(MANIFEST.read_text(encoding="utf-8"))["required_paths"])
    pre_tool_commands = [
        hook["command"]
        for group in hooks["PreToolUse"]
        for hook in group["hooks"]
    ]

    assert "SessionStart" in hooks and "Stop" in hooks and "PreToolUse" in hooks
    assert not any("3can_convergence.py" in item for item in pre_tool_commands)
    assert hooks["PreToolUse"][0]["matcher"] == "Bash|mcp__.*create_pull_request.*"
    assert not any("3can_research_harness.py" in item for item in pre_tool_commands)
    assert "UserPromptSubmit" not in hooks
    assert not any(
        "3can_research_harness.py" in hook["command"]
        for groups in hooks.values()
        for group in groups
        for hook in group["hooks"]
    )
    assert all(
        "commandWindows" in hook
        for event in ("SessionStart", "PreToolUse", "Stop")
        for group in hooks[event]
        for hook in group["hooks"]
        if "3can_convergence.py" in hook["command"]
    )
    convergence_hooks = [
        hook
        for event in ("SessionStart", "PreToolUse", "Stop")
        for group in hooks[event]
        for hook in group["hooks"]
        if "3can_convergence.py" in hook["command"]
    ]
    pr_hooks = [
        hook
        for group in hooks["PreToolUse"]
        for hook in group["hooks"]
        if "3can_pr_harness.py" in hook["command"]
    ]
    assert all(item["timeout"] == 30 for item in convergence_hooks)
    assert all(item["timeout"] == 10 for item in pr_hooks)
    assert {
        "docs/CODEX_CONVERGENCE_HOOK.md",
        "examples/codex-cli-project-kit/.codex/convergence.example.json",
        "examples/codex-cli-project-kit/.codex/task-hooks/generic-delivery.example.json",
        "examples/codex-cli-project-kit/.codex/task-hooks/registry.example.json",
        "examples/codex-cli-project-kit/.codex/hooks.json",
        "examples/codex-cli-project-kit/scripts/3can_convergence.py",
        "examples/codex-cli-project-kit/scripts/task_oracle.py",
        "neural-memory/tests/test_codex_convergence_hook.py",
        "neural-memory/tests/test_task_oracle.py",
    } <= manifest


def test_source_has_no_session_transcript_or_3can_network_dependency():
    source = SCRIPT.read_text(encoding="utf-8")

    for forbidden in (
        "transcript_path",
        "session.jsonl",
        "urllib.request",
        "requests.",
        "127.0.0.1:9700",
        "/api/",
    ):
        assert forbidden not in source
