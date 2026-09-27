# RuntimeHook: mandatory global entry, task-owned state

Owner request (2026-09-27): every new session and dispatched session must invoke
RuntimeHook. A second read-only chat in the same workspace must not need another
Git worktree merely to obtain semantic supervision.

## Decision and native contract

The previous controller stored one mutable state per physical Git worktree.
A new chat sharing that directory consequently had no valid scope and could not
adopt the existing chat's intent. The scope refusal was correct; the storage
granularity did not satisfy the global requirement.

Reuse the installed plugin and native lifecycle. No service, queue, scheduler,
transcript parser, shared graph execution state, or new dependency is needed.

Opened evidence:

- [Official Hooks contract](https://learn.chatgpt.com/docs/hooks): user/plugin
  hooks are global; SessionStart, SubagentStart, UserPromptSubmit, PostToolUse,
  Stop and SubagentStop are supported. Stop continuation is bounded and tool
  hooks are not a complete security boundary.
- [Official plugin packaging](https://developers.openai.com/plugins/build/plugins):
  the installed plugin owns its hooks; changed hook definitions require host trust.
- Installed App backend is `rust-v0.158.0-alpha.2.1`, commit
  `0d9c7cbfa6cf1489f55a8a9542b75ddd2c061807`.
  [Its hook runtime](https://github.com/openai/codex/blob/rust-v0.158.0-alpha.2.1/codex-rs/core/src/hook_runtime.rs)
  identifies a spawned agent by its actual thread ID, including forked agents.
  [Its PostToolUse serializer](https://github.com/openai/codex/blob/rust-v0.158.0-alpha.2.1/codex-rs/hooks/src/events/post_tool_use.rs)
  includes optional `agent_id` and `agent_type`; the common `session_id` may
  identify the parent. Prefer the supplied child `agent_id` for execution state.
  Missing child identity must never select the parent implicitly.

Selected design:

1. Store semantic state, checkpoint records, Jev receipts and writeback receipts
   under `CODEX_HOME/runtimehook/sessions/<sha256(actual-thread-id)>/`.
   The workspace remains a verified binding, not the state owner. Concurrent
   source writers still need isolated worktrees; read-only chats do not.
2. Use the existing scope cache for local automatic enrollment at native entry.
   With the Owner-enabled mandatory policy, new/resumed and spawned tasks must
   initialize their own real goal and acceptance through the existing `on`
   command. A hook does not invent a goal, inherit parent intent, or upload a
   raw prompt. Stop requests the existing one bounded continuation if this
   initialization or review is missing. Native callbacks remain offline.
3. Git and non-Git workspaces use the same task state path. Non-Git tasks have
   no fabricated HEAD or Git acceptance; output review remains necessary.
4. Import legacy state only when this exact session's existing scope and
   activation match the legacy owner. Preserve the legacy files and migrate
   only relevant bounded artifacts. The new controller never writes the shared
   worktree state. No fallback to a peer's state if a new session store exists
   or if ownership cannot be established.
5. Keep Jev authorization, node/project/writer/credential checks and meaningful
   writeback unchanged. Global invocation does not grant an arbitrary project
   a graph node or allow claiming an unverified host event.

Rejected alternatives: auto-binding every new session to the shared activation
would leak/change peer intent; allocating one worktree per diagnostic chat is
unnecessary; a new coordinator/daemon would duplicate native lifecycle; reading
transcripts to guess agent identity would depend on an unstable private format.

## Validation and rollback

Exercise two sessions sharing a directory, parent plus sibling agents sharing a
parent session ID, missing child identity, non-Git tasks, missing initialization,
resume, changed host/root markers, symlink/redirection, exact-owner legacy import,
foreign legacy refusal, checkpoint/writeback separation and normal regressions.
Use the installed-version public payload schema for executable hook probes.
Fixture payloads prove controller behavior, not actual App delivery; report real
native events separately. Never create or message another user chat just to
manufacture acceptance.

Install only an exact tested package through the official plugin manager. Keep
the prior package and pre-migration state/binding for rollback. Rollback restores
the previous package and this task's verified scope only; preserve new receipts
and never restore one chat's state onto another. Shared Runtime and proxy are
outside this change.

Implementation and installation results: pending.
