"""Mandatory checkpoint adversarial cases. All provider replies here are fixtures."""
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import test_runtimehook_jev as existing
from test_runtimehook_plugin import _activate, _git, _session_id

controller, jev = existing.controller, existing.jev
cp = controller.checkpoints
isolated_scope_cache = existing.isolated_scope_cache
plain_repo = existing.plain_repo
packet = existing.packet


def point(key):
    return {"id": key, "title": "检查解析模块及验证边界", "criteria": ["A01"],
            "parameters": ["tests_passed"],
            "rubric": ["缺少真实证据或明确失败", "存在重大未解决问题", "当前环节证据充分且范围准确"],
            "minimum_score": 2, "minimum_confidence": 0.65, "minimum_probability": 0.7}


def prepare(root, packet, keys=("final",)):
    _activate(root)
    policy = Path(os.environ["CODEX_HOME"]) / "runtimehook/policy.json"
    policy.parent.mkdir(exist_ok=True, parents=True)
    policy.write_text(json.dumps({"schema": cp.POLICY_SCHEMA, "jev_required": True}))
    spec = root / ".codex/runtimehook/checkpoints-spec.json"
    spec.write_text(json.dumps({"schema": cp.SPEC_SCHEMA, "final_checkpoint": keys[-1],
                               "checkpoints": [point(k) for k in keys]}, ensure_ascii=False), encoding="utf-8")
    supplied = root / ".codex/runtimehook/input.json"
    supplied.write_text(json.dumps({**packet, "parameters": {"tests_passed": 3}}, ensure_ascii=False), encoding="utf-8")
    return SimpleNamespace(root=root, native_cwd=root, session_id=_session_id(root),
                           spec=spec, checkpoint_id=keys[0], packet=supplied,
                           kind="stage", label="", next_objective="继续本模块")


def review(root, result="PASS", stage="episode"):
    return controller.record_review(SimpleNamespace(root=root, native_cwd=root,
        session_id=_session_id(root), scope="main", stage=stage, result=result,
        reference="test-evidence:not-production-proof", next_objective="继续真实验收", timeout=10))


def response(request, failure=None):
    value = existing.response({**request, "questions": {k: v for k, v in request["questions"].items() if v["type"] == "choice"}})
    for answer in value["answers"].values():
        answer["confidence"] = 0.99
    if "checkpoint_quality" in request["questions"]:
        last = len(request["questions"]["checkpoint_quality"]["criteria"]) - 1
        value["answers"]["checkpoint_quality"] = {"type": "score", "score": last,
            "confidence": 0.99, "probabilities": {str(i): float(i == last) for i in range(last + 1)}}
    if failure == "score":
        value["answers"]["checkpoint_quality"].update(score=0, probabilities={"0": 1, "1": 0, "2": 0})
    if failure == "confidence":
        value["answers"]["checkpoint_quality"]["confidence"] = 0.1
    if failure == "probability":
        value["answers"]["checkpoint_quality"]["score"] = 1
        value["answers"]["checkpoint_quality"]["probabilities"] = {"0": 0.5, "1": 0, "2": 0.5}
    if failure == "claim":
        answer = value["answers"]["claim_1"]
        answer.update(choice="UNSUPPORTED", probabilities={k: float(k == "UNSUPPORTED") for k in answer["probabilities"]})
    return value


def test_required_review_cannot_skip_checkpoint(plain_repo, packet, monkeypatch):
    prepare(plain_repo, packet)
    monkeypatch.setattr(jev, "request_gateway", lambda *a: pytest.fail("no packet must not upload"))
    with pytest.raises(controller.RuntimeHookError, match="CHECKPOINT_REQUIRED"):
        review(plain_repo)
    assert review(plain_repo, "PARTIAL")["result"] == "PARTIAL"


def test_checkpoint_records_actual_values_without_network(plain_repo, packet, monkeypatch):
    args = prepare(plain_repo, packet)
    monkeypatch.setattr(jev, "request_gateway", lambda *a: pytest.fail("capture network"))
    result = controller.record_checkpoint(args)
    record = cp.read_json(cp.record_path(plain_repo, "final"))
    assert result["online_calls"] == 0 and result["local_elapsed_ms"] >= 0
    assert record["packet"]["checkpoint"]["parameters"] == {"tests_passed": 3}
    assert controller._load_state(plain_repo)["schema"] == controller.CHECKPOINT_STATE_SCHEMA
    assert record["review"] is None


