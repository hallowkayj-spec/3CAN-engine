from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
HARNESS_PATH = (
    ROOT / "examples" / "codex-cli-project-kit" / "scripts" / "3can_research_harness.py"
)
SPEC = importlib.util.spec_from_file_location("threecan_research_harness", HARNESS_PATH)
assert SPEC is not None and SPEC.loader is not None
HARNESS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HARNESS)


GOOD_SCORES = {
    "authority": 4,
    "recency": 4,
    "practice_value": 4,
    "reproducibility": 4,
    "task_relevance": 5,
    "community_signal": 4,
    "risk": 1,
    "conflict": 1,
}


def _urls(count: int) -> list[str]:
    return [f"https://example.com/source-{index}" for index in range(count)]


def _source_types(count: int, *types: str) -> list[str]:
    return [types[index % len(types)] for index in range(count)]


def _bind_requirement(
    *,
    state_file: Path,
    prompt: str,
    tier: str,
    session_id: str,
    turn_id: str,
) -> dict:
    selected_tier = HARNESS._normalize_research_tier(tier)
    return HARNESS._save_requirement(
        state_file=state_file,
        session_id=session_id,
        turn_id=turn_id,
        prompt=prompt,
        requirement={
            "requires_research": True,
            "research_tier": selected_tier,
            "time_budget": HARNESS.TIME_BUDGETS[selected_tier],
            "source_strategy": HARNESS.SOURCE_STRATEGIES[selected_tier],
            "query_planning": {"required": True},
            "sidecar_judges": ["evidence_sufficiency", "task_fit"],
        },
    )


def _record(tmp_path: Path, *, tier: str, source_types: list[str], **overrides):
    count = len(source_types)
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_files: list[str] = []
    for index, (url, source_type) in enumerate(zip(_urls(count), source_types)):
        path = artifact_dir / f"source-{index}.json"
        path.write_text(
            json.dumps(
                {
                    "ok": True,
                    "status": "collected",
                    "adapter": "test_public_url_extract",
                    "url": url,
                    "source_type": source_type,
                    "http_status": 200,
                    "content_hash": hashlib.sha256(url.encode()).hexdigest(),
                    "title": f"Source {index}",
                    "text_excerpt": f"Relevant source evidence {index}",
                    "stores_raw_html": False,
                    "stores_secrets": False,
                }
            ),
            encoding="utf-8",
        )
        artifact_files.append(str(path))
    values = {
        "question": "How should the existing 3CAN research skill investigate difficult engineering work?",
        "source_urls": [],
        "source_artifact_files": artifact_files,
        "session_id": "session-research-test",
        "turn_id": f"turn-{tier}-{count}",
        "state_file": tmp_path / "state.json",
        "ledger_dir": tmp_path / "ledgers",
        "research_tier": tier,
        "elapsed_minutes": 8 if tier in {"standard", "quick"} else 25,
        "source_types": [],
        "query_terms": ["3CAN research"],
        "query_variants": [
            f"query {index}"
            for index in range(6 if tier in {"deep", "rpa_deep"} else 3)
        ],
        "evidence_scores": GOOD_SCORES,
        "sidecar_evidence_sufficiency": "pass",
        "sidecar_task_fit": "pass",
        "context_status": "used",
        "context_refs": ["DEC-20260502-3can-deep-research-semantic-rpa-evidence-v2"],
        "contradiction_status": "resolved",
        "platform_relevant": tier in {"deep", "rpa_deep"},
    }
    values.update(overrides)
    requirement = _bind_requirement(
        state_file=values["state_file"],
        prompt=values["question"],
        tier=values["research_tier"],
        session_id=values["session_id"],
        turn_id=values["turn_id"],
    )
    values["requirement_id"] = requirement["requirement_id"]
    return HARNESS.record_done(**values)


def test_two_tier_classifier_and_timeboxes() -> None:
    standard = HARNESS.detect_research_requirement("请联网调研当前 Python API 文档")
    deep = HARNESS.detect_research_requirement("RPA 部署连续失败，需要深度研究平台问题")

    assert standard["research_tier"] == "standard"
    assert standard["time_budget"]["hard_cap_minutes"] == 10
    assert standard["time_budget"]["min_sources"] == 5
    assert standard["time_budget"]["min_source_families"] == 3
    assert "targeted_web" in standard["source_strategy"]
    assert deep["research_tier"] == "deep"
    assert deep["time_budget"]["hard_cap_minutes"] == 30
    assert deep["time_budget"]["min_sources"] == 12
    assert deep["time_budget"]["min_source_families"] == 4
    assert "targeted_web" in deep["source_strategy"]


