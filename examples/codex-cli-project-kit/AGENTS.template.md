# Codex Project Runtime Rules

This project uses 3CAN as a local project-reality / coordination substrate
sidecar. Keep its graph, runtime database, token ledger, and generated state
isolated from other projects. Execution tools do the work; Git, CI, runtime,
and providers retain domain truth; 3CAN organizes durable project cognition,
provenance, retrieval, and coordination.

## Startup And Readiness

Initialize a fresh project explicitly:

```powershell
..\3CAN-engine\scripts\init-project.ps1 -ProjectDir . -Port 9711 -StartServer
python ..\3CAN-engine\scripts\verify_project.py `
  --base-url http://127.0.0.1:9711 --min-nodes 10
```

A fresh graph uses `THREECAN_READINESS_MODE=development`. Production
acceptance requires a pinned profile and
`verify_project.py --require-production-ready`.

The Codex wrapper may check readiness, but it must never request, spawn,
terminate, restart, or replace the shared production runtime. When 3CAN is
offline, route, ticket, and writeback are `UNAVAILABLE`; local Git, coding,
builds, and offline tests continue. Only the machine operator/Supervisor owns
production 9700 lifecycle. One failed probe is evidence, not restart authority.

## Ordinary Agent Use

For read-only orientation, call route directly:

```powershell
scripts\codex-3can.cmd route `
  -Task "current task" `
  -BaseUrl $env:THREECAN_BASE_URL
