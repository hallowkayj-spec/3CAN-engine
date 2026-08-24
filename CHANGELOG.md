# Changelog

All notable changes to 3CAN-engine are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [Unreleased] — v0.2 error knowledge release candidate — 2026-07-29

The implementation and migration tooling below are staged and locally tested.
They are not yet tagged or published as a release, and this changelog does not
claim that any private production graph has been migrated.

### 2026-08-23 Codex convergence hook

- Added the optional `3CAN RuntimeHook` Skill and one local control adapter.
  Natural-language or implicit activation records one ignored semantic state:
  Owner Intent, the Agent-selected light/medium/max review timing and reason,
  optional episode, and semantic review result/reference. It does not create a
  Task Hook, selector, proof lifecycle, candidate-freshness path, or Stop
  decision; those remain independently owned by the existing convergence engine
  and Git. Final semantic `PASS` now records only the reviewed clean Git HEAD;
  a later commit or dirty edit makes that review stale without adding artifact
  hashing or another candidate kernel. Owner off retains the state as
  `disabled_by_owner` and cannot bypass another gate.
- Kept `/3CAN` as an unimplemented target shorthand because Codex exposes a
  fixed native slash-command set. The supported explicit entry is
  `$3can-runtimehook` (or `/skills`), with implicit Chinese/English aliases; no
  slash parser, daemon, network call, graph dependency, or global enablement
  was added.
- Added one standard-library, project-local convergence helper that records a
  small goal/acceptance contract and current Git/workspace-bound evidence
  receipt. Native `SessionStart(compact)` restores the accepted boundary after
  context compaction; `Stop` requests at most one bounded continuation when
  evidence is missing or stale.
- Added optional project-declared `PreToolUse` guards for high-cost operations.
  No domain action is hard-coded, command checks run without a shell, and
  captured command output is represented only by byte count and SHA-256.
- Removed convergence from the default per-tool Hook path and narrowed the PR
  adapter matcher. The existing strict Task Oracle remains unchanged and
  independently runnable.
- Added a design-only adaptive review proposal that keeps Git-frozen candidates,
  current evidence, honest typed outcomes, and criterion-level strict proof
  while moving any future Lite experiment outside the existing strict path.
  Global enablement remains blocked pending adversarial equivalence and real
  task cost evidence.
- Added clean-clone execution of the exact installed `Stop` command for both a
  stale-candidate block and a current `CONVERGED` allow on Linux and Windows.
- Tightened the Lite design so stable Acceptance IDs map to criterion evidence
  and the Final Gate re-resolves current non-Git artifacts against their frozen
  manifest identity.
- Kept ordinary development free of per-tool 3CAN calls. The hook makes no
  network call, does not parse Codex transcripts, and only marks 3CAN writeback
  eligible at `AUTO_CLOSEOUT` after every acceptance condition has passing
  bound evidence; `CANDIDATE_READY` and Owner-requested writeback remain
  separate.
- Removed the broad research prompt/edit/stop gate from the default hook set.
  Research remains an explicit skill for current, uncertain, or failure-driven
  investigation instead of a universal development prerequisite.
- Kept candidate readiness distinct from Owner acceptance, merge, deployment,
  and publication. Invalid hook state fails open as typed `UNAVAILABLE`, and a
  second Stop pass requires an honest `PARTIAL` report rather than looping.
- Bound every acceptance condition to named final evidence, made incomplete and
  candidate Stop outcomes request an explicit report, applied asymmetric
  fail-open behavior at Stop, and rejected dirty submodules under the narrow
  `current_repository_only` scope.
- Added an installed-project smoke that executes the exact Windows or POSIX
  `SessionStart` command from `hooks.json`, rather than proving only that the
  referenced script file exists.
- Added a versioned, content-addressed Task Hook layer without adding another
  native lifecycle hook or runtime. Run bindings, current candidate identity,
  evaluator version/kind, and external proof receipts now form one freshness
  chain; old candidate or revision evidence cannot unlock convergence.