def test_classifier_avoids_routine_local_development_false_positives() -> None:
    local_prompts = (
        "修复这个 API 失败",
        "检查当前 release 状态",
        "优化这个 query",
        "运行当前 model 单元测试",
    )

    for prompt in local_prompts:
        assert HARNESS.detect_research_requirement(prompt)["requires_research"] is False

    assert HARNESS.detect_research_requirement(
        "联网调研当前 Python API 文档"
    )["research_tier"] == "standard"
    assert HARNESS.detect_research_requirement(
        "OpenAI SDK 对比并查官方文档"
    )["research_tier"] == "deep"
    assert HARNESS.detect_research_requirement(
        "这个 API 反复失败，还是不行"
    )["research_tier"] == "deep"


def test_failure_signal_escalates_only_at_repeat_threshold(tmp_path: Path) -> None:
    state_file = tmp_path / "failures.json"
    results = [
        HARNESS.record_failure_signal(
            state_file=state_file,
            command="python -m pytest",
            target_files=["tests/test_example.py"],
            error_text="same deterministic failure",
        )
        for _ in range(3)
    ]

    assert [item["status"] for item in results] == [
        "recorded_failure",
        "recorded_failure",
        "stop_and_research",
    ]
    assert results[-1]["research_tier"] == "deep"


def test_query_plan_covers_required_evidence_axes() -> None:
    plan = HARNESS.build_query_plan(
        "RPA 视频质量连续失败，需要调研 GitHub Hugging Face 和社区证据",
        tier="deep",
        focus_terms=["video quality"],
    )

    axes = set(plan["query_plan"]["expansion_axes"])
    queries = "\n".join(item["query"] for item in plan["query_plan"]["query_variants"])
    assert plan["research_tier"] == "deep"
    assert {
        "official_terms",
        "academic_terms",
        "implementation_terms",
        "community_terms",
    } <= axes
    assert "platform_terms" in axes
    assert "GitHub" in queries
    assert "Hugging Face" in queries
    assert "Reddit" in queries
    assert "TikTok" in queries or "抖音" in queries


def test_standard_passes_only_with_complete_evidence(tmp_path: Path) -> None:
    result = _record(
        tmp_path,
        tier="standard",
        source_types=_source_types(
            5,
            "official_primary",
            "academic_or_standard",
            "github_or_issue",
            "community_forum",
            "targeted_web",
        ),
        platform_relevant=False,
    )

    assert result["ok"] is True
    assert result["status"] == "pass"
    assert result["sidecar_decision"]["decision"] == "ready_for_decision"
    state = HARNESS._safe_load_json(tmp_path / "state.json", {})
    assert next(iter(state["turns"].values()))["status"] == "research_done"


def test_source_count_alone_cannot_complete(tmp_path: Path) -> None:
    result = _record(
        tmp_path,
        tier="standard",
        source_types=["targeted_web"] * 5,
        context_status="",
        context_refs=[],
        contradiction_status="",
        sidecar_evidence_sufficiency="not_recorded",
        sidecar_task_fit="not_recorded",
    )

    assert result["ok"] is False
    assert "insufficient_source_family_coverage" in result["sidecar_decision"]["risks"]
    assert "missing_internal_context_status" in result["sidecar_decision"]["risks"]
    assert "missing_contradiction_status" in result["sidecar_decision"]["risks"]
    state = HARNESS._safe_load_json(tmp_path / "state.json", {})
    assert next(iter(state["turns"].values()))["status"] == "needs_research"