def test_review_automatically_calls_once_and_reuses_opinion(plain_repo, packet, monkeypatch):
    args = prepare(plain_repo, packet)
    controller.record_checkpoint(args)
    calls = []
    monkeypatch.setattr(jev, "request_gateway", lambda r, t: calls.append(r) or response(r))
    first, second = review(plain_repo), review(plain_repo)
    assert first["result"] == second["result"] == "PASS" and len(calls) == 1
    assert first["jev"]["opinion"]["answers"]["checkpoint_quality"]["score"] == 2
    assert second["jev"]["opinion"]["reused"]
    assert str(plain_repo) not in json.dumps(calls) and args.session_id not in json.dumps(calls)


@pytest.mark.parametrize("failure", ["score", "confidence", "probability", "claim"])
def test_negative_or_uncertain_jev_cannot_be_signed_pass(plain_repo, packet, monkeypatch, failure):
    args = prepare(plain_repo, packet)
    controller.record_checkpoint(args)
    calls = []
    monkeypatch.setattr(jev, "request_gateway", lambda r, t: calls.append(r) or response(r, failure))
    for _ in range(2):
        result = review(plain_repo)
        assert not result["ok"] and result["status"] == "REVIEW_REQUIRED" and result["issues"]
    assert len(calls) == 1
    assert controller._load_state(plain_repo)["semantic_review"]["result"] == "PENDING"
    assert review(plain_repo, "PARTIAL")["result"] == "PARTIAL"


def test_unavailable_is_cached_and_never_promoted(plain_repo, packet, monkeypatch):
    args = prepare(plain_repo, packet)
    controller.record_checkpoint(args)
    calls = []
    def unavailable(*args):
        calls.append(1)
        raise jev.JevError("HTTP_402")
    monkeypatch.setattr(jev, "request_gateway", unavailable)
    assert review(plain_repo)["status"] == "REVIEW_REQUIRED"
    assert review(plain_repo, "PARTIAL")["result"] == "PARTIAL"
    assert review(plain_repo)["status"] == "REVIEW_REQUIRED"
    assert len(calls) == 1


def test_final_cannot_skip_configured_earlier_checkpoint(plain_repo, packet, monkeypatch):
    args = prepare(plain_repo, packet, ("design", "final"))
    args.checkpoint_id = "final"
    controller.record_checkpoint(args)
    monkeypatch.setattr(jev, "request_gateway", lambda *a: pytest.fail("coverage checked before charge"))
    with pytest.raises(controller.RuntimeHookError, match="CHECKPOINT_COVERAGE_MISSING"):
        review(plain_repo, stage="final")


def test_full_two_checkpoint_flow(plain_repo, packet, monkeypatch):
    args = prepare(plain_repo, packet, ("design", "final"))
    calls = []
    monkeypatch.setattr(jev, "request_gateway", lambda r, t: calls.append(r) or response(r))
    controller.record_checkpoint(args)
    assert review(plain_repo)["result"] == "PASS"
    args.checkpoint_id = "final"
    controller.record_checkpoint(args)
    assert review(plain_repo, stage="final")["result"] == "PASS"
    assert len(calls) == 2


@pytest.mark.parametrize("change", ["head", "packet", "spec", "scope"])
def test_stale_or_foreign_checkpoint_not_reused(plain_repo, packet, monkeypatch, change):
    args = prepare(plain_repo, packet)
    controller.record_checkpoint(args)
    monkeypatch.setattr(jev, "request_gateway", lambda r, t: response(r))
    assert review(plain_repo)["result"] == "PASS"
    monkeypatch.setattr(jev, "request_gateway", lambda *a: pytest.fail("stale must not charge"))
    if change == "head":
        _git(plain_repo, "commit", "--allow-empty", "-qm", "new candidate")
    elif change == "packet":
        path = cp.record_path(plain_repo, "final")
        value = cp.read_json(path)
        value["packet"]["claims"][0]["text"] = "已经部署"
        cp.save(path, value)
    elif change == "spec":
        value = cp.read_json(args.spec)
        value["checkpoints"][0]["minimum_confidence"] = 0.5
        cp.save(args.spec, value)
    else:
        binding = controller._read_scope(args.session_id)
        binding["worktree"] = str(plain_repo.parent)
        controller._save_scope(binding)
    with pytest.raises(controller.RuntimeHookError):
        review(plain_repo)


