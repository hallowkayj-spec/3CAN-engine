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
   Keep this per-run selector ignored; track the Task Hook itself. A repeatable
   Task Hook must be committed before activation.
4. Keep `test-results/3can/` ignored. Review `.codex/hooks.json`, enable Codex
   Hooks, and use `/hooks` to review and trust the exact native definitions.
   The default file installs convergence only at `SessionStart` and `Stop`; it
   does not launch a convergence process for every tool call.

Do not activate the unchanged example and do not globally enable it as a side
effect of installation. Missing contract plus missing receipt is a no-op, so
ordinary safe development has no added ceremony.

The default file deliberately starts no convergence process for ordinary tool
calls. A project with a measured high-cost operation may explicitly add the
existing convergence handler under a narrow `PreToolUse` matcher for its
declared guard. No first-write gate is shipped. The proposed FAST/EPISODIC
reduction and its unresolved generic-shell boundary are documented separately
in [`ADAPTIVE_REVIEW_HARNESS.md`](./ADAPTIVE_REVIEW_HARNESS.md).

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

Unknown fields fail closed. Add a versioned schema field only when an implemented
consumer, tests, and migration need it; inert extension bags are rejected.

## Hardcoding association rule

The hook does not grep for constants and contains no `if video`, `if seo`, or
other domain classifier. It asks a narrower, enforceable question:

> Did the current candidate explicitly attest every decision declared mutable
> for this run, and did it use only a fallback allowed by both the Task Hook and
> the current run?

- Stable protocol, schema, interface, and safety bounds belong in `invariants`.
- Asset paths, model/version choices, tenant or product inputs, strategy values,
  and other run-varying decisions belong in `mutable_bindings`.
- A command Candidate Provider declares `consumes_bindings`. The helper passes
  only those values through `THREECAN_TASK_BINDINGS_JSON`; a repeatable provider
  must consume every mutable binding. Its `argv` is exactly the launcher,
  optional explicitly declared `invariant_argv`, and versioned adapter. Fixed
  interpreter flags such as `-I` belong in `invariant_argv`; all run
  configuration, including short, numeric, stale, or current defaults, enters
  through bindings rather than extra arguments. The invariant declaration is
  an Owner-reviewed governance assertion, not proof that a disguised run value
  is genuinely stable.
- The provider returns `3can.candidate/v1` with an opaque
  candidate fingerprint, the SHA-256 of every current binding value, and all
  fallbacks actually used.
- A missing binding is `IMPLICIT_MUTABLE_BINDING`.
- A used but unapproved fallback is `FALLBACK_NOT_ALLOWED`.
- A constant is not a violation merely because it is a literal.

This catches the important class of failure where implementation silently uses
an old mutable default while QC proves only that the wrong candidate is stable.
The adapter remains project-owned code: its receipt is not proof that its own
implementation is honest. When artifact lineage matters, bind a separate
relational or semantic Oracle that compares the resolved bindings, candidate
manifest, and actual output. The Global Hook does not claim to infer this
domain relation from arbitrary source code.

A command Candidate Provider is expected to be synchronous and read-only. The
stable capture detects workspace, declared evidence, and passive artifact
changes completed before that provider exits. It cannot sandbox a deliberately
detached child that writes after return; project review must reject such an
adapter instead of adding a second process supervisor.

For a high-risk or long task, add a separate requirement/result consistency
Oracle to that Task Hook (not a second native lifecycle Hook). It compares the
current Goal and Acceptance IDs with the Git diff, resolved run bindings, actual
Candidate, and runtime/artifact evidence, then emits one bound
`3can.proof-receipt/v1`. Its typed result should distinguish `CONTRADICTS`,
`UNREQUESTED`, `IMPLICIT_MUTABLE_BINDING`, `FALLBACK_NOT_ALLOWED`, and
`STALE_EVIDENCE`. A semantic reviewer must declare `independent` or
`same_agent`; the latter remains visible as a review-independence downgrade.

## Candidate Providers

The Task Hook declares exactly how to identify the current deliverable:

- `workspace`: the current branch, tracked content index, dirty paths, and
  bounded content fingerprints. For v2, the per-run selector, selected Task
  Hook, and retained promotion receipts are a separately hashed control plane
  and are excluded from the deliverable fingerprint. This permits an honest
  lifecycle transition without changing the candidate it closes out.
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
- evaluator ID, version, kind, and declared independence (`independent`,
  `same_agent`, `owner`, or `not_applicable` as appropriate);