- Classified hardcoding through undeclared mutable decisions and hidden
  fallbacks rather than constant grep or domain branches. Repeatable Task Hooks
  remain parameterized across worktrees and require reproduced receipt evidence
  plus explicit review before promotion.
- Added explicit proposed/superseded/retired lifecycle states and retained
  closeout receipts. SessionStart restores an active selector at startup,
  resume, clear, and compaction; local receipt files remain unsigned coordination
  evidence rather than authentication.
- Rejected large generic artifacts as unverifiable instead of trusting
  size/mtime. Large outputs use a content-addressed manifest or project-owned
  command provider, while external reviewer receipt changes participate in
  current-evidence and verification-race checks.
- Closed lifecycle laundering gaps: same-revision semantic re-pinning is
  rejected, closeout recomputes the current candidate and proof sources, and
  promotion references must resolve to retained self-validating receipts.
- Added one deterministic reusable-family registry selector for later
  worktrees. Repeatable command adapters receive only declared bindings through
  a stable environment contract; embedded run values and undeclared fallbacks
  remain typed failures. No prompt classifier, second runtime, or LLM hot path
  was added.
- Hardened the reusable path after adversarial review: native `SessionStart`
  can materialize an exact registry selection from explicit launcher intent;
  repeatable same-revision meaning is checked against bounded tracked Git
  lineage even when a prior local receipt is absent; final CLI verification
  exits nonzero until the requested stage genuinely converges.
- Replaced current-value substring guessing with a structural command-adapter
  boundary, bound singular external-receipt criterion IDs, added aggregate
  native read budgets, recaptured the complete control plane before allowing a
  converged Stop, and enabled valid closeout of later `REUSABLE_ACTIVE` runs.
- Bound repeatable activation to the current Git `HEAD` meaning and exact
  registry value, distinguished reviewed invariant launcher flags from mutable
  run arguments, capped proof and generated control files, and added a terminal
  full Stop recapture plus exact native environment-selection coverage for
  startup, resume, clear, and compaction.
- Bracketed every verify/status/record/Stop/closeout Candidate Provider call
  with post-provider data-plane fingerprints and a closing control-plane read,
  made oversized generated receipts atomically replace prior success with typed
  `UNAVAILABLE`, and kept 128-family native selection bounded by validating only
  the explicitly selected family. Complete registry
  validation remains an explicit offline command.

### 2026-08-20 portable public-release packaging

- Reworked the repository landing page into a Chinese-first OPC guide while
  retaining `README.en.md` for international users. The docs clarify that 3CAN
  is a local service, not a mandatory dedicated management chat Session.
- Added one cross-platform, standard-library release builder. It archives one
  exact clean Git commit, extracts the result, runs the strict privacy scan,
  and emits a ZIP, SHA-256 checksum, and source-bound JSON receipt.
- Expanded the minimum release manifest to cover the visible UI image, user
  guide, Windows/POSIX initializers, full dependency profile, uninstall guide,
  and release-builder tests. CI now verifies the real extracted archive.
- Kept runtime graphs, embeddings, ledgers, activity, local paths, accounts,
  credentials, machine receipts, and personal preferences outside the public
  package. No local graph migration or deletion is part of this change.

### 2026-08-11 pre-dogfood convergence

- Renamed the unreleased durable-current source contract from misleading
  `Authority` terminology to one canonical `Provenance` contract. User direction
  remains an explicit audit assertion. Existing ErrorKnowledge receipts verify
  resolutions but do not bind an arbitrary target node/field/value, so machine
  claims remain fail-closed for protected current writes instead of borrowing
  an unrelated valid receipt.
- Ordered project-scoped current retrieval as exact project, explicit
  shared/global, unscoped legacy fallback, then mismatch exclusion. Missing
  project metadata is never guessed to be global.
- Reused request-local supersession, core-scope, and hot-edge derivations inside
  one route; no persistent cache, service, dependency, or new state owner was
  added.
