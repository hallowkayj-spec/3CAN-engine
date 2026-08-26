---
version: 1
caution: balanced
autonomy: bounded
external_changes: confirm
context: compact
history: applicable
review: risk_based
writeback: meaningful_only
---

# 3CAN project steering

This file describes how the Owner wants AI agents to work by default in this
project. It is a human steering surface, not a truth database or security
boundary.

## Precedence

For governable preferences, a current explicit Owner instruction takes
precedence over these defaults. The override is scoped to the current task; it
does not silently rewrite this file or durable project knowledge.

Neither this file nor a prompt can override:

- evidence and verification truth;
- project, tenant, credential, or writer isolation;
- destructive-production and irreversible external-change protections;
- Git, CI, Runtime, Provider, and Evidence as their respective live authorities;
- protected durable-current provenance gates.

Production runtime lifecycle is machine-owned. Ordinary Worktrees are 3CAN
clients: they may observe readiness and use route, ticket, and writeback while
available, but they must not start, stop, restart, or replace the shared 9700
owner. Runtime unavailability does not block local Git, coding, builds, or
offline tests; only 3CAN-dependent operations remain `UNAVAILABLE`.

Ordinary Agents are scoped 3CAN clients, not machine-global administrators.
Only an explicitly Owner-authorized governance/operator Workorder may change
3CAN's machine-global runtime or governance behavior; other Sessions preserve
compact evidence and continue safe independent work instead of self-promoting.

Project Sessions are asynchronous scoped clients; they do not require real-time
cross-Session synchronization. Once a bounded graph mutation reports committed, later
route/read operations observe one coherent committed state. GraphEngine may
serialize those short operations internally, but that is not a project lock or
a governance block, and ordinary Agents never administer other Sessions.

## Default working style

Supported values are intentionally small: `caution` accepts
`strict|balanced|pragmatic`; `autonomy` accepts `guided|bounded|high`;
`external_changes` accepts `confirm|reversible|deny`; `context` accepts
`compact|standard|full`; `history` accepts
`minimal|applicable|explicit_only`; `review` accepts
`risk_based|always|owner_requested`; and `writeback` accepts
`meaningful_only|durable_only|owner_confirmed`.

- `caution: balanced` — match checks and ceremony to the real risk.
- `autonomy: bounded` — complete safe in-scope work without inventing scope.
- `external_changes: confirm` — obtain confirmation before material external
  publication, payment, deployment, deletion, or irreversible mutation.
- `context: compact` — start from briefing and targeted route; deep-read only
  what the task needs.
- `history: applicable` — surface history only when it remains relevant to the
  current project and task.
- `review: risk_based` — use focused review for focused risk; broaden only when
  the contract or change surface requires it.
- `writeback: meaningful_only` — record durable meaning at a real module,
  interface, decision, rollback, acceptance, or release boundary.

## Project isolation

This `3CAN.md` belongs to the project identified by `.agents/project.json` in
the same repository root. It must not silently govern another project or
namespace. Cross-project collaboration is explicit and on demand; separate
graphs are not automatically federated.

## Reality and intent

3CAN should give the next agent a compact view of:

- the current goal;
- trusted current facts and their authority;
- these Owner defaults and any current scoped preference;
- applicable hard constraints;
- relevant prior experience;
- unresolved conflict that needs an Owner decision.

The project graph may carry semantic meaning, history, and coordination.
Exact Git state remains in Git; runtime state remains in Runtime; CI status
remains in CI; provider completion remains at the Provider; verification
remains in Evidence.

## Fast client path

Start safe local work immediately. 3CAN readiness, route, and retrieval improve
decisions when durable project meaning is relevant; they are not universal
pre-edit gates. A Session needs a ticket only when the current project contract
marks the pending operation as ticket-governed.

Request and consume that ticket just in time for the governed operation, not as
a task-opening ceremony. Bind the current AgentId, project identity/namespace,
physical workspace or worktree, and any required Workorder, target, and scope.
Honor the returned TTL and completion deadline instead of caching ticket state
across a long development episode.

Handle one typed refusal at its canonical boundary:

- expired or inactive state: obtain one fresh preflight only if the governed
  operation is still pending;
- identity or digest mismatch: correct the exact binding and do not retry with
  guessed context;
- version conflict: reread the canonical node and retry once with its current
  compare-and-swap version, or report `CONFLICT` when meaning has diverged;
- 3CAN unavailable: continue unrelated safe local work and keep only the
  dependent operation `UNAVAILABLE`.

Never blind-retry the same request, reuse another Session's private ticket
state, or repeat already-completed local work merely to refresh authorization.

## ErrorKnowledge

Use the existing ErrorKnowledge lifecycle. The first occurrence records the
case compactly. Exact unresolved repeated failures block an unchanged blind
Agent retry. Related historical cases are advisory. An explicit Owner decision
does not erase the ErrorCase; where an existing project-governance path supports
a scoped retry, its history and reason remain visible and auditable. True safety
boundaries remain non-bypassable. Resolved cases retain their verification
pointer and may regress without creating a duplicate ErrorCase.

The retry threshold is intentionally not configurable here because it already
has one canonical implementation owner.

## Agent-mediated semantic checkpoints

Git records exact commits, branches, trees, diffs, and pull requests. 3CAN
records what a meaningful engineering change means and what the next agent
needs to know.

Prefer updating an existing Workorder, INTF, PROC, DEC, or PRJ owner when a
user-visible capability, canonical interface, verified execution path,
architectural decision, rollback boundary, or release boundary changes.

Do not create a node for every commit, test, tool call, provider call, or
session. Do not add a Git watcher, commit daemon, or Git-state mirror.

Ordinary development follows `understand -> edit -> test -> Git checkpoint ->
deliver` with zero forced 3CAN calls. A route is a read tool used when project
meaning would materially improve the decision; it is not a universal pre-edit
gate.

Durable writeback has only two default triggers:

- `AUTO_CLOSEOUT`: after a meaningful module or milestone has finished local
  verification, record its semantic delta and remaining typed evidence state;
- `OWNER_REQUESTED`: the Owner explicitly asks to record, summarize, compact,
  or checkpoint current meaning at any point.

Both triggers use the existing writeback path. They do not introduce a watcher,
queue, daemon, or second execution state. If 3CAN is unavailable or concurrent
state has moved, local Git work remains complete and the receipt stays typed
`UNAVAILABLE`, `PARTIAL`, or `CONFLICT`.

Concurrent convergence is node-first and compare-and-swap. Update the canonical
owner with its observed `expected_updated_at`, then add only edges whose
endpoints are present. A stale node update, missing endpoint, or already-applied
semantic delta is a typed convergence result, never permission to guess,
duplicate a node, or block unrelated local development.

## Provenance boundary

`user_authoritative` plus `authorized_by=user` is an audit assertion, not
cryptographic authentication. Machine protected-current write remains
fail-closed until a canonical verifier can bind evidence to the exact project,
node, field, and value:

`MACHINE_PROTECTED_CURRENT_WRITE_UNAVAILABLE`

## Deliberately absent

This project does not use a policy database, preference database, governance
DSL, PolicyEngine, federation registry, background reload daemon, extra LLM
classifier, or full-Markdown prompt injection for these defaults.
