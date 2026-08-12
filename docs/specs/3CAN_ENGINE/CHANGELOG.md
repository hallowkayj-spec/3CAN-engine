# Changelog

> Historical v0.1 record. The v0.2 release candidate supersedes the
> concurrent blue/green claim: the shared graph runtime lock permits one
> writable backend, so green/blue are rotation slots and automatic failover is
> disabled. See `ARCHITECTURE.md` section 8.1 and the package-root
> `CHANGELOG.md`.

All notable changes to 3CAN Engine are documented in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [v0.1.0] — 2026-04-xx (first public release, active prototype)

First public, source-available release under PolyForm Noncommercial 1.0.0. Internal development ran from 2026-02 through 2026-04 under "v9.0" → "v9.5" internal numbering; v0.1.0 is the first publicly tagged version.

### Added (cumulative since private v9.0 start)

**Core engine**
- Graph-backed project memory: nodes + edges + activation decay, BGE-M3 embedding (1024-d, multilingual), bge-reranker-v2-m3 cross-encoder, 4-signal RRF fusion (Cormack 2009), Leiden community detection
- HTTP API on `localhost:9700` via blue-green proxy (green 9701 / blue 9702)
- 35+ REST endpoints (nodes / edges / route / retrieve / writeback / agents / activity / audit / skills / handoff / lifecycle / admin)
- Pack modes: `skeleton` (~20 tok/node) / `slim` (~50) / `full` (~500-800), `budget_tokens` enforcement
- Hash-chain activity log with SHA-256, `/api/audit/verify` integrity check
- Lifecycle: 30-day dormant / 60-day archive + revive on route-hit

**Project substrate layer**
- 9 node types (knowledge / feedback / session / decision / process / tool / reference / secret / skill)
- 20+ ID-prefix semantic classifications (DEC / DOC / FEE / ERR / SES / HO / INTF / MOD / SEC / MCP / MEM / RES / ARCH / SKILL / PROPOSED / STR / AGT / TASK / PRO)
- Multi-agent registry (`/api/agents/checkin`), session briefing (`/api/briefing`), cross-session handoff pending
- INTF contract nodes (484 in dogfood graph, first-class API-schema objects)

**Governance layer (S66g)**
- PreToolUse Route Ticket Gate: `route → read ERR / INTF / API_USAGE → issue ticket (600s TTL) → tool call`, deny on missing/expired/scope-mismatch
- PostToolUse writeback: mandatory `/api/activity/log` on Edit / Write / mutating Bash, failure logged to `~/.claude/logs/3can-writeback-fail.jsonl`
- Behavioral Gate Stage 2: LLM content-judge (4 questions: data-freshness / evasive-attribution / cheating-proposal / unchecked-ERR)
- Sentinel bootstrap bypass: `~/.claude/logs/3can-gate-bootstrap` sentinel file (documented in DEPLOYMENT.md §1.7), every bypass logged

**LLM integration layer**
- 7 multi-role integration points: embedding / reranker / content gate / keyword calibration / short-code calibration / edge inference / node-health check / summary enrichment / project bootstrapper / judge
- BYOK design (user-provided API key via `~/.claude/secrets.json`)
- Provider-neutral architecture (DeepSeek / OpenAI / Anthropic / local `llama.cpp` — final `tools/llm_provider.py` abstraction: planned v0.1.x)

**Benchmark layer**
- Internal 46-query benchmark (standard IR formulas, self-built): MRR 0.9239 / R@1 0.7826
- LongMemEval balanced 60 (oracle variant, DeepSeek self-judge): ablation 0.23 (baseline) → 0.75 (3 runner fixes)
- substrate-bench v1 pilot (10 cases, 5 dimensions): top1 0.70, top3-recall 0.85
- harness-bench v1 pilot (8 cases, 3 categories): 8/8 passed

**Documentation**
- 22 documents under `docs/specs/3CAN_ENGINE/`: PRD / ARCHITECTURE / FEATURES / API_USAGE / AGENT_BINDING / CONTRACTS / PROTOCOL.yaml / DEPLOYMENT / BENCHMARK_POLICY / BENCHMARK / LIMITATIONS / ATTRIBUTION / LLM_POLICY / TOKEN_OPTIMIZATION / NAMING / SELF_AUDIT_SCORECARD / STABILITY_TIERS / REAL_UAT_PLAN / EVIDENCE / recipes × 2 / OPEN_SOURCE_CHECKLIST / CHANGELOG
- LICENSE (PolyForm Noncommercial 1.0.0), NOTICE, LICENSING.md (source-available FAQ)

### Security

- Default bind: `127.0.0.1` (localhost-only). `--host 0.0.0.0` prints explicit security warning (the API has no authentication layer).
- `secrets.json` documented as `.gitignore` required.
- Hash chain audit window: 500 entries (window-limited; full append-only log is v0.1.x planned).

### Known limitations (from LIMITATIONS.md)

- No bi-temporal validity (valid_from / valid_until) — important future-work item
- `kw_df` static (computed at engine start) — online IDF recompute planned
- Single-node deployment only (no pgvector / HNSW migration yet, needed at 10K+ nodes)
- Single developer dogfood (2.5 months); no external-user deployment validated at v0.1 release
- Short-code retrieval remains a weak category (MRR 0.667 on 4 cases in 46-query internal benchmark)

### Not included in v0.1.0

Planned for v0.1.x (first incremental releases after initial open-source):
- `tools/llm_provider.py` unified multi-provider abstraction
- Provider-specific tokenizer integration (tiktoken / anthropic-tokenizer / local)
- `--estimate-cost` preview flag on all LLM tools
- substrate-bench v2 (20+ cases, prefix-based expected targets for portability)
- harness-bench v2 (valid-ticket scenarios + production trigger-rate measurement)
- Docker Compose multi-container setup
- Windows PowerShell installer (`install.ps1`)
- Ablation C compute (mode=full + cumulative ingest + str-fix)

Planned for v0.2:
- Bi-temporal validity
- Real UAT ≥ 20 scenarios closed
- Cross-IDE validation (Zed / Cursor / Continue.dev)
- Append-only activity log beyond 500-entry window

---

## Internal history (not a public release)

- **v9.0 → v9.4** (2026-02 to 2026-04-18): private development, the maintainer dogfood single-user on medium-sized coding project. Graph grew 0 → 1400 nodes. Multiple internal ruff / lint / benchmark cycles.
- **v9.5** (S66g, 2026-04-19): public-release prep. 3-bug runner debug (int answer crash / slim-mode char truncation / cumulative-ingest contamination). Gate first real production block event. substrate-bench v1 + harness-bench v1 + EVIDENCE.md + BENCHMARK_POLICY.md authored. LICENSE decision PolyForm Noncommercial 1.0.0.

v9.5 was the internal designation; the first publicly tagged version is v0.1.0 per semantic versioning convention for pre-1.0 experimental prototypes.
