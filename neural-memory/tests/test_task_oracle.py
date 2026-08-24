from __future__ import annotations

import copy
import importlib.util
import io
import json
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


def load_module():
    spec = importlib.util.spec_from_file_location("task_oracle_convergence", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def init_repo(root: Path) -> tuple[Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "oracle@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "config", "user.name", "Task Oracle Test"],
        check=True,
    )
    (root / ".codex" / "task-hooks").mkdir(parents=True)
    (root / "outputs").mkdir()
    (root / ".gitignore").write_text(
        "test-results/\noutputs/\n", encoding="utf-8"
    )
    (root / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(root), "add", ".gitignore", "tracked.txt"], check=True
    )
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "fixture"], check=True)
    return (
        root / ".codex" / "convergence.json",
        root / "test-results" / "3can" / "convergence" / "receipt.json",
    )


def task_hook(
    *,
    lifecycle: str = "one_off",
    status: str = "EPHEMERAL_ACTIVE",
    candidate: dict | None = None,
    oracles: list[dict] | None = None,
    mutable_bindings: list[str] | None = None,
    allowed_fallback_ids: list[str] | None = None,
) -> dict:
    declared_oracles = oracles or [
        {
            "id": "mechanical-check",
            "type": "command",
            "kind": "DETERMINISTIC",
            "version": "v1",
            "argv": ["git", "diff", "--check"],
            "stages": ["episode", "final"],
            "timeout_seconds": 10,
        }
    ]
    return {
        "schema": "3can.task-hook/v1",
        "task_family": "generic-delivery",
        "status": status,
        "lifecycle": lifecycle,
        "revision": "v1",
        "parent_revision": None,
        "goal": "Produce the current observable candidate without hidden defaults.",
        "applicability": "An explicitly selected generic delivery run.",
        "candidate": {"provider": candidate or {"type": "workspace"}},
        "acceptance": [
            {
                "id": "current-candidate-valid",
                "text": "Declared evidence proves the current candidate.",
                "oracle_ids": [item["id"] for item in declared_oracles],
            }
        ],
        "oracles": declared_oracles,
        "invariants": ["Evidence must target the current candidate."],
        "mutable_bindings": mutable_bindings or [],
        "fallback_policy": "explicit_only",
        "allowed_fallback_ids": allowed_fallback_ids or [],
    }


def write_active(
    module,
    root: Path,
    contract_path: Path,
    task: dict,
    *,
    run_id: str = "run-1",
    bindings: dict | None = None,
    allowed_fallbacks: list[str] | None = None,
) -> tuple[Path, str, dict]:
    task_path = root / ".codex" / "task-hooks" / "generic.json"
    task_path.write_text(json.dumps(task), encoding="utf-8")
    digest = module.task_oracle.sha256_json(task)
    contract = {
        "schema": "3can.convergence-contract/v2",
        "status": "active",
        "scope": "current_repository_only",
        "run_id": run_id,
        "task_hook": {
            "path": ".codex/task-hooks/generic.json",
            "sha256": digest,
            "revision": task["revision"],
        },
        "activation": {
            "task_hook_sha256": digest,
            "confirmed_revision": task["revision"],
            "confirmed_by": "owner",
            "confirmation_ref": "owner-selected-task-family",
        },
        "bindings": bindings or {},
        "allowed_fallbacks": allowed_fallbacks or [],
        "non_goals": ["Do not merge, deploy, publish, or write back automatically."],
        "guards": [],
    }
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    return task_path, digest, contract


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


def receipt_ref(receipt: dict) -> dict:
    return {
        "run_id": receipt["task"]["run_id"],
        "candidate_fingerprint": receipt["task"]["candidate"]["fingerprint"],
        "bindings_sha256": receipt["task"]["bindings_sha256"],
        "receipt_sha256": receipt["receipt_sha256"],
        "outcome": "CONVERGED",
    }


def test_code_candidate_change_stales_current_receipt(tmp_path):
    module = load_module()
    contract, receipt = init_repo(tmp_path)
    write_active(module, tmp_path, contract, task_hook())

    result = module.verify(
        tmp_path, contract, receipt, stage="final", next_objective=""
    )
    (tmp_path / "tracked.txt").write_text("changed\n", encoding="utf-8")
    stopped = hook(module, tmp_path, contract, receipt, {"hook_event_name": "Stop"})

    assert result["outcome"] == "CONVERGED"
    assert result["task"]["candidate"]["status"] == "PASS"
    assert stopped["decision"] == "block"
    assert "stale" in stopped["reason"]


