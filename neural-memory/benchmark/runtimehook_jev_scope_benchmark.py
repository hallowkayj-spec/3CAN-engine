"""Opt-in six-case adapter comparison; not native-event or business acceptance."""
import argparse
import hashlib
import importlib
import json
import statistics
import time
from pathlib import Path

from runtimehook_checkpoint_benchmark import load_plugin, save_report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--plugin-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rh, identity = load_plugin(args.plugin_root.resolve())
    jev = importlib.import_module("runtimehook_jev")
    dataset = Path(__file__).with_name("runtimehook_jev_scope_cases.json")
    cases = json.loads(dataset.read_text(encoding="utf-8"))
    if len(cases) != 6 or args.output.exists():
        raise RuntimeError("Expected six fixed cases and a new receipt path")
    report = {"schema": "3can.jev-scope-benchmark/v1", "scope": "SYNTHETIC_ADAPTER_REAL_PROVIDER",
              "dataset_sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
              "tested_plugin": identity, "contract": jev.CONTRACT, "rows": []}
    if not args.run:
        print(json.dumps({**report, "status": "PLAN_ONLY", "max_requests": len(cases)}))
        return
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for case in cases:
        point = {"id": "scope", "title": "Assess only this checkpoint's listed acceptance criteria.",
                 "criteria": case["criteria"], "parameters": {},
                 "rubric": ["Evidence contradicts the criterion or provides no independent support.",
                            "Some support, but a material verification gap remains for this criterion.",
                            "Supplied evidence supports this criterion at the stated scope, without claiming future work."],
                 "minimum_score": 1.5, "minimum_confidence": 0.65, "minimum_probability": 0.7}
        packet = {"latest_user_request": case["request"], "claims": case["claims"],
                  "evidence": case["evidence"], "next_step": case["next_step"],
                  "checkpoint": {k: v for k, v in point.items() if not k.startswith("minimum_")}}
        intent = {"goal": case["request"], "acceptance": case["acceptance"], "non_goals": []}
        request, mapping = jev.build_request(packet, intent)
        started = time.monotonic()
        try:
            opinion = {"status": "OBSERVED", **jev.parse_response(jev.request_gateway(request, 15), request["questions"], mapping)}
        except jev.JevError as exc:
            opinion = {"status": "UNAVAILABLE", "error_code": str(exc)}
        issues = rh.checkpoints.concerns(opinion, point)
        row = {"id": case["id"], "expected_accepted": case["expected_accepted"],
               "expected_step": case["expected_step"], "accepted": not issues,
               "elapsed_ms": round((time.monotonic() - started) * 1000, 3), "opinion": opinion, "issues": issues,
               "request_sha256": rh.checkpoints.digest(request)}
        report["rows"].append(row)
        save_report(args.output, report)
        print(json.dumps({k: row[k] for k in ("id", "expected_accepted", "accepted", "issues")}), flush=True)
        if opinion["status"] != "OBSERVED" or sum(r["opinion"].get("usage", {}).get("cost", 0) for r in report["rows"]) >= 0.01:
            report["stopped_on"] = opinion.get("error_code", "SPEND_LIMIT")
            break
    rows = report["rows"]
    report["summary"] = {
        "attempted": len(rows),
        "negative_cases_accepted": sum(r["accepted"] and not r["expected_accepted"] for r in rows),
        "supported_cases_not_accepted": sum(not r["accepted"] and r["expected_accepted"] for r in rows),
        "step_matches": sum(r["opinion"].get("answers", {}).get("next_step", {}).get("choice") == r["expected_step"] for r in rows),
        "cost_usd": sum(r["opinion"].get("usage", {}).get("cost", 0) for r in rows),
        "median_ms": statistics.median(r["elapsed_ms"] for r in rows),
    }
    save_report(args.output, report)
    print(json.dumps(report["summary"]))


if __name__ == "__main__":
    main()
