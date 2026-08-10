from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


token_usage = load_module("token_usage_under_test", "backend/token_usage.py")


def test_record_provider_usage_and_summary(tmp_path):
    store = token_usage.TokenUsageStore(tmp_path / "usage.sqlite3")
    event = store.record_event({
        "request_id": "req_1",
        "provider": "openai",
        "model": "gpt-test",
        "agent_id": "codex-main",
        "session_id": "ses_1",
        "task_id": "task_1",
        "prompt_tokens": 100,
        "completion_tokens": 25,
        "cached_tokens": 10,
        "reasoning_tokens": 5,
        "cost_usd": 0.0123,
        "latency_ms": 900,
        "metadata": {
            "route_nodes": ["DOC-1"],
            "prompt": "must not persist",
            "safe_tag": "uat",
        },
    })

    assert event["total_tokens"] == 125
    assert event["metadata"]["safe_tag"] == "uat"
    assert "prompt" not in event["metadata"]

    summary = store.summary(group_by="model")
    assert summary["totals"]["event_count"] == 1
    assert summary["totals"]["input_tokens"] == 100
    assert summary["totals"]["output_tokens"] == 25
    assert summary["groups"][0]["key"] == "gpt-test"
    assert summary["groups"][0]["total_tokens"] == 125


def test_record_local_estimate_defaults_to_estimated_status(tmp_path):
    store = token_usage.TokenUsageStore(tmp_path / "usage.sqlite3")
    event = store.record_event({
        "request_id": "req_est",
        "usage_source": "local_estimate",
        "input_tokens": 42,
    })
    assert event["status"] == "estimated"
    assert event["total_tokens"] == 42


def test_invalid_usage_source_rejected(tmp_path):
    store = token_usage.TokenUsageStore(tmp_path / "usage.sqlite3")
    try:
        store.record_event({"usage_source": "unknown"})
    except ValueError as exc:
        assert "invalid usage_source" in str(exc)
    else:
        raise AssertionError("invalid usage source should fail")


def test_duplicate_request_id_rejected(tmp_path):
    store = token_usage.TokenUsageStore(tmp_path / "usage.sqlite3")
    store.record_event({"request_id": "req_duplicate", "input_tokens": 1})
    try:
        store.record_event({"request_id": "req_duplicate", "input_tokens": 2})
    except ValueError as exc:
        assert "duplicate request_id" in str(exc)
    else:
        raise AssertionError("duplicate request_id should fail")


def test_summary_filters_by_provider(tmp_path):
    store = token_usage.TokenUsageStore(tmp_path / "usage.sqlite3")
    store.record_event({"request_id": "req_a", "provider": "openai", "input_tokens": 10, "output_tokens": 2})
    store.record_event({"request_id": "req_b", "provider": "anthropic", "input_tokens": 100, "output_tokens": 20})

    summary = store.summary(provider="openai")
    assert summary["totals"]["event_count"] == 1
    assert summary["totals"]["total_tokens"] == 12


def test_summary_groups_by_agent_for_codex_mimo_opus(tmp_path):
    store = token_usage.TokenUsageStore(tmp_path / "usage.sqlite3")
    store.record_event({"request_id": "req_codex", "agent_id": "codex-main", "input_tokens": 10, "output_tokens": 5})
    store.record_event({"request_id": "req_mimo", "agent_id": "mimo", "input_tokens": 20, "output_tokens": 5})
    store.record_event({"request_id": "req_opus", "agent_id": "opus", "input_tokens": 30, "output_tokens": 5})

    summary = store.summary(group_by="agent_id")
    grouped = {item["key"]: item["total_tokens"] for item in summary["groups"]}

    assert summary["totals"]["total_tokens"] == 75
    assert grouped["codex-main"] == 15
    assert grouped["mimo"] == 25
    assert grouped["opus"] == 35


def test_summary_groups_by_date_and_request_kind(tmp_path):
    store = token_usage.TokenUsageStore(tmp_path / "usage.sqlite3")
    store.record_event({
        "request_id": "req_day_a",
        "created_at": "2026-05-01T10:00:00+00:00",
        "request_kind": "runtime_status",
        "input_tokens": 10,
        "output_tokens": 2,
    })
    store.record_event({
        "request_id": "req_day_b",
        "created_at": "2026-05-02T10:00:00+00:00",
        "request_kind": "route",
        "input_tokens": 20,
        "output_tokens": 3,
    })

    by_date = {item["key"]: item["total_tokens"] for item in store.summary(group_by="date")["groups"]}
    by_kind = {item["key"]: item["total_tokens"] for item in store.summary(group_by="request_kind")["groups"]}

    assert by_date["2026-05-01"] == 12
    assert by_date["2026-05-02"] == 23
    assert by_kind["runtime_status"] == 12
    assert by_kind["route"] == 23