- Tightened serious-milestone fact matching against generic status words,
  partial digests, and token-substring false positives.
- Enabled GitHub pull-request CI for stacked branches and documented
  module-level development governance without commit-count ceremony.

### 2026-08-09 public-repository preparation

- Replaced the prior license with the canonical PolyForm Noncommercial 1.0.0
  license so every commercial use requires separate written permission.
- Consolidated licensing guidance around one root `LICENSE`, removed the
  maintainer-local upload guide, anonymized private-repository test fixtures,
  and expanded the strict release scan for private identity leakage.

### 2026-08-09 release-governance and route-contract update

- Made canonical readiness the sole source for `/api/stats.healthy`. Shallow
  checks reuse prior deep evidence only while the graph/embedding fingerprint
  is unchanged and expose typed verification state and evidence age.
- Removed backend/proxy spawn, termination, wrong-graph cleanup, and duplicate
  recovery branches from the Codex project wrapper. Ordinary clients now only
  observe readiness: offline 3CAN operations are typed `UNAVAILABLE`, local
  Git/coding/build/test work continues, and lifecycle remains an explicit
  machine operator/service-manager action.
- Versioned route responses as `3can.route-response/v1`; budget compaction now
  retains mandatory-selection, injection, and temporal-policy metadata, while
  token estimates expose stable `response_tokens` and
  `post_budget_tokens` aliases.
- Bound route and substrate benchmark fixtures to the generic seed graph.
  Runners refuse to score an incompatible graph and report
  `INVALID_GRAPH_BINDING`.
- Added typed deep readiness to the clean-clone verifier and made development
  mode explicit for fresh project initialization.
- Promoted release CI from critical-error-only Ruff rules to the complete
  configured Ruff rule set.

### Added

- Added [`docs/ERROR_KNOWLEDGE_LIFECYCLE.md`](./docs/ERROR_KNOWLEDGE_LIFECYCLE.md),
  the implementation contract that separates raw occurrences, canonical error
  cases, verified resolutions, evidence, and versioned policy.
- Added a deterministic, path-sanitizing `ek2:sha256` fingerprint core. A first
  occurrence remains an observation; a second matching occurrence promotes a
  reusable case.
- Added evidence-backed `done` resolution. Resolution creates solution and
  evidence nodes with `resolves` and `verified_by` edges; verified regressions
  reopen knowledge instead of silently overwriting history.
- Added a process-safe SQLite/WAL route-ticket ledger with indexed append-only
  events for issue, consume, expiry, and completion; it includes stable lease
  reuse, typed project/workspace/workorder scope, completion request
  compare-and-swap, and recovery from an interrupted completion journal.
- Added an authoritative SQLite occurrence/case ledger. Graph ErrorCase nodes
  are rebuildable projections, so a projection failure is reported as
  `PARTIAL` without losing the occurrence.
- Added a dry-run-first legacy error migration with complete backup, JSONL
  archive, drift detection, deterministic manifest, explicit apply guard,
  embedding rebuild marker, idempotency, checksum validation, and rollback.
  Low-value records with a missing or one-off occurrence count are archived
  unless recurrence, explicit promotion, a solution, or a resolution edge
  makes them reusable; diagnosis text alone does not retain a node. Public
  manifest lists are bounded and publish explicit total/truncation metadata,
  while the rollback backup and JSONL archive remain complete.
- Added focused lifecycle, ticket, migration, route-budget, and benchmark
  coverage.
- Added fully synthetic public route/substrate fixtures that target only the
  generic seed graph. Private-graph-derived prompts and node identifiers are
  not part of the release fixtures.
- Added a project-kit ignore template for local ticket, route, outbox, and
  receipt state.

### Changed

- Ordinary routes no longer carry legacy or canonical `ERR-*` error knowledge.
  Explicit error queries return at most three relevant records, prioritize
  verified solutions, and keep the overall route payload within the requested
  node budget.
