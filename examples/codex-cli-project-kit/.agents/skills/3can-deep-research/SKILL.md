---
name: 3can-deep-research
description: Use for mandatory deep web/RPA research, semantic research triggers, repeated-failure escalation, official documentation checks, latest/current information, community feedback, technical selection, model/provider/API comparisons, pricing/law/regulation checks, keyword/query planning, or whenever the 3CAN research hook says research is required. Produces cited conclusions plus a 3CAN source ledger/writeback plan.
---

# 3CAN Deep Research

Use this skill when a task needs current, external, contested, or practice-heavy information before a conclusion or code change. Treat the 3CAN hook as mandatory: if it marks a turn as research-required, do not mutate files or give final conclusions until a source ledger is recorded.

## Trigger Model

Research can trigger from two layers:

1. User-prompt semantics: explicit web/research requests, latest/current facts, technical selection, model/API/provider comparisons, platform/RPA intelligence, keyword planning, or complex multi-constraint requests.
2. Execution failure escalation: repeated failures, unstable provider/API behavior, repeated edits to the same area, or loop-detector output should stop blind edits and trigger research.

## Workflow

1. Define the research question, freshness requirement, and decision the result will support.
2. Choose a timebox:
   - quick: first sufficiency decision at about 5 minutes; target 5 minutes.
   - standard: first sufficiency decision at about 5 minutes; target 10 minutes; hard cap around 20 minutes.
   - deep: target 20 minutes; hard cap around 30 minutes unless the task value justifies continuing.
   - RPA deep: target 30 minutes or more only when the RPA pipeline is mature and the task requires platform/video/comment evidence.
3. When taking over an existing research turn or auditing the skill state, run the read-only status command:

```bash
scripts/3can_research_harness.py status
```

It summarizes skill files, hook configuration, unresolved hook state, source-ledger counts, source-artifact counts, and failure signatures. It does not print source excerpts, raw HTML, or secrets.
4. Plan queries before searching when the task is broad. Expand the user's terms into official terms, community terms, platform slang, Chinese/English variants, and failure-oriented terms.

```bash
scripts/3can_research_harness.py plan \
  --question "<research question>" \
  --research-tier standard \
  --focus-term "<important term>"
```

5. Search primary sources first for contracts and boundaries: official docs, vendor changelogs, API references, GitHub repos, pricing pages, release notes, standards, or regulator pages.
6. Add practice sources when they are directly useful: GitHub issues, user reports, benchmarks, creator videos, comments, engagement signals, subtitles, ASR/OCR, or public platform observations.
7. For public http(s) pages, collect a source artifact before relying on the page:

```bash
scripts/3can_research_harness.py collect-url \
  --url https://example.com/source \
  --source-type official_primary
```

The adapter stores title, metadata, text excerpt, content hash, HTTP status, and content type. It does not store raw HTML or secrets.

For provider search output that is already available as JSON, normalize it without making a live provider call:

```bash
scripts/3can_research_harness.py import-search-result \
  --input-file search-results.json \
  --provider tavily \
  --query "<query>" \
  --source-type community_practice
```

For RPA evidence produced by an external/mainbrain pipeline, import only the offline artifact. Do not launch browser automation from this skill:

```bash
scripts/3can_research_harness.py import-rpa-artifact \
  --input-file rpa-evidence.json \
  --approval-id APR-optional-when-required
```

The input should contain public `source_url`, `platform`, `task_id`, `evidence_kind`, bounded text/transcript/OCR excerpt, engagement signals, capture time, and any approval risk flags. Login, private data, paid API, bulk scraping, store writes, account writes, or publishing must carry an approval id.

For RPA-heavy research, use a safe local RPA probe when existing offline adapters can add practical evidence before the final judgement:

```bash
scripts/3can_research_harness.py rpa-probe \
  --mode adapter-review \
  --task-id R5 \
  --platform creator-content \
  --adapter-task-id KB_CREATOR_RPA \
  --params-json '{"source_platform":"douyin"}'
```

This runs the local adapter review pipeline, emits `rpa_pipeline_artifact` source artifacts, and can be fed into `done --source-artifact`. It is allowed for offline/local adapters such as `creator-content` and mock/record-backed `taobao` paths. Login-state platform automation, live platform collection, paid APIs, bulk scraping, account writes, store writes, or publishing still require an approval id and should not be started from the skill unless explicitly approved.

8. Score evidence by task fit, not a fixed A/B/C rank. Consider authority, recency, practice value, reproducibility, relevance, community signal, risk, and conflicts.
9. Use sidecar judgement for standard/deep work: decide whether evidence is sufficient and whether it actually supports the task decision. Stop when marginal value is low.
10. Separate sourced facts from inference. Use absolute dates for time-sensitive claims.
11. Record the ledger in the repo:

```bash
scripts/3can_research_harness.py done \
  --session-id <session_id> \
  --turn-id <turn_id> \
  --question "<research question>" \
  --research-tier standard \
  --source-artifact test-results/3can/research_sources/source.json \
  --source-type official_primary \
  --source-url https://example.com/source-1 \
  --evidence-score authority=5 \
  --evidence-score task_relevance=5 \
  --source-url https://example.com/source-2 \
  --source-url https://example.com/source-3
```

12. Run the sidecar judge when the tier is standard/deep/RPA or the decision is expensive:

```bash
scripts/3can_research_harness.py judge --ledger-file <ledger.json>
```

13. Write back durable results to 3CAN when the research changes architecture, interfaces, provider choice, risk policy, or reusable operations knowledge.

## Source Standards

- Prefer official and primary sources for protocol truth, contracts, pricing, and compliance.
- Use practice/community sources for real-world model quality, benchmark reproducibility, platform operations, failure patterns, and implementation direction.
- For RPA/platform sources, record public URL, timestamp, engagement signals, transcript/OCR/frame evidence when available, and whether login/paid/automation approval was required.
- Do not cite pages you did not open or inspect.
- Do not quote long copyrighted passages; summarize and link.
- If web/MCP access is unavailable, mark the result as not verified instead of filling gaps from memory.

## Output

Every final research answer must include source links, date-sensitive caveats, and a short statement of what remains unverified. For engineering work, include the concrete effect on files, contracts, tests, and 3CAN nodes.

For ledger fields and 3CAN node mapping, read `references/research-ledger.md` only when you are recording or auditing a research result.
