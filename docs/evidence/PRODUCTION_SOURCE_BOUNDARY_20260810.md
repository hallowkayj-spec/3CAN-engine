# Production source boundary — 2026-08-10

Status: `observed`, based on the private production manifest and the public
candidate head. This document contains classifications and equivalence results,
not private graph data or deployment paths.

At public commit `cb532da`, the 15 category-A semantic-core files matched the
generation-15 immutable production release after LF/CRLF normalization (13 were
byte-exact and two frontend files differed only by line endings). The current
candidate now contains this round's not-yet-deployed lifecycle, authority and
recovery-probe convergence, so its source is intentionally ahead again.

Public positioning remains:

> Source-available development/reference profile. Baseline core parity was
> observed at `cb532da`; the current candidate is not production parity until
> it is tested and represented by a new immutable production manifest.

The earlier `15 drift + 7 missing + 1 exact` raw-byte summary overstated core
drift because it mixed line endings, engine semantics, and operator tooling.
LF/CRLF-normalized review found all 15 category-A core files present. Generation
15 closed the earlier `backend/app.py` heartbeat-TTL drift. This round modifies
`backend/app.py`, `backend/graph_engine.py`, and `backend/models.py`; those three
are candidate-only until a later bound cutover receipt says otherwise.

## File classification

| Production manifest file | Class | Public result | Release decision |
|---|---:|---|---|
| `backend/app.py` | A | candidate ahead | validate lifecycle/authority/feedback convergence before parity claim |
| `backend/error_knowledge.py` | A | normalized equivalent | no copy |
| `backend/graph_engine.py` | A | candidate ahead | validate lifecycle/authority convergence before parity claim |
| `backend/graph_runtime_lock.py` | A | normalized equivalent | no copy |
| `backend/models.py` | A | candidate ahead | validate archived/authority contract before parity claim |
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
`production-parity core` statement requires a source-commit receipt, the
post-deployment immutable manifest and candidate acceptance evidence; this
document does not grant it.
