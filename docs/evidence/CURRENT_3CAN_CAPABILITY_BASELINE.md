# Current 3CAN Capability Baseline

Status: `observed`

Observation date: 2026-08-11

Public baseline commit: `cb532daee7c17954b2cdf93b536f65683db4c9a2`

This worksheet reconstructs the pre-convergence baseline at
`cb532daee7c17954b2cdf93b536f65683db4c9a2`. It separates implementation,
reproducible evidence, production parity, and real OPC usefulness. Node and
edge counts are not semantic-quality evidence.

## Baseline truth

- Production had one direct immutable owner on `127.0.0.1:9700`; deep readiness
  reported `production_ready=true` and a verified runtime/graph identity.
- The selected immutable release and the public baseline contained the same 15
  semantic-core files after LF normalization. Private operator/maintenance
  scripts were outside the public behavioral core.
- The public 16-node hashing seed fixture was reproducible, but it did not prove
  real OPC usefulness. The offline OPC scorer existed without a committed real
  gold/observation receipt, so semantic quality correctly remained `validating`.
- Git, CI, Runtime, and providers remained their own sources of truth. 3CAN was
  the cognition/provenance layer and did not replace any of them.

## Capability inventory

| Capability | Canonical owner | Baseline evidence | Baseline status | Confirmed weakness | Duplicate if rebuilt? |
|---|---|---|---|---|---|
| INTF / PROC / DEC | semantic ID families, node store, route/writeback policy | route and writeback tests | `ALREADY_HAVE` | durable authority was not uniform | Yes |
| Evidence | ErrorKnowledge evidence and signed-attestation projection | ticket and verified-solution tests | `PARTIAL` | strongest only in ErrorKnowledge | No; harden existing metadata |
| ErrorOccurrence | ticket ledger plus graph projection | occurrence/replay/crash tests | `ALREADY_HAVE` | ledger must remain truth | Yes |
| ErrorCase / Resolution | ErrorKnowledge promotion, FIX/EVD lineage | promotion and verified-solution tests | `ALREADY_HAVE` | no second Error Book needed | Yes |
| Route | `GraphEngine.route` and route API | hybrid/RRF/rerank/current/error tests | `ALREADY_HAVE` | real OPC recovery and determinism not yet proven | A second retrieval engine would duplicate it |
| Skeleton / slim / full | existing response-budget owner | mode/budget tests | `ALREADY_HAVE` | no need for Quick/Standard/Deep aliases | Yes |
| BGE-M3 | model/cache owner and readiness profile | embedding-cache/readiness tests | `ALREADY_HAVE` | no evidence justified replacement | Yes |
| Current / history | current-reality policy and SES/HO demotion | current/history counterexamples | `PARTIAL` | archive state and generic supersession were inconsistent | Temporal DB not justified |
| Project identity | capsule plus normalized Git/project/namespace | project-kit/server tests | `ALREADY_HAVE` | shared paths were not uniformly strict | No new registry |
| Agent identity | registry and heartbeat projection | API/TTL tests | `PARTIAL` | audit assertion, not authentication | Do not add an auth platform here |
| Worktree binding | Git common-dir family, physical worktree, absolute target | linked-worktree/ticket tests | `ALREADY_HAVE` | legacy clients required normal rollout | No new lock registry |
| Ticket | SQLite/WAL ledger and exact consume contract | expiry/identity/target tests | `ALREADY_HAVE` | reuse this mutation primitive | A second lease store would duplicate it |
| Writeback | `GraphEngine.session_writeback` | atomicity/project tests | `PARTIAL` | inferred/untrusted facts could affect durable current fields | Harden this owner |
| Supersession | `supersedes` edge plus route exclusion | lineage tests | `PARTIAL` | generic mutation lacked complete family/project/authority checks | No temporal subsystem yet |
| Readiness | deep runtime/graph identity evaluator | readiness suite | `ALREADY_HAVE` | integrity intentionally does not prove usefulness | Must stay separate |
| Route/activity telemetry | route metadata and activity/token stores | telemetry tests | `PARTIAL` | exact route/session attribution was incomplete | No second ledger |
| Capability writeback recovery | no single acceptance owner | no clean-agent recovery receipt | `MISSING` | HTTP success did not prove fact recovery | Compose route/readback/evidence |
| Hot/history separation | route projection and diagnostics | hot/history tests | `ALREADY_HAVE` | semantic quality still `validating` | No archive store |
| Multi-project primitives | capsule/namespace/workspace/target checks | project-kit/ticket tests | `PARTIAL` | unscoped durable nodes could still compete | Federation not justified |
| Archive lifecycle | lifecycle sweep and status contract | no archived-state acceptance | `HARDEN_EXISTING` | docs claimed an archived state the enum did not contain | Fix existing state machine |

## Real gaps selected for investigation

1. Risk-matched durable authority/provenance on the existing write boundary.
2. Serious-milestone compile -> probe -> refine acceptance.
3. Truthful archive/current/supersession behavior without a temporal database.
4. Deterministic public contract/release evidence.
5. Real-graph route correctness and stability before claiming improvement.

The baseline did not authorize a Markdown wiki, WikiKV/vector/temporal database,
second memory engine, second Error Book, federation, new daemon, new approval
database, new MCP server, or BGE-M3 replacement.
