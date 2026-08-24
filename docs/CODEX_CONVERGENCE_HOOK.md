# Codex Convergence Hook

The project kit ships one optional, local Codex lifecycle hook for long tasks.
It keeps the task boundary outside model context, restores it at session start
and after compaction, and refuses to call stale or unrelated evidence
`CONVERGED`.

The hook is deliberately small and offline. It does not schedule work, parse
session JSONL, call an LLM, call 3CAN, manage a runtime, commit, merge, deploy,
publish, or write back to the graph.

## Authority and two-layer contract

- Codex `/goal` owns thread-level continuation.
- Git owns exact source state and Task Hook history.
- `.codex/task-hooks/*.json` owns versioned task-family meaning: Goal,
  Acceptance, Candidate Provider, Oracle bindings, invariants, and named mutable
  bindings.
- `.codex/convergence.json` selects one exact Task Hook digest for one run and
  supplies only that run's bindings and explicitly allowed fallbacks.
- Declared tools and reviewer receipts prove behavior for the current candidate.
- `test-results/3can/convergence/receipt.json` records the current local result.
- 3CAN is outside the hot path. A `CONVERGED` receipt only makes
  `AUTO_CLOSEOUT` eligible; the hook always records `performed=false`.

`authorized_by`, `confirmed_by`, and `confirmation_ref` fields are audit
assertions, not authentication or cryptographic approval. They never bypass
repository, credential, tenant, destructive-action, or exact-ticket gates.

## Enable it deliberately

The shipped files are examples and are inactive until copied:

1. Copy `.codex/task-hooks/generic-delivery.example.json` to a new tracked Task
   Hook file. Replace Goal, applicability, Acceptance, Candidate Provider, and
   Oracles with the current task contract.
