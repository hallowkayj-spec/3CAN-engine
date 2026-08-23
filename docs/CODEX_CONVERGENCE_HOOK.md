# Codex Convergence Hook

The project kit includes an optional, local convergence hook for long Codex
tasks. It keeps an accepted goal and observable acceptance conditions outside
the model context, re-injects that compact contract immediately after Codex
compaction, and checks current evidence before a task stops or a project-declared
high-cost operation runs.

It is not a second Agent runtime. It does not schedule work, parse Codex session
JSONL, inspect chain-of-thought, call an LLM, call 3CAN, commit Git changes, merge,
deploy, or publish.

## Authority Boundary

- Codex `/goal` owns the thread-level objective and continuation budget.
- `.codex/convergence.json` owns the small project acceptance contract.
- Git owns exact source state; declared commands and artifacts prove behavior.
- The local receipt records evidence for the current Git/workspace fingerprint.
- 3CAN receives durable meaning only after `CONVERGED` makes an
  `AUTO_CLOSEOUT` eligible, or at an explicit
  `OWNER_REQUESTED` checkpoint. Hook execution never performs that writeback.
- `CANDIDATE_READY` is not Owner acceptance, merge, deployment, or publication.

Missing configuration is a no-op because convergence is opt-in. Invalid
configuration or hook I/O fails open during development so safe local work can
continue. At `Stop`, the same failure requests one bounded correction and can
only finish with an explicit `UNAVAILABLE`/`PARTIAL` report, never proved
convergence.

## Enable It Deliberately

1. Copy `.codex/convergence.example.json` to `.codex/convergence.json`.
2. Replace the example goal, acceptance list, checks, and optional guards.
3. Keep the receipt path ignored. The project-kit `.gitignore.template` already
   ignores `test-results/3can/`.
4. Review `.codex/hooks.json`, enable Codex Hooks, and use `/hooks` to review and
   trust the exact definitions. A changed hook definition requires fresh trust.

Do not enable the example contract unchanged. Global machine activation is a
separate Owner decision; this repository only ships the reviewed project-local
configuration.

## Contract

```json
{
  "schema": "3can.convergence-contract/v1",
  "status": "active",
  "scope": "current_repository_only",
  "goal": "Deliver one independently reviewable module.",
  "acceptance": [
    {
      "id": "mechanical-integrity",
      "text": "The focused test suite passes and the result has verified lineage.",
      "evidence": ["focused-tests", "result-artifact"]
    },
    {
      "id": "owner-acceptance",
      "text": "The Owner accepts the final result.",
      "evidence": ["owner-review"]
    }
  ],
  "non_goals": [
    "Do not merge, deploy, or publish."
  ],
  "checks": [
    {
      "id": "focused-tests",
      "type": "command",
      "argv": ["python", "-m", "pytest", "-q", "tests/test_module.py"],
      "stages": ["episode", "final"],
      "timeout_seconds": 120
    },
    {
      "id": "result-artifact",
      "type": "artifact",
      "path": "test-results/module/receipt.json",
      "min_bytes": 1,
      "stages": ["final"]
    },
    {
      "id": "owner-review",
      "type": "owner_review",
      "stages": ["final"]
    }
  ],
  "guards": []
}
```

Every acceptance condition must have a stable ID, human-readable text, and one
or more evidence check IDs. All referenced evidence runs at the final stage.
Unbound or legacy string acceptance is invalid and cannot produce
`CANDIDATE_READY` or `CONVERGED`. The hook verifies coverage and evidence state;
it does not pretend that a mechanical check proves visual quality, product
fitness, or taste. Those conditions must bind to an appropriate review receipt
or `owner_review` check selected by the project.

Commands are argv arrays and run without a shell. Artifact paths must stay
inside the project root. Command output is not persisted; the receipt stores
only byte counts and SHA-256 digests. This reduces accidental credential or
private-payload capture.

`guards` are optional and project-declared. No provider, renderer, deployment,
or domain action is hard-coded into the hook:

```json
{
  "tool_name_glob": "exec_command",
  "input_contains": "replace-with-the-exact-expensive-operation",
  "requires_check_ids": ["focused-tests"]
}
```

