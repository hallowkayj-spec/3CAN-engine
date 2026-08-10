"""Harness Bench Runner — L3 Governance/Harness benchmark.

For each test case, spawn 3can-behavioral-gate.js as subprocess, feed tool_input
as stdin JSON, capture stdout + exit code, compare decision to expected.

Gate contract (from 3can-behavioral-gate.js):
- exit 0, no stdout → implicit allow (bypass case)
- exit 0, stdout JSON with hookSpecificOutput.additionalContext → warn (allow with note)
- exit 0, stdout JSON with hookSpecificOutput.permissionDecision="deny" → deny

Run: python benchmark/harness_bench_runner.py
"""
from __future__ import annotations
import json
import os
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUITE = ROOT / "benchmark" / "harness_bench_v1.json"
OUT_DIR = ROOT / "benchmark" / "_harness"
GATE = Path(os.path.expanduser("~")) / ".claude" / "scripts" / "hooks" / "3can-behavioral-gate.js"


def invoke_gate(stdin_payload: dict, timeout_s: int = 15) -> dict:
    stdin_bytes = json.dumps(stdin_payload).encode("utf-8")
    t0 = time.perf_counter()
    try:
        p = subprocess.run(
            ["node", str(GATE)], input=stdin_bytes,
            capture_output=True, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return {"decision": "timeout", "latency_ms": int((time.perf_counter() - t0) * 1000),
                "stdout": "", "stderr": "TIMEOUT"}
    latency_ms = int((time.perf_counter() - t0) * 1000)
    stdout = p.stdout.decode("utf-8", errors="replace").strip()
    stderr = p.stderr.decode("utf-8", errors="replace").strip()
    exit_code = p.returncode

    if not stdout:
        return {"decision": "allow-implicit", "latency_ms": latency_ms,
                "stdout": "", "stderr": stderr, "exit_code": exit_code}

    try:
        out = json.loads(stdout)
        hso = out.get("hookSpecificOutput", {})
        if hso.get("permissionDecision") == "deny":
            reason = hso.get("permissionDecisionReason", "")
            return {"decision": "deny", "reason": reason,
                    "latency_ms": latency_ms, "stdout": stdout[:800], "stderr": stderr,
                    "exit_code": exit_code}
        if hso.get("additionalContext"):
            return {"decision": "warn", "reason": hso.get("additionalContext", ""),
                    "latency_ms": latency_ms, "stdout": stdout[:800], "stderr": stderr,
                    "exit_code": exit_code}
        return {"decision": "allow-explicit", "latency_ms": latency_ms,
                "stdout": stdout[:800], "stderr": stderr, "exit_code": exit_code}
    except Exception as e:
        return {"decision": "parse-error", "error": str(e)[:200],
                "latency_ms": latency_ms, "stdout": stdout[:800], "stderr": stderr,
                "exit_code": exit_code}


def score_case(case: dict, result: dict) -> dict:
    expected_dec = case["expected_decision"]
    expected_reason = case.get("expected_reason_contains")
    actual_dec = result["decision"]
    # Normalize allow-implicit / allow-explicit both count as "allow"
    dec_match = (expected_dec == "allow" and actual_dec.startswith("allow")) or (expected_dec == actual_dec)
    reason_match = True
    if expected_reason is not None:
        actual_reason = (result.get("reason") or "").lower()
        reason_match = expected_reason.lower() in actual_reason
    overall = dec_match and reason_match
    return {
        "pass": overall, "decision_match": dec_match, "reason_match": reason_match,
        "expected_decision": expected_dec, "actual_decision": actual_dec,
    }


def main() -> int:
    suite = json.loads(SUITE.read_text(encoding="utf-8"))
    cases = suite["cases"]
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    results = []
    for c in cases:
        r = invoke_gate(c["tool_input_stdin"])
        s = score_case(c, r)
        s["id"] = c["id"]
        s["category"] = c["category"]
        s["latency_ms"] = r["latency_ms"]
        s["actual_reason"] = (r.get("reason") or "")[:200]
        results.append(s)
        flag = "PASS" if s["pass"] else "FAIL"
        print(f"  [{c['id']}] {flag:5s} {c['category']:20s} "
              f"expect={s['expected_decision']:10s} got={s['actual_decision']:18s} lat={r['latency_ms']}ms")
        if not s["pass"]:
            print(f"    reason_match={s['reason_match']} actual_reason={s['actual_reason'][:120]}")

    total = len(results)
    passed = sum(1 for r in results if r["pass"])
    pass_rate = passed / total if total else 0
    by_cat = {}
    for cat in set(r["category"] for r in results):
        sub = [r for r in results if r["category"] == cat]
        by_cat[cat] = round(sum(1 for r in sub if r["pass"]) / len(sub), 3)
    lat = sorted(r["latency_ms"] for r in results)
    p50 = lat[len(lat) // 2] if lat else 0
    p95 = lat[int(len(lat) * 0.95)] if lat else 0

    print()
    print(f"=== Harness-Bench v1 - n={total} ===")
    print(f"  pass_rate: {pass_rate:.3f} ({passed}/{total})")
    print(f"  by_category: {dict(sorted(by_cat.items()))}")
    print(f"  latency: p50={p50}ms p95={p95}ms")

    out = OUT_DIR / f"harness_{int(time.time())}.json"
    out.write_text(json.dumps({
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "n_cases": total,
        "pass_rate": round(pass_rate, 4),
        "by_category": by_cat,
        "latency_p50_ms": p50, "latency_p95_ms": p95,
        "results": results,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print(f"Saved: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