def test_ignored_artifact_candidate_change_stales_old_proof(tmp_path):
    module = load_module()
    contract, receipt = init_repo(tmp_path)
    candidate_path = tmp_path / "outputs" / "candidate.bin"
    candidate_path.write_bytes(b"first")
    task = task_hook(candidate={"type": "artifact", "path": "outputs/candidate.bin"})
    write_active(module, tmp_path, contract, task)
    module.verify(tmp_path, contract, receipt, stage="final", next_objective="")

    candidate_path.write_bytes(b"second")
    stopped = hook(module, tmp_path, contract, receipt, {"hook_event_name": "Stop"})

    assert stopped["decision"] == "block"
    assert "stale" in stopped["reason"]


def test_large_generic_artifact_requires_content_addressed_provider(tmp_path):
    module = load_module()
    init_repo(tmp_path)
    large = tmp_path / "outputs" / "large.bin"
    with large.open("wb") as handle:
        handle.seek(module.MAX_HASH_BYTES)
        handle.write(b"x")
    check = {
        "id": "large-output",
        "type": "artifact",
        "path": "outputs/large.bin",
        "min_bytes": 1,
        "stages": ["final"],
    }

    result = module._run_check(check, tmp_path, "final")

    assert result["status"] == "fail"
    assert result["state"] == "unverifiable_large"
    assert "manifest" in result["requires"]


def test_acceptance_edit_and_proposed_revision_cannot_self_activate(tmp_path):
    module = load_module()
    contract, receipt = init_repo(tmp_path)
    task_path, _, _ = write_active(module, tmp_path, contract, task_hook())
    module.verify(tmp_path, contract, receipt, stage="final", next_objective="")

    changed = json.loads(task_path.read_text(encoding="utf-8"))
    changed["acceptance"][0]["text"] = "A weaker condition."
    task_path.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(module.task_oracle.TaskOracleError) as stale_pin:
        module.load_contract(tmp_path, contract)
    assert stale_pin.value.code == "REVISION_PENDING"

    proposed = copy.deepcopy(changed)
    proposed.update(
        {"revision": "v2", "parent_revision": "v1", "status": "PROPOSED_REVISION"}
    )
    write_active(module, tmp_path, contract, proposed)
    with pytest.raises(module.task_oracle.TaskOracleError) as not_active:
        module.load_contract(tmp_path, contract)
    assert not_active.value.code == "REVISION_PENDING"


def test_unknown_safety_field_and_duplicate_promotion_evidence_fail_closed(tmp_path):
    module = load_module()
    contract, _ = init_repo(tmp_path)
    unknown = task_hook()
    unknown["unimplemented_safety_gate"] = True
    with pytest.raises(module.task_oracle.TaskOracleError) as unsupported:
        module.task_oracle.validate_task_hook(unknown, tmp_path)
    assert unsupported.value.code == "INVALID_TASK_HOOK"

    repeatable = task_hook(lifecycle="repeatable", status="REUSABLE_ACTIVE")
    reference = {
        "run_id": "run-one",
        "candidate_fingerprint": "1" * 64,
        "bindings_sha256": "2" * 64,
        "receipt_sha256": "3" * 64,
        "outcome": "CONVERGED",
    }
    repeatable["promotion"] = {
        "mode": "reproduced",
        "qualifying_receipts": [reference, copy.deepcopy(reference)],
        "confirmed_by": "owner",
        "confirmation_ref": "review",
    }
    write_active(module, tmp_path, contract, repeatable)
    with pytest.raises(module.task_oracle.TaskOracleError) as duplicate:
        module.task_oracle.validate_task_hook(repeatable, tmp_path)
    assert duplicate.value.code == "REVISION_PENDING"


