# 3CAN RuntimeHook

RuntimeHook is a thin semantic supervisor for Codex tasks. It remembers the
current Owner Intent across resume/clear/compaction and prompts an Agent to
review four general failure modes: goal drift, unjustified hardcoding, hidden
fallback or stale state, and unrequested behavior.

It is not a second Task Oracle. RuntimeHook owns one ignored file only:

```text
.codex/runtimehook/state.json
```

That file contains enabled/disabled state, an activation ID, RUN_INTENT,
Agent-selected internal intensity, an optional current episode, and the latest
semantic review result/reference. A final `PASS` also records the clean Git HEAD
that was reviewed. This narrow anchor answers only whether the reviewed code is
still current; it is not an artifact hash, candidate fingerprint, proof receipt,
or history ledger. RuntimeHook does not own a convergence selector, binding
policy, or Stop decision. Git remains engineering truth; the existing
`3can_convergence.py` and Task Oracle remain the evidence kernel.

The state is scoped to one physical Git worktree, not to one chat. At most one
current RuntimeHook task may use that worktree; concurrent tasks require
separate worktrees so one task cannot replace another task's Intent.

## Use

The user need not choose a profile or run a command. Once the Skill and project
kit are installed, Codex may invoke `$3can-runtimehook` implicitly for a long,
multi-stage, drift-prone, or explicitly requested task. Natural requests such as
`这个任务按 RuntimeHook 执行` and `关闭 RuntimeHook` are supported.

Codex has a fixed built-in slash-command set. This kit does not register or
simulate `/3CAN`; it is only product shorthand. The real explicit route is
`$3can-runtimehook` or `/skills`. See the official
[Codex skills](https://learn.chatgpt.com/docs/build-skills),
[developer commands](https://learn.chatgpt.com/docs/developer-commands), and
[hooks](https://learn.chatgpt.com/docs/hooks) documentation.

The Agent chooses the lightest useful review timing semantically:

- `light`: one final semantic review for a small, clear task;
- `medium`: meaningful episode reviews plus final review for long or cross-module work;
- `max`: the same semantic reviews plus an existing targeted strict Oracle for
  only the criteria that need mechanical proof.

These are internal audit values, not user modes. RuntimeHook contains no
duration, file-count, domain, command-name, or Oracle-name classifier.

## Independence and OFF behavior

The native project hooks run RuntimeHook and `3can_convergence.py` independently.
With no enabled RuntimeHook state, the semantic hook exits silently. When
enabled, SessionStart reinjects RUN_INTENT; Stop emits a non-owning reminder
while final semantic review is due, non-successful, or stale. Recording final
`PASS` requires a clean Git checkpoint. It is silent only while HEAD still
matches that checkpoint and the worktree remains clean; later changes produce a
semantic-review reminder. SessionStart uses native
`hookSpecificOutput.additionalContext` with a bounded matching handler limit, so
the context reaches the model rather than only the UI event stream. RuntimeHook
never returns a Stop allow/block decision.

`off` changes the retained state to `disabled_by_owner`; later RuntimeHook hooks
are silent. It does not delete evidence and cannot disable credentials,
deployment, publication, security, or the independent PR15 convergence gate.

## Install and remove

From the 3CAN-engine release root on Windows, install the discoverable Skill:

```powershell
New-Item -ItemType Directory -Force "$HOME\.agents\skills" | Out-Null
Copy-Item -Recurse examples\codex-cli-project-kit\installable-skills\3can-runtimehook "$HOME\.agents\skills\3can-runtimehook"
```

Copy the project kit into each governed repository through that project's normal
reviewed installation path. This PR ships but does not install the global Skill
or change global hooks.

To remove the Skill, first say `关闭 RuntimeHook` in any active governed worktree,
then remove only its exact global directory:

```powershell
Remove-Item -Recurse -LiteralPath "$HOME\.agents\skills\3can-runtimehook"
```

That is a global Skill uninstall. Full project-kit removal should revert its
reviewed installation commit; do not delete `.codex/hooks.json` wholesale
because it may contain unrelated hooks.

## Short smoke

From a disposable project-kit worktree, the internal command path is:

```powershell
$root = (git rev-parse --show-toplevel).Trim()
python (Join-Path $root 'scripts\3can_runtimehook.py') --root $root on --goal 'Deliver the requested bounded result.' --acceptance 'A01=The requested result is complete.' --intensity light --reason 'Small and clear.'
python (Join-Path $root 'scripts\3can_runtimehook.py') --root $root review --stage final --result PASS --reference 'git:reviewed-commit'
python (Join-Path $root 'scripts\3can_runtimehook.py') --root $root off
```

This smoke tests semantic state only. Existing convergence tests separately
prove candidate freshness and Stop behavior.
