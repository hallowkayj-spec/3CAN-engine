# ErrorKnowledge real-query baseline — 2026-08-23

Status: **FAIL (candidate improvement required)**.

This is an internal production-graph baseline, not a public benchmark or a
cross-product claim. The reviewed query text and project-specific node IDs stay
in the local evidence directory and are excluded from the release package.
The committed runner stores only query hashes, result IDs, aggregates, runtime
binding, and gate evidence in its receipt.

## Bound protocol

- schema: `3can.error-knowledge-benchmark/v1`
- policy: `3can.error-knowledge-benchmark-policy/v1`
- suite SHA-256:
  `412e19df066d7fca46159578de8fed7bc9c441c333f137e353d31e92cfee919e`
- runner: `neural-memory/benchmark/error_knowledge_benchmark.py`
- route mode: production `9700`, deep-ready BGE-M3, `mode=slim`
- judge: deterministic match against node IDs selected by manual review of
  canonical ek2 identities and incident diagnoses; route output was not used
  to create ground truth
- cases: 10 positive queries (five exact identities and five natural-language
  paraphrases) plus three ordinary-development negative controls
- hardware class: local Windows workstation; single baseline run
- raw receipt SHA-256:
  `53f56a96ad4a28cf954f6f4a9b103b4da9ae45e8a5ac801c41c6293c5221ea11`

## Baseline result

| Gate | Result | Threshold | Typed state |
|---|---:|---:|---|
| Top-1 recall | 0.50 | >= 0.80 | fail |
| Top-3 recall | 0.50 | >= 0.95 | fail |
| Incorrect-family pollution | 0.00 | <= 0.01 | pass |
| Ordinary-query false error route | 0.00 | <= 0.01 | pass |
| Latency p95 | 264.1 ms | <= 10% versus matching baseline | validating |

All five exact-identity queries ranked the canonical case first. All five
natural-language paraphrases missed the canonical case in the first three
results. Therefore exact ek2 enforcement is sound, but semantic ErrorKnowledge
retrieval is not yet release-acceptable. The negative controls and incompatible
family checks did not leak an ErrorKnowledge false positive.

The latency gate is deliberately `validating`: this run establishes the
matching-suite baseline and cannot compare against itself. A candidate run may
pass latency only when its receipt carries the same suite SHA-256, even if the
graph source-manifest binding changes.

## Reproduction boundary

```text
python -B neural-memory/benchmark/error_knowledge_benchmark.py \
  --dataset <internal-reviewed-suite.json> \
  --output <internal-receipt.json> \
  --base-url http://127.0.0.1:9700 \
  --timeout-sec 15
```

The runner returns `INVALID_GRAPH_BINDING` when the production profile,
source manifest, or reviewed node set drifts. It returns `UNAVAILABLE` on
transport/readiness failure and never skips failed queries.