- The Codex harness sends `done` as a real completion action, records structured
  operation/component/error fields, and promotes only repeated exact cases.
- Route-ticket TTL is aligned to 900 seconds and active identical intents reuse
  one lease instead of issuing unbounded tickets.
- Codex completion binds the exact `ticket_id` returned by its own `prepare`;
  concurrent sessions no longer rely on shared wrapper state to select a ticket.
- The edge contract now includes `grouped_in`, `resolves`, `verified_by`,
  `applies_to`, `supersedes`, and `regressed_from`.

### Fixed

- Removed the global registry-to-error `requires` pattern that allowed error
  history to dominate unrelated routes and cause accidental task pauses.
- Prevented must-consume injection from exceeding `max_nodes`.
- Replaced volatile path/UUID/ticket/time fragments in error fingerprints and
  removed private machine paths from staged defaults.
- Deferred token-usage database creation until first use so importing the
  application does not mutate a release tree.
- Kept Cilin synonym expansion functional in the minimal install when the
  optional `jieba` tokenizer is absent.
- Kept clean-clone seed bootstrap compatible with reserved Error Knowledge
  ownership; only ERR/FIX/EVD seed writes use the trusted migration owner.
- Disabled pickle loading for every production NPZ embedding-cache consumer and
  added a prerelease allowlist that rejects unreviewed `numpy.load` call sites.
- Corrected the proxy lifecycle contract for the exclusive graph-runtime lock:
  one writable backend may exist at a time. Ordinary inactive-slot deploy and
  automatic failover now fail fast instead of starting a backend that must
  lose the graph lock. Orphan slot state is repaired only when persisted
  identity is structurally valid, the OS proves the PID is absent, and listener
  inspection proves the port is empty. An explicitly confirmed
  `/api/admin/recover-active` path can restart that same proven-stopped active
  slot without lying about a successful switch. This release keeps 9700 as the
  stable ingress but does not claim zero-downtime standby or immutable-code
  rollback.
- On Windows, an exited process object can remain open briefly after stable
  handle exit confirmation. The proxy now checks `GetExitCodeProcess` before
  executable and command-line inspection, so a non-`STILL_ACTIVE` object is
  conclusive exit evidence rather than a false inspection failure.
- Hardened proxy process ownership with OS-backed identity, fail-closed
  stable-handle retirement, serialized admin transactions, managed-target
  verification, cancellation cleanup, and atomic state rollback.
- Hardened runtime identity: `/api/stats` publishes path-safe engine and graph
  digests. The earlier candidate's wrapper-owned deploy/cleanup branch was
  superseded and removed in the 2026-08-09 update; process ownership now stays
  with the runtime Supervisor/service manager.
- Added a shared graph-runtime lock for engine and migration coordination while
  excluding local `neural-memory/.3can-locks/` lease state from release content.
- Made authoritative node, edge, agent, and activity JSON replacement atomic
  and durability-aware: temporary data is flushed before replacement and the
  containing directory is synchronized where the platform supports it.
- Documented fail-closed target/evidence roots and the signed
  `3can.verification-attestation/v1` HMAC contract; the example secret remains
  intentionally blank.
- Made runtime graph and bundled project-kit state deny-by-default in
  `.gitignore`, including migration backups, archives, journals, locks,
  SQLite sidecars, local sessions, pending writebacks, and receipts.
- Preserved a directly named unresolved ErrorCase through strict route budgets
  and solution attachment while continuing to attach only server-verified
  resolutions.
- Made legacy migration checkpoints skip completed node writes and removals on
  resume, with batched journal persistence and bounded retry for transient
  Windows replace failures.
- Made the large Chinese cross-encoder a process-wide single-flight load shared
  by background warmup and foreground routing; pytest disables heavyweight
  warmup by default and exercises concurrency with a fake model.
- Validated proxy switch statistics before mutating active-slot state, so a
  malformed successful response cannot leave an unpersisted in-memory switch.

