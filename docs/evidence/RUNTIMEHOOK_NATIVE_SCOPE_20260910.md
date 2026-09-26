# RuntimeHook native working-directory isolation

Status: `LOCAL_SCOPE_GUARD_VERIFIED / DESKTOP_REBIND_NOT_APPLIED`.
Candidate plugin version: `0.1.5-rc.1`; the installed global version is not replaced.

## Decision and observed failure

Workorder: `RUNTIMEHOOK-NATIVE-SCOPE-20260910`.
Owner: the RuntimeHook maintainer, not either business workflow.

Two business tasks used separate physical source roots, but the Desktop host
still registered both tasks with one legacy native cwd. The global plugin
selected that cwd's state, while manual controller calls selected the newer
source root. The roots, rather than graph routing or Git's shared object
database, explained the wrong Intent and Stop reminder. An in-memory replay
also intercepted a stage-event write targeting the wrong state. No real state
was modified by that diagnostic.

Opened primary evidence:

- [Native Hook fields](https://learn.chatgpt.com/docs/hooks#common-input-fields):
  cwd is the session directory; a command's workdir is not a native rebind.
- [App Server turns](https://learn.chatgpt.com/docs/app-server#turns): cwd can
  be overridden at the protocol boundary. This does not establish a Desktop
  command for rebinding an existing loaded task.
- [Resume command](https://learn.chatgpt.com/docs/developer-commands#codex-resume):
  CLI supports an explicit directory override. Attaching another CLI to a
  Desktop-owned task is not a safe substitute for the missing host operation.

The installed Windows executable advertises an app-server proxy but reports
that daemon lifecycle is supported only on Unix. The observed Desktop
App Server uses stdio and no listener was found on its owning process. Current
task-management tools expose Git-moving handoff, not arbitrary cwd editing.
There is therefore no verified hot-rebind path in this environment. This is
an operator boundary, not permission to edit Session JSONL or SQLite.

## Smallest scoped change

Reuse the existing controller and the single worktree-local state file:

- Optional `--native-cwd` checks an independently observed absolute host cwd
  before `status`, activation, review, checkpoint, or off. Mismatch returns
  `UNAVAILABLE` with `CONTEXT_MISMATCH` and a nonzero exit; neither state nor
  local Git exclude is changed.
- A conflicting explicit Hook root cannot override a provided native cwd.
- Active Hook context and reminders identify their actual worktree.
- Skill guidance distinguishes physical command workdir, native task cwd and
  real native-event acceptance. Native scope cannot be guessed from task names.

No new Session ownership state, global mapping, resolver, queue, watcher,
artifact fingerprint, or graph call is introduced. The optional check is not
authentication or automatic discovery. A caller that omits it is not certified
as correctly bound. Two tasks that still share a wrongly registered native cwd
can still select the same state: fixing that host binding remains necessary.

## Bounded verification

The focused tests cover mismatch before state access/write, no orphan
activation, no change to Git exclude, same-worktree subdirectory resolution,
all four native command events in two linked worktrees, and conflicting
explicit-root payloads. The foreign state must remain byte-for-byte unchanged.

Run from `neural-memory`:

```text
python -X utf8 -m pytest tests/test_codex_runtimehook.py tests/test_runtimehook_plugin.py -q -p no:cacheprovider
```

These are component/native-command tests, not a successful Desktop rebind or
proof of business completion. A synthetic completed-plan label is only a
fixture, never a business-stage acceptance claim.

Local results: the ten initial scope counterexamples passed. One complete
component run reported 41 passed, 3 failed, 2 skipped: all three failures were
the old UTF-8 fixture expecting an unrelated, nonexistent hardcoded cwd to be
ignored. Replacing that fixture with a real same-worktree Unicode subdirectory
preserves its original encoding test and honors native scope. The focused
rerun passed all five selected cases (three encoding modes, a new test that
native payload cannot be replaced by the local-only option, and package
consistency). Ruff F, diff whitespace checks and Skill validation passed.
Skill validation required Python UTF-8 mode on this Windows host; its initial
default-encoding run failed, and no validator source was changed.

The actual two source roots were also checked read-only: the observed native
cwd mismatched the intended new source root and was refused before state
access; a hypothetical correct cwd matched the intended activation. That
hypothetical input is not evidence that Desktop has been rebound. The peer
task remains independently active, so its state may legitimately advance;
do not restore an earlier hash over its newer work.

## Remaining host repair and rollback

Do not move either source checkout, clear either semantic state, disable the
peer's Hook, or start/stop production runtimes. Preserve both tasks' source,
history and original binding. Do not invoke Git-moving handoff for this repair.

Before a cold native repair, obtain a maintenance window and prove that the
affected task has no live writer. Save a verified target-only backup and the
original configuration. Use the official protocol with the original thread
ID and only the intended cwd changed; do not use a synthetic new task to claim
the original task was repaired. Verify the effective response and persistence.
An already-loaded resume is not sufficient evidence that overrides applied.

After Desktop reopens, confirm its actual native events address the intended
worktree and activation across new prompt, stage, Stop and resume. Confirm no
peer-state mutation; explicitly supersede the previously injected foreign
Intent in the affected task's context. Do not clear real review debt merely to
obtain a silent Stop.

Rollback uses the same supported native configuration path to restore the
recorded original binding, only while no writer owns the task. It does not
rewind business commits or manually rewrite transcript/database contents. If
that path cannot be verified, stop before applying the rebind. Deployment and
the original incident remain incomplete until this acceptance succeeds.
