---
name: 3can-deep-research
description: Use when current external evidence materially affects an engineering decision, tool selection, repeated failure, or RPA/platform behavior. Connect opened sources to local constraints, a concrete decision and executable validation; do not use for routine local edits or treat source counts as task success.
---

# 3CAN Deep Research

Research serves a working solution, not a large link list. Resolve the helper
from this loaded Skill's `scripts/3can_research_harness.py` when installed
globally, otherwise from the current project's Project Kit. Do not borrow
another project's client or private state.

## When to research

Use current evidence when the Owner asks, before adopting an unfamiliar
subsystem, when an external contract is uncertain, or when local observation
contradicts the proposed fix. A single known local typo needs no research.
The automatic hook is a keyword heuristic, not semantic understanding: read
the complete request, including negation and quoted examples. Do not browse
against an explicit Owner instruction not to browse; mark unstable claims
unverified instead.

Use the existing `failure-signal` for deterministic repeats; its third matching
failure is a backstop, not permission for two blind repairs. Stop speculative
patches earlier when their diagnosis lacks evidence.

Choose one budget per focused evidence question:

- `standard`: up to 10 active research minutes for a bounded decision;
- `deep`: up to 30 for conflicting evidence, recurring failures or integration.

These are not minimum waiting times or limits on the entire engineering task.
Legacy `quick` maps to `standard`; `rpa_deep` maps to `deep`. No new tiers.
Split a broad task into real independent decisions, not quota-filling rounds.

## Evidence to decision to local verification

1. State the exact question/failure, pending decision, freshness requirement,
   physical project, local constraints and canonical owner. Inspect local
   evidence first. Route relevant 3CAN context only when useful; record
   `unavailable` and continue safely if that service is down.
2. Check official/native capability and maintained alternatives before custom
   code. Open the relevant version's documentation and actual implementation.
   Search issues and practitioner reports for failure patterns; use papers or
   reproducible benchmarks for comparative claims. Separate facts, inference
   and unresolved contradictions. Community anecdotes are not universal facts.
3. Write one decision note in the project's existing evidence location:
   question → opened sources/versions → local constraints → selected approach
   → rejected alternative/counterexample → executable validation plan and
   expected failure behavior. A reference's presence does not prove its meaning.
4. Implement the smallest supported solution and run the local probe against
   the real acceptance surface. Attach results and remaining gaps to the same
   note. Unit tests, health checks and a prepared RPA job are not end-to-end
   execution. RPA diagnosis must follow the actual work through its canonical
   queue, worker, browser/action, output and independent readback as applicable.
5. Review applicability and results against Owner Intent. Independent review
   is useful for material risk when authorized, not to fill a `pass` string.
   Reuse RuntimeHook/project review references; do not create another kernel.

Standard 5-source / deep 12-source and family/query counts are coverage prompts,
not universal quotas. A narrow question may need fewer; an unexplained material
failure needs targeted evidence, not padded links. Explain missing coverage.
Platform claims need public platform evidence or an approved local RPA artifact.
Do not require unrelated papers/community posts just to complete a checklist.

## Bounded evidence helper

Use `plan` to organize a search and `collect-url` to preserve bounded public
source metadata/excerpts. Open every cited source. `import-search-result` is
discovery only and cannot establish opened evidence. `import-rpa-artifact`
accepts bounded evidence from an existing authorized lane. Read
`references/research-ledger.md` when recording or auditing a ledger.

```text
<helper> plan --question "<decision>" --research-tier standard
<helper> collect-url --url <opened-public-url> --source-type official_primary
<helper> done --question "<decision>" --research-tier standard \
  --elapsed-minutes <actual-active-minutes> --source-artifact <source.json> \
  --decision-ref <existing-decision-note> --context-status used \
  --context-ref <project-evidence-ref> --contradiction-status resolved
<helper> judge --ledger-file <ledger.json>
```

For a hook-bound turn include the exact `--session-id`, `--turn-id` and
`--requirement-id` emitted by the hook. Never guess or reuse another turn's
binding. Standalone manual ledgers may omit these identifiers.

The helper returns structural `ready_for_review`, not semantic correctness or
implementation acceptance. Scores and legacy sidecar PASS strings are only
declarations. Actual reviewed evidence determines whether the solution works.
Stop searching when the decision is supported and local verification is the
next useful step. At the budget cap, or earlier with `--incomplete-reason`,
preserve typed `PARTIAL` / `UNAVAILABLE` rather than waiting out a timer.

## Execution and safety boundary

Safe experiments and unrelated local work continue while research is incomplete.
Defer only the claim or operation whose missing evidence matters. Research is
not another authorization system: credential, tenant, repository, writer,
ticket, destructive-action, deployment and publication gates remain independent.
Stop requests at most one review continuation and then permits an honest
incomplete answer. A global Skill is discoverable; native hooks provide lifecycle
reminders, not universal invocation or proof of review quality.

The optional `rpa-probe` reuses adapters from explicit `--project-root`, then
`THREECAN_PROJECT_ROOT`, then cwd. No `tools/rpa` means typed `unavailable`;
never build a second RPA runtime. Default state/evidence stays under that
physical project. Login, private data, paid APIs, bulk collection, account/store
writes and publishing retain their existing approvals.

Write back only durable meaning: a decision, verified incompatibility, reusable
operating guidance or ErrorKnowledge. Preserve Git/source-ledger references
and typed uncertainty; do not mirror each query/source into the graph.
