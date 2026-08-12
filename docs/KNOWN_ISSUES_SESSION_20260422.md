# 3CAN Engine — Known Issues (real session 2026-04-22)

> Captured during a new Opus session bootstrap against 3CAN 1435 nodes / 1034 edges.
> Real-usage findings, not lab-tests. Open-source backlog for v8.

## 1. MCP tools not visible in new agent sessions (P1)
**Symptom**: route/read_node/writeback/briefing documented as MCP优先 but absent from deferred tools in a fresh session. Forced fallback to raw curl + python urllib.
**Fix**: verify MCP auto-bind on session start, or document curl as primary.

## 2. /api/route?mode=full returns empty nodes (P1)
**Symptom**: identical query slim returns 8 nodes, full returns nodes=[].
**Hypothesis**: budget_tokens hard cap collapses to 0 when caller omits the field.
**Fix**: degrade to top-1 slim with degraded_to_slim flag instead of empty.

## 3. /api/nodes/{id} vs /api/retrieve/{id} confusion (P0)
**Symptom**: /api/nodes/{id} returns shell with empty current_state/description. Only /api/retrieve/{id} populates content.* fields.
**Impact**: new agents conclude 3CAN lost memory when it has not.
**Fix**: unify or document clearly.

## 4. Cross-cluster synthesis recall gap (P0)
**Symptom**: query 武商端 B线开发进度 returns shallow picks (FEE-no-prompt, MOD-028e84) instead of roadmap nodes (ARCH-saas-coach-v1-draft, DEC-saas-coach-defer-until-3can-stable).
**Root cause**: short-code boost v7.3 only scopes code tokens (T8/KB4); synthesis queries spanning 5+ clusters hit flat-distribution.
**Fix**: two-stage retrieval (per-cluster top-K then cluster-balanced RRF) or priority boost for PLAN-*/ARCH-* nodes on roadmap queries.

## 5. Ticket gate vs tool schema mismatch (P2)
**Symptom**: PreToolUse hook blocks Write without tool_input.meta.3can_ticket_id, but Write schema has additionalProperties:false rejecting extras.
**Impact**: only workaround is Bash+python open().write(), defeating gate intent.
**Fix**: hook must also intercept Bash write patterns, or use env var/sidecar file for ticket.

## 6. Parent-directory pitfall on new-session cwd (P2)
**Symptom**: harness primary_working_directory=父目录, real project root is one level deeper. New agent drops docs too high.
**Fix**: adopter doc should warn — project_root detection via CLAUDE.md/tools/docs/specs/ presence.

## Severity for v8 release

| # | Severity | User-visible? |
|---|---|---|
| 3 | P0 | Yes — breaks new-agent bootstrap |
| 4 | P0 | Yes — makes 3CAN appear useless on roadmap queries |
| 1 | P1 | Yes (if MCP marketed) |
| 2 | P1 | No (workaround=slim) |
| 5 | P2 | Only if gate adopted |
| 6 | P2 | Documentation only |

Authored: opus-4.7 / 2026-04-22