def test_external_proof_is_bound_to_exact_candidate_and_digest(tmp_path, monkeypatch):
    module = load_module()
    contract, receipt = init_repo(tmp_path)
    candidate_path = tmp_path / "outputs" / "report.txt"
    candidate_path.write_text("version one\n", encoding="utf-8")
    oracle = {
        "id": "independent-review",
        "type": "external_receipt",
        "kind": "SEMANTIC",
        "version": "review-v1",
        "receipt_path": "test-results/external-review.json",
        "stages": ["final"],
    }
    write_active(
        module,
        tmp_path,
        contract,
        task_hook(
            candidate={"type": "artifact", "path": "outputs/report.txt"},
            oracles=[oracle],
        ),
    )
    normalized, _ = module.load_contract(tmp_path, contract)
    workspace = module.workspace_fingerprint(tmp_path)
    candidate = module.task_snapshot(normalized, tmp_path, workspace)["candidate"]
    proof = module.task_oracle.proof_receipt(
        oracle,
        normalized["_task_context"],
        candidate,
        status="PASS",
        reason="Independent evaluator accepted the exact report.",
        evidence_refs=["sha256:" + "1" * 64],
    )
    proof_path = tmp_path / "test-results" / "external-review.json"
    proof_path.parent.mkdir(parents=True)
    proof_path.write_text(json.dumps(proof), encoding="utf-8")

    first = module.verify(
        tmp_path, contract, receipt, stage="final", next_objective=""
    )
    proof_path.write_text(json.dumps(proof) + "\n", encoding="utf-8")
    stale_source = hook(
        module, tmp_path, contract, receipt, {"hook_event_name": "Stop"}
    )
    proof_path.write_text(json.dumps(proof), encoding="utf-8")
    original_loader = module.task_oracle.load_external_proof

    def racing_loader(*args, **kwargs):
        loaded = original_loader(*args, **kwargs)
        proof_path.write_text(json.dumps(proof) + " ", encoding="utf-8")
        return loaded

    with monkeypatch.context() as patcher:
        patcher.setattr(module.task_oracle, "load_external_proof", racing_loader)
        raced = module.verify(
            tmp_path,
            contract,
            receipt,
            stage="final",
            next_objective="Retry against a stable reviewer receipt.",
        )
    proof_path.write_text(json.dumps(proof), encoding="utf-8")
    tampered = {**proof, "reason": "Changed without recomputing the digest."}
    proof_path.write_text(json.dumps(tampered), encoding="utf-8")
    invalid_digest = module.verify(
        tmp_path,
        contract,
        receipt,
        stage="final",
        next_objective="Recreate the reviewer receipt.",
    )
    proof_path.write_text(json.dumps(proof), encoding="utf-8")
    candidate_path.write_text("version two\n", encoding="utf-8")
    second = module.verify(
        tmp_path,
        contract,
        receipt,
        stage="final",
        next_objective="Repeat independent review.",
    )

    assert first["outcome"] == "CONVERGED"
    assert stale_source["decision"] == "block"
    assert "stale" in stale_source["reason"]
    assert raced["outcome"] == "CONFLICT"
    assert invalid_digest["outcome"] == "UNVERIFIABLE"
    assert second["outcome"] == "STALE_EVIDENCE"
    assert second["proof_eligible"] is False


@pytest.mark.parametrize(
    ("reported_bindings", "fallbacks_used", "expected"),
    [
        ({}, [], "IMPLICIT_MUTABLE_BINDING"),
        ({"asset_id": "CURRENT"}, ["legacy-default"], "FALLBACK_NOT_ALLOWED"),
    ],
)
def test_hidden_mutable_decision_or_fallback_cannot_converge(
    tmp_path, reported_bindings, fallbacks_used, expected
):
    module = load_module()
    contract, receipt = init_repo(tmp_path)
    binding_digest = module.task_oracle.sha256_json("asset-current")
    bindings = {
        key: binding_digest if value == "CURRENT" else value
        for key, value in reported_bindings.items()
    }
    provider_output = {
        "schema": "3can.candidate/v1",
        "fingerprint": "a" * 64,
        "binding_fingerprints": bindings,
        "fallbacks_used": fallbacks_used,
    }
    provider = tmp_path / "candidate_provider.py"
    provider.write_text(
        "import json\nprint(json.dumps(" + repr(provider_output) + "))\n",
        encoding="utf-8",
    )
    task = task_hook(
        candidate={
            "type": "command",
            "argv": [sys.executable, "candidate_provider.py"],
        },
        mutable_bindings=["asset_id"],
        allowed_fallback_ids=["legacy-default"],
    )
    write_active(
        module,
        tmp_path,
        contract,
        task,
        bindings={"asset_id": "asset-current"},
    )

    result = module.verify(
        tmp_path,
        contract,
        receipt,
        stage="final",
        next_objective="Bind the current decision explicitly.",
    )

    assert result["outcome"] == expected
    assert result["proof_eligible"] is False


