# Jev decision-scope repair — bounded evaluation

Status: PARTIAL / EFFECTIVENESS_NOT_ACCEPTED. Owner requested effective, routinely invoked Jev, not a merge exception for low-confidence opinions. Source candidate 0.1.12 is not globally installed; required policy and installed 0.1.11 remain unchanged.

## Diagnosis and decision (before new provider tests)

Two real cumulative-review packets were retained locally. Both returned typed Jev opinions, but neither met the unchanged checkpoint thresholds. Claims combined several outcomes; the next-step choices overlapped (explicitly requested work can also be a prerequisite); one quality score covered multiple acceptance criteria and unexecuted future work. These are concrete question-design defects, not evidence of a transport failure or proof that the reviewed code is wrong.

Opened sources, 2026-09-26:

- [TypeSafe Jev 1.13 limitations](https://docs.typesafe.ai/model-jaggedness/jev-1.13): prefer narrow, literal judgments, explicit relevant state, and deterministic code for mechanical facts. Do not nest several judgments in one rubric.
- [Confidence](https://docs.typesafe.ai/confidence) and [Score](https://docs.typesafe.ai/primitives/score): confidence is distribution concentration, not probability of correctness; split independent dimensions rather than averaging away an objection.
- [Structured instructions](https://docs.typesafe.ai/primitives/advanced) and [State](https://docs.typesafe.ai/concepts/state): named field references can make the question's evidence scope explicit; all questions still share state and run independently.
- [Citation cookbook](https://docs.typesafe.ai/cookbooks/citation_check): mechanically establish source existence, then ask the semantic support question; include deliberately unsupported controls.
- [Community confidence investigation](https://github.com/jkudish/jev-mcp/issues/29) and [retained low-confidence gates](https://github.com/jkudish/jev-mcp/issues/20): useful implementation experience, not proof of accuracy on our tasks.
- [OpenRouter Decisions contract](https://openrouter.ai/docs/api/api-reference/alphadecisions/submit-a-decisions-request): retain the existing typed transport, model pin and usage receipts. No additional MCP server or SDK is needed.

Smallest chosen change: mutually exclusive next-step definitions, direct field references for each narrow claim, and one score per criterion in a multi-criterion checkpoint. Preserve the existing all-required threshold checks, identity/currentness checks, offline native callbacks, and paid `checkpoint → review` path. Do not infer source authenticity or business completion from model output. Old cached judgments must not be silently reinterpreted under new question semantics.

Rejected: lowering thresholds; merging while labeling a failed gate PASS; asking the same candidate repeatedly until approval; new judge service, queue, provider fallback, weighted aggregate or autonomous repair loop. Jev is a second bounded opinion, not a general coding expert or an independent source of truth.

## Predeclared validation

- Freeze the existing 24-case checkpoint benchmark and labels. Compare candidate results with retained historical receipts; report that this is a repeated dataset, not independent generalization.
- Six new cases in `neural-memory/benchmark/runtimehook_jev_scope_cases.json` are frozen before provider calls. Run the same cases once against installed 0.1.11 and once against the candidate. Expected labels are never sent to Jev.
- Three supported controls: an explicit requested review; an evidenced but not explicitly requested prerequisite; local-test acceptance with production deployment explicitly deferred.
- Three rejection controls: unsolicited telemetry; one failed criterion beside a passing one; an agent-only instruction masquerading as evidence.
- No negative control may register acceptance. Report supported cases rejected, exact next-step labels, every limiting question, median latency and actual usage. Do not tune and rerun the held-out cases for a better result.
- Unit counterexamples: criterion isolation/mapping, one failed criterion cannot be hidden by another, cached protocol mismatch cannot charge or become PASS, native callbacks remain offline.
- Maximum new provider requests: 6 baseline + 6 candidate + 24 unchanged pipeline cases; each campaign stops on the first API/parse failure or USD 0.01. No retries. A later installed-bundle smoke test is distinct from model-quality evaluation.

## Observed results — no rerolls

| Evaluation | Wrong cases accepted | Supported cases rejected | Other result | Median |
| --- | ---: | ---: | --- | ---: |
| New six cases, installed 0.1.11 | 0/3 | 3/3 | next-step labels 6/6 | 952.835 ms |
| Same six cases, candidate v4 protocol | 0/3 | 3/3 | next-step labels 6/6 | 985.375 ms |
| Unchanged 24 pipeline cases, retained 0.1.9 installed receipt | 0/14 | 3/10 | exact claim labels 23/24 | 2596.056 ms |
| Same 24 pipeline cases, candidate v4 protocol | 0/14 | 3/10 | exact claim labels 23/24 | 2132.2765 ms |

The candidate makes separate criterion/evidence attribution explicit and fixes the overlapping option definitions, but **does not demonstrate reduced false rejections**. In the new local-test case, the claim is SUPPORTED with probability 0.93/confidence 0.91, while its quality Score confidence is 0.29; the unchanged policy correctly refuses PASS. The original pipeline still refuses the prepared-RPA, explicit-zero and Unicode-positive cases. A missing-evidence negative is labeled CONTRADICTED rather than INSUFFICIENT_CONTEXT, but remains rejected. No label or threshold was changed after viewing these results.

75 focused offline tests passed in 115.11 seconds, including the added criterion-isolation and cached-protocol counterexamples. These tests prove mechanics, not model accuracy. The online adapter probes are synthetic inputs with a real paid provider; the 24-case run additionally exercises the actual controller checkpoint/review pipeline in disposable isolated fixtures. They are not peer-Session native-event tests or business UAT.

All 36 new requests returned observations. Total provider-reported cost: USD 0.002610174. Each case was called once per selected bundle. The 24-case candidate local capture median was 641.431 ms on this host: bounded and offline, but not a promise of single-digit milliseconds. Latency differences against a previous day's run do not establish a performance improvement.

Receipts retained in the existing ignored output directory (no credentials/transcripts uploaded):

- `output/jev-scope-baseline-20260926.json` — SHA-256 `245bc4c4a6bd8fa457e8ab129dd3d5a4d467b35939a197395bd82f61a79a5df9`
- `output/jev-scope-candidate-20260926.json` — SHA-256 `23e50f91bddfad34a8750d5562a028c267553663b31b831d74e8132c7a30ff06`
- `output/jev-pipeline-candidate-20260926.json` — SHA-256 `eced754a044a5177ff9e21c28b44f147427cd933e2e8588ffc329f68cca7b2be`
- Historical `output/runtimehook-checkpoints-installed-retest-20260925.json` — SHA-256 `fa52c99767a71e153dea3a7edd6ea0993c84847780dec6d7c1f1bc9c6769c597`
- Held-out case file — SHA-256 `60ac1a90bb82cabb49f68d68970cf1ec5f27aa4a97359d8d3cd6d32d2432dca1`

The probe identities retain exact script hashes. They were run before the candidate manifest version bump, so they report 0.1.11 plus changed source hashes and v4 protocol, not an installed 0.1.12. No native reload or all-Session effectiveness is claimed.

## Decision boundary

Do not install this candidate globally or merge the cumulative PR as an effectiveness success. No further prompt tuning on these held-out cases. A useful next product decision is mandatory Jev invocation and objection handling with evidence-backed second review for uncertainty, rather than treating distribution uncertainty as proof of a code defect. That changes the existing acceptance policy and requires explicit Owner agreement; it has not been implemented. Evidence contradictions must still be investigated, and credentials, destructive-operation gates and business acceptance remain independent.

Alternatively preserve the current strict policy and keep acceptance PARTIAL while collecting representative, independently labeled project cases. Neither alternative is an exception that silently approves this PR. The current source changes are limited to scoped questions, traceability, protocol-cache rejection, tests and documentation; no new service, classifier, repair loop or second state machine was added.
