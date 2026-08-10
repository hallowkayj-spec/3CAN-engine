# Production source boundary — 2026-08-10

Status: `observed`, based on the private production manifest and the public
candidate head. This document contains classifications and equivalence results,
not private graph data or deployment paths.

Public positioning remains:

> Source-available development/reference profile. Production parity is not
> claimed until this candidate is deployed, tested, and represented by a new
> immutable production manifest.

The earlier `15 drift + 7 missing + 1 exact` raw-byte summary overstated core
drift because it mixed line endings, engine semantics, and operator tooling.
LF/CRLF-normalized review found all 15 category-A core files present. Fourteen
were source-equivalent; public `backend/app.py` was ahead of the production
snapshot with the heartbeat-TTL API projection.

## File classification

| Production manifest file | Class | Public result | Release decision |
|---|---:|---|---|
| `backend/app.py` | A | present; public ahead | keep public candidate; validate before any parity claim |
| `backend/error_knowledge.py` | A | normalized equivalent | no copy |
| `backend/graph_engine.py` | A | normalized equivalent | no copy |
| `backend/graph_runtime_lock.py` | A | normalized equivalent | no copy |
| `backend/models.py` | A | normalized equivalent | no copy |
| `backend/query_expander.py` | A | normalized equivalent | no copy; documentation corrected |
| `backend/readiness.py` | A | normalized equivalent | no copy |
| `backend/ticket_ledger.py` | A | normalized equivalent | no copy |
| `backend/token_usage.py` | A | normalized equivalent | no copy |
| `expansions/base.py` | A | normalized equivalent | no copy |
| `expansions/cn_cilin.py` | A | normalized equivalent | no copy |
| `expansions/domain_aliases.py` | A | normalized equivalent | no copy |
| `expansions/jieba_synonyms.py` | A | normalized equivalent | no copy |
| `frontend/index.html` | A | normalized equivalent | no copy |
| `frontend/token_usage.html` | A | normalized equivalent | no copy |
| `proxy/server.py` | B | normalized equivalent | public operator layer; not semantic-core parity |
| `maintenance/build_full_recovery_candidate.py` | B | intentionally absent | private/operator recovery workflow |
| `maintenance/build_runtime_release.py` | B | intentionally absent | immutable production release builder |
| `maintenance/cutover_full_recovery.py` | B | intentionally absent | graph cutover transaction tooling |
| `maintenance/direct_runtime_bootstrap.py` | B | intentionally absent | host authority launcher |
| `maintenance/direct_runtime_handoff.py` | B | intentionally absent | production switch/rollback controller |
| `maintenance/finalize_full_recovery_candidate.py` | B | intentionally absent | offline embedding finalization |
| `maintenance/reconcile_token_usage_sidecar.py` | B | intentionally absent | legacy operator migration |

Class definitions:

- A: public-safe engine core required for the claimed behavior.
- B: maintainer/operator workflow not required for public engine semantics.
- C: environment/private deployment material. No manifest payload file required
  class C in this snapshot.

No production file was copied merely to reduce a drift count. Raw-byte hashes,
normalized-source equivalence, and category are separate facts. A future
`production-parity core` statement requires the post-deployment immutable
manifest and candidate acceptance evidence; this document does not grant it.
