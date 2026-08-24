# Intent-Preserving Adaptive Review Harness

Status: `DESIGN_ONLY / EXPERIMENT_NOT_IMPLEMENTED / GLOBAL_ROLLOUT_BLOCKED`

Strict reference: PR #15 commit
`5995c3f6d694297f8994de252371fdaca7664cd4`.

This document narrows the next experiment. It does not change the current
strict Task Oracle, add a lifecycle event, enable a Hook, or claim that a Lite
path is ready. The design objective is:

> Boundary strong, runtime light.

## Preserved guarantees

Any Lite experiment must preserve the current strict baseline or demonstrate a
simpler equivalent for all of these guarantees:

1. Every acceptance criterion used for convergence has named evidence or a
   named reviewer; natural-language self-assertion is not proof.
2. Only a current completed result may stop silently. `CANDIDATE_READY`,
   `PARTIAL`, `BLOCKED`, `UNAVAILABLE`, `CONFLICT`, and `UNVERIFIABLE` remain
   non-success states.
3. Owner or semantic acceptance cannot be inferred from mechanical checks.
   `CANDIDATE_READY` is not `CONVERGED`, merged, deployed, or published, and is
   not eligible for `AUTO_CLOSEOUT`.
4. A changed Intent, candidate, artifact manifest, evaluator, evidence source,
   or review receipt makes old proof stale.
5. After governance has been activated, deleting its control file cannot turn
   the task into a silent success.
6. A changed or dirty reality cannot be laundered by an earlier PASS.
7. Stop and repair are bounded and honest; no infinite self-heal loop is
   introduced.
8. The Global Hook stays domain-neutral. Domain semantics live in the Intent,
   review rubric, or a criterion-specific Oracle.

## Composition model

Execution and evidence are independent axes:

| Axis | Values | Meaning |
| --- | --- | --- |
| execution | `FAST`, `EPISODIC` | one final bookend review, or a small number of incremental episode reviews plus a final review |
| evidence | `LIGHT`, `TARGETED_STRICT` | semantic review by default, with deterministic or relational proof only for named risky criteria |

The Builder may propose stronger handling. It must not silently downgrade an
active execution mode or remove a mandatory strict criterion. A downgrade is
an Intent revision and requires explicit Owner confirmation. Criteria retained
because of a real incident are sticky until such a revision.

## Minimal future contracts

The experiment should add only three small content-addressed records.

### Run Intent

`RUN_INTENT` describes what must be delivered, not how to implement it:

```json
{
  "schema": "3can.run-intent/v1",
  "goal": "...",
  "acceptance": ["..."],
  "non_goals": ["..."],
  "mutable_decisions": ["..."],
  "final_evidence_expectation": ["..."],
  "execution_mode": "FAST",
  "mandatory_strict_criteria": [],
  "revision": "v1"
}
```

It stays within one screen. The active canonical digest and revision form the
governance boundary. A semantic change after work begins is
`REVISION_PENDING`; the Builder cannot edit, confirm, and accept its own new
goal in the same boundary.

### Frozen Candidate

For a code-only task, the candidate is an immutable Git commit/tree. When the
user-visible result is outside Git, it is the same commit plus a small
content-addressed manifest:

```json
{
  "schema": "3can.candidate-manifest/v1",
  "git_commit": "...",
  "artifacts": [{"role": "final-result", "identity": "sha256:..."}],
  "runtime_revision": null
}
```

The manifest identifies what was reviewed; it does not prove the result is
correct. Reuse an existing immutable object digest or run revision rather than
rehashing a costly artifact. Without a stable identity, that result is
`UNVERIFIABLE`.

### Review Receipt

```json
{
  "schema": "3can.review-receipt/v1",
  "intent_revision": "v1",
  "intent_sha256": "...",
  "candidate_git_commit": "...",
  "candidate_manifest_sha256": null,
  "review_scope_from": "...",
  "review_scope_to": "...",
  "reviewer": {
    "id": "...",
    "version": "...",
    "independence": "same_agent"
  },
  "status": "PASS",
  "findings": [],
  "receipt_sha256": "..."
}
```

