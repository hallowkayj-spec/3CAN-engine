# Claude Code Integration Recipe (S66g 2026-04-19)

> 针对使用 Claude Code (Anthropic 官方 CLI) 的开发者. 演示: 接入 3CAN → route → ticket → Edit → PostToolUse writeback 全回路.

## 前提

- Python ≥ 3.11 (测过 3.14)
- 3CAN 引擎本地运行: `localhost:9700` + backend green 9701 或 blue 9702
- Claude Code 安装 + `~/.claude/` 目录存在

## 快速验证 3CAN 活

```bash
curl http://localhost:9700/api/stats
# → {"total_nodes": 1407, "total_edges": 1023, ...}
```

如果 404 或 connection refused, 先启动:
```bash
cd neural-memory/backend && python app.py --port 9701 &
cd neural-memory/proxy && python server.py &
```

## 3 步最小接入

### Step 1. Agent checkin (每个新 session 开头做一次)

```bash
curl -X POST http://localhost:9700/api/agents/checkin \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "claude-code-main",
    "name": "Claude Code 主 session",
    "role": "coding agent",
    "current_task": "implement feature X",
    "capabilities": ["code", "refactor", "test"]
  }'
```

响应: `{"ok": true, "agent_id": "claude-code-main"}`

### Step 2. Briefing (冷启动拉全局)

```bash
curl http://localhost:9700/api/briefing
# → {"agents_active": [...], "recent_activity": [...], "err_warnings_7d": [...], "project_state": {...}}
```

把返回字段浓缩后作 session 开场 context (约 2K token, 省 grep `memory/` 几 K/次).

### Step 3. Route before action (查知识)

```bash
curl -X POST http://localhost:9700/api/route \
  -H "Content-Type: application/json" \
  -d '{
    "task": "视频管线 bug 修复决定",
    "max_nodes": 5,
    "agent_id": "claude-code-main",
    "mode": "slim",
    "confirm_low_confidence": true,
    "allow_degraded": true
  }'
```

响应含 `nodes: [{id, summary, ...}]`, `confidence: high|medium|low`, 足够判断要不要 `/api/retrieve/{id}` 取全文.

## 硬 Gate 回路 (v9.5 S66g — 写文件前必须做)

Claude Code PreToolUse hook (`~/.claude/scripts/hooks/3can-behavioral-gate.js`) 强制: Write / Edit / MultiEdit / NotebookEdit + 高危 Bash → **必须带 ticket**, 否则 deny.

### Step 4. 动手前拿 ticket

```bash
curl -X POST http://localhost:9700/api/route/ticket \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "claude-code-main",
    "task_description": "fix slim mode bug in longmemeval_runner.py",
    "target_files": ["neural-memory/benchmark/longmemeval_runner.py"],
    "scope_keywords": ["runner", "benchmark", "slim mode"],
    "task_type": "Edit"
  }'
```

响应关键字段:
- `ticket_id`: `rt_abc123...`
- `err_warnings`: 相关 ERR-* 列表 (agent **必读**, 否则重复历史错误)
- `intf_anchors`: 相关 INTF-* 契约 (改 API 前对齐)
- `api_usage_hints`: API_USAGE.md 相关片段
- `ttl_sec`: 900 (15 min)
- `target_digest` / `scope_digest`: consume 时必须原样回传，服务端据此
  拒绝跨 agent、跨目标或跨 scope 复用

### Step 5. Edit with ticket

把 `ticket_id` 放到 Edit tool 调用的 `tool_input.meta.3can_ticket_id` 或设 env `THREECAN_TICKET_ID`. Gate 会验证 + 放行.

### Step 6. Gate 自动 log + PostToolUse 自动 writeback

- Gate 决策全 log 到 `~/.claude/logs/3can-gate.jsonl` (允许 / 拒绝 / 原因)
- Edit 成功后 PostToolUse hook 自动 POST `/api/activity/log`, agent 的改动进 hash chain

## Troubleshooting

**"ticket_not_found_or_expired"**
- TTL 过期 → 重新 POST `/api/route/ticket`
- 票据保存在 SQLite/WAL 中，正常 backend 重启不会丢失；若账本被明确
  更换、清理或 ticket 从未存在，则重新 prepare

**"scope_mismatch"**
- 本次 Edit 的 file_path 不在 ticket `scope.target_files` → 重新 POST 带正确 target_files 的新 ticket

**"no_ticket"**
- Edit 调用没带 `meta.3can_ticket_id` → 补上或设 env `THREECAN_TICKET_ID`

**Gate 初装死锁 (Gate 要 ticket, 但 agent 要改 Gate 自身)**
- 紧急 bypass: `touch ~/.claude/logs/3can-gate-bootstrap`, 完成后 `rm` 删除
- 每次 bypass log 到 `~/.claude/logs/3can-gate.jsonl` stage="bootstrap-bypass"
- **正常运行时 sentinel 必须不存在** (CI / 部署检查应 assert 其缺失)

## 写回节奏 (3CAN 治理核心)

Claude Code 在 session 中应该:
1. **session 开头**: `/api/briefing`
2. **每次任务前**: `POST /api/route/ticket` (含 scope)
3. **Edit 成功后**: PostToolUse hook 自动调 `/api/activity/log`, agent 不用手动做
4. **阶段性成果** (bug 修完 / 接口定了 / 错误教训): 主动 `POST /api/writeback` 或 `POST /api/nodes` 建 ERR-* / DEC-* / FEE-*
5. **session 关**: 可选 `POST /api/handoff/create` 给下一 session / 其他 agent

## 推荐 CLAUDE.md 规则片段

```markdown
## 3CAN 接入规则

- 新 session 必做 `GET /api/briefing`
- 写文件前必做 `POST /api/route/ticket` + 读 err_warnings
- 禁 grep `memory/` / `handoffs/active/` — 走 route
- compact 前必写 SES-* 节点 (包含 3CAN node IDs 列表, 不注入原文)
- 查知识: route → 不命中再考虑 grep (route 命中率目标 ≥90%)
```

## 完整 reference

- [EVIDENCE.md](../EVIDENCE.md) — 硬事实 + benchmark 数据
- [API_USAGE.md](../API_USAGE.md) — 每个端点详细
- [AGENT_BINDING.md](../AGENT_BINDING.md) — 接入规范
- [DEPLOYMENT.md](../DEPLOYMENT.md) — 5 组件安装
- [STABILITY_TIERS.md](../STABILITY_TIERS.md) — 哪些 API 稳定
