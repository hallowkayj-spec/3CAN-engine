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
- `elapsed_minutes`: active research time. A passing ledger is no higher than the tier hard cap; an incomplete run becomes typed terminal at or after the cap.
- `source_urls` / `source_records`: opened sources and their types.
- `source_count`: unique valid source count.
- `query_plan.query_variants`: materially different queries used.
- `internal_context.status`: `used`, `unavailable`, or `not_applicable`; `used` also requires evidence references.
- `contradiction_status`: `checked_no_material_conflict`, `resolved`, `unresolved`, or `not_applicable`.
- `evidence_scores`: bounded `0..5` authority, recency, practice value, reproducibility, relevance, community signal, risk, and conflict evidence.
- `sidecar_judgement`: evidence sufficiency and task fit.
- `session_id` and `turn_id`: current correlation identifiers when available.
- `requirement_id`: required for a hook-bound turn and must match the prompt hash emitted by `UserPromptSubmit`; standalone manual ledgers may omit it.

## Tier gates

`standard` has a 10-minute hard cap and requires at least five opened, relevant, unique external sources backed by successful collected or approved RPA artifacts, three source families, three query variants, a primary/boundary source, implementation or practice evidence, context status, contradiction review, evidence scores, and sidecar pass.

`deep` has a 30-minute hard cap and requires at least 12 opened, relevant, unique external sources backed by successful collected or approved RPA artifacts, four external source families, six query variants, primary/boundary evidence, paper/standard or benchmark evidence, GitHub or Hugging Face implementation evidence, community evidence, context status, contradiction review, evidence scores, and sidecar pass. Platform/RPA topics additionally require a public platform signal or approved RPA artifact.

Source families are:

- primary: official docs, specifications, releases, regulators;
- academic: papers, standards, reproducible benchmarks;
- implementation: GitHub source/issues/releases and Hugging Face model/dataset artifacts;
- community: Reddit, professional forums, and practitioner cases;
- platform: public creator/video/comment evidence or approved RPA artifacts;
- web: other targeted current sources;
- internal: applicable 3CAN context, counted only when recorded as `used`.

Source count alone never completes a run. The harness also checks family coverage, queries, contradictions, elapsed time, context status, evidence quality, and sidecar judgement. Before the hard cap, a missing gate remains `block`. At the hard cap, the harness records terminal `PARTIAL` when at least one external source was verified or `UNAVAILABLE` when none was verified. Stop may then return the matching typed result, but mutation stays blocked unless status is `pass`.

## Safe collection

`collect-url` accepts only public `http`/`https` pages and stores a bounded text excerpt plus metadata and content hash, never raw HTML or credentials. Dynamic/login pages, paid APIs, private content, bulk scraping, account/store writes, and publishing stay behind their existing approval boundaries.

`import-search-result` normalizes already available provider JSON for discovery and makes no provider call; those records do not satisfy the opened-source gate until collected. `import-rpa-artifact` imports bounded output from an existing project-owned lane. `rpa-probe` may call adapters from the explicit `--project-root`, `THREECAN_PROJECT_ROOT`, or current working directory, in that order; absence returns typed `unavailable`. Project RPA modules are isolated per call, and default state/evidence output stays under the selected/current physical project. A global Skill is discoverable, while project hooks are the enforcement boundary. This Skill never creates a second RPA runtime.

Do not store prompts, completions, secrets, cookies, recovery codes, private messages, raw runtime logs, or copyrighted long-form source copies.

## Durable 3CAN mapping

- `DOC-*`: reusable research summary or operating guide.
- `DEC-*`: technology or architecture decision.
- `INTF-*`: interface/contract fact future code must obey.
- `ERR-*`: deterministic reusable failure knowledge.
- `SES-*`: bounded handoff or milestone evidence status.

Prefer updating or superseding the canonical existing node. Write one concise conclusion with Git/source-ledger evidence references; do not create a node per source or mirror the ledger into the graph.
