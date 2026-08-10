# 3CAN Resource Governor

This optional Codex plugin coordinates scarce or conflicting local development
resources. It does not cap agents or subagents. Independent work remains fully
parallel.

The default `performance` profile records a conflict as `ADVISORY` and exits
successfully without granting a lease. Set
`THREECAN_RESOURCE_PROFILE=constrained` on a resource-limited machine to make
the same conflict return `BLOCKED`.

## What to lease

Lease only resources that cannot safely be shared:

- `docker-build:<repository-or-image>`
- `compose-project:<unique-project-name>`
- `port:<port-number>`
- `3can-writer:<graph-or-project-id>`

Do not lease generic concepts such as `agent`, `task`, or `session`. There is no
global task-count or agent-count limit. The CLI rejects those generic lease
keys so the coordination boundary cannot accidentally become an agent cap.

## CLI use

Installed agents should invoke the bundled `resource-governor` skill. It
resolves the script from its own skill location; `PLUGIN_ROOT` is not assumed
in an ordinary task shell. From this source directory, resolve the same script
directly:

```powershell
$hub = (Resolve-Path '.\scripts\3can_resource_hub.py').Path
$env:THREECAN_AGENT_ID = 'codex-runtime-video'
$env:THREECAN_WORKORDER_ID = 'VIDEO-UAT-004'
```

The default state is machine-global at
`%LOCALAPPDATA%\3can\resource-governor`, so separate worktrees see the same
port, Docker, and writer leases. Use `THREECAN_RESOURCE_HUB_DIR` only for an
intentional isolated test.

Acquire a conflicting resource before starting it:

```powershell
python $hub acquire `
  --resource-key "compose-project:video-uat-$env:CODEX_THREAD_ID" `
  --metadata-json '{"cleanup_intent":"docker compose down for this project only"}'
```

Release an individual lease in the task harness `finally` block, after the
owner-scoped Docker command has completed:

```powershell
python $hub release `
  --lease-id <lease-id> `
  --cleanup-verified `
  --reason compose_down_verified
```

Use `finish` to mark every active lease owned by the session as
`cleanup_pending` and write a cleanup manifest. It never makes the resource
available again:

```powershell
python $hub finish
```

TTL expiry follows the same rule: a stale active lease becomes
`cleanup_pending`; it is not silently released while a container, process, or
port may still be live.

Cleanup manifests carry structured ownership for `docker-build:*` and
`compose-project:*` leases. The records are review candidates, not executable
commands or cleanup authorization. They explicitly prohibit hook-driven
Docker, `docker system prune`, and `docker volume prune`.

## Read-only Codex session audit

Run the optional audit manually when `CodexHome\sessions` grows. It opens the
selected Codex SQLite database read-only, normalizes `\\?\` long-path
references in memory, and reports open spawn edges, missing referenced
rollouts, large child rollouts that may reflect full-history forks, and files
not referenced by the selected database:

```powershell
python $hub audit-sessions `
  --state-db 'C:\CodexHome\state_5.sqlite' `
  --sessions-dir 'C:\CodexHome\sessions'
```

The command writes a candidate manifest under the Resource Governor state
directory. It does not update SQLite, close spawn edges, or delete JSONL.
Candidates stay non-actionable until a separate workflow verifies all database
references, writes a durable summary, and resolves the lifecycle edge.

The preferred space optimization is not an agent limit: use
`fork_turns=none` or a bounded history with a self-contained prompt where
practical, and reuse a live subagent for related follow-up work.

## Lifecycle safety

The plugin bundles `SessionEnd` and `SubagentStop` hooks. They only:

1. mark leases owned by the ending session or attributed subagent as
   `cleanup_pending`;
2. write a JSON cleanup manifest in the shared resource-governor state;
3. report that no Docker command and no Codex-session deletion was executed.

`SessionEnd` has a three-second maximum, so real Docker cleanup belongs in the
task harness, not the hook. A lease requires an explicit actor ID at acquire
time because delegated shells do not always expose `CODEX_AGENT_ID`.
`SubagentStop` can only match leases attributed to its payload `agent_id`.
Never use this plugin as a wrapper around
`docker system prune`, `docker volume prune`, or deletion of Codex rollout
files. Hooks never run the heavier Codex session audit.
