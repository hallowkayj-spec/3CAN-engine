---
name: 3can-deep-research
description: Use for current external engineering research, evidence gathering, technical selection, repeated-failure diagnosis, RPA/platform investigation, model or provider quality analysis, and any turn the 3CAN research hook marks as required. Selects a bounded standard tier of at most 10 minutes or a deep tier of at most 30 minutes, then records a cited evidence ledger and durable 3CAN conclusion when warranted.
---

# 3CAN Deep Research

Use this skill when current external evidence can materially improve an engineering decision or when repeated failure means further blind editing is wasteful. The goal is a decision-quality evidence packet, not elapsed time or a large link list.

## Choose one tier

- `standard` — at most 10 active research minutes. Use for medium development work that needs current facts, implementation examples, tool choices, API details, or a bounded evidence refresh.
- `deep` — at most 30 active research minutes. Use for difficult or recurring failures, RPA/platform behavior, model-quality regressions, multi-system integration, personalized technical constraints, or conflicting evidence.

Legacy `quick` maps to `standard`; legacy `rpa_deep` maps to `deep`. Do not create additional tiers.

Before searching, state the question, decision to be made, freshness requirement, known constraints, and—when debugging—the exact failure signature. If an applicable 3CAN runtime is available, route/retrieve current project context first and record the node or evidence references. If it is unavailable, record `unavailable` and continue safe web and local research; never invent context.

Plan the search:

```bash
scripts/3can_research_harness.py plan \
  --question "<research question>" \
  --research-tier standard \
  --focus-term "<important term>"
```

## Completion gates

`standard` requires at least 30 opened, relevant, unique sources across at least five source families, six materially different queries, a primary/boundary source, implementation or practice evidence, a contradiction check, evidence scores, recorded 3CAN-context status, and sidecar evidence/task-fit judgement.

`deep` requires at least 90 opened, relevant, unique sources across all six external source families and 18 materially different queries. It must include primary/boundary evidence, a paper/standard or benchmark, GitHub or Hugging Face implementation evidence, community evidence such as Reddit or a professional forum, contradiction/counterexample evidence, recorded 3CAN-context status, and sidecar judgement. RPA, creator, video, or platform questions also require public platform or approved RPA evidence.

Do not stop merely because the clock elapsed or a source count was reached. Stop early only when all gates pass and further searching has low decision value. At the hard cap, return `PARTIAL` or `UNAVAILABLE` with precise missing evidence; do not fake completion.

## Evidence workflow

1. Open and verify every cited URL. Never cite a generated or search-result URL that was not opened.
2. Use official docs, specifications, changelogs, release notes, or regulator material for contract boundaries.
3. Use papers and reproducible benchmarks for mechanisms and comparative claims.
4. Inspect GitHub source, issues, pull requests, and releases for implementation reality.
5. Inspect Hugging Face model/dataset cards, revisions, licenses, evaluation data, and discussions for model or dataset claims.
6. Use Reddit and relevant professional forums for failure patterns and field counterexamples; do not promote anecdotes to universal facts.
7. For creator or short-video evidence, record the public URL, date, observable claim, and engagement/transcript/OCR evidence when available. Respect login, privacy, platform, copyright, and rate limits.
8. Maintain a claim-to-source matrix and search deliberately for contradictions. Separate sourced facts, inference, and remaining uncertainty.

For public pages, preserve a bounded source artifact when practical:

```bash
scripts/3can_research_harness.py collect-url \
  --url https://example.com/source \
  --source-type official_primary
```

Use `import-search-result` for existing provider-neutral result JSON. Use `import-rpa-artifact` for bounded output produced by an existing project-owned RPA lane. `rpa-probe` is only an optional bridge to already installed project RPA adapters; pass the current physical worktree with `--project-root`, or set `THREECAN_PROJECT_ROOT`; otherwise it uses the current working directory. If that project has no `tools/rpa`, return typed `unavailable`. Do not build a second browser/RPA subsystem here. Login, private data, paid APIs, bulk collection, account/store writes, and publishing still require their existing approvals.

A global Skill installation makes the workflow discoverable to supported Sessions and Agents. Project hooks make its gates automatic for that project. Other clients must invoke the Skill or the harness explicitly; a Skill alone cannot force an arbitrary Agent runtime to use RPA.

Record the evidence ledger:

```bash
scripts/3can_research_harness.py done \
  --session-id <session_id> \
  --turn-id <turn_id> \
  --question "<research question>" \
  --research-tier deep \
  --elapsed-minutes <active-research-minutes> \
  --source-artifact <collected-source.json> \
  --source-url https://example.com/another-source \
  --source-type official_primary \
  --query-variant "<query used>" \
  --context-status used \
  --context-ref <3CAN-node-or-evidence-ref> \
  --contradiction-status resolved \
  --evidence-score authority=5 \
  --evidence-score task_relevance=5 \
  --sidecar-evidence-sufficiency pass \
  --sidecar-task-fit pass
```

Then independently evaluate it:

```bash
scripts/3can_research_harness.py judge --ledger-file <ledger.json>
```

The harness, not prose or source count alone, owns the completion result. Write back to 3CAN only when the research creates durable project meaning: an architecture/interface decision, reusable operating guidance, a verified incompatibility, or ErrorKnowledge. Do not write every query or source into the graph; Git and the source ledger remain the exact evidence owners.

Read `references/research-ledger.md` only when recording or auditing a run.
