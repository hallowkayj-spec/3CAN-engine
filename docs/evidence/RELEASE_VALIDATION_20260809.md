# 3CAN v0.2 Release Candidate Validation

Status: `VERIFIED_CANDIDATE`

This receipt covers candidate engine commit
`f712563743ab39d7891a1b3ff99d70c0b5ad89af`. It does not claim that a private
production installation has deployed this candidate; see
[`CURRENT_PRODUCTION.md`](CURRENT_PRODUCTION.md).

## Gates

| Gate | Command or evidence | Result |
| --- | --- | --- |
| Full release tests | `python -m pytest neural-memory/tests -q` | 406 passed |
| Focused provenance/project/probe tests | two focused modules | 78 passed |
| Public Python lint | Ruff `0.15.7`: `python -m ruff check neural-memory examples scripts` | clean |
| Release isolation | `python scripts/prerelease_scan.py --strict .` | clean |
| Syntax compilation | in-memory `compile()` over tracked Python | 80 files |
| JSON / YAML / OpenAPI parse | tracked JSON plus release CI/protocol YAML | clean |
| Seed-graph benchmark | `SEED_GRAPH_BENCHMARK_20260809.json` | verified candidate |
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

The private queries and graph are excluded from the public package. Aggregate
receipts bind the same copied graph, BGE-M3 revision, engine file hashes, and
reranker-off profile.

| Suite | Metric | Parent `380804c` | Candidate `f712563` |
| --- | --- | ---: | ---: |
| original 12 | Hit@1 / Hit@3 / Hit@5 / Hit@10 | .3333 / .5000 / .7500 / .8333 | unchanged |
| original 12 | MRR | .4591 | .4591 |
| original 12 | p50 / p95 | 1901.6 / 4162.7 ms | 1673.1 / 3890.6 ms |
| held-out 12 | Hit@1 / Hit@3 / Hit@5 / Hit@10 | .2500 / .5833 / .9167 / 1.0000 | unchanged |
| held-out 12 | MRR | .4882 | .4882 |
| held-out 12 | p50 / p95 | 1994.5 / 5107.6 ms | 1412.2 / 3591.1 ms |

Cross-process ranks and expanded-query hashes were identical with
`PYTHONHASHSEED=0` and `1`. Request-local reuse reduced superseded-set calls
from `18.04` to `1.83` per route, superseded work from `60.35` to `5.28`
ms/route, and hot edge-count work from `59.41` to `0.02` ms/route. BGE encoding
remained the largest cost and varied between sequential processes, so the full
end-to-end latency difference is an observation, not a universal speed claim.

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