def test_unopened_urls_and_search_results_do_not_satisfy_source_gate(
    tmp_path: Path,
) -> None:
    source_types = _source_types(
        5,
        "official_primary",
        "academic_or_standard",
        "github_or_issue",
        "community_forum",
        "targeted_web",
    )
    common = {
        "question": "Can discovery-only URLs complete research?",
        "session_id": "session-unopened",
        "state_file": tmp_path / "state.json",
        "research_tier": "standard",
        "elapsed_minutes": 8,
        "query_variants": [f"query {index}" for index in range(3)],
        "evidence_scores": GOOD_SCORES,
        "sidecar_evidence_sufficiency": "pass",
        "sidecar_task_fit": "pass",
        "context_status": "unavailable",
        "contradiction_status": "resolved",
    }
    bare = HARNESS.record_done(
        **common,
        turn_id="turn-bare",
        ledger_dir=tmp_path / "bare-ledgers",
        source_urls=_urls(5),
        source_types=source_types,
    )

    artifact_dir = tmp_path / "search-results"
    artifact_dir.mkdir()
    search_result_files: list[str] = []
    for index, (url, source_type) in enumerate(zip(_urls(5), source_types)):
        path = artifact_dir / f"search-{index}.json"
        path.write_text(
            json.dumps(
                {
                    "ok": True,
                    "status": "search_result",
                    "adapter": "search_result_import",
                    "url": url,
                    "source_type": source_type,
                    "content_hash": hashlib.sha256(url.encode()).hexdigest(),
                }
            ),
            encoding="utf-8",
        )
        search_result_files.append(str(path))
    discovery_only = HARNESS.record_done(
        **common,
        turn_id="turn-search-results",
        ledger_dir=tmp_path / "search-ledgers",
        source_urls=[],
        source_artifact_files=search_result_files,
    )

    for result in (bare, discovery_only):
        assert result["ok"] is False
        assert result["verified_external_source_count"] == 0
        assert (
            "insufficient_verified_external_source_count"
            in result["sidecar_decision"]["risks"]
        )


def test_rpa_artifact_requires_observed_content_proof(tmp_path: Path) -> None:
    path = tmp_path / "empty-rpa.json"
    path.write_text(
        json.dumps(
            {
                "ok": True,
                "status": "rpa_artifact",
                "url": "https://example.com/platform-source",
                "source_type": "rpa_pipeline_artifact",
                "content_hash": "derived-only",
                "rpa_metadata": {"content_hash": ""},
            }
        ),
        encoding="utf-8",
    )

    artifacts, invalid = HARNESS.load_source_artifacts([str(path)])

    assert invalid == []
    assert artifacts[0]["opened_verified"] is False


def test_deep_requires_community_and_platform_evidence(tmp_path: Path) -> None:
    result = _record(
        tmp_path,
        tier="deep",
        source_types=_source_types(
            12,
            "official_primary",
            "academic_or_standard",
            "github_or_issue",
            "model_hub_or_dataset",
            "targeted_web",
        ),
    )

    assert result["ok"] is False
    assert "missing_community_evidence" in result["sidecar_decision"]["risks"]
    assert "missing_platform_signal" in result["sidecar_decision"]["risks"]


def test_deep_complete_evidence_passes(tmp_path: Path) -> None:
    result = _record(
        tmp_path,
        tier="deep",
        source_types=_source_types(
            12,
            "official_primary",
            "academic_or_standard",
            "github_or_issue",
            "model_hub_or_dataset",
            "community_forum",
            "public_platform_signal",
            "targeted_web",
        ),
    )

    assert result["ok"] is True
    assert result["sidecar_decision"]["source_family_count"] >= 4


def test_missing_or_excess_elapsed_time_blocks_completion(tmp_path: Path) -> None:
    types = _source_types(
        5,
        "official_primary",
        "academic_or_standard",
        "github_or_issue",
        "community_forum",
        "targeted_web",
    )
    missing = _record(
        tmp_path / "missing", tier="standard", source_types=types, elapsed_minutes=0
    )
    exceeded = _record(
        tmp_path / "exceeded", tier="standard", source_types=types, elapsed_minutes=10.1
    )

    assert "missing_elapsed_time" in missing["sidecar_decision"]["risks"]
    assert "research_timebox_exceeded" in exceeded["sidecar_decision"]["risks"]
    assert missing["status"] == "block"
    assert exceeded["status"] == "PARTIAL"
    exceeded_state = HARNESS._safe_load_json(
        tmp_path / "exceeded" / "state.json", {}
    )
    assert next(iter(exceeded_state["turns"].values()))["status"] == "research_partial"