def test_local_estimator_returns_guardrail_estimate():
    result = token_usage.estimate_tokens_for_payload({
        "text": "hello world",
        "tools": [{"name": "route", "description": "3CAN route"}],
    })
    assert result["usage_source"] == "local_estimate"
    assert result["estimated_input_tokens"] > 0
    assert result["estimated_output_tokens"] == 0
    assert result["estimated_tool_tokens"] > 0


def test_local_estimator_splits_input_and_output_payloads():
    result = token_usage.estimate_tokens_for_payload({
        "input": {"task": "route token status"},
        "output": {"nodes": [{"id": "DEC-token", "summary": "token meter status"}]},
    })

    assert result["estimated_input_tokens"] > 0
    assert result["estimated_output_tokens"] > 0
    assert result["estimated_total_tokens"] == result["estimated_input_tokens"] + result["estimated_output_tokens"]


def test_integration_status_marks_mock_rows_as_test_only(tmp_path):
    store = token_usage.TokenUsageStore(tmp_path / "usage.sqlite3")
    store.record_event({
        "request_id": "smoke_codex",
        "provider": "litellm",
        "model": "mock-model",
        "agent_id": "codex-main",
        "input_tokens": 300,
        "output_tokens": 118,
        "metadata": {"source": "smoke_test"},
    })

    status = store.integration_status()
    codex = next(item for item in status["tracked_agents"] if item["agent_id"] == "codex-main")

    assert codex["status"] == "test_only"
    assert codex["actual"]["total_tokens"] == 0
    assert codex["test"]["total_tokens"] == 418
    assert status["hook_status"]["codex_runtime_bridge"] == "test_only"


def test_integration_status_marks_expected_provider_usage_as_actual(tmp_path):
    store = token_usage.TokenUsageStore(tmp_path / "usage.sqlite3")
    store.record_event({
        "request_id": "real_codex",
        "provider": "openai",
        "model": "gpt-5.5",
        "agent_id": "codex-main",
        "usage_source": "provider_response",
        "input_tokens": 1200,
        "output_tokens": 300,
    })

    status = store.integration_status()
    codex = next(item for item in status["tracked_agents"] if item["agent_id"] == "codex-main")

    assert codex["status"] == "actual_connected"
    assert codex["actual"]["total_tokens"] == 1500
    assert codex["test"]["total_tokens"] == 0


def test_runtime_status_counts_as_actual_for_expected_codex_model(tmp_path):
    store = token_usage.TokenUsageStore(tmp_path / "usage.sqlite3")
    store.record_event({
        "request_id": "codex_status_unit",
        "provider": "codex-cli",
        "model": "gpt-5.5",
        "agent_id": "codex-main",
        "session_id": "codex-thread",
        "usage_source": "runtime_status",
        "request_kind": "runtime_status",
        "input_tokens": 900,
        "cached_tokens": 600,
        "output_tokens": 100,
        "reasoning_tokens": 25,
    })

    status = store.integration_status()
    codex = next(item for item in status["tracked_agents"] if item["agent_id"] == "codex-main")

    assert codex["status"] == "actual_connected"
    assert codex["actual"]["total_tokens"] == 1000
    assert codex["actual"]["cached_tokens"] == 600
    assert codex["recent_events"][0]["usage_source"] == "runtime_status"


