from __future__ import annotations

import hashlib
import importlib.util
import json
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


def test_unopened_urls_and_search_results_do_not_satisfy_source_gate(
    tmp_path: Path,
) -> None:
    source_types = _source_types(
        30,
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
        "query_variants": [f"query {index}" for index in range(6)],
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
        source_urls=_urls(30),
        source_types=source_types,
    )

    artifact_dir = tmp_path / "search-results"
    artifact_dir.mkdir()
    search_result_files: list[str] = []
    for index, (url, source_type) in enumerate(zip(_urls(30), source_types)):
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