def test_hard_cap_records_typed_terminal_without_unlocking_mutation(
    tmp_path: Path,
) -> None:
    partial_root = tmp_path / "partial"
    unavailable_root = tmp_path / "unavailable"
    partial = _record(
        partial_root,
        tier="standard",
        source_types=["official_primary"],
        elapsed_minutes=10,
    )
    unavailable = _record(
        unavailable_root,
        tier="standard",
        source_types=[],
        elapsed_minutes=10,
    )

    assert partial["status"] == "PARTIAL"
    assert partial["terminal"] is True
    assert partial["sidecar_decision"]["decision"] == "PARTIAL"
    assert unavailable["status"] == "UNAVAILABLE"
    assert unavailable["terminal"] is True
    assert unavailable["sidecar_decision"]["decision"] == "UNAVAILABLE"

    for root, source_count, typed_status in (
        (partial_root, 1, "PARTIAL"),
        (unavailable_root, 0, "UNAVAILABLE"),
    ):
        state_file = root / "state.json"
        turn_id = f"turn-standard-{source_count}"
        pretool_code, _ = HARNESS._hook_json(
            {
                "hook_event_name": "PreToolUse",
                "session_id": "session-research-test",
                "turn_id": turn_id,
                "tool_name": "apply_patch",
                "tool_input": {},
            },
            state_file,
        )
        first_stop_code, first_stop = HARNESS._hook_json(
            {
                "hook_event_name": "Stop",
                "session_id": "session-research-test",
                "turn_id": turn_id,
            },
            state_file,
        )
        second_stop_code, second_stop = HARNESS._hook_json(
            {
                "hook_event_name": "Stop",
                "session_id": "session-research-test",
                "turn_id": turn_id,
            },
            state_file,
        )

        assert pretool_code == 2
        assert first_stop_code == second_stop_code == 0
        assert typed_status in first_stop["systemMessage"]
        assert first_stop == second_stop

    replay = _record(
        partial_root,
        tier="standard",
        source_types=["official_primary"],
        elapsed_minutes=10,
    )
    assert replay["idempotent_replay"] is True
    assert replay["ledger_path"] == partial["ledger_path"]


def test_done_cli_exits_successfully_only_for_terminal_result(
    tmp_path: Path, capsys
) -> None:
    common = [
        "--state-file",
        str(tmp_path / "state.json"),
        "done",
        "--question",
        "Manual bounded research",
        "--ledger-dir",
        str(tmp_path / "ledgers"),
        "--context-status",
        "not_applicable",
        "--contradiction-status",
        "not_applicable",
    ]

    blocked_code = HARNESS.main([*common, "--elapsed-minutes", "5"])
    blocked = json.loads(capsys.readouterr().out)
    terminal_code = HARNESS.main([*common, "--elapsed-minutes", "10"])
    terminal = json.loads(capsys.readouterr().out)

    assert blocked_code == 2
    assert blocked["status"] == "block"
    assert terminal_code == 0
    assert terminal["status"] == "UNAVAILABLE"
    assert terminal["terminal"] is True


def test_hook_binds_prompt_tier_and_completion_identity(tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    prompt = "联网调研当前 Python API 文档"
    event = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "session-bound",
        "turn_id": "turn-bound",
        "prompt": prompt,
    }
    code, payload = HARNESS._hook_json(event, state_file)
    state = HARNESS._load_state(state_file)
    item = state["turns"]["session-bound::turn-bound"]

    assert code == 0
    assert f"--requirement-id {item['requirement_id']}" in payload[
        "hookSpecificOutput"
    ]["additionalContext"]

    common = {
        "question": prompt,
        "source_urls": [],
        "session_id": "session-bound",
        "turn_id": "turn-bound",
        "state_file": state_file,
        "ledger_dir": tmp_path / "ledgers",
    }
    with pytest.raises(ValueError, match="research_requirement_binding_mismatch"):
        HARNESS.record_done(
            **common,
            requirement_id="wrong",
            research_tier="standard",
        )
    with pytest.raises(ValueError, match="research_tier_binding_mismatch"):
        HARNESS.record_done(
            **common,
            requirement_id=item["requirement_id"],
            research_tier="deep",
        )
    assert HARNESS._load_state(state_file)["turns"][
        "session-bound::turn-bound"
    ]["status"] == "needs_research"

    HARNESS._mark_terminal(
        state_file=state_file,
        session_id="session-bound",
        turn_id="turn-bound",
        ledger_path=tmp_path / "terminal.json",
        status="research_done",
    )
    replay_code, replay_payload = HARNESS._hook_json(event, state_file)
    conflict_code, conflict_payload = HARNESS._hook_json(
        {**event, "prompt": "联网调研当前 Rust API 文档"}, state_file
    )

    assert replay_code == 0
    assert "already terminal" in replay_payload["systemMessage"]
    assert conflict_code == 2
    assert conflict_payload["reason"] == "research_turn_identity_conflict"
    assert HARNESS._load_state(state_file)["turns"][
        "session-bound::turn-bound"
    ]["status"] == "research_done"


