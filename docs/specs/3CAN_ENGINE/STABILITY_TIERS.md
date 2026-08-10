# 3CAN Stability Tiers (updated for v0.2 candidate, 2026-07-29)

> Every module / endpoint / file is labeled with one of 3 tiers. External users and contributors can expect: Stable = safe to rely on; Experimental = may change, breakage unlikely; Research = may be removed or totally redesigned.

## Stable (Core — API stable, changes require MAJOR version bump)

**Engine core** (`neural-memory/backend/`):
- `GraphEngine.route()` — 4-signal RRF + cross-encoder pipeline (ARCHITECTURE.md §4)
- `GraphEngine.create_node / update_node / delete_node` CRUD
- `GraphEngine.list_edges / create_edge` CRUD
- `GraphEngine.log_activity` — hash-chained append
- Node / Edge / RoutingRequest schemas (`models.py`)

**HTTP endpoints**:
- `GET /api/stats` — counts and last_updated
- `POST /api/route` — main retrieval
- `GET /api/route/simple` — curl-friendly GET variant
- `GET /api/retrieve/{node_id}` — CCR stage 2 (full node fetch)
- `GET /api/nodes/{id}` / `POST /api/nodes` / `PUT /api/nodes/{id}` — node CRUD
- `GET /api/edges` / `POST /api/edges` / `DELETE /api/edges` — edge CRUD
- `POST /api/agents/checkin` / `GET /api/agents` — agent registry
- `GET /api/briefing` — session cold-start briefing
- `POST /api/writeback` — batch field updates
- `GET /api/audit/verify` — hash chain integrity check
- `GET /api/activity` — recent activity

**Frontend**:
- `neural-memory/frontend/index.html` — 3d-force-graph visualizer

## Experimental (S66g additions — working but limited real-world usage)

**Route Ticket Gate and Error Knowledge lifecycle (v0.2 candidate)**:
- `POST /api/route/ticket` — issue ticket
- `GET /api/route/ticket/{id}` — validate
- `POST /api/route/ticket/{id}/consume` — mark used
- `POST /api/errors/occurrences` — record one idempotent occurrence
- `GET /api/errors/cases` — retrieve the authoritative case state
- `POST /api/activity/log` — PostToolUse writeback entry
- **Current design**: process-safe SQLite/WAL storage, typed scope/digests,
  900-second lease, append-only indexed events, and a replay-safe completion
  journal. ErrorCase graph nodes are projections of the occurrence ledger.
- **Risk**: still Experimental until migration and multi-process soak tests run
  on a public fixture; a graph projection can be `PARTIAL` and must be retried.

**Behavioral Gate hooks**:
- `~/.claude/scripts/hooks/3can-behavioral-gate.js` (Stage 1 ticket + Stage 2 content LLM)
- `~/.claude/scripts/hooks/3can-post-tool-capture.js` (PostToolUse writeback)
- **Risk**: 1 real block recorded (S66g first trigger) + 19 sentinel bootstrap bypasses. Not yet harness-bench-field-verified beyond 8-case pilot.

**Cross-encoder reranker**:
- `bge-reranker-v2-m3` as primary, FlashRank `ms-marco-MiniLM-L-12-v2` as fallback
- **Risk**: FlagEmbedding 1.3.5 + Python 3.14 + transformers 5.x compatibility drift; fallback path rarely tested

**Single-writer slot proxy**:
- `neural-memory/proxy/server.py` (9700 → one of 9701/9702)
- **Boundary**: the shared graph runtime lock permits one writable backend;
  green/blue are rotation slots, not concurrent standbys
- **Risk**: no immutable per-slot release roots or automatic code rollback;
  upgrades accept a short 503 window and stale state is repaired only from
  conclusive OS PID plus listener evidence

