# RuntimeHook observed-scope fast path

Workorder: `RUNTIMEHOOK-SCOPE-CACHE-20260913`.
Owner: RuntimeHook maintainer. Status: `COMPONENT_VERIFIED`, not business acceptance.

## Decision

The Owner requested one small, millisecond-scale relationship check, not a task
manager. The existing controller now caches a once-observed native task / Git
worktree / activation relation. Native events use that local observation and
detect root, marker and activation changes; they never scan transcripts or
contact 9700. Binding does not move a worktree, change a task or grant a lease.

Opened source: [official native Hook common fields](https://learn.chatgpt.com/docs/hooks#common-input-fields).
The host supplies `session_id` and `cwd`; subagent events may use the parent's
session ID. Transcript paths are not a stable identity API. A command's workdir
does not update native task cwd. This precludes an implementation based on
parsing conversation logs, guessing titles or treating parent IDs as unique
subagent writer identity.

The disposable cache has one bounded atomic JSON entry per task under
`CODEX_HOME/runtimehook/scopes/`; semantic state stays in the existing ignored
worktree state file. No service, shared registry lock, scheduler, new port,
watcher, business permission system or second semantic kernel was introduced.
An existing activation is imported only after host/current-Intent evidence has
been checked. Directory equality alone is not ownership. A cache hit is an
observation, not authentication or exclusive-writer proof.

3CAN project/handoff evidence is compared only at a relevant registration or
handoff. The retained reference/comparison is an explicit observation, not a
live network check. Missing 3CAN evidence is `UNVERIFIED` and does not block
local work. A known mismatch gives advisory feedback; it cannot replace App,
Git or Owner authority. The registering Agent is responsible for opening the
referenced evidence; the adapter does not pretend to prove its semantics.

Unknown, corrupt, moved or stale bindings do not inject a peer Intent, change
its state, or emit `decision:block` / `continue:false`. Feedback is carried by
SessionStart/UserPromptSubmit or Stop system messages, not repeated after every
tool call. Correctly scoped semantic review timing and independent safety
gates retain their existing behavior. The Agent must not turn this advisory
into a whole-task pause; only actually unsafe affected operations are deferred.

## Validation

Counterexamples cover distinct native tasks sharing cwd, linked-worktree
isolation, a task moving to a peer, a new nested Git root, changed activation,
missing/corrupt cache, preserved peer bytes, scoped CLI writes, incomplete
migration import, and a contradictory 3CAN snapshot. The fast lookup test
rejects any Git subprocess or semantic-state access.

Initial focused scope run: 11 passed. The initial component run recorded
44 passed / 1 failed / 2 skipped: its legacy fixture began adding optional
native-scope metadata to an exact inactive-status assertion. Restricting that
fixture's native-cwd argument to activation preserves the inactive API test;
no production result was weakened to satisfy it. The final complete RuntimeHook
component regression passed 57 tests with 2 platform-specific skips in 166.92 s.
Ruff, diff whitespace checks and UTF-8 Skill validation passed. This was the
complete Hook component suite, not a rerun of unrelated graph/RPA/backend tests.

One 1,000-query local measurement on this Windows host (including fresh file
reads, ID construction and path resolution): p50 0.89455 ms, p95 1.1447 ms,
max 1.5206 ms. This is not full Hook latency: interpreter startup, existing Git
freshness/safety checks and model review are excluded. This is a small local
sample, not a universal hardware or cold-cache guarantee.

Live 9700 shallow liveness succeeded. One bounded deep-ready request reached
its 8-second timeout; exact node reads succeeded. Runtime readiness is not
promoted from these reads. No restart, second runtime or readiness repair is
part of this change. This observation reinforces keeping network calls out of
the local lookup path.

## Rollout and rollback

Candidate version: `0.1.6-rc.1`. Use the existing configured `3can-engine`
marketplace and native plugin installer. Do not edit session JSONL/SQLite,
clear peer state or restart active tasks. Preserve the old installed version
and exact configuration before installation. New code installation is not proof
that already-running tasks reloaded it; real native-event acceptance is separate.

Rollback reinstalls the recorded old plugin through the same supported path.
The disposable scope cache can be retained or removed by an exact task-file
allowlist; deleting it means advisory unbound supervision, never implied PASS.
Do not rewind any worktree, activation, Git commit or business output.
