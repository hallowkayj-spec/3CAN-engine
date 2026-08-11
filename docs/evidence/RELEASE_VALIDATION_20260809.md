# 3CAN v0.2 Release Candidate Validation

Status: `VERIFIED_CANDIDATE`

This receipt covers candidate engine commit
`8cef3c4ba36c833341cd423b0626aff4f4d75e32` and benchmark receipt commit
`19e0752dde2d2c21eacd30d6d6710b00b1eb6b18`. It does not claim that a private
production installation has deployed this candidate; see
[`CURRENT_PRODUCTION.md`](CURRENT_PRODUCTION.md).

## Gates

| Gate | Command or evidence | Result |
| --- | --- | --- |
| Full release tests | `python -m pytest neural-memory/tests -q` | 427 passed |
| Owner Intent and applicable-reality tests | `test_owner_intent.py` plus focused routing/release suites | passed |
| Public Python lint | Ruff `0.15.7`: `python -m ruff check neural-memory examples scripts` | clean |
| Release isolation | `python scripts/prerelease_scan.py --strict .` | clean |
| Syntax compilation | in-memory `compile()` over tracked Python | 82 files |
| JSON / YAML / OpenAPI parse | tracked JSON plus release CI/protocol YAML | clean |
| Seed-graph benchmark | `SEED_GRAPH_BENCHMARK_20260809.json` | verified candidate |
| Immutable candidate | release `03cf214a...`, 24-file manifest `ab5fa950...` | builder, bootstrap, handoff, finalizer, negative dependency, and post-UAT verification passed |
| Isolated real-graph UAT | copied 5432-node / 6920-edge graph on `9701` | BGE-M3 5432x1024; Owner Intent `server_local_file`; compact default produced skeleton; partial identity returned 422; listener removed |
| Private real-graph regression | frozen copied graph, original 12 + 12 held-out, four warm repeats | deterministic; correctness unchanged |
| Clean-clone public RC | fresh clone at `24c6161`, fresh `requirements-min.txt` venv, new seed graph/project on `9701` | start, readiness, route, exact read, writeback, ErrorKnowledge, project isolation, project kit, and clean stop passed |
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
- One root `3CAN.md` supplies seven bounded project steering defaults. The
  server-local file overrides an unverifiable client assertion; neither source
  is authentication, objective truth, or permission to bypass a hard gate.
- Applicable project reality is a transient four-field route projection over
  existing selected nodes and route metadata. It adds no graph store, registry,
  watcher, daemon, or second lifecycle.
- Protected durable-current machine writes remain fail-closed: existing
  ErrorKnowledge evidence verifies a resolution but is not claim-bound to an
  arbitrary target node/field/value. Arbitrary valid or invalid pointers do not
  qualify.

## Real-graph route regression

The comparative 24-case profile was rerun from a clean detached clone at
`8cef3c4` against a copied private graph. Four warm repeats used the pinned
BGE-M3 revision, offline mode, reranker off, and `PYTHONHASHSEED=0`. Query text
and graph content remain private; only aggregate results are reported here.

The private queries and graph are excluded from the public package. Aggregate
receipts bind the same copied graph, BGE-M3 revision, engine file hashes, and
reranker-off profile.

| Suite | Hit@1 / Hit@3 / Hit@5 / Hit@10 | MRR | p50 / p95 |
| --- | ---: | ---: | ---: |
| original 12 | .3333 / .5000 / .7500 / .8333 | .4591 | 1291.9 / 3108.7 ms |
| held-out 12 | .2500 / .5833 / .9167 / 1.0000 | .4882 | 1152.9 / 2810.2 ms |

Ranks, metrics, and expanded-query hashes matched the frozen pre-change
baselines. Per route, current-policy, traversal, and core-memory stages each ran
once. Superseded-set work averaged `1.833` calls and current-applicability checks
averaged `10` calls. Wall-clock figures are machine observations, not a
cross-machine latency guarantee or a claim that Owner Intent improves model
quality.

The first 9701 attempt failed only because its test harness carried the path
identity of an earlier content-addressed candidate. It stopped cleanly, left
production unchanged, and caused no code change. The corrected immutable-path
expectation then passed the full UAT. Both receipts remain in the private task
audit chain.

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