**Tools (maintenance scripts, user-invoked)**:
- `tools/llm_guided_health.py` — 8-node pilot only
- `tools/short_code_curator.py` — LLM-based
- `tools/kw_precision_audit.py` — hotspot analysis
- `tools/edge_inferrer.py` — orphan node edge suggestions
- `tools/project_bootstrapper.py` — seed node generator
- `tools/archive_manager.py` — physical archival of dormant>60d
- `tools/session_aggregator.py` — activity_log → SES-* aggregation
- `tools/leiden_community.py` — Leiden community detection (re-run periodically)
- `tools/node_gdi_scorer.py` — 5-dim node scoring
- `tools/skill_sync.py` — SKILL.md ↔ SKILL-* node sync
- `tools/bootstrap_check.py` — 39-item cold-start diagnostics
- `tools/uat_recorder.py` (v0.1 S66g) — real-task UAT recording
- **Risk**: most tools have minimal test coverage; user should read code before running on their graph

**Benchmarks**:
- `benchmark/longmemeval_runner.py` — 3-flag variant runner (v9.5)
- `benchmark/substrate_bench_runner.py` (S66g pilot) + `substrate_bench_v1.json` (10 cases)
- `benchmark/harness_bench_runner.py` (S66g pilot) + `harness_bench_v1.json` (8 cases)
- **Risk**: pilot suites only; not yet in CI; node IDs in substrate_bench are snapshot-dependent

## Research / Draft (May be deleted or redesigned — do not depend on)

**Experimental directory candidates (to be moved in P2)**:
- Cumulative-ingest mode of LongMemEval runner (`--no-reset-per-question` + `--no-str-fix` flags) — only for ablation reproducibility, not for practical benchmark use
- Sentinel bootstrap bypass (`~/.claude/logs/3can-gate-bootstrap`) — temporary, documented in DEPLOYMENT.md §1.7
- Any LLM-based auto-node-creation path (currently gated through PROPOSED- status, manually approved)

**Future removal candidates**:
- 2024 draft reference numbers in any docstring ("GPT-4o ~0.74, Mem0 ~0.57, Letta ~0.53") — replaced by 2026 SOTA references
- `3CAN_BENCHMARK_REPORT_2026Q2.md` tombstone (may be collapsed into _archive/ only, without tombstone, once external links are updated)

**Under active design (no code yet, see ROADMAP)**:
- bi-temporal validity (Zep/Graphiti parity) — LIMITATIONS §1.1
- online IDF re-computation — LIMITATIONS §1.5
- hierarchical Leiden — LIMITATIONS §1.4
- full append-only activity log (break 500-entry window) — LIMITATIONS §3.4
- provider abstraction layer (`tools/llm_provider.py`) — LLM_POLICY §8
- pgvector/HNSW at 10K+ node scale — LIMITATIONS §1.3
- substrate-bench v2 + harness-bench v2 (20+ cases each, CI-integrated) — P1 extension

## Semantic versioning commitment

| Change type | Version bump | Notice |
|---|---|---|
| Breaking Stable API (remove endpoint, change schema incompatibly) | MAJOR | ≥ 30 days notice + migration doc |
| Breaking Experimental module (change behavior, remove flag) | MINOR | ≥ 14 days notice |
| Research module change or removal | PATCH | Changelog entry only |
| Bug fixes in any tier | PATCH | Release note |

Current public baseline: **v0.1.0**. The Error Knowledge work is an
**unreleased v0.2 candidate**; internal v9.x labels remain historical only.

## Usage implications

- **If you're writing a CI integration**: use only Stable endpoints + schemas.
- **If you're dogfooding**: Experimental modules are fine, expect minor behavior changes across versions.
- **If you're reading academic-style comparisons**: ignore Research tier, they are work-in-progress and subject to total rewrite.

## Why 3 tiers (not 5)

Keeping this simple to avoid over-engineering. If a module needs finer gradation (e.g., "beta-1 → beta-2 → rc-1"), promote to its own sub-section in EVIDENCE.md. For now, Stable / Experimental / Research is enough.
