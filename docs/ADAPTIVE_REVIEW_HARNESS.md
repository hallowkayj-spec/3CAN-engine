# Intent-Preserving Adaptive Review Harness

Status: `ISOLATED_SEMANTIC_SUPERVISOR / DOGFOOD_PENDING / GLOBAL_ROLLOUT_BLOCKED`

Strict evidence reference: Draft PR #15; Git is authoritative for its current
review head.

## First-principles split

The harness has three owners and no fourth one:

```text
RuntimeHook = semantic intent and review timing
PR15 convergence / Task Oracle = mechanical evidence and Stop correctness
Git = exact engineering truth
```

RuntimeHook exists because a long model context can compress or drift. It must
therefore restore a concise Owner Intent and trigger semantic review, but it
must not reproduce the evidence kernel merely to prove that it restored the
Intent correctly.

## Minimal RuntimeHook state

RuntimeHook writes one Git-ignored project-local file under
`.codex/runtimehook/`. Its current state contains only:

- enabled or `disabled_by_owner`;
- a unique activation ID;
- RUN_INTENT Goal, stable Acceptance IDs, and explicit non-goals;
- the Agent-selected internal intensity and reason;
- an optional current episode objective;
- the latest semantic review result and durable reference;
- for final `PASS` only, the clean Git HEAD reviewed for semantic currentness.

It is replaceable current-task state, not a history database. Git commits, PR
reviews, project receipts, and artifact manifests retain durable evidence.
The final-review HEAD is not a candidate or artifact fingerprint: Stop only
reminds when HEAD changes or the worktree becomes dirty after semantic review.

## Semantic review contract

At a selected review boundary, the Agent compares the actual request, current
diff, tests, and delivered result. It answers four general questions:

1. Did the result drift from Goal or any Acceptance criterion?
2. Was a decision hardcoded without a traceable requirement, declared contract,
   or unavoidable platform constraint?
3. Is a hidden fallback, stale state, or superseded artifact being treated as
   current truth?
4. Was unrequested behavior added, or requested behavior silently omitted?

This is semantic judgment, not a keyword scanner. The implementation contains
no domain list, duration threshold, file-count rule, command-name denylist, or
Oracle-name classifier.

The result is one of `PASS`, `PARTIAL`, `FAIL`, `UNVERIFIABLE`, `CONTRADICTS`,
or `UNREQUESTED`, plus a durable reference. RuntimeHook does not invent another
proof format; the reference points to the existing Git, PR, test, artifact, or
project evidence surface.

## Internal review timing

The user does not select a profile. The Agent records the lightest sufficient
timing by task meaning:

- `light`: final semantic review only;
- `medium`: meaningful episode reviews plus final review;
- `max`: medium timing plus an existing targeted strict Oracle for only the
  criterion that needs mechanical proof.

Uncertainty between light and medium resolves to medium. `max` never means
enabling every strict check. If the project has no suitable strict Oracle, the
Agent reports the criterion `UNVERIFIABLE`; RuntimeHook does not grow one.

## Explicit non-ownership

RuntimeHook does not own or wrap:

- `.codex/convergence.json` selector lifecycle;
- Candidate Provider or candidate freshness;
- proof/receipt freshness or history;
- mutable bindings or fallback enforcement;
- credential, deployment, publication, or security gates;
- Stop allow/block decisions;
- 3CAN Runtime, graph, network, database, daemon, or per-tool LLM calls.

The native RuntimeHook and convergence hooks run independently. RuntimeHook OFF
silences only semantic reinjection; it cannot bypass another gate.

## Dogfood acceptance

Before wider installation, run three isolated paths:

1. no RuntimeHook state: normal work gets no semantic output;
2. implicit or natural-language activation: RUN_INTENT survives
   resume/clear/compaction and a semantic review reference is recorded;
3. Owner off: state remains `disabled_by_owner`, RuntimeHook becomes silent,
   and an independent convergence gate remains unchanged.

Then freeze the architecture and dogfood exactly three real tasks:

1. a small code task explicitly requested with RuntimeHook, observing automatic
   `light` timing, overhead, and final-review value;
2. a one-to-two-hour cross-module task, observing automatic `medium` timing and
   whether episodes reduce drift without adding ceremony;
3. a video or artifact workflow where RuntimeHook owns semantics and a known
   lineage criterion calls the existing targeted strict Oracle.

For each, record only total time, RuntimeHook overhead, review count, valuable
findings, false reminders, manual intervention, and whether goal drift or bad
hardcoding escaped. Foundation development time remains separate. Active-Intent
replacement and real natural-language implicit activation stay P2 dogfood
questions; do not add lifecycle machinery or synthetic tests for them.

No broad rollout is implied by local tests. A rollout decision requires owner
review plus evidence that the semantic layer reduces real mistakes without
turning ordinary development into ceremony.
