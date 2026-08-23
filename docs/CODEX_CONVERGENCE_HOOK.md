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
- 3CAN receives durable meaning only at `AUTO_CLOSEOUT` or an explicit
  `OWNER_REQUESTED` checkpoint. Hook execution never performs that writeback.
- `CANDIDATE_READY` is not Owner acceptance, merge, deployment, or publication.

Missing configuration is a no-op. Invalid configuration or hook I/O fails open
with a typed `UNAVAILABLE` message so safe local work can continue.

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
  "goal": "Deliver one independently reviewable module.",
  "acceptance": [
    "The focused test suite passes.",
    "The produced artifact has verified lineage."
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
create friction and should not be used.

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
  objective.
- `FAIL`: at least one automated check failed.
- `CANDIDATE_READY`: automated final checks pass; an Owner review check remains.
- `CONVERGED`: all declared final checks are automated and pass.
- `PARTIAL`, `BLOCKED`, `UNAVAILABLE`, `CONFLICT`: honest non-success terminal
  states for the current turn, recorded with an exact reason.

Record an incomplete state without pretending that it passed:

```powershell
python scripts\3can_convergence.py record `
  --status BLOCKED `
  --reason "Owner must choose between two incompatible acceptance paths." `
  --next-objective "Wait for the scoped Owner decision."
```

`Stop` requests at most one automatic continuation per turn. If evidence is
still missing or stale while `stop_hook_active=true`, the hook allows the stop
and requires an honest `PARTIAL` report instead of creating an infinite loop.

## Native Hook Events

- `SessionStart` with `source=compact`: injects only goal, acceptance, non-goals,
  latest typed receipt, open checks, and next objective.
- `PreToolUse`: evaluates only explicitly declared guards.
- `Stop`: accepts a current terminal receipt, otherwise requests one bounded
  continuation.

The hook deliberately ignores the assistant message and transcript path. Codex
does not guarantee transcript file shape or location as a stable integration
contract.

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
