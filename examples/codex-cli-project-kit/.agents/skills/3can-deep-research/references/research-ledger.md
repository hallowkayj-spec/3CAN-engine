# Research Ledger Reference

Use this reference only when recording or auditing a 3CAN research run.

## Authority boundary

- Git owns exact source, history, and the checked-in Skill contract.
- Opened external sources and bounded artifacts own external evidence.
- Tests and runtime receipts prove behavior.
- 3CAN stores durable project meaning, evidence status, and superseded lineage; it does not duplicate the entire search log.

## Required ledger fields

- `question`: exact question and decision supported.
- `research_tier`: `standard` or `deep`; legacy `quick` and `rpa_deep` normalize to these two values.
- `elapsed_minutes`: active research time, greater than zero and no higher than the tier hard cap.
- `source_urls` / `source_records`: opened sources and their types.
- `source_count`: unique valid source count.
- `query_plan.query_variants`: materially different queries used.
- `internal_context.status`: `used`, `unavailable`, or `not_applicable`; `used` also requires evidence references.
- `contradiction_status`: `checked_no_material_conflict`, `resolved`, `unresolved`, or `not_applicable`.
- `evidence_scores`: bounded `0..5` authority, recency, practice value, reproducibility, relevance, community signal, risk, and conflict evidence.
- `sidecar_judgement`: evidence sufficiency and task fit.
- `session_id` and `turn_id`: current correlation identifiers when available.

## Tier gates

`standard` has a 10-minute hard cap and requires at least 30 opened, relevant, unique sources, five source families, six query variants, a primary/boundary source, implementation or practice evidence, context status, contradiction review, evidence scores, and sidecar pass.

`deep` has a 30-minute hard cap and requires at least 90 opened, relevant, unique sources, all six external source families, 18 query variants, primary/boundary evidence, paper/standard or benchmark evidence, GitHub or Hugging Face implementation evidence, community evidence, context status, contradiction review, evidence scores, and sidecar pass. Platform/RPA topics additionally require a public platform signal or approved RPA artifact.

Source families are:

- primary: official docs, specifications, releases, regulators;
- academic: papers, standards, reproducible benchmarks;
- implementation: GitHub source/issues/releases and Hugging Face model/dataset artifacts;
- community: Reddit, professional forums, and practitioner cases;
- platform: public creator/video/comment evidence or approved RPA artifacts;
- web: other targeted current sources;
- internal: applicable 3CAN context, counted only when recorded as `used`.

Source count alone never completes a run. The harness also checks family coverage, queries, contradictions, elapsed time, context status, evidence quality, and sidecar judgement. If any required gate is missing, status remains `block`; at the hard cap report `PARTIAL` or `UNAVAILABLE` with the missing gate.

## Safe collection

`collect-url` accepts only public `http`/`https` pages and stores a bounded text excerpt plus metadata and content hash, never raw HTML or credentials. Dynamic/login pages, paid APIs, private content, bulk scraping, account/store writes, and publishing stay behind their existing approval boundaries.

`import-search-result` normalizes already available provider JSON and makes no provider call. `import-rpa-artifact` imports bounded output from an existing project-owned lane. `rpa-probe` may call adapters from the explicit `--project-root`, `THREECAN_PROJECT_ROOT`, or current working directory, in that order; absence returns typed `unavailable`. A global Skill is discoverable, while project hooks are the enforcement boundary. This Skill never creates a second RPA runtime.

Do not store prompts, completions, secrets, cookies, recovery codes, private messages, raw runtime logs, or copyrighted long-form source copies.

## Durable 3CAN mapping

- `DOC-*`: reusable research summary or operating guide.
- `DEC-*`: technology or architecture decision.
- `INTF-*`: interface/contract fact future code must obey.
- `ERR-*`: deterministic reusable failure knowledge.
- `SES-*`: bounded handoff or milestone evidence status.

Prefer updating or superseding the canonical existing node. Write one concise conclusion with Git/source-ledger evidence references; do not create a node per source or mirror the ledger into the graph.