def test_overview_returns_total_daily_session_and_tool_rollups(tmp_path):
    store = token_usage.TokenUsageStore(tmp_path / "usage.sqlite3")
    store.record_event({
        "request_id": "codex_status_unit",
        "created_at": "2026-05-02T10:00:01+00:00",
        "provider": "codex-cli",
        "model": "gpt-5.5",
        "agent_id": "codex-main",
        "session_id": "codex-019unit-thread",
        "task_id": "codex-status",
        "request_kind": "runtime_status",
        "usage_source": "runtime_status",
        "input_tokens": 100,
        "cached_tokens": 40,
        "output_tokens": 10,
        "reasoning_tokens": 3,
        "metadata": {"thread_id": "019unit-thread", "session_file": "~/.codex/sessions/unit.jsonl"},
    })
    store.record_event({
        "request_id": "codex_route_estimate",
        "created_at": "2026-05-02T10:01:01+00:00",
        "provider": "codex-cli",
        "model": "gpt-5.5-estimate",
        "agent_id": "codex-main",
        "session_id": "SES-unit",
        "task_id": "route",
        "request_kind": "route",
        "usage_source": "local_estimate",
        "input_tokens": 20,
        "output_tokens": 5,
    })

    overview = store.overview(limit=10)
    sessions = {item["key"]: item for item in overview["groups"]["sessions"]}
    dates = {item["key"]: item for item in overview["groups"]["dates"]}
    kinds = {item["key"]: item for item in overview["groups"]["request_kinds"]}

    assert overview["totals"]["actual"]["total_tokens"] == 110
    assert overview["totals"]["actual"]["fresh_input_tokens"] == 60
    assert overview["totals"]["actual"]["cached_input_ratio"] == 40.0
    assert overview["totals"]["actual"]["fresh_output_ratio"] == 6.0
    assert overview["totals"]["estimate"]["total_tokens"] == 25
    assert overview["impact"]["summary"]["runtime_actual"]["fresh_input_tokens"] == 60
    assert overview["impact"]["summary"]["route_context_estimate"]["total_tokens"] == 25
    assert overview["impact"]["summary"]["route_context_share_of_runtime_fresh_pct"] == 41.67
    assert overview["impact"]["summary"]["baseline_scenario"]["estimated_avoided_tokens"] == 79975
    assert dates["2026-05-02"]["total_tokens"] == 135
    assert dates["2026-05-02"]["fresh_input_tokens"] == 80
    assert sessions["codex-019unit-thread"]["label"].startswith("Codex TUI")
    assert sessions["codex-019unit-thread"]["thread_id"] == "019unit-thread"
    assert sessions["codex-019unit-thread"]["fresh_input_tokens"] == 60
    assert kinds["runtime_status"]["total_tokens"] == 110
    assert kinds["route"]["total_tokens"] == 25


def test_impact_keeps_measured_runtime_and_3can_estimate_separate(tmp_path):
    store = token_usage.TokenUsageStore(tmp_path / "usage.sqlite3")
    store.record_event({
        "request_id": "codex_status_impact",
        "created_at": "2026-05-02T10:00:01+00:00",
        "provider": "codex-cli",
        "model": "gpt-5.5",
        "agent_id": "codex-main",
        "session_id": "codex-impact-thread",
        "task_id": "codex-status",
        "request_kind": "runtime_status",
        "usage_source": "runtime_status",
        "input_tokens": 1000,
        "cached_tokens": 900,
        "output_tokens": 20,
        "total_tokens": 1020,
    })
    store.record_event({
        "request_id": "route_estimate_impact",
        "created_at": "2026-05-02T10:01:01+00:00",
        "provider": "codex-cli",
        "model": "gpt-5.5-estimate",
        "agent_id": "codex-main",
        "session_id": "SES-impact",
        "task_id": "route",
        "request_kind": "route",
        "usage_source": "local_estimate",
        "input_tokens": 10,
        "output_tokens": 40,
        "total_tokens": 50,
        "metadata": {"source": "3can_codex_helper_auto_estimate", "estimate_method": "unit"},
    })
    store.record_event({
        "request_id": "unrelated_estimate_impact",
        "request_kind": "chat",
        "usage_source": "local_estimate",
        "input_tokens": 999,
        "total_tokens": 999,
    })

    impact = store.impact(limit=10)["impact"]
    summary = impact["summary"]

    assert summary["runtime_actual"]["fresh_input_tokens"] == 100
    assert summary["runtime_actual"]["fresh_output_ratio"] == 5.0
    assert summary["threecan_estimate"]["total_tokens"] == 50
    assert summary["route_context_estimate"]["event_count"] == 1
    assert summary["threecan_share_of_runtime_fresh_pct"] == 50.0
    assert summary["baseline_scenario"]["kind"] == "scenario_not_provider_measured"
    assert summary["baseline_scenario"]["compression_ratio"] == 1600.0
    assert impact["recent_context_events"][0]["estimate_method"] == "unit"