def test_hook_without_stable_turn_identity_does_not_create_shared_state(
    tmp_path: Path,
) -> None:
    state_file = tmp_path / "state.json"
    code, payload = HARNESS._hook_json(
        {
            "hook_event_name": "UserPromptSubmit",
            "prompt": "联网调研当前 Python API 文档",
        },
        state_file,
    )

    assert code == 0
    assert "Automatic mutation/Stop gating is unavailable" in payload[
        "hookSpecificOutput"
    ]["additionalContext"]
    assert not state_file.exists()

    HARNESS._safe_write_json(
        state_file,
        {
            "turns": {
                "unknown-session::unknown-turn": {
                    "requires_research": True,
                    "status": "needs_research",
                }
            }
        },
    )
    pretool_code, _ = HARNESS._hook_json(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "apply_patch",
            "tool_input": {},
        },
        state_file,
    )
    stop_code, _ = HARNESS._hook_json(
        {"hook_event_name": "Stop"},
        state_file,
    )
    assert pretool_code == stop_code == 0


def test_pending_research_blocks_mutating_shell_and_mcp_tools(tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    session_id = "session-tools"
    turn_id = "turn-tools"
    HARNESS._hook_json(
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": session_id,
            "turn_id": turn_id,
            "prompt": "联网调研当前 Python API 文档",
        },
        state_file,
    )

    def pretool(tool_name: str, tool_input: dict) -> int:
        code, _ = HARNESS._hook_json(
            {
                "hook_event_name": "PreToolUse",
                "session_id": session_id,
                "turn_id": turn_id,
                "tool_name": tool_name,
                "tool_input": tool_input,
            },
            state_file,
        )
        return code

    assert pretool("mcp__server__list_items", {}) == 0
    assert pretool("mcp__server__create_item", {}) == 2
    assert pretool("mcp__server__fetch_items", {}) == 2
    assert pretool("Bash", {"command": "git status"}) == 0
    assert pretool("Bash", {"command": "git status && rm -rf ./tmp"}) == 2
    assert pretool(
        "PowerShell", {"command": "Get-Content input.txt | Out-File output.txt"}
    ) == 2
    assert pretool("PowerShell", {"command": "Set-Content output.txt value"}) == 2


def test_legacy_tiers_normalize_without_a_second_protocol(tmp_path: Path) -> None:
    quick = HARNESS.build_query_plan("research current API", tier="quick")
    deep = HARNESS.build_query_plan("research recurring RPA failure", tier="rpa_deep")

    assert quick["research_tier"] == "standard"
    assert deep["research_tier"] == "deep"


def test_missing_project_rpa_adapter_is_typed_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    real_import = __import__

    def deny_project_rpa(name, *args, **kwargs):
        if name.startswith("tools.rpa"):
            raise ModuleNotFoundError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", deny_project_rpa)
    result = HARNESS.run_rpa_probe(mode="control-plane", output_dir=tmp_path)

    assert result == {
        "ok": False,
        "status": "unavailable",
        "adapter": "rpa_probe",
        "error": "project_rpa_adapter_unavailable",
    }


def test_rpa_probe_loads_adapter_from_explicit_physical_project(
    tmp_path: Path,
) -> None:
    projects: list[Path] = []
    for adapter in ("project-a-rpa", "project-b-rpa"):
        project = tmp_path / adapter
        package = project / "tools" / "rpa"
        package.mkdir(parents=True)
        (project / "tools" / "__init__.py").write_text("", encoding="utf-8")
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "control_plane.py").write_text(
            "def build_control_plane_summary(db_path):\n"
            f"    return {{'adapter': '{adapter}', 'db_path': str(db_path)}}\n",
            encoding="utf-8",
        )
        projects.append(project)

    first = HARNESS.run_rpa_probe(
        mode="control-plane",
        project_root=projects[0],
    )
    second = HARNESS.run_rpa_probe(mode="control-plane", project_root=projects[1])

    assert first["control_plane"]["adapter"] == "project-a-rpa"
    assert second["ok"] is True
    assert second["project_root_source"] == "argument"
    assert second["control_plane"]["adapter"] == "project-b-rpa"
    assert str(projects[1] / "test-results" / "3can" / "rpa_probe") in second[
        "control_plane"
    ]["db_path"]
    assert second["output_dir"] == str(
        projects[1] / "test-results" / "3can" / "research_sources"
    )


def test_skill_distribution_files_are_present() -> None:
    skill = (
        ROOT
        / "examples"
        / "codex-cli-project-kit"
        / ".agents"
        / "skills"
        / "3can-deep-research"
    )
    assert (skill / "SKILL.md").is_file()
    assert (skill / "agents" / "openai.yaml").is_file()
    assert (skill / "references" / "research-ledger.md").is_file()
