# 3CAN v0.2 Release Candidate Validation

Status: `VERIFIED_CANDIDATE`

This receipt covers candidate engine commit
`b5abff7eaba5955a2928ea612d8f73e572fed78a`. It does not claim that a private
production installation has deployed this candidate; see
[`CURRENT_PRODUCTION.md`](CURRENT_PRODUCTION.md).

## Gates

| Gate | Command or evidence | Result |
| --- | --- | --- |
| Full release tests | `python -m pytest neural-memory/tests -q` | 411 passed |
| Focused project identity tests | `test_route_feedback_hardening.py` | 72 passed |
| Public Python lint | Ruff `0.15.7`: `python -m ruff check neural-memory examples scripts` | clean |
| Release isolation | `python scripts/prerelease_scan.py --strict .` | clean |
| Syntax compilation | in-memory `compile()` over tracked Python | 80 files |
| JSON / YAML / OpenAPI parse | tracked JSON plus release CI/protocol YAML | clean |
| Seed-graph benchmark | `SEED_GRAPH_BENCHMARK_20260809.json` | verified candidate |
| Immutable candidate | release `b0cd0601...`, 23-file manifest `4c98f50d...` | strict pre/post-UAT verification passed |
| Isolated real-graph UAT | copied graph on `9701` | paired identity route passed; partial identity returned 422; listener removed |
| Private real-graph regression | frozen copied graph, original 12 + 12 held-out, four warm repeats | deterministic; correctness unchanged |
| Bounded secret/private-path scan | candidate diff only | clean |
| GitHub-hosted CI | final Draft PR head | required; GitHub is the live status owner |

The test suite emits one upstream warning because the installed `jieba`
version imports the deprecated `pkg_resources` API. It does not fail the
candidate, but dependency compatibility should be rechecked before a future
Python or Setuptools upgrade.

## Architecture Checks

- Agent wrappers do not launch or terminate backend or proxy processes.
- Windows recovery can only request the configured external Scheduled Task;
  the external Supervisor remains the sole runtime owner.
- A failed route is observed once. The project kit does not blind-retry it or
  probe an admin endpoint as a recovery side path.
- Readiness is typed; shallow evidence cannot silently become verified deep
  production evidence.
- Route budget compaction preserves mandatory and temporal fields under
  `3can.route-response/v1`.
- Benchmark fixtures declare their seed-graph binding and refuse to score a
  different graph.
- Hooks, tickets, and wrappers remain optional adapters rather than the product
  surface or a second state machine.
- Project-scoped current retrieval orders exact project, explicit shared/global,
  unscoped fallback, then excludes explicit mismatch.
- Protected durable-current machine writes remain fail-closed: existing
  ErrorKnowledge evidence verifies a resolution but is not claim-bound to an
  arbitrary target node/field/value. Arbitrary valid or invalid pointers do not
  qualify.

## Real-graph route regression

The comparative 24-case profile below remains bound to predecessor `8fa7242`.
It was not relabelled as evidence for `b5abff7`. The repaired candidate changes
only the paired project-identity gate and was revalidated by the full suite,
focused counterexamples, and isolated `9701` UAT against a copied 5411-node /
6916-edge graph. Performance comparison for the repaired candidate therefore
remains `UNAVAILABLE`, rather than inferred from the predecessor run.

The private queries and graph are excluded from the public package. Aggregate
receipts bind the same copied graph, BGE-M3 revision, engine file hashes, and
reranker-off profile.

| Suite | Metric | Parent `380804c` | Candidate `8fa7242` |
| --- | --- | ---: | ---: |
| original 12 | Hit@1 / Hit@3 / Hit@5 / Hit@10 | .3333 / .5000 / .7500 / .8333 | unchanged |
| original 12 | MRR | .4591 | .4591 |
| original 12 | p50 / p95 | 1901.6 / 4162.7 ms | 2490.2 / 7563.5 ms |
| held-out 12 | Hit@1 / Hit@3 / Hit@5 / Hit@10 | .2500 / .5833 / .9167 / 1.0000 | unchanged |
| held-out 12 | MRR | .4882 | .4882 |
| held-out 12 | p50 / p95 | 1994.5 / 5107.6 ms | 2283.4 / 6148.5 ms |

Cross-process ranks and expanded-query hashes were identical with
`PYTHONHASHSEED=0` and `1`. Request-local reuse reduced superseded-set calls
from `18.04` to `1.83` per route, superseded work from `60.35` to `5.28`
ms/route in the first candidate profile and `5.88` ms/route in the final
profile; current-reality allow checks fell from `33.79` to `0.05` ms/route.
The final seed-0 process spent `2128.33` ms/route in BGE encoding versus
`1266.41` ms/route in the parent process, so its end-to-end p50/p95 regressed.
The final seed-1 process produced the same ranks and expansion hashes with
original p50/p95 `1485.2/3974.2` ms and held-out `1406.4/3188.9` ms. This wide
process variance is reported as observed, not attributed to the request-local
change and not called a universal performance improvement.

## Review

The simplicity review removed the automatic route retry/admin-health side path,
an unused proxy-state writer, and dead process-control test fixtures (net 73
lines). A subsequent focused review found no additional removable abstraction
in the changed governance path: `Lean already. Ship.`

## Published Benchmark Boundary

The content-addressed seed receipt records route MRR 0.9783, exact top-1
0.8261, query-level Hit@3 1.0, substrate top-1 1.0, and mean top-3 recall
0.8167. These are synthetic development-profile results, not private-corpus,
BGE-M3 production, or cross-product claims.

## Two-week dogfood observation readiness

No monitor, dashboard, daemon, or automatic analysis job is started. For each
meaningful module or Workorder, reuse existing route metadata, activity,
tickets, token telemetry, Git/PR/CI, runtime/provider receipts, and incident
notes to record only what is available:

- whether a clean Session found the current canonical fact and reused known-good
  capability or prior ErrorKnowledge;
- route/expansion count, grep/search, file reads, wrong paths, user corrections,
  elapsed time, and token usage, with missing values kept `UNAVAILABLE`;
- what changed since the last trusted module checkpoint, what is verified, what
  remains external/UAT-only, the current blocker, and the next human decision;
- legitimate safety blocks versus stale governance blocks or identity repair.

Do not count commits as success. After approximately two weeks, compare real
cases with historical observations and build nothing new unless evidence shows
a P0/P1 or repeated OPC cost.

## License Boundary

The included license is PolyForm Noncommercial 1.0.0. The package is source-available
and explicitly not OSI-approved open source. A change to an OSI-approved
license is a maintainer product/legal decision and is outside this technical
validation.