2. Validate the Task Hook and obtain its canonical digest:

   ```powershell
   python scripts\3can_convergence.py task-digest `
     --task-hook .codex/task-hooks/my-task-v1.json
   ```

3. Copy `.codex/convergence.example.json` to `.codex/convergence.json`. Pin the
   exact Task Hook path, revision, and digest; choose a unique `run_id`; provide
   every declared mutable binding; and record the real activation review.
4. Keep `test-results/3can/` ignored. Review `.codex/hooks.json`, enable Codex
   Hooks, and use `/hooks` to review and trust the exact native definitions.

Do not activate the unchanged example and do not globally enable it as a side
effect of installation. Missing contract plus missing receipt is a no-op, so
ordinary safe development has no added ceremony.

## Task Hook

The minimum tracked contract is domain-neutral:

```json
{
  "schema": "3can.task-hook/v1",
  "task_family": "generic-delivery",
  "status": "EPHEMERAL_ACTIVE",
  "lifecycle": "one_off",
  "revision": "v1",
  "parent_revision": null,
  "goal": "Produce the exact observable result.",
  "applicability": "An explicitly selected run of this task family.",
  "candidate": {"provider": {"type": "workspace"}},
  "acceptance": [{
    "id": "mechanical-integrity",
    "text": "The current candidate passes its declared checks.",
    "oracle_ids": ["focused-tests"]
  }],
  "oracles": [{
    "id": "focused-tests",
    "type": "command",
    "kind": "DETERMINISTIC",
    "version": "v1",
    "argv": ["python", "-m", "pytest", "-q", "tests/test_module.py"],
    "stages": ["episode", "final"],
    "timeout_seconds": 120
  }],
  "invariants": [],
  "mutable_bindings": [],
  "fallback_policy": "explicit_only",
  "allowed_fallback_ids": []
}
```

Every Acceptance criterion binds stable IDs to one or more final-stage Oracles.
An Oracle has an explicit type, kind, and version. Mechanical checks do not
prove visual quality, semantic consistency, product fitness, or Owner
acceptance unless the Task Hook binds an appropriate external or human Oracle.

Unknown fields fail closed. Optional experimental metadata belongs under an
`extensions` object and never changes an allow decision by being ignored.

## Hardcoding association rule

The hook does not grep for constants and contains no `if video`, `if seo`, or
other domain classifier. It asks a narrower, enforceable question:

> Did the current candidate explicitly attest every decision declared mutable
> for this run, and did it use only a fallback allowed by both the Task Hook and
> the current run?

- Stable protocol, schema, interface, and safety bounds belong in `invariants`.
- Asset paths, model/version choices, tenant or product inputs, strategy values,
  and other run-varying decisions belong in `mutable_bindings`.
- A command Candidate Provider returns `3can.candidate/v1` with an opaque
  candidate fingerprint, the SHA-256 of every current binding value, and all
  fallbacks actually used.
- A missing binding is `IMPLICIT_MUTABLE_BINDING`.
- A used but unapproved fallback is `FALLBACK_NOT_ALLOWED`.
- A constant is not a violation merely because it is a literal.

This catches the important class of failure where implementation silently uses
an old mutable default while QC proves only that the wrong candidate is stable.
It does not claim to discover every semantic hardcode without a project-owned
Candidate Provider or Oracle.

## Candidate Providers

The Task Hook declares exactly how to identify the current deliverable:

- `workspace`: the current repository HEAD, branch, dirty paths, and bounded
  content fingerprints. Use for code tasks without run-specific bindings.
- `artifact`: a small file, either a one-off literal path or a repeatable
  `path_binding`. Files up to 8 MiB are content-hashed.
- `command`: a project adapter that returns a bounded `3can.candidate/v1` JSON
  receipt. Use for multi-file outputs, large media, resolved mutable references,
  or domain-specific lineage manifests.

Large generic artifacts deliberately fail as `unverifiable_large`; size and
mtime are not proof because same-size content can restore an old timestamp.
Use a content-addressed manifest or command provider instead. The Global Hook
does not add media-specific hashing logic.

## Proof Receipts

Every generated proof binds:

- Task Hook revision and canonical digest;
- run ID and bindings digest;
- current candidate fingerprint;
- Acceptance criterion IDs;
- evaluator ID, version, and kind;
- typed status, reason, and content-addressed evidence references.

An `external_receipt` Oracle reads a bounded `3can.proof-receipt/v1` file. A
passing external receipt must retain its own canonical digest and at least one
`sha256:` evidence reference. Its file fingerprint is also part of convergence
freshness: modification, deletion, replacement, or a read-time race makes the
old convergence receipt stale or `CONFLICT`.

These are unsigned local bookkeeping receipts, not in-toto attestations. If the
threat model includes a hostile actor with the same filesystem write access,
use a separately authenticated signer and verifier; that is intentionally not
part of this lightweight hook.

## Verification and race boundary

One verification performs:

```text
load pinned Task Hook
capture contract + workspace + current candidate
run applicable Oracles
validate proof byproducts
recapture contract + workspace + candidate + proof sources
changed => CONFLICT
stable => atomically write convergence receipt
```

It uses no lock, daemon, database, or transaction manager. This protects normal
local concurrency and accidental drift, not hostile mutate-and-revert attacks.
Stop and declared guards always recompute current identity. Old evidence cannot
prove a changed Task Hook, binding, candidate, artifact, or external proof.

Run an episode gate after one evidence-bearing module:

```powershell
python scripts\3can_convergence.py verify `
  --stage episode `
  --next-objective "Implement the next bounded acceptance gap."
```

Run final Oracles only for a genuine candidate:

```powershell
python scripts\3can_convergence.py verify `
  --stage final `
  --next-objective "Await Owner review."