- typed status, reason, and content-addressed evidence references.

An `external_receipt` Oracle reads a small, bounded `3can.proof-receipt/v1` file
(at most 8 KiB). Large evaluator payloads stay outside the receipt and are
represented by `sha256:` evidence references. A passing external receipt must
retain its own canonical digest and at least one such reference. Its file
fingerprint is also part of convergence
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
Stop and declared guards always recompute current identity. Artifact byproducts
and external receipts are fingerprinted again after their check, so a post-check
replacement is `CONFLICT`. Old evidence cannot prove a changed Task Hook,
binding, candidate, artifact, or external proof. Immediately before a
`CONVERGED` Stop is allowed, the Hook reloads the selector, Task Hook, receipt,
workspace, candidate, and evidence once more; a control-plane change blocks the
stop as stale.

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

An active digest is pinned by `.codex/convergence.json`. Re-pinning and
re-confirming changed Goal, Acceptance, Candidate, or Oracle semantics under the
same evidenced revision is still `REVISION_PENDING`; a successor must name the
evidenced parent revision. Owner-controlled meaning changes require an Owner
activation assertion. A `PROPOSED_REVISION` cannot execute. After review, the
new exact digest receives a new activation and all old candidate proofs are
ineligible. A superseded file retains a confirmed transition and successor
revision.

For repeatable Task Hooks this rule also survives a missing prior run receipt:
the current family, revision, and semantic digest must first exist in Git
`HEAD`, then the Hook compares that digest with bounded tracked history across
all direct Task Hook JSON files (up to 64 file-touching commits and 4 MiB).
An untracked Task Hook cannot borrow an unrelated file's history. A
same-revision mismatch is `REVISION_PENDING`; a history beyond that audit bound
requires a successor revision or a deliberate offline audit. Reusable registry
selection additionally requires the registry and selected active Task Hook to
match their exact Git `HEAD` JSON values.

Lifecycle states are:

```text
one-off:    EPHEMERAL_ACTIVE -> RETIRED
repeatable: EPHEMERAL_ACTIVE -> REUSABLE_CANDIDATE -> REUSABLE_ACTIVE
revision:   ACTIVE -> PROPOSED_REVISION -> ACTIVE; old revision -> SUPERSEDED
```

A one-off closeout must retain the final `CONVERGED` receipt and link the
`RETIRED` transition to the previously active Task Hook digest. Closeout
recomputes the current workspace, candidate, bindings, and evidence twice and
closes with another control read; changing a deliverable after the final
receipt or during closeout cannot be laundered by retiring the hook. Deleting
the contract is not retirement.

The first repeatable success may become `REUSABLE_CANDIDATE`. Default promotion
to `REUSABLE_ACTIVE` requires two distinct run IDs and two distinct
candidate/binding subjects, plus Owner or independent-review confirmation. Each
qualifying reference must resolve to a real, self-digest-valid, proof-eligible
`CONVERGED` receipt retained under `.codex/task-hooks/evidence/`; invented hash
strings and duplicate replay never increase the count. Owner fast-track is
explicit and may use one retained receipt.

After promotion, a later successful run may close with
`disposition=reusable_active` while leaving the exact active Task Hook in the
registry. Its current final receipt must match that active digest, and the
promotion receipts must still be readable and valid.

Reusable executable fields contain parameter names, not a previous run's path,
asset, URL, product, or prompt. Historical promotion evidence may reference the
qualifying run receipts. Each future worktree selects the tracked Task Hook by
exact digest and supplies a new run ID and bindings. A tracked registry contains
only exact `REUSABLE_ACTIVE` entries. Select one deterministically in a later
worktree without hand-editing the selector:

```powershell
python scripts\3can_convergence.py select-task `
  --registry .codex/task-hooks/registry.json `
  --task-family generic-delivery `
  --run-id run-20260824-001 `
  --confirmed-by owner `
  --confirmation-ref owner-selected-family `
  --binding 'candidate_path="outputs/current.bin"'
```

Native selection exact-HEAD validates the registry structure and only opens,
checks lineage for, and activates the requested family. This keeps a legal
128-family registry inside the 30-second lifecycle budget and does not require
an unrelated family file to be currently available. Registry structural errors
and unreadable committed Task Hook history still fail closed. Run the
deliberately slower complete audit outside a native lifecycle event:

