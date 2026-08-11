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
- an exact unresolved ErrorKnowledge retry block;
- protected durable-current provenance gates.

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

## ErrorKnowledge

Use the existing ErrorKnowledge lifecycle. The first occurrence records the
case compactly. A second exact occurrence reuses the same case and may block a
blind retry. A semantically related case is advisory, not an automatic block.
Resolved cases retain their verification pointer and may regress without
creating a duplicate ErrorCase.

The retry threshold is intentionally not configurable here because it already
has one canonical implementation owner.

## Semantic checkpoints

Git records exact commits, branches, trees, diffs, and pull requests. 3CAN
records what a meaningful engineering change means and what the next agent
needs to know.

Prefer updating an existing Workorder, INTF, PROC, DEC, or PRJ owner when a
user-visible capability, canonical interface, verified execution path,
architectural decision, rollback boundary, or release boundary changes.

Do not create a node for every commit, test, tool call, provider call, or
session. Do not add a Git watcher, commit daemon, or Git-state mirror.

When Git is newer than durable project meaning, route to the relevant module,
verify Git as the exact authority, and use the existing writeback path only for
an accepted meaningful checkpoint.

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
