from __future__ import annotations

import importlib.util
from pathlib import Path


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


def _record(tmp_path: Path, *, tier: str, source_types: list[str], **overrides):
    count = len(source_types)
    values = {
        "question": "How should the existing 3CAN research skill investigate difficult engineering work?",
        "source_urls": _urls(count),
        "session_id": "session-research-test",
        "turn_id": f"turn-{tier}-{count}",
        "state_file": tmp_path / "state.json",
        "ledger_dir": tmp_path / "ledgers",
        "research_tier": tier,
        "elapsed_minutes": 8 if tier in {"standard", "quick"} else 25,
        "source_types": source_types,
        "query_terms": ["3CAN research"],
        "query_variants": [
            f"query {index}"
            for index in range(18 if tier in {"deep", "rpa_deep"} else 6)
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
    return HARNESS.record_done(**values)


def test_two_tier_classifier_and_timeboxes() -> None:
    standard = HARNESS.detect_research_requirement("请联网调研当前 Python API 文档")
    deep = HARNESS.detect_research_requirement("RPA 部署连续失败，需要深度研究平台问题")

    assert standard["research_tier"] == "standard"
    assert standard["time_budget"]["hard_cap_minutes"] == 10
    assert standard["time_budget"]["min_sources"] == 30
    assert standard["time_budget"]["min_source_families"] == 5
    assert "targeted_web" in standard["source_strategy"]
    assert deep["research_tier"] == "deep"
    assert deep["time_budget"]["hard_cap_minutes"] == 30
    assert deep["time_budget"]["min_sources"] == 90
    assert deep["time_budget"]["min_source_families"] == 6
    assert "targeted_web" in deep["source_strategy"]


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
            30,
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
        source_types=["targeted_web"] * 30,
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
    assert not (tmp_path / "state.json").exists()


def test_deep_requires_community_and_platform_evidence(tmp_path: Path) -> None:
    result = _record(
        tmp_path,
        tier="deep",
        source_types=_source_types(
            90,
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
            90,
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
    assert result["sidecar_decision"]["source_family_count"] >= 6


def test_missing_or_excess_elapsed_time_blocks_completion(tmp_path: Path) -> None:
    types = _source_types(
        30,
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
    package = tmp_path / "tools" / "rpa"
    package.mkdir(parents=True)
    (tmp_path / "tools" / "__init__.py").write_text("", encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "control_plane.py").write_text(
        "def build_control_plane_summary(db_path):\n"
        "    return {'adapter': 'task-project-rpa', 'db_path': str(db_path)}\n",
        encoding="utf-8",
    )

    result = HARNESS.run_rpa_probe(
        mode="control-plane",
        output_dir=tmp_path / "output",
        project_root=tmp_path,
    )

    assert result["ok"] is True
    assert result["project_root_source"] == "argument"
    assert result["control_plane"]["adapter"] == "task-project-rpa"
    assert str(tmp_path / "test-results" / "3can" / "rpa_probe") in result[
        "control_plane"
    ]["db_path"]


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