### License and disclosure

- 3CAN-engine remains **source-available under PolyForm Noncommercial License 1.0.0**.
  It is not OSI-approved open source.
- No third-party code or fixtures are added by this release candidate.
- Historical internal observations are labeled non-reproducible when their
  frozen graph or raw receipt is absent. The public seed-fixture benchmark has
  a content-addressed candidate receipt; it remains synthetic and is not a
  production-profile claim. Private prompts, private node identifiers,
  credentials, user data, and raw error payloads are excluded.

## [v0.1.0] — 2026-04-xx — First public (source-available) release

3CAN-engine v0.1.0 is the **first publicly tagged version**. This release is an **active prototype / experimental developer preview**, not release-ready in the strict sense. Built by the original maintainer with substantial assistance from Claude Code and other AI coding agents.

### 2026-04-28 Codex compatibility update

- Protocol spec bumped to `9.5.0` to document the runtime harness contract that already exists in backend code: session bootstrap, route ticket issue/validate/consume, activity log, and compact continuation.
- Codex CLI project harness now has a one-shot `scripts\codex-3can.cmd bootstrap` entrypoint that checks or starts 3CAN, checks in the agent, fetches briefing, and routes the current task.
- Codex docs now explicitly distinguish Claude native hooks from Codex pseudo-hooks. Claude can hard-deny unsafe tool use; Codex currently uses explicit wrapper discipline through `bootstrap`, `prepare`, `done`, and `compact`.
- Release staging docs were updated so the public package explains non-Claude agent attachment instead of implying Claude-only hooks.
- Added bilingual `CHINESE_ROUTE_SEMANTICS.md` to clarify the Chinese route stack: online BGE-M3 dense recall, bge-reranker-v2-m3, graph co-occurrence expansion, `/api/route/simple`, plus the prepared but not yet fully wired `query_expander.py` / `jieba_synonyms.py` plugin path.
- 中文补充：新增 `CHINESE_ROUTE_SEMANTICS.md`，明确区分“已在线中文 route 栈”和“预留中文语义扩展点”，避免把 query_expander TODO 误写成已全量接入。

### Added

**Core engine**
- Graph-backed project memory: nodes + edges + activation decay, BGE-M3 multilingual embedding (1024-d), bge-reranker-v2-m3 cross-encoder, 4-signal RRF fusion (Cormack 2009), Leiden community detection
- HTTP API on `localhost:9700` via blue-green proxy (green 9701 / blue 9702)
- 35+ REST endpoints (nodes / edges / route / retrieve / writeback / agents / activity / audit / skills / handoff / lifecycle / admin)
- Pack modes: skeleton (~20 tok/node) / slim (~50) / full (~500-800), `budget_tokens` enforcement
- Hash-chain activity log with SHA-256, `/api/audit/verify` integrity check
- Lifecycle: 30-day dormant / 60-day archive + revive on route-hit

**Project substrate layer**
- 9 node types, 20+ ID-prefix semantic classifications (DEC / DOC / FEE / ERR / SES / HO / INTF / MOD / SEC / MCP / MEM / RES / ARCH / SKILL / PROPOSED / STR / AGT / TASK / PRO)
- Multi-agent registry, session briefing, cross-session handoff pending
- INTF contract nodes (first-class API-schema objects)

**Governance layer**
- PreToolUse Route Ticket Gate with 600s TTL and scope check
- PostToolUse writeback hook (failure logged, not silent-dropped)
- Behavioral Gate Stage 2 content-judge (4-question LLM check)
- Sentinel bootstrap bypass documented in `DEPLOYMENT.md §1.7`
- **§0 Engine Liveness Hard Gate** (2026-04-20, superseded 2026-08-12): the original hook denied ordinary mutation and allowed direct startup/cleanup commands when 3CAN was offline. The current runtime-ownership boundary removes that recovery whitelist: ordinary Worktrees never control production 9700, local Git/coding/build/offline tests continue, and only 3CAN-dependent operations report `UNAVAILABLE`.
- **Engine liveness harness** (`neural-memory/benchmark/engine_liveness.py`): 3-5 second probe producing human or `--json` report, `exit 0/1`, intended for CI health-check, UAT pre-gate, or operator sanity-check. Thresholds: `total_nodes ≥ 1000`, `total_edges ≥ 500`; optional `--strict-route` validates `/api/route` end-to-end.

