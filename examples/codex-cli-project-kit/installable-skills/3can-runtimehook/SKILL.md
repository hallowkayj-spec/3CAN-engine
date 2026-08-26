---
name: 3can-runtimehook
description: Keep Owner Intent and semantic review timing stable across long, multi-stage, cross-module, drift-prone Codex work. Use implicitly when this lightweight supervision materially helps, or when the user says RuntimeHook, 开启 RuntimeHook, 按 RuntimeHook 执行, 这个任务用 RuntimeHook, /3CAN, or asks to turn it off. Reviews goal drift, unjustified hardcoding, hidden fallback or stale state, and unrequested behavior without replacing Git or project evidence gates.
---

# 3CAN RuntimeHook

RuntimeHook is a semantic supervisor, not an evidence kernel. Apply it without
asking the user to choose a mode when the task is long, multi-stage,
cross-module, historically drift-prone, or explicitly requests RuntimeHook. Do
not invoke it for every small edit merely because it is installed.

## Locate and inspect

Resolve the physical root with `git rev-parse --show-toplevel`. Use the absolute
`<root>/scripts/3can_runtimehook.py` path and pass that same root through
`--root`. If the script or native project hooks are absent, report
`UNAVAILABLE`; do not create a daemon, parser, graph call, global state store, or
replacement Hook.

RuntimeHook state is current-task state for one physical Git worktree, not
per-chat state. Never run concurrent RuntimeHook tasks in the same worktree;
use a separate worktree for each concurrent task instead of adding Session
ownership or another state machine.

Run `status`. If the active RUN_INTENT still matches the current Owner task,
reuse it. If this is a materially new task or Intent, activate a new state with
`on`; current semantic state is replaceable and Git/PR artifacts retain durable
engineering history.

## Activate without ceremony

Derive one concise Goal and stable `ID=observable text` Acceptance list from the
Owner request. Keep implementation choices out of Acceptance unless the Owner
actually required them.

Choose and record the lightest useful review depth by semantic judgment:

- `light`: a short Goal/Acceptance check at each observed boundary;
- `medium`: inspect the completed episode output and relevant diff at each boundary;
- `max`: only when a named criterion needs an existing project-owned targeted
  strict Oracle in addition to semantic review.

When uncertain between light and medium, use medium. Do not classify by elapsed
minutes, file count, domain name, command string, or Oracle name. Do not ask the
user to choose the intensity. Run `on` with Goal, repeated Acceptance, selected
intensity, and a short semantic reason. Then work normally—understand, edit,
test, Git checkpoint, PR or delivery. Native Hooks observe Git HEAD changes,
completed `update_plan` stages, and the next Owner prompt after a reviewed
conversation episode; do not parse command text or add per-tool calls.

## Review semantically

Every observed Git, completed-plan, or new Owner-prompt boundary creates one
coalesced semantic review debt and immediately reinjects RUN_INTENT. A new
prompt closes the previously reviewed conversation episode automatically. When
a meaningful internal stage completes without one of those signals, run one
generic boundary:

```text
checkpoint --kind stage|episode --label "what completed" --next-objective "what is next"
```

Do not duplicate a boundary already observed through Git or `update_plan`, and
do not encode domain names, S-number lists, file paths, or command patterns in
the controller. At each debt and at the final boundary, inspect the actual
request, current diff or output, tests, and delivered result. Review:

1. Does the result still satisfy the Goal and every Acceptance criterion?
2. Is any decision hardcoded without a traceable Owner requirement, declared
   contract, or unavoidable platform constraint?
3. Is a hidden fallback, stale state, or superseded artifact being treated as
   current truth?
4. Was unrequested behavior added, or requested behavior silently dropped?

Record an honest result (`PASS`, `PARTIAL`, `FAIL`, `UNVERIFIABLE`,
`CONTRADICTS`, or `UNREQUESTED`) and a durable reference with `review`. For an
episode review, include the next bounded objective. A reference may be a Git
commit, PR review, or project evidence path; do not create a duplicate proof
format. Before recording final `PASS`, create the task's normal Git checkpoint
and verify the worktree is clean. RuntimeHook records that HEAD only to make the
semantic review stale after a later commit or dirty edit; it does not fingerprint
the candidate or artifact.

`SessionStart` reasserts current Intent. `UserPromptSubmit` also creates at most
one coalesced episode debt when the prior boundary was reviewed. If review debt
is still due at `Stop`, the native Hook requests one continuation so the Agent
must review or report an honest typed non-success state; it does not loop
forever or override an independent project gate.

If a criterion needs mechanical proof, use the project's existing convergence
or Task Oracle path separately and reference that result. RuntimeHook never
creates or owns its selector, candidate freshness, receipt lifecycle, bindings,
fallback policy, history ledger, or project evidence Stop decision.

## Disable only this layer

When the Owner says `关闭 RuntimeHook`, `这个任务不用 RuntimeHook`, or equivalent,
run `off`. This retains the current local semantic state and makes subsequent
RuntimeHook hooks silent. It does not disable independent credentials,
deployment, publication, security, or PR15 convergence gates.

## Real invocation boundary

The explicit Codex route is `$3can-runtimehook` (or `/skills`), and natural
language may invoke it implicitly. `/3CAN` is product shorthand only; Codex does
not currently expose a reliable custom slash-command registration path, so do
not implement a slash parser.