def test_concurrent_boundary_not_overwritten_even_for_partial(plain_repo, packet, monkeypatch):
    args = prepare(plain_repo, packet)
    controller.record_checkpoint(args)
    def mutate(request, timeout):
        state = controller._load_state(plain_repo)
        state = controller._mark_boundary(state, kind="stage", label="另一个当前边界", observed_git_head=state["boundary"]["observed_git_head"])
        controller._write_state(plain_repo, state)
        return response(request)
    monkeypatch.setattr(jev, "request_gateway", mutate)
    assert review(plain_repo, "PARTIAL")["status"] == "STALE"
    assert controller._load_state(plain_repo)["boundary"]["last_label"] == "另一个当前边界"


def test_required_stop_continues_once_without_online_call(plain_repo, packet, monkeypatch, capsys):
    args = prepare(plain_repo, packet)
    controller.record_checkpoint(args)
    monkeypatch.setattr(jev, "request_gateway", lambda *a: pytest.fail("native event network"))
    for continued in (False, True):
        payload = {"cwd": str(plain_repo), "session_id": args.session_id,
                   "hook_event_name": "Stop", "stop_hook_active": continued}
        monkeypatch.setattr(controller.sys, "stdin", io.StringIO(json.dumps(payload)))
        assert controller.hook(SimpleNamespace(root=None, session_orientation=False)) == 0
        result = json.loads(capsys.readouterr().out)
        assert "Jev" in json.dumps(result, ensure_ascii=False)
        assert (result.get("decision") == "block") is (not continued)


def test_bad_parameters_never_recorded(plain_repo, packet):
    args = prepare(plain_repo, packet)
    value = cp.read_json(args.packet)
    value["parameters"] = {"wrong": 3}
    cp.save(args.packet, value)
    with pytest.raises(jev.JevError, match="PARAMETERS_MISMATCH"):
        controller.record_checkpoint(args)
    assert not cp.record_path(plain_repo, "final").exists()


def test_score_contract_roundtrip_and_bad_confidence(plain_repo, packet):
    args = prepare(plain_repo, packet)
    controller.record_checkpoint(args)
    intent = controller._load_state(plain_repo)["run_intent"]
    record = cp.read_json(cp.record_path(plain_repo, "final"))
    request, mapping = jev.build_request(record["packet"], intent)
    raw = response(request)
    parsed = jev.parse_response(raw, request["questions"], mapping)
    assert jev.parse_response(parsed, request["questions"], mapping) == parsed
    raw["answers"]["checkpoint_quality"]["confidence"] = float("nan")
    with pytest.raises(jev.JevError, match="CONFIDENCE"):
        jev.parse_response(raw, request["questions"], mapping)


def test_main_and_temporary_checkpoint_files_do_not_collide(plain_repo):
    assert cp.record_path(plain_repo, "design", "main") != cp.record_path(plain_repo, "design", "temporary")


def test_temporary_required_review_clears_only_temporary(plain_repo, packet, monkeypatch):
    args = prepare(plain_repo, packet)
    controller.record_checkpoint(args)
    main_path = cp.record_path(plain_repo, "final")
    main_before = main_path.read_bytes()
    controller.task_relation(SimpleNamespace(root=plain_repo, kind="temporary", reference="Owner insertion",
        goal="先检查用户新增的解析案例", acceptance=["A01=该临时用例通过"], resume_objective="返回主目标"))
    controller.record_checkpoint(args)
    monkeypatch.setattr(jev, "request_gateway", lambda r, t: response(r))
    result = controller.record_review(SimpleNamespace(root=plain_repo, native_cwd=plain_repo,
        session_id=args.session_id, scope="temporary", stage="final", result="PASS",
        reference="temporary-test-evidence", next_objective="", timeout=10))
    assert result["temporary_cleared"]
    assert not controller._load_state(plain_repo).get("temporary_task")
    assert main_path.read_bytes() == main_before


def test_legacy_pass_resume_reports_jev_missing(plain_repo, packet, monkeypatch, capsys):
    _activate(plain_repo)
    assert review(plain_repo, stage="final")["result"] == "PASS"
    policy = Path(os.environ["CODEX_HOME"]) / "runtimehook/policy.json"
    policy.parent.mkdir(exist_ok=True, parents=True)
    policy.write_text(json.dumps({"schema": cp.POLICY_SCHEMA, "jev_required": True}))
    payload = {"cwd": str(plain_repo), "session_id": _session_id(plain_repo),
               "hook_event_name": "SessionStart", "source": "resume"}
    monkeypatch.setattr(controller.sys, "stdin", io.StringIO(json.dumps(payload)))
    controller.hook(SimpleNamespace(root=None, session_orientation=False))
    result = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "语义复核状态：STALE" in result and "必经 Jev" in result