```

Read-only route/retrieve/status calls require no ticket, check-in, hook, or
mutation. Retrieve full node content only for selected results. Treat typed
`PARTIAL`, `BLOCKED`, `UNAVAILABLE`, and readiness reason codes as real
outcomes; do not collapse them into one `healthy` boolean.

Ordinary development has zero forced 3CAN calls. Understand the task, edit,
test, and create the appropriate Git checkpoint without per-prompt, per-tool,
per-test, or per-commit graph traffic. Use 3CAN writeback only for an accepted
meaningful closeout (`AUTO_CLOSEOUT`) or an explicit Owner request
(`OWNER_REQUESTED`). A typed 3CAN failure never fabricates completion and does
not block safe local work.

Ordinary safe work has no mandatory ceremony. Convergence is opt-in only when
the project declares an evidence-bearing long task.

The harness derives a stable, execution-specific `AgentId` and carries it on
the protocol. The generic `codex-main` id is rejected. The wrapper never stores
or selects ticket state; the shared API/ledger remains the canonical evidence
surface.

## Optional Guarded-Write Workflow

Use `prepare` and the exact returned ticket ID only when this project has a
specific guarded-write or signed-evidence requirement. After verified work,
pass that ID to `done`. A Ticket is a scoped mutation-evidence envelope: Owner
instruction supplies task authority, while the Ticket binds execution identity,
project/workspace/Workorder, target, scope, consulted context, action, and
outcome. It is not a worktree lock or universal prerequisite for edits.

`compact` remains available for a durable handoff before archive or context
compaction. Pass files explicitly; it never imports files from ticket or wrapper
state.
Store decisions, verified outcomes, and source locations; do not store raw
chain-of-thought, credentials, cookies, or private logs.

For durable `INTF` / `PROC` / `DEC` / `PRJ` current fields, declare the source
in the writeback JSON. User direction stays lightweight:

```json
{
  "source_provenance": "user_authoritative",
  "authorized_by": "user",
  "project_id": "<project-id>",
  "project_namespace": "<project-namespace>",
  "changes": [{
    "node_id": "DEC-example",
    "field": "current_state",
    "value": "...",
    "expected_updated_at": "<exact-node-updated-at-from-read>"
  }]
}
```

Machine facts may declare `source_provenance=machine_verifiable`, but the
protected-current gate remains fail-closed until a canonical evidence owner
binds the target node/field/value. Existing ErrorKnowledge resolution receipts,
raw hashes, paths, URLs, web content, Agent guesses, activity completion, and
Session summaries cannot be borrowed across facts. `user_authoritative` records
a caller assertion, not authentication or an approval subsystem. Protected
`INTF` / `PROC` / `DEC` / `PRJ` node POST/PUT calls put the same provenance plus
project ID/namespace in `content.extra`; direct DELETE is not the lifecycle path.

After a serious milestone, run the repository's
`neural-memory/benchmark/milestone_recovery_probe.py` with a new AgentId and a
small private spec containing expected graph hash/readiness, expected node IDs,
and non-empty critical/evidence facts whose `node_id` names one expected node.
A writeback is complete only when the probe returns `PASS`; otherwise
refine the existing node and keep the milestone `PARTIAL`. Do not run this for
every edit or API call.

## Modular Development Discipline

- Organize medium or large work around one meaningful, independently reviewable
  functional module or milestone whenever practical.
- Machine execution may be fine-grained; human governance stays coarse-grained.
- A module may contain many edits, tests, tool calls, and Git commits; do not
  create user ceremony around commit count.
- Start a new Workorder when the user-visible capability, risk, canonical owner,
  rollback boundary, or acceptance surface materially changes.
- Git records code history and checkpoints; tests, CI, runtime, and provider
  evidence prove behavior; 3CAN summarizes project meaning and current evidence.
- Report module completion with what is verified, what is not, and what decision
  is required.

## Concurrency

- Treat one physical Git worktree as one writer.
- Parallel writers need distinct worktrees, branches, AgentIds, WorkorderIds,
  and non-overlapping file allowlists.
- `GET /api/agents` is a heartbeat-TTL view of registrations, not a process
  inventory. Old registrations project as `offline` without being deleted.
- Use the repository's external worktree lease as write authority. Agent cards,
  tickets, and PIDs do not prove ownership.
- Give Docker lanes unique Compose project names, ports, image tags, and
  writable volumes. Never use broad system or volume prune.

## Optional Hooks And PR Adapter

Hook examples under `.codex/` and the Claude Code examples are optional
policy adapters. Enable only the bounded hooks a project needs; they are not
engine dependencies.

For long tasks, create a tracked versioned Task Hook from
`.codex/task-hooks/generic-delivery.example.json`, validate its canonical digest,
then copy `.codex/convergence.example.json` to `.codex/convergence.json` and pin
that exact revision for one run. The single convergence hook restores the
selected boundary at startup, resume, and compaction, evaluates only declared
high-cost guards, and requires a current candidate-bound evidence receipt before
a success stop.

Keep `.codex/convergence.json` untracked. Track reusable Task Hooks, their exact
registry entries, and sanitized qualifying receipts under
`.codex/task-hooks/evidence/`. In a later worktree, use `select-task` with an
exact task family, new run ID, and JSON bindings; never hand-carry a prior run's
paths or defaults into executable fields. A launcher may instead declare that
same exact selection through the documented `THREECAN_TASK_*` environment at
native `SessionStart`; it is explicit run intent, not prompt inference, and
never replaces an existing selector.

Native selection validates only the explicitly selected family so an unrelated
registry entry cannot consume the lifecycle timeout. Audit every registered
family explicitly with `validate-registry` outside a native Hook event.

Declare stable protocol and safety constants as invariants. Declare every
run-varying asset, path, model/version, tenant/product input, strategy, and
fallback as a mutable binding. Candidate providers must attest all current
binding fingerprints and every fallback used; hidden defaults are typed
`IMPLICIT_MUTABLE_BINDING` or `FALLBACK_NOT_ALLOWED`. Do not use constant grep or
add domain branches to the Global Hook. A repeatable command provider contains
its launcher, any explicitly reviewed fixed interpreter flags in
`invariant_argv`, and its versioned adapter; all run configuration enters
through bindings. `invariant_argv` is a governance assertion, not an escape
hatch for a run value. When correctness depends on artifact lineage, bind a
project-owned relational or semantic Oracle that compares those bindings with
the actual candidate manifest.

Changing Goal, Acceptance, Oracle meaning/version, mutable-binding policy, or
Candidate Provider requires a new pinned Task Hook digest and explicit review;
old evidence cannot prove the new revision. One-off Task Hooks retire only with
a retained final receipt whose current candidate and evidence still match.
Repeatable hooks remain parameterized and require real retained reproduced
receipts plus review before promotion. Re-pinning changed semantics under the
same revision is invalid even across runs or after a local receipt is removed;
repeatable Task Hooks must be committed so the bounded Git lineage can enforce
that rule; the current semantic value, registry, and reusable active entry must
match Git `HEAD` before activation. `confirmed_by` is an audit assertion, not
authentication.

Mechanical checks do not prove semantic, visual, or Owner acceptance unless the
Task Hook binds an appropriate reviewer receipt. Scope covers only the current
repository and rejects dirty submodules. The Hook never parses session JSONL,
calls an LLM or 3CAN, commits, merges, deploys, publishes, or performs writeback.
`CANDIDATE_READY` still requires the named pending review and is not eligible
for `AUTO_CLOSEOUT`.
Review and trust exact definitions with `/hooks`; global enablement is not
implied by copying this template.

The local GitHub REST adapter is also optional. PR creation is an external
publish action: prepare the candidate first, obtain explicit approval, then
publish and read back the result. Never print tokens.

## Safety

- Bind 3CAN to `127.0.0.1` unless an authenticated network boundary exists.
- Do not reuse another project's graph, token database, or readiness profile.
- Run `python ..\3CAN-engine\scripts\prerelease_scan.py --strict` before
  publishing or copying a release package.
- Treat token telemetry by source: provider/runtime usage is evidence; local
  estimates are guardrails.