def test_integration_status_keeps_local_estimates_separate(tmp_path):
    store = token_usage.TokenUsageStore(tmp_path / "usage.sqlite3")
    store.record_event({
        "request_id": "estimate_codex",
        "provider": "local",
        "model": "gpt-5.5",
        "agent_id": "codex-main",
        "session_id": "SES-unit-codex",
        "usage_source": "local_estimate",
        "input_tokens": 200,
        "output_tokens": 30,
    })

    status = store.integration_status()
    codex = next(item for item in status["tracked_agents"] if item["agent_id"] == "codex-main")

    assert codex["status"] == "estimate_only"
    assert codex["actual"]["total_tokens"] == 0
    assert codex["estimate"]["total_tokens"] == 230
    assert codex["recent_events"][0]["session_id"] == "SES-unit-codex"
    assert codex["recent_events"][0]["input_tokens"] == 200
    assert codex["recent_events"][0]["output_tokens"] == 30


def test_collect_codex_status_events_dedupes_repeated_status_snapshots(tmp_path):
    sessions_root = tmp_path / "sessions"
    session_file = sessions_root / "2026" / "05" / "02" / "rollout-2026-05-02T10-00-00-019unit-thread.jsonl"
    session_file.parent.mkdir(parents=True)
    repeated = {
        "timestamp": "2026-05-02T10:00:01.000Z",
        "type": "event_msg",
        "payload": {
            "model": "gpt-5.5",
            "info": {
                "last_token_usage": {
                    "input_tokens": 10,
                    "cached_input_tokens": 4,
                    "output_tokens": 2,
                    "reasoning_output_tokens": 1,
                    "total_tokens": 12,
                },
                "total_token_usage": {
                    "input_tokens": 10,
                    "cached_input_tokens": 4,
                    "output_tokens": 2,
                    "reasoning_output_tokens": 1,
                    "total_tokens": 12,
                },
                "model_context_window": 258400,
            },
        },
    }
    second = {
        "timestamp": "2026-05-02T10:00:02.000Z",
        "type": "event_msg",
        "payload": {
            "info": {
                "last_token_usage": {
                    "input_tokens": 7,
                    "cached_input_tokens": 3,
                    "output_tokens": 1,
                    "reasoning_output_tokens": 0,
                    "total_tokens": 8,
                },
                "total_token_usage": {
                    "input_tokens": 17,
                    "cached_input_tokens": 7,
                    "output_tokens": 3,
                    "reasoning_output_tokens": 1,
                    "total_tokens": 20,
                },
                "rate_limits": {"limit_name": "GPT-5.5", "plan_type": "pro"},
            },
        },
    }
    session_file.write_text(
        "\n".join(json.dumps(item) for item in (repeated, repeated, second)) + "\n",
        encoding="utf-8",
    )

    collected = token_usage.collect_codex_status_events(sessions_root=sessions_root, max_files=1, max_events=10)

    assert collected["event_count"] == 2
    assert [event["total_tokens"] for event in collected["events"]] == [12, 8]
    assert collected["latest_snapshot"]["total_token_usage"]["total_tokens"] == 20
    assert collected["events"][0]["metadata"]["source"] == "codex_session_jsonl_status"
    assert "prompt" not in json.dumps(collected["events"], ensure_ascii=False).lower()


def test_import_codex_status_events_skips_duplicates(tmp_path):
    sessions_root = tmp_path / "sessions"
    session_file = sessions_root / "2026" / "05" / "02" / "rollout-2026-05-02T10-00-00-019unit-thread.jsonl"
    session_file.parent.mkdir(parents=True)
    session_file.write_text(json.dumps({
        "timestamp": "2026-05-02T10:00:01.000Z",
        "type": "event_msg",
        "payload": {
            "model": "gpt-5.5",
            "info": {
                "last_token_usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
                "total_token_usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
            },
        },
    }) + "\n", encoding="utf-8")
    store = token_usage.TokenUsageStore(tmp_path / "usage.sqlite3")

    first = store.import_codex_status_events(sessions_root=sessions_root)
    second = store.import_codex_status_events(sessions_root=sessions_root)

    assert first["imported_events"] == 1
    assert second["imported_events"] == 0
    assert second["skipped_duplicates"] == 1
    assert store.summary()["totals"]["total_tokens"] == 12


def test_health_exposes_tracked_agent_and_hook_status(tmp_path):
    store = token_usage.TokenUsageStore(tmp_path / "usage.sqlite3")

    health = store.health()

    assert health["ok"] is True
    assert "tracked_agents" in health
    assert "hook_status" in health
    assert health["hook_status"]["token_api"] == "ready"