A matching `PreToolUse` is denied until the named checks passed in a receipt
whose contract and workspace fingerprints are still current. Broad patterns
create friction and should not be used. This is an efficiency/proof-order guard,
not a security or authorization boundary; equivalent commands can bypass a
substring convention.

## Episode Loop

Run the smallest meaningful checks after an evidence-bearing episode:

```powershell
python scripts\3can_convergence.py verify `
  --stage episode `
  --next-objective "Implement the next bounded acceptance gap."
```

Run final checks only when the module is a genuine candidate:

```powershell
python scripts\3can_convergence.py verify `
  --stage final `
  --next-objective "Await Owner review."
```

Outcomes are intentionally distinct:

- `PASS`: the current episode checks pass; work continues toward the next
  objective. It is evidence, not a Git checkpoint. Before the next destructive
  episode, preserve an accepted episode through the project's normal reviewed
  Git checkpoint.
- `FAIL`: at least one automated check failed.
- `CANDIDATE_READY`: automated final evidence passes, but at least one bound
  acceptance condition (normally Owner review) remains pending. It is not
  eligible for `AUTO_CLOSEOUT`.
- `CONVERGED`: every bound acceptance condition has passing evidence. Only this
  outcome is eligible for `AUTO_CLOSEOUT`; the hook still performs no writeback.
- `PARTIAL`, `BLOCKED`, `UNAVAILABLE`, `CONFLICT`: honest non-success terminal
  states for the current turn, recorded with an exact reason.

Record an incomplete state without pretending that it passed:

```powershell
python scripts\3can_convergence.py record `
  --status BLOCKED `
  --reason "Owner must choose between two incompatible acceptance paths." `
  --next-objective "Wait for the scoped Owner decision."
```

`Stop` requests at most one automatic continuation per turn. Current
`CANDIDATE_READY`, `PARTIAL`, `BLOCKED`, `UNAVAILABLE`, and `CONFLICT` receipts
are never silently accepted: the first Stop is blocked with the exact typed
state and open evidence; the second receives the same developer-system warning
and may stop without creating an infinite loop. The hook does not inspect the
assistant message, so it guarantees the correction signal, not linguistic
compliance through transcript parsing.

## Native Hook Events

- `SessionStart` with `source=compact`: injects only goal, acceptance, non-goals,
  latest typed receipt, open checks, and next objective.
- `PreToolUse`: evaluates only explicitly declared guards.
- `Stop`: silently accepts only current `CONVERGED`; every incomplete or
  candidate state requests one bounded explicit report.

The hook deliberately ignores the assistant message and transcript path. Codex
does not guarantee transcript file shape or location as a stable integration
contract.

The v1 scope is deliberately `current_repository_only`. A dirty Git submodule
is rejected as unsupported rather than treated as covered evidence. Run a
separate convergence contract inside that repository; this hook is not a
cross-repository orchestrator.

Efficiency counters beyond check elapsed time are deliberately deferred. First
dogfood the contract on several real tasks; add a budget only if measured
failures show that a new field changes decisions.

## Why This Shape

The design reuses Codex native lifecycle hooks rather than adding a daemon or
workflow engine. Its convergence vocabulary follows the useful parts of
Spec Kit's missing/partial/contradictory gap model, Autoresearch's bounded
keep/discard experiment loop, and long-horizon harness work that separates
execution from independent evidence. Larger issue-orchestration runtimes such
as Symphony remain appropriate for fleets of isolated issue workspaces, not
for this small per-project completion contract.

Primary references:

- [OpenAI Codex Hooks](https://learn.chatgpt.com/docs/hooks)
- [OpenAI Codex goals](https://learn.chatgpt.com/use-cases/follow-goals)
- [OpenAI long-running work](https://learn.chatgpt.com/docs/long-running-work)
- [OpenAI Symphony specification](https://github.com/openai/symphony/blob/main/SPEC.md)
- [GitHub Spec Kit](https://github.com/github/spec-kit)
- [Karpathy Autoresearch](https://github.com/karpathy/autoresearch)
- [Lost in Compaction](https://arxiv.org/abs/2608.11242)
- [LoopsBench](https://arxiv.org/abs/2608.00267)
- [LongHorizon Harness](https://github.com/AMAP-ML/LongHorizon-Harness)
