# Codex Project Runtime Rules

This project uses 3CAN as a local project-memory sidecar. Keep its graph,
runtime database, token ledger, and generated state isolated from other
projects. Git remains authoritative for code history; 3CAN coordinates
retrieval, durable project memory, and evidence.

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

The Codex wrapper may check readiness and, on a managed Windows installation,
request the configured 3CAN Supervisor Scheduled Task. It must never spawn,
terminate, or replace backend/proxy processes itself. One failed readiness
probe is evidence to inspect, not permission to restart services.

## Ordinary Agent Use

For read-only orientation, call route directly:

```powershell
scripts\codex-3can.cmd route `
  -AgentId codex-main `
  -Task "current task" `
  -BaseUrl $env:THREECAN_BASE_URL
```

Read-only route/retrieve/status calls require no ticket, check-in, hook, or
mutation. Retrieve full node content only for selected results. Treat typed
`PARTIAL`, `BLOCKED`, `UNAVAILABLE`, and readiness reason codes as real
outcomes; do not collapse them into one `healthy` boolean.

## Optional Guarded-Write Workflow

Use `prepare` and the exact returned ticket ID only when this project has a
specific guarded-write or signed-evidence requirement. After verified work,
pass that ID to `done`. Tickets are authorization/evidence receipts, not
worktree locks and not a universal prerequisite for edits.

`compact` remains available for a durable handoff before archive or context
compaction. Store decisions, verified outcomes, and source locations; do not
store raw chain-of-thought, credentials, cookies, or private logs.

## Concurrency

- Treat one physical Git worktree as one writer.
- Parallel writers need distinct worktrees, branches, AgentIds, WorkorderIds,
  and non-overlapping file allowlists.
- Use the repository's external worktree lease as write authority. Agent cards,
  tickets, and PIDs do not prove ownership.
- Give Docker lanes unique Compose project names, ports, image tags, and
  writable volumes. Never use broad system or volume prune.

## Optional Hooks And PR Adapter

Hook examples under `.codex/` and the Claude Code examples are optional
policy adapters. Enable only the bounded hooks a project needs; they are not
engine dependencies.

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