def test_legitimate_invariant_does_not_create_hardcode_violation(tmp_path):
    module = load_module()
    contract, _ = init_repo(tmp_path)
    task = task_hook()
    task["invariants"] = ["Protocol schema remains 3can.task-hook/v1."]
    write_active(module, tmp_path, contract, task)

    normalized, _ = module.load_contract(tmp_path, contract)

    assert normalized["_task_context"]["task_hook"]["invariants"] == task["invariants"]


def test_task_digest_validates_and_canonicalizes_key_order(tmp_path, capsys):
    module = load_module()
    contract, _ = init_repo(tmp_path)
    task = task_hook()
    task_path, digest, _ = write_active(module, tmp_path, contract, task)
    reordered = {key: task[key] for key in reversed(list(task))}
    task_path.write_text(json.dumps(reordered), encoding="utf-8")

    result = module.main(
        [
            "--root",
            str(tmp_path),
            "task-digest",
            "--task-hook",
            ".codex/task-hooks/generic.json",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert result == 0
    assert output["task_hook_sha256"] == digest


def test_one_off_retires_only_with_retained_final_receipt(tmp_path):
    module = load_module()
    contract_path, receipt_path = init_repo(tmp_path)
    task_path, active_digest, contract = write_active(
        module, tmp_path, contract_path, task_hook()
    )
    final = module.verify(
        tmp_path, contract_path, receipt_path, stage="final", next_objective=""
    )
    retired = task_hook(status="RETIRED")
    retired["transition"] = {
        "from_sha256": active_digest,
        "confirmed_by": "owner",
        "confirmation_ref": "owner-closeout",
    }
    task_path.write_text(json.dumps(retired), encoding="utf-8")
    retired_digest = module.task_oracle.sha256_json(retired)
    contract.update(
        {
            "status": "complete",
            "task_hook": {
                "path": ".codex/task-hooks/generic.json",
                "sha256": retired_digest,
                "revision": "v1",
            },
            "closeout": {
                "task_hook_sha256": retired_digest,
                "final_receipt_sha256": final["receipt_sha256"],
                "disposition": "retired",
                "confirmed_by": "owner",
                "confirmation_ref": "owner-closeout",
            },
        }
    )
    contract_path.write_text(json.dumps(contract), encoding="utf-8")

    stopped = hook(
        module, tmp_path, contract_path, receipt_path, {"hook_event_name": "Stop"}
    )
    assert stopped == {}

    contract["closeout"]["final_receipt_sha256"] = "0" * 64
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    rejected = hook(
        module, tmp_path, contract_path, receipt_path, {"hook_event_name": "Stop"}
    )
    assert rejected["decision"] == "block"
    assert "REVISION_PENDING" in rejected["reason"]


def test_superseded_revision_requires_explicit_successor_transition(tmp_path):
    module = load_module()
    init_repo(tmp_path)
    active = task_hook(lifecycle="repeatable")
    active_digest = module.task_oracle.sha256_json(active)
    proposed = copy.deepcopy(active)
    proposed.update(
        {"revision": "v2", "parent_revision": "v1", "status": "PROPOSED_REVISION"}
    )
    module.task_oracle.validate_task_hook(proposed, tmp_path)

    superseded = copy.deepcopy(active)
    superseded["status"] = "SUPERSEDED"
    superseded["transition"] = {
        "from_sha256": active_digest,
        "successor_revision": "v2",
        "confirmed_by": "owner",
        "confirmation_ref": "owner-approved-v2",
    }

    assert module.task_oracle.validate_task_hook(superseded, tmp_path)[
        "status"
    ] == "SUPERSEDED"


def test_repeatable_hook_is_parameterized_and_promoted_after_reproduction(tmp_path):
    module = load_module()
    first_root = tmp_path / "first"
    first_contract_path, first_receipt_path = init_repo(first_root)
    first_candidate = first_root / "outputs" / "first.txt"
    first_candidate.write_text("first candidate\n", encoding="utf-8")
    reusable = task_hook(
        lifecycle="repeatable",
        candidate={"type": "artifact", "path_binding": "candidate_path"},
        mutable_bindings=["candidate_path"],
    )
    task_path, _, first_contract = write_active(
        module,
        first_root,
        first_contract_path,
        reusable,
        run_id="run-first",
        bindings={"candidate_path": "outputs/first.txt"},
    )
    first = module.verify(
        first_root,
        first_contract_path,
        first_receipt_path,
        stage="final",
        next_objective="",
    )
    candidate_hook = copy.deepcopy(reusable)
    candidate_hook["status"] = "REUSABLE_CANDIDATE"
    candidate_hook["promotion"] = {
        "mode": "candidate",
        "qualifying_receipts": [receipt_ref(first)],
    }
    task_path.write_text(json.dumps(candidate_hook), encoding="utf-8")
    candidate_digest = module.task_oracle.sha256_json(candidate_hook)
    first_contract.update(
        {
            "status": "complete",
            "task_hook": {
                "path": ".codex/task-hooks/generic.json",
                "sha256": candidate_digest,
                "revision": "v1",
            },
            "closeout": {
                "task_hook_sha256": candidate_digest,
                "final_receipt_sha256": first["receipt_sha256"],
                "disposition": "reusable_candidate",
                "confirmed_by": "owner",
                "confirmation_ref": "first-run-closeout",
            },
        }
    )
    first_contract_path.write_text(json.dumps(first_contract), encoding="utf-8")
    closed, _ = module.load_contract(first_root, first_contract_path)
    assert module.closeout_is_valid(closed, first) is True

    second_root = tmp_path / "second"
    second_contract_path, second_receipt_path = init_repo(second_root)
    (second_root / "outputs" / "second.txt").write_text(
        "second candidate\n", encoding="utf-8"
    )
    write_active(
        module,
        second_root,
        second_contract_path,
        candidate_hook,
        run_id="run-second",
        bindings={"candidate_path": "outputs/second.txt"},
    )
    second = module.verify(
        second_root,
        second_contract_path,
        second_receipt_path,
        stage="final",
        next_objective="",
    )

    active_hook = copy.deepcopy(candidate_hook)
    active_hook["status"] = "REUSABLE_ACTIVE"
    active_hook["promotion"] = {
        "mode": "reproduced",
        "qualifying_receipts": [receipt_ref(first), receipt_ref(second)],
        "confirmed_by": "independent_reviewer",
        "confirmation_ref": "two-run-review",
    }
    module.task_oracle.validate_task_hook(active_hook, second_root)
    serialized = json.dumps(active_hook, sort_keys=True)

    assert first["outcome"] == second["outcome"] == "CONVERGED"
    assert first["task"]["candidate"]["fingerprint"] != second["task"]["candidate"][
        "fingerprint"
    ]
    executable_contract = json.dumps(
        {
            "candidate": candidate_hook["candidate"],
            "acceptance": candidate_hook["acceptance"],
            "oracles": candidate_hook["oracles"],
            "mutable_bindings": candidate_hook["mutable_bindings"],
        }
    )
    assert "run-first" not in executable_contract
    assert "outputs/first.txt" not in executable_contract
    assert "outputs/first.txt" not in serialized


def test_implementation_change_keeps_revision_but_requires_fresh_proof(tmp_path):
    module = load_module()
    contract, receipt = init_repo(tmp_path)
    _, digest, _ = write_active(module, tmp_path, contract, task_hook())
    first = module.verify(
        tmp_path, contract, receipt, stage="final", next_objective=""
    )
    (tmp_path / "tracked.txt").write_text("implementation v2\n", encoding="utf-8")
    normalized, _ = module.load_contract(tmp_path, contract)
    second = module.verify(
        tmp_path, contract, receipt, stage="final", next_objective=""
    )

    assert normalized["_task_context"]["task_hook_sha256"] == digest
    assert first["task"]["candidate"]["fingerprint"] != second["task"]["candidate"][
        "fingerprint"
    ]
    assert second["outcome"] == "CONVERGED"


def test_global_hook_has_no_domain_classifier_network_or_external_side_effects():
    source = SCRIPT.read_text(encoding="utf-8").lower()
    helper = (SCRIPT.parent / "task_oracle.py").read_text(encoding="utf-8").lower()
    combined = source + helper

    for forbidden in (
        "if video",
        "if seo",
        "if image",
        "urllib.request",
        "requests.",
        "127.0.0.1:9700",
        "git merge",
        "git push",
    ):
        assert forbidden not in combined