**LLM integration layer**
- 7 multi-role integration points, BYOK, provider-neutral design
- Unified `tools/llm_provider.py` abstraction is planned for v0.1.x (not yet shipped)

**Benchmark layer**
- Internal 46-query benchmark (standard IR formulas, self-built): MRR 0.9239, R@1 0.7826
- LongMemEval oracle balanced 60 (DeepSeek self-judge): 0.75 with four caveats
- substrate-bench v1 pilot (10 cases): top1 0.70, top3-recall 0.85
- harness-bench v1 pilot (8 cases): 8/8 passed

**Documentation**
- 22 documents under `docs/specs/3CAN_ENGINE/`
- LICENSE (PolyForm Noncommercial 1.0.0), NOTICE, LICENSING.md (plain-language FAQ)
- Integration recipes for Claude Code and Codex CLI

### Security

- Default bind: `127.0.0.1` (localhost-only)
- `--host 0.0.0.0` prints explicit security warning (API has no authentication layer)

### Known limitations

See `docs/specs/3CAN_ENGINE/LIMITATIONS.md` for the full list. Summary: no bi-temporal validity yet, static `kw_df`, single-node deployment, single-developer dogfood (2.5 months pre-release), short-code retrieval weak category.

### Planned for v0.1.x (next incremental releases)

- Unified LLM provider abstraction + provider-specific tokenizer integration
- `--estimate-cost` flag on all LLM tools
- substrate-bench v2 (20+ cases, portable) + harness-bench v2 (valid-ticket + production trigger-rate)
- Docker Compose setup, Windows PowerShell installer
- Ablation C (mode=full + cumulative ingest + str-fix)

### Planned for v0.2

- Bi-temporal validity
- Real UAT ≥ 20 scenarios closed
- Cross-IDE validation
- Append-only activity log beyond 500-entry window

---

## Pre-release lineage (pre-v0.1.0, not public)

> The entries below are **internal development milestones before the first public tag**.
> **They are not part of public source-available release history.** They are listed here only so contributors reading this file can understand the engineering journey that led to v0.1.0. No binaries, images, or public artifacts exist for v9.x; the internal designators do not map to any previously released version.

- **v9.0** (2026-02 internal start): baseline retrieval engine, first 100 nodes, initial hook scaffold.
- **v9.1** (mid-Feb to Mar internal): skill bidirectional sync (12 user skills ingested), NodeType schema v2.
- **v9.2** (late Mar internal): 4-signal RRF + Leiden community boost (modularity 0.9189), confidence gating, L2 summary LLM backfill, short-code resolver.
- **v9.3** (internal): SHA-256 hash chain audit, GDI 5-dimension node scoring, physical archival.
- **v9.4** (2026-04-18 internal): 20-point self-audit scorecard, route hardening, merge/deprecate governance rules.
- **v9.5** (2026-04-19 internal, public-release prep): runner three-bug debug session (int-answer crash / slim-mode char truncation / cumulative-ingest contamination); Route Ticket Gate first real production-block event; substrate-bench and harness-bench v1 pilots authored; EVIDENCE.md and BENCHMARK_POLICY.md finalized; LICENSE decision (PolyForm Noncommercial 1.0.0); release staging prepared.

The first publicly tagged version is **v0.1.0**. We chose to restart semantic versioning from 0.1.0 for the public release rather than use v9.x numbers, following the convention of starting pre-1.0 semantic versioning at a modest baseline for experimental prototypes.
