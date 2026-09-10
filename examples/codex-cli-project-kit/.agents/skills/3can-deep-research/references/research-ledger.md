# Research Ledger Reference

Use only when recording or auditing a research run. Git owns exact source and
history; opened sources own external evidence; tests/runtime/provider receipts
prove bounded behavior; 3CAN retains durable meaning and lineage.

## Structural readiness

- `question`, `research_tier`, `elapsed_minutes`: decision, budget and actual time.
  `standard` budgets 10 minutes; `deep` budgets 30 for one evidence question.
- `source_records` / `source_artifacts`: opened evidence; discovery-only URLs
  never count as opened. Primary/boundary and implementation/practice evidence
  are required; deep work needs implementation and platform claims need platform
  evidence. Papers/benchmarks support comparative claims, not every small fix.
- `internal_context.status`: `used`, `unavailable`, `not_applicable`;
  `used` also requires evidence references.
- `contradiction_status`: `checked_no_material_conflict`, `resolved`,
  `unresolved`, `not_applicable`; unresolved conflicts cannot pass readiness.
- `decision_reference`: existing project note linking the question, sources,
  local constraints, chosen/rejected approach, counterexample and validation
  plan. The helper checks presence only; the reviewer must inspect its meaning.
- `incomplete_reason`: concrete missing evidence or interruption, allowing an
  honest `PARTIAL`/`UNAVAILABLE` before the budget cap.
- `session_id`, `turn_id`, `requirement_id`: exact hook-emitted binding for a
  bound turn. Manual standalone ledgers may omit it. Never borrow another turn.

The 5/12-source, 3/4-family and 3/6-query counts are coverage prompts; shortfalls
produce warnings rather than forced link padding. Scores and legacy
`sidecar_judgement` PASS strings are optional declarations, not independent proof.
`ready_for_review` explicitly means `validation_scope=structural_evidence_only`,
`semantic_review_required=true`, `implementation_verified=false`. Review actual
sources and the decision note, then execute the local probe. Attach results and
remaining uncertainty to that same note. Do not create another receipt protocol.

Incomplete evidence remains typed. At the cap, or with `--incomplete-reason`,
`done` closes as `PARTIAL` if some sources were opened or `UNAVAILABLE` if none
were. Safe local experiments continue. PreToolUse is a compatibility no-op;
Stop permits one review continuation, never an infinite loop. Credential,
tenant, writer, ticket, deployment and publication gates remain independent.

## Safe collection

`collect-url` accepts public HTTP(S) and stores bounded excerpts plus metadata,
not raw HTML or credentials. `import-search-result` is discovery only.
`import-rpa-artifact` imports approved/offline bounded evidence; `rpa-probe`
reuses an existing lane from explicit `--project-root`, then
`THREECAN_PROJECT_ROOT`, then cwd. Missing adapters mean `unavailable`, not a new
runtime. Each call isolates project modules and keeps output in that project.
Login, private data, paid APIs, bulk collection and account/store writes or
publication retain their existing approvals. Never store secrets, private
payloads or copyrighted long-form copies in a public research packet.

## Durable writeback

Use the existing canonical DOC/DEC/INTF/ERR/SES node when appropriate. Preserve
changed meaning, verification status, superseded lineage and Git/source-ledger
references. Do not write a node for every source or mirror the search history.