```

## Revisions and lifecycle

Changing implementation does not require a Task Hook revision; it changes the
candidate and therefore requires fresh evidence. Changing Goal, Acceptance,
Oracle meaning/version, mutable-binding policy, or Candidate Provider creates a
new content digest and normally a new revision.

An active digest is pinned by `.codex/convergence.json`. In-place edits make the
run `REVISION_PENDING`; a `PROPOSED_REVISION` cannot execute. After review, the
new exact digest receives a new activation and all old candidate proofs are
ineligible. A superseded file retains a confirmed transition and successor
revision.

Lifecycle states are:

```text
one-off:    EPHEMERAL_ACTIVE -> RETIRED
repeatable: EPHEMERAL_ACTIVE -> REUSABLE_CANDIDATE -> REUSABLE_ACTIVE
revision:   ACTIVE -> PROPOSED_REVISION -> ACTIVE; old revision -> SUPERSEDED
```

A one-off closeout must retain the final `CONVERGED` receipt and link the
`RETIRED` transition to the previously active Task Hook digest. Deleting the
contract is not retirement.

The first repeatable success may become `REUSABLE_CANDIDATE`. Default promotion
to `REUSABLE_ACTIVE` requires two distinct run IDs and two distinct
candidate/binding subjects, content-addressed qualifying receipts, plus Owner
or independent-review confirmation. Owner fast-track is explicit and may use
one receipt. Duplicate replay never increases the count.

Reusable executable fields contain parameter names, not a previous run's path,
asset, URL, product, or prompt. Historical promotion evidence may reference the
qualifying run receipts. Each future worktree selects the tracked Task Hook by
exact digest and supplies a new run ID and bindings. The same native Hook loads
that active selector at `SessionStart`; there is no thread-ID binding.

Semantic matching of an arbitrary user prompt to a task family remains an
explicit task-start decision. The offline Global Hook enforces the selected
contract; it does not pretend to infer applicability without a prompt
classifier or add a per-prompt LLM hot path.

## Native events and bounded behavior

- `SessionStart` for `startup`, `resume`, and `compact` injects only Goal,
  current Task Hook revision/status, short Acceptance text, current candidate
  identity, open criteria, current/stale receipt state, critical non-goals, and
  next objective. It never injects full history.
- Unmatched `PreToolUse` returns before scanning Git. Matching project-declared
  high-cost guards require current proof-eligible named checks.
- `Stop` accepts only a current `CONVERGED` receipt. The first incomplete stop
  requests one bounded continuation; when `stop_hook_active=true`, it emits an
  explicit typed report instead of creating a loop.

Development-path hook failures are fail-open so safe local work continues.
Stop failures never fabricate convergence. Contract missing plus prior receipt
is `UNAVAILABLE`; contract missing plus receipt missing is the opt-out no-op.

## Outcomes

- `PASS`: episode evidence passed; work continues.
- `FAIL`: an automated Oracle failed.
- `CANDIDATE_READY`: automated evidence passed but bound human/Owner evidence
  remains pending.
- `CONVERGED`: every bound criterion passed for the current task/candidate.
- `MISSING`, `PARTIAL`, `CONTRADICTS`, `UNREQUESTED`, `STALE_EVIDENCE`,
  `UNBOUND`, `IMPLICIT_MUTABLE_BINDING`, `FALLBACK_NOT_ALLOWED`,
  `UNVERIFIABLE`, `REVISION_PENDING`, `BLOCKED`, `UNAVAILABLE`, and `CONFLICT`
  are explicit non-success states.

Manual incomplete reporting remains available:

```powershell
python scripts\3can_convergence.py record `
  --status BLOCKED `
  --reason "Owner must choose between incompatible acceptance paths." `
  --next-objective "Wait for the scoped Owner decision."
```

## Compatibility and scope

`3can.convergence-contract/v1` remains supported for simple project-local
contracts. New task-specific work should use v2. Receipt schema v2 adds the
content digest and Task Hook/candidate identity; older receipts become stale
and must be regenerated.

Scope remains `current_repository_only`. Dirty submodules are rejected rather
than silently treated as covered. Use a separate contract inside the owning
repository; this Hook is not a cross-repository orchestrator.

## Design sources

The implementation borrows structures, not runtimes:

- [OpenAI Codex Hooks](https://developers.openai.com/codex/hooks): native event
  and bounded Stop semantics.
- [GitHub Spec Kit](https://github.com/github/spec-kit): separation of intent,
  requirements, implementation, and consistency review.
- [Inspect AI tasks and scorers](https://inspect.aisi.org.uk/tasks.html): task,
  execution, and evaluator separation with versioned parameters.
- [in-toto Attestation validation](https://github.com/in-toto/attestation/blob/main/docs/validation.md)
  and [SLSA provenance](https://slsa.dev/spec/v1.2/build-provenance): subject,
  input, evaluator, and current-digest binding. No signing subsystem is added.
- [Hypothesis stateful testing](https://hypothesis.readthedocs.io/en/latest/stateful.html):
  lifecycle invariants are exercised in tests, not added to runtime.

Controller platforms, evaluation frameworks, LLM judges, queues, and a second
runtime were rejected because the standard library plus tracked JSON and one
atomic receipt satisfies this contract with fewer moving parts.
