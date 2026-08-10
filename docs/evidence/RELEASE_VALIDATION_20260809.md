# 3CAN v0.2 Release Candidate Validation

Status: `VERIFIED_CANDIDATE`

This receipt covers the source tree in this release package. It does not claim
that a private production installation has deployed this candidate.

## Gates

| Gate | Command or evidence | Result |
| --- | --- | --- |
| Full release tests | `python -m pytest -q` | 316 passed |
| Public Python lint | `python -m ruff check neural-memory examples scripts` | clean |
| Release isolation | `python scripts/prerelease_scan.py --strict .` | clean |
| Syntax compilation | in-memory `compile()` over shipped and repository 3CAN Python | 157 files |
| Repository governance | six repository `tests/test_3can_*.py` modules | 62 passed |
| Seed-graph benchmark | `SEED_GRAPH_BENCHMARK_20260809.json` | verified candidate |

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

## License Boundary

The included license is PolyForm Noncommercial 1.0.0. The package is source-available
and explicitly not OSI-approved open source. A change to an OSI-approved
license is a maintainer product/legal decision and is outside this technical
validation.
