# Research Ledger Reference

Use this reference only when recording or auditing a mandatory 3CAN research run.

Required fields:

- `question`: exact research question.
- `source_urls`: opened source URLs used for material claims.
- `source_count`: number of unique valid sources.
- `session_id` and `turn_id`: Codex hook identifiers when available.
- `notes`: short synthesis, decision impact, or caveat.

Recommended fields:

- `research_tier`: `quick`, `standard`, `deep`, or `rpa_deep`.
- `time_budget`: decision check, target minutes, hard cap, and whether sidecar judgement was required.
- `query_plan`: seed terms, semantic variants, platform terms, and terms that were discarded.
- `source_types`: official primary, GitHub issue, community practice, benchmark/user report, public platform signal, RPA video/comment/ASR/OCR, or internal 3CAN context.
- `source_artifacts`: public URL collector artifacts with title, meta description, text excerpt, content hash, HTTP status, content type, and adapter name.
- `evidence_scores`: authority, recency, practice value, reproducibility, task relevance, community signal, risk, and conflict.
- `sidecar_judgement`: whether evidence is sufficient and whether it fits the task decision.
- `rpa_metadata`: public URL, platform, capture time, engagement signals, transcript/OCR/frame evidence, and approval id when automation/login/paid access was used.

Sidecar decisions:

- `ready_for_decision`: enough sources, source type coverage, evidence score, and sidecar judgement.
- `continue_research`: missing source count, missing practice/platform evidence, low score, or unresolved conflicts.
- `needs_review`: enough raw material exists but sidecar judgement is absent or ambiguous.

Evidence scores are `0..5`. Higher is better for authority, recency, practice value, reproducibility, task relevance, and community signal. Higher is worse for risk and conflict.

3CAN mapping:

- `DOC-*`: reusable research summary or operating guide.
- `DEC-*`: technology selection or architecture decision.
- `INTF-*`: API or contract facts that future code must respect.
- `ERR-*`: lesson learned from failed search, stale docs, hallucinated claim, or provider mismatch.
- `SES-*`: handoff or stage completion with evidence.

Do not store prompts, completions, secrets, cookies, recovery codes, private messages, or raw runtime logs in the ledger.

Do not treat official sources as automatically best for practice-heavy questions. For model quality, RPA operations, creator workflows, and benchmark realism, user reports and reproducible field evidence can outweigh vendor claims, while official docs remain boundary constraints.

The `public_url_extract` adapter only accepts `http` and `https`. It stores a bounded text excerpt and content hash, not raw HTML. Dynamic pages, logged-in pages, paid APIs, platform automation, or bulk scraping must move to an approval-gated collector.

When `done` receives `--source-artifact`, the ledger should derive `source_urls`, `source_type`, title, content hash, HTTP status, and artifact path from the artifact. This keeps collector output and final research ledgers connected.

The `search_result_import` adapter is provider-neutral and offline. It accepts JSON with `results`, `items`, `data`, or `organic_results`, and normalizes result `url/link/href`, `title/name`, `snippet/content/description`, and score fields into source artifacts. It does not call Tavily, Firecrawl, or any paid API by itself.

The `rpa_pipeline_artifact_import` adapter is also offline. It exists so the mainbrain RPA pipeline can later pass bounded evidence artifacts into the research ledger without this skill taking over RPA collection. Expected input fields include `source_url`, `platform`, `task_id`, `evidence_kind`, title/text/transcript/OCR excerpt, `engagement`, `captured_at`, `content_hash`, `risk_flags`, and optional `approval_id`. Login, private data, paid API, bulk scrape, account write, publish, or store-data-write flags require an approval id. The adapter stores no cookies, secrets, raw HTML, private messages, or runtime logs.

The `status` command is a read-only audit surface. It reports skill file presence, Codex hook configuration, unresolved hook turns, ledger/source-artifact counts, recent status/type summaries, and failure-signature counts. It hashes questions and URLs and must not print source excerpts, raw HTML, secrets, cookies, private messages, or runtime logs.

The `rpa-probe` command is the controlled bridge from research skill to local RPA execution. `rpa-probe --mode control-plane` is read-only. `rpa-probe --mode adapter-review` can run the local adapter review pipeline and emit `rpa_pipeline_artifact` source artifacts from review cards. Safe default targets are offline/local adapters such as `creator-content` and mock/record-backed `taobao` paths. Login, unknown platforms, live platform collection, paid APIs, bulk scrape, account write, publish, or store-data-write paths require an approval id. Probe artifacts should be passed to `done --source-artifact`; raw review cards and raw RPA ledgers should not be copied into 3CAN writeback.
