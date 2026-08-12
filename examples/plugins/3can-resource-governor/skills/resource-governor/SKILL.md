---
name: resource-governor
description: Coordinate machine-global leases for conflicting local development resources without limiting Codex agent concurrency. Use before and after Docker builds, Compose stacks, fixed host ports, or 3CAN single-writer operations, and when inspecting cleanup-pending resources left by SessionEnd or SubagentStop.
---

# 3CAN Resource Governor

Use the bundled SQLite hub only for resources that cannot safely be shared.
Never lease generic agents, tasks, or sessions.

## Locate the hub

Resolve the plugin root from this `SKILL.md` location; do not assume
`PLUGIN_ROOT` exists in an ordinary task shell. The plugin root is two parent
directories above this skill folder. On PowerShell:

```powershell
$pluginRoot = (Resolve-Path (Join-Path '<skill-directory>' '..\..')).Path
$hub = Join-Path $pluginRoot 'scripts\3can_resource_hub.py'
```

The CLI and lifecycle hooks share
`%LOCALAPPDATA%\3can\resource-governor` by default. Override both with
`THREECAN_RESOURCE_HUB_DIR` only when isolation is intentional.

## Use the lease lifecycle

1. Set a stable `THREECAN_AGENT_ID` and `THREECAN_WORKORDER_ID`. Codex does not
   guarantee `CODEX_AGENT_ID` in delegated shells, so pass `--actor-id`
   explicitly when a delegation wrapper knows the hook actor ID.
2. Run `status` before a heavy operation.
3. Acquire the exact resource immediately before use. Use keys such as
   `docker-build:<image-or-fingerprint>`, `compose-project:<name>`,
   `port:<number>`, or `3can-writer:<graph-id>`.
4. In the task harness `finally`, stop or remove only the owner-scoped
   resource. Then call `release --cleanup-verified`.
5. Treat `CLEANUP_PENDING` as evidence that the lifecycle ended without
   verified cleanup. Inspect the manifest, perform the exact cleanup, and only
   then release the lease.

TTL expiry also becomes `cleanup_pending`; never interpret an elapsed TTL as
proof that a Docker container, process, or port stopped.

```powershell
python $hub acquire `
  --resource-key 'compose-project:video-uat-thread-id' `
  --actor-id 'subagent-id'

python $hub release `
  --lease-id '<lease-id>' `
  --actor-id 'subagent-id' `
  --cleanup-verified `
  --reason 'compose_down_verified'
```

The `performance` profile reports an exact conflict as advisory; the
`constrained` profile exits nonzero. An advisory does not grant a lease and
must never be reported as acquired.

For disk diagnosis, `audit-sessions --state-db <state_5.sqlite>` is read-only.
It reports open spawn edges, normalizes long-path references, and emits
full-history-fork and unreferenced-rollout review candidates. It never closes
an edge or deletes SQLite/JSONL. Prefer bounded or `fork_turns=none` delegation
and follow-up reuse; never impose a global agent limit.

Never run global Docker prune, delete Codex session data, release another
owner's lease, or mark cleanup verified from a lifecycle hook.