```powershell
python scripts\3can_convergence.py validate-registry `
  --registry .codex/task-hooks/registry.json
```

The selector refuses to overwrite an existing run. Once selected, the same
native Hook automatically loads it at `SessionStart`; there is no thread-ID
binding.

An external launcher can ask the native `SessionStart` hook to materialize the
same selector without a separate CLI step. It must supply the exact registered
family and per-run intent through `THREECAN_TASK_FAMILY`, `THREECAN_RUN_ID`,
`THREECAN_TASK_CONFIRMED_BY`, `THREECAN_TASK_CONFIRMATION_REF`, and optional
`THREECAN_TASK_BINDINGS_JSON`, `THREECAN_ALLOWED_FALLBACKS_JSON`, and
`THREECAN_TASK_REGISTRY`. This path runs only when both selector and prior
receipt are absent, and it refuses to replace an existing run. These values are
explicit audit assertions from the launcher, not prompt classification or
authentication.

Semantic matching of an arbitrary user prompt to a task family remains an
explicit task-start decision. The offline Global Hook enforces the selected
contract; it does not pretend to infer applicability without a prompt
classifier or add a per-prompt LLM hot path.

## Native events and bounded behavior

- `SessionStart` for `startup`, `resume`, and `compact` injects only Goal,
  current Task Hook revision/status, short Acceptance text, current candidate
  identity, open criteria, current/stale receipt state, critical non-goals, and
  next objective. It never injects full history.
- The default kit starts no convergence `PreToolUse` process. A project may
  explicitly configure the existing handler under a narrow matcher for a real
  declared high-cost guard; matching guards require current proof-eligible
  named checks.
- `Stop` accepts only a current `CONVERGED` receipt. The first incomplete stop
  requests one bounded continuation; when `stop_hook_active=true`, it emits an
  explicit typed report instead of creating a loop. Before allowing a success
  stop and closeout, every `verify`/`status`/`record`/Stop/closeout Candidate
  Provider call is bracketed by workspace, evidence, and passive
  artifact-candidate fingerprints and followed by another control-plane read.
  Stop and closeout then compare a second complete capture before one final
  contract/receipt read. A provider side effect or concurrent save becomes
  stale, `CONFLICT`, or `REVISION_PENDING` instead of returning a false success.

Native convergence handlers have a 30-second outer budget. Their hot path runs
no Oracle suite; Git probes and Candidate Provider subprocesses are individually
bounded to 3 seconds. Contracts are capped at 16 Oracles, 32 Acceptance
criteria, and 16 promotion receipts; external proof receipts are capped at
8 KiB, while contract and generated/current receipt control JSON are each
capped at 256 KiB both before read and before write. Registry size, Git
metadata, lineage, changed-file count/bytes, and evidence bytes also have named
aggregate bounds. Long checks run only through explicit `verify` commands.
If a generated receipt would exceed its read bound, the writer atomically stores
a small current `UNAVAILABLE` receipt instead; it never leaves an older
`CONVERGED` receipt authoritative.

The `verify` CLI exits zero only for `PASS` at the episode stage or `CONVERGED`
at the final stage. Every typed incomplete, stale, conflicting, or
candidate-only outcome exits nonzero while retaining its JSON receipt.

## Efficiency is a separate rollout gate

Quality convergence and Harness efficiency are separate axes. Command checks
retain their own `elapsed_ms`, but the Hook cannot know a truthful no-Hook task
baseline. It therefore does not invent a universal threshold or convert a
quality `CONVERGED` result into an efficiency claim.

Dogfood reports only three operational metrics:

1. Escape rate: required problems that the Hook failed to stop.
2. False-block rate: correct work that the Hook stopped.
3. Harness tax: `(hooked task time - comparable baseline time) / comparable
   baseline time`.

Initial development, review, CI, and infrastructure engineering hours are
reported separately from another Session's business-task Harness tax. Measure
the latter on paired, comparable real tasks, including one 10--20 minute task,
before any wider enablement. Native process latency may be benchmarked with the
exact trusted hook commands, but it is only one component of total task tax.
See [`CONVERGENCE_HOOK_EFFICIENCY_20260824.md`](./evidence/CONVERGENCE_HOOK_EFFICIENCY_20260824.md)
for the current typed evidence and
[`ADAPTIVE_REVIEW_HARNESS.md`](./ADAPTIVE_REVIEW_HARNESS.md) for the design-only
Lite experiment boundary.

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
