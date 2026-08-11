from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


RUNNER = Path(__file__).resolve().parents[1] / "benchmark" / "milestone_recovery_probe.py"


def load_runner():
    spec = importlib.util.spec_from_file_location("milestone_recovery_probe", RUNNER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def probe_spec():
    return {
        "schema": "3can.milestone-recovery-probe/v1",
        "probe_id": "PUBLIC-runtime-owner-recovery",
        "task": "current canonical runtime owner and verified release evidence",
        "agent_id": "PUBLIC-clean-probe",
        "project_id": "public-demo",
        "project_namespace": "public-demo",
        "workspace_id": "git-family-worktree",
        "expected_graph_root_sha256": "a" * 64,
        "expected_readiness": "development",
        "expected_node_ids": ["INTF-PUBLIC-runtime"],
        "critical_facts": [
            {
                "fact_id": "owner",
                "node_id": "INTF-PUBLIC-runtime",
                "any_of": ["immutable runtime", "release owner"],
            }
        ],
        "evidence_facts": [
            {
                "fact_id": "commit",
                "node_id": "INTF-PUBLIC-runtime",
                "any_of": ["cb532da"],
            }
        ],
    }


def test_probe_passes_only_after_route_and_exact_read_recover_gold():
    runner = load_runner()
    calls = []

    def request_json(_base_url, path, **kwargs):
        calls.append((path, kwargs))
        if path == "/api/stats?deep=true":
            return True, {
                "runtime_identity": {
                    "schema": "3can.runtime-identity/v1",
                    "graph_root_sha256": "a" * 64,
                },
                "readiness": {
                    "schema": "3can.production-readiness/v1",
                    "mode": "development",
                    "development_ready": True,
                },
            }
        if path == "/api/route":
            return True, {
                "route_response_schema": "3can.route-response/v1",
                "mode": "skeleton",
                "nodes": [{"id": "INTF-PUBLIC-runtime"}],
                "route_meta": {"route_id": "route-PUBLIC-1"},
            }
        return True, {
            "id": "INTF-PUBLIC-runtime",
            "content": {
                "description": "immutable runtime is the release owner",
                "extra": {"commit": "cb532da"},
            },
        }

    result = runner.run_probe(
        probe_spec(),
        base_url="http://127.0.0.1:9701",
        request_json=request_json,
    )

    assert result["status"] == "PASS"
    assert result["route_id"] == "route-PUBLIC-1"
    assert calls[1][1]["payload"]["project_id"] == "public-demo"
    assert calls[2][0] == "/api/nodes/INTF-PUBLIC-runtime"


def test_probe_is_partial_when_route_misses_gold_even_if_other_node_has_terms():
    runner = load_runner()

    def request_json(_base_url, path, **_kwargs):
        if path == "/api/stats?deep=true":
            return True, {
                "runtime_identity": {
                    "schema": "3can.runtime-identity/v1",
                    "graph_root_sha256": "a" * 64,
                },
                "readiness": {
                    "schema": "3can.production-readiness/v1",
                    "mode": "development",
                    "development_ready": True,
                },
            }
        if path == "/api/route":
            return True, {
                "route_response_schema": "3can.route-response/v1",
                "mode": "skeleton",
                "nodes": [{"id": "SES-PUBLIC-noise"}],
                "route_meta": {"route_id": "route-PUBLIC-2"},
            }
        return True, {
            "id": "SES-PUBLIC-noise",
            "content": {
                "description": "immutable runtime release owner cb532da"
            },
        }

    result = runner.run_probe(
        probe_spec(),
        base_url="http://127.0.0.1:9701",
        request_json=request_json,
    )

    assert result["status"] == "PARTIAL"
    assert result["missing_node_ids"] == ["INTF-PUBLIC-runtime"]


def test_probe_fails_closed_on_invalid_fact_contract():
    runner = load_runner()
    spec = probe_spec()
    spec["critical_facts"] = [{"fact_id": "owner", "any_of": []}]

    with pytest.raises(
        ValueError,
        match="probe_fact_requires_fact_id_node_id_and_any_of",
    ):
        runner.run_probe(
            spec,
            base_url="http://127.0.0.1:9701",
            request_json=lambda _base_url, path, **_kwargs: (
                (
                    True,
                    {
                        "runtime_identity": {
                            "schema": "3can.runtime-identity/v1",
                            "graph_root_sha256": "a" * 64,
                        },
                        "readiness": {
                            "schema": "3can.production-readiness/v1",
                            "mode": "development",
                            "development_ready": True,
                        },
                    },
                )
                if path == "/api/stats?deep=true"
                else (
                    True,
                    {
                        "route_response_schema": "3can.route-response/v1",
                        "mode": "skeleton",
                        "nodes": [{"id": "INTF-PUBLIC-runtime"}],
                        "route_meta": {},
                    },
                )
            ),
        )


def test_probe_does_not_combine_facts_across_expected_nodes():
    runner = load_runner()
    spec = probe_spec()
    spec["expected_node_ids"] = ["INTF-PUBLIC-runtime", "EVD-PUBLIC-release"]
    spec["evidence_facts"] = [
        {
            "fact_id": "commit",
            "node_id": "EVD-PUBLIC-release",
            "any_of": ["cb532da"],
        }
    ]

    def request_json(_base_url, path, **_kwargs):
        if path == "/api/stats?deep=true":
            return True, {
                "runtime_identity": {
                    "schema": "3can.runtime-identity/v1",
                    "graph_root_sha256": "a" * 64,
                },
                "readiness": {
                    "schema": "3can.production-readiness/v1",
                    "mode": "development",
                    "development_ready": True,
                },
            }
        if path == "/api/route":
            return True, {
                "route_response_schema": "3can.route-response/v1",
                "mode": "skeleton",
                "nodes": [
                    {"id": "INTF-PUBLIC-runtime"},
                    {"id": "EVD-PUBLIC-release"},
                ],
                "route_meta": {"route_id": "route-PUBLIC-3"},
            }
        if path.endswith("INTF-PUBLIC-runtime"):
            return True, {
                "id": "INTF-PUBLIC-runtime",
                "content": {
                    "description": "immutable runtime is the release owner",
                    "extra": {"commit": "cb532da"},
                },
            }
        return True, {
            "id": "EVD-PUBLIC-release",
            "content": {"description": "release receipt without commit"},
        }

    result = runner.run_probe(
        spec,
        base_url="http://127.0.0.1:9701",
        request_json=request_json,
    )

    assert result["status"] == "PARTIAL"
    assert result["evidence_facts"] == [
        {
            "fact_id": "commit",
            "node_id": "EVD-PUBLIC-release",
            "recovered": False,
            "matched": None,
        }
    ]


def test_probe_ignores_low_ceremony_notes_and_session_text():
    runner = load_runner()

    def request_json(_base_url, path, **_kwargs):
        if path == "/api/stats?deep=true":
            return True, {
                "runtime_identity": {
                    "schema": "3can.runtime-identity/v1",
                    "graph_root_sha256": "a" * 64,
                },
                "readiness": {
                    "schema": "3can.production-readiness/v1",
                    "mode": "development",
                    "development_ready": True,
                },
            }
        if path == "/api/route":
            return True, {
                "route_response_schema": "3can.route-response/v1",
                "mode": "skeleton",
                "nodes": [{"id": "INTF-PUBLIC-runtime"}],
                "route_meta": {"route_id": "route-PUBLIC-notes"},
            }
        return True, {
            "id": "INTF-PUBLIC-runtime",
            "content": {
                "description": "unverified placeholder",
                "notes": "immutable runtime is the release owner cb532da",
                "last_session": "cb532da",
            },
        }

    result = runner.run_probe(
        probe_spec(),
        base_url="http://127.0.0.1:9701",
        request_json=request_json,
    )

    assert result["status"] == "PARTIAL"
    assert result["critical_facts"][0]["recovered"] is False
    assert result["evidence_facts"][0]["recovered"] is False


def test_probe_matches_trusted_leaf_values_not_field_names_or_partial_digests():
    runner = load_runner()
    spec = probe_spec()
    spec["critical_facts"][0]["any_of"] = ["description"]
    spec["evidence_facts"][0]["any_of"] = ["a" * 8]

    def request_json(_base_url, path, **_kwargs):
        if path == "/api/stats?deep=true":
            return True, {
                "runtime_identity": {
                    "schema": "3can.runtime-identity/v1",
                    "graph_root_sha256": "a" * 64,
                },
                "readiness": {
                    "schema": "3can.production-readiness/v1",
                    "mode": "development",
                    "development_ready": True,
                },
            }
        if path == "/api/route":
            return True, {
                "route_response_schema": "3can.route-response/v1",
                "mode": "skeleton",
                "nodes": [{"id": "INTF-PUBLIC-runtime"}],
                "route_meta": {"route_id": "route-PUBLIC-leaves"},
            }
        return True, {
            "id": "INTF-PUBLIC-runtime",
            "content": {
                "description": "placeholder",
                "extra": {"sha256": "a" * 64},
            },
        }

    result = runner.run_probe(
        spec,
        base_url="http://127.0.0.1:9701",
        request_json=request_json,
    )

    assert result["status"] == "PARTIAL"
    assert result["critical_facts"][0]["recovered"] is False
    assert result["evidence_facts"][0]["recovered"] is False


def test_probe_rejects_one_character_fact_alternatives_before_network_access():
    runner = load_runner()
    spec = probe_spec()
    spec["critical_facts"][0]["any_of"] = ["x"]

    with pytest.raises(ValueError, match="probe_fact_alternative_invalid"):
        runner.run_probe(
            spec,
            base_url="http://127.0.0.1:9701",
            request_json=lambda *_args, **_kwargs: pytest.fail(
                "invalid spec must fail before network access"
            ),
        )


def test_probe_rejects_generic_status_facts_before_network_access():
    runner = load_runner()
    spec = probe_spec()
    spec["critical_facts"][0]["any_of"] = ["current"]

    with pytest.raises(
        ValueError,
        match="probe_fact_alternative_not_discriminative",
    ):
        runner.run_probe(
            spec,
            base_url="http://127.0.0.1:9701",
            request_json=lambda *_args, **_kwargs: pytest.fail(
                "non-discriminative spec must fail before network access"
            ),
        )


def test_probe_uses_token_boundaries_and_full_digest_equality():
    runner = load_runner()
    digest = "a" * 64

    assert runner._fact_matches_leaf("pass", "bypass") is False
    assert runner._fact_matches_leaf("cb532da", "commit cb532da verified") is True
    assert runner._fact_matches_leaf("cb532da", f"sha256:{digest}") is False
    assert runner._fact_matches_leaf("a" * 8, f"sha256:{digest}") is False
    assert runner._fact_matches_leaf(digest, f"sha256:{digest}") is True
    assert runner._fact_matches_leaf(f"sha256:{digest}", digest) is True
    assert runner._fact_matches_leaf("immutable runtime", "immutable runtime owner") is True


def test_probe_requires_both_fact_classes_before_network_access():
    runner = load_runner()
    spec = probe_spec()
    spec["critical_facts"] = []

    with pytest.raises(ValueError, match="probe_critical_facts_required"):
        runner.run_probe(
            spec,
            base_url="http://127.0.0.1:9701",
            request_json=lambda *_args, **_kwargs: pytest.fail(
                "invalid spec must fail before network access"
            ),
        )


def test_probe_stops_before_route_when_runtime_schema_is_wrong():
    runner = load_runner()
    calls = []

    def request_json(_base_url, path, **_kwargs):
        calls.append(path)
        return True, {
            "runtime_identity": {
                "schema": "wrong",
                "graph_root_sha256": "a" * 64,
            },
            "readiness": {
                "schema": "3can.production-readiness/v1",
                "mode": "development",
                "development_ready": True,
            },
        }

    result = runner.run_probe(
        probe_spec(),
        base_url="http://127.0.0.1:9701",
        request_json=request_json,
    )

    assert result["status"] == "PARTIAL"
    assert result["reason"] == "runtime_binding_mismatch"
    assert calls == ["/api/stats?deep=true"]
