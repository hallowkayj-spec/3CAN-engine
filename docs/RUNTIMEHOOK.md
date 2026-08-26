# 3CAN RuntimeHook

RuntimeHook is a thin semantic supervisor for Codex tasks. It remembers the
current Owner Intent across resume/clear/compaction and prompts an Agent to
review four general failure modes: goal drift, unjustified hardcoding, hidden
fallback or stale state, and unrequested behavior.

The Plugin also emits one bounded 3CAN fast-path orientation at `SessionStart`,
including outside Git repositories. That orientation tells a Session to start
safe local work immediately, use fresh ticket context only just in time for a
ticket-governed operation, and write durable meaning only at a real checkpoint.
It does not activate RuntimeHook or contact 9700.

It is not a second Task Oracle. RuntimeHook owns one ignored file only:

```text
.codex/runtimehook/state.json
```

That file contains enabled/disabled state, an activation ID, RUN_INTENT,
Agent-selected internal intensity, an optional current episode, the latest
semantic review result/reference, and one bounded review-boundary epoch. The
epoch retains only its sequence, reviewed sequence, last generic kind/label,
and observed Git HEAD; it is not an event log. A final `PASS` also records the
clean Git HEAD that was reviewed. These narrow anchors answer only whether a
new review is due and whether the reviewed code is still current; they are not
artifact hashes, candidate fingerprints, proof receipts, or a history ledger.
RuntimeHook does not own a convergence selector, binding policy, or project
evidence decision. Git remains engineering truth; the existing
`3can_convergence.py` and Task Oracle remain the evidence kernel.

The state is scoped to one physical Git worktree, not to one chat. At most one
current RuntimeHook task may use that worktree; concurrent tasks require
separate worktrees so one task cannot replace another task's Intent.

## Use

The user need not choose a profile or run a controller command. Once the Plugin
is installed and its exact Hook definition is trusted, Codex may invoke
`$3can-runtimehook` implicitly for a long, multi-stage, drift-prone, or
explicitly requested task. Natural requests such as
`这个任务按 RuntimeHook 执行` and `关闭 RuntimeHook` are supported.

Codex has a fixed built-in slash-command set. RuntimeHook does not register or
simulate `/3CAN`; it is only product shorthand. The real explicit route is
`$3can-runtimehook` or `/skills`. See the official
[Codex skills](https://learn.chatgpt.com/docs/build-skills),
[developer commands](https://learn.chatgpt.com/docs/developer-commands), and
[hooks](https://learn.chatgpt.com/docs/hooks) documentation.

The SessionStart orientation follows the repository [3CAN steering
contract](../3CAN.md): Git owns exact source state; 3CAN supplies durable
project meaning and relevant history. Read-only and safe local work needs no
ticket. A ticket is requested and consumed immediately before the operation
whose current project contract requires it, with the current Agent, project,
workspace/worktree, Workorder, target, and scope bindings. The Agent honors the
returned TTL and completion deadline and never blind-retries an expired,
conflicting, or mismatched request.

The Agent chooses the lightest useful review depth semantically:

- `light`: a concise Goal/Acceptance check at each observed boundary;
- `medium`: inspect completed output and relevant diff at each boundary;
- `max`: the same semantic reviews plus an existing targeted strict Oracle for
  only the criteria that need mechanical proof.

These are internal audit values, not user modes. RuntimeHook contains no
duration, file-count, domain, command-name, or Oracle-name classifier.

## Independence and OFF behavior

The Plugin bundles RuntimeHook only. Existing project-owned convergence,
credential, security, deployment, or publication gates remain independent and
all matching native Hooks continue to run. With no enabled RuntimeHook state,
only `SessionStart` emits the stateless 3CAN fast path; other RuntimeHook events
remain silent. When enabled, `SessionStart` emits that fast path plus RUN_INTENT,
and `UserPromptSubmit` reinjects RUN_INTENT. A Git HEAD
change observed after `Bash`, or an `update_plan` call containing a completed
step, advances one review boundary without parsing commands or project domains.
For a semantic stage/episode with neither signal, the Agent uses the same
generic `checkpoint --kind stage|episode --label ...` command. Multiple events
before review coalesce into one current debt; no history is retained.

Stop requests at most one native continuation while final semantic review is
due or stale. This owns semantic review timing only; it does not allow, deny, or
replace an independent evidence, credential, deployment, or publication gate.
On the already-continued Stop pass it reports the typed state instead of
looping. Recording final `PASS` requires a clean Git checkpoint. It is silent
only while HEAD still matches that checkpoint and the worktree remains clean;
later changes produce semantic-review debt. SessionStart uses the same currentness check and
reinjects the review as `STALE`, never as the persisted `PASS`, after either
change. SessionStart uses native
`hookSpecificOutput.additionalContext` with a bounded matching handler limit, so
the context reaches the model rather than only the UI event stream.

`off` changes the retained state to `disabled_by_owner`; later semantic
RuntimeHook reminders are silent while the stateless SessionStart orientation
remains available. It does not delete evidence and cannot disable credentials,
deployment, publication, security, or the independent PR15 convergence gate.

## Install and remove

RuntimeHook is distributed as the repository Plugin at
`plugins/3can-runtimehook` and is exposed by
`.agents/plugins/marketplace.json`. It requires Git and Python 3, but no 3CAN
Runtime, graph, credentials, network service, or 9700 restart.

Add the public repository as a Codex marketplace and install the Plugin:

```text
codex plugin marketplace add hallowkayj-spec/3CAN-engine --ref main
```

Restart the ChatGPT desktop app, open the Plugins Directory, choose the
`3CAN Engine` marketplace source, and install `3CAN RuntimeHook`. In Codex CLI,
open `/plugins` and install it from the configured marketplace, then start a new
session. Review the exact bundled Hook definition and trust it before use; in
Codex CLI, `/hooks` is the native inspection and trust surface. Installation
does not activate a task or create state. It only makes the bounded 3CAN
SessionStart orientation automatic; semantic supervision starts when Codex
selects the Skill or the Owner asks for RuntimeHook.

The first activation in a Git repository adds only
`/.codex/runtimehook/` to that repository's local Git exclude when an existing
ignore does not already cover it. This keeps state untracked without editing
`.gitignore` or any project source file. A tracked, redirected, or unsafe state
root remains typed `UNAVAILABLE`.

To update, upgrade the `3can-engine` marketplace and reinstall the Plugin from
that source. To remove it, first say `关闭 RuntimeHook` in active worktrees, then
disable or uninstall it through the Plugins browser. Uninstalling deliberately
does not delete retained project-local state or rewrite a repository's Git
exclude file.

The legacy project-kit copy remains a compatibility and clean-clone test fixture;
new users do not need to copy it into each repository. Distribution is governed
by the repository's PolyForm Noncommercial license. It is public
source-available software, not an OSI-approved open-source license.

## Short smoke

From a disposable Git worktree in a source checkout, the internal command path
is:

```powershell
$root = (git rev-parse --show-toplevel).Trim()
$cli = Join-Path $root 'plugins\3can-runtimehook\skills\3can-runtimehook\scripts\3can_runtimehook.py'
python $cli --root $root on --goal 'Deliver the requested bounded result.' --acceptance 'A01=The requested result is complete.' --intensity light --reason 'Small and clear.'
python $cli --root $root checkpoint --kind episode --label 'Implementation completed' --next-objective 'Review the result.'
python $cli --root $root review --stage final --result PASS --reference 'git:reviewed-commit'
python $cli --root $root off
```

This smoke tests semantic state only. Existing convergence tests separately
prove candidate freshness and Stop behavior.