The receipt digest is an integrity fingerprint over canonical receipt content,
not a signature, authentication mechanism, or proof of reviewer independence.
Independence is supplied by workflow separation or Owner review. A reviewer is
read-only; changing the candidate invalidates the review and requires a new
snapshot and receipt.

## Execution flows

### FAST + LIGHT

```text
Owner prompt -> active Intent -> normal development -> focused test
-> candidate commit (+ optional manifest) -> final adversarial review
-> PASS | FAIL | PARTIAL | UNVERIFIABLE -> PR or delivery
```

This is the default experiment for a bounded 10--60 minute task. Git freezes the
review object before review; a commit is a candidate, not automatic acceptance.

### EPISODIC + LIGHT

```text
Intent -> E1 candidate commit -> E1 incremental review PASS
       -> E2 candidate commit -> E2 incremental review PASS
       -> final base..HEAD review
```

Use a few meaningful episodes for a longer or cross-module task. A quick review
examines only the last accepted snapshot through the current snapshot. Review
failure creates one bounded repair episode and a new candidate; it does not
start an unbounded retry loop.

### Targeted Strict evidence

Strict proof attaches to a named criterion, not the whole task. For the known
video-lineage incident, a strict relational Oracle must compare the current
director/accepted asset decision with the actual assembly manifest and final
artifact identity. AI review cannot replace this check by saying the files
look consistent. If mandatory evidence cannot be observed, the result is
`UNVERIFIABLE`, `BLOCKED`, or `PARTIAL`.

The existing strict Task Oracle remains independently runnable and is the
reference implementation for these targeted checks. This design does not
weaken its binding, fallback, candidate-freshness, or Stop tests.

## First-write boundary: experiment requirements

The future start gate must have one narrow bootstrap operation that can create
and activate only `RUN_INTENT`. It may not edit business files. Generic shell
tools can read or write, and current Hook payloads do not provide a reliable
capability classification. The experiment must therefore either conservatively
block shell once before Intent activation or report mutation coverage as
`PARTIAL`; it must not grow a shell-string classifier or claim complete write
coverage.

No first-write gate is implemented or shipped by this revision. In particular,
the rejected `apply_patch --require-task-for-edit` prototype is not part of the
public contract.

## Adversarial equivalence gate

Lite cannot enter real-task dogfood until the same fixtures compare it with the
strict reference for:

| Incident | Required Lite result | Current evidence |
| --- | --- | --- |
| stale Intent/evidence | block or typed incomplete | `NOT_RUN` |
| hidden fallback | finding or targeted strict block | `NOT_RUN` |
| mutable decision hardcoding | finding or targeted strict block | `NOT_RUN` |
| goal drift / contradiction | finding | `NOT_RUN` |
| unrequested global constraint | finding | `NOT_RUN` |
| old Git or artifact candidate | stale/block | `NOT_RUN` |
| reused or modified review receipt | stale/block | `NOT_RUN` |

`NOT_RUN` is intentional: a design is not evidence. Existing strict tests stay
authoritative until a separate Lite experiment implements these contracts and
passes this matrix.

## Efficiency gate

Record only boundary-level absolute costs: task wall time, Hook runtime, review
runtime, Oracle runtime, block count, review rounds, full-suite count, manual
interventions, and repair time. Do not add per-tool tracing.

Foundation development and review hours are reported separately from another
Session's business-task cost. Calculate Harness Tax Ratio only when a historical
or paired comparable baseline exists. Without one, report absolute cost. A real
one-hour task becoming three or four hours is an efficiency failure regardless
of correctness.

## Explicit non-goals and rollout

This experiment adds no daemon, database, Episode manager, Review database,
prompt classifier, global hardcode scanner, per-tool LLM call, second Runtime,
or automatic 3CAN writeback. It does not delete strict tests or globally enable
Hooks.

Progression is deliberately staged:

1. preserve strict reference and remove default convergence per-tool work;
2. review this design;
3. implement one isolated Lite experiment without changing the strict path;
4. run adversarial equivalence and absolute-cost measurements;
5. dogfood one small code task, one UI/SaaS task, and one video task with the
   known lineage criterion;
6. consider wider enablement only after low escape, low false-block, and low
   observed task tax.
