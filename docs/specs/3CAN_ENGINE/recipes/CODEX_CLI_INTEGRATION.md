# Codex CLI Integration Recipe (S66g 2026-04-19)

> 针对使用 OpenAI Codex CLI (或其他非 Claude coding CLI) 的用户. 3CAN 无 Codex 专用客户端 — 通过纯 HTTP 接入, 任何 CLI 只要能 curl 就能用.

## 前提

- 3CAN 引擎运行: `localhost:9700`
- Codex CLI 安装 + 能调用 shell

## 关键前提: 无 MCP 依赖

3CAN 不需要 MCP server / client. 所有交互通过标准 HTTP API. Codex CLI 只需:
1. 能执行 shell command (`curl` 或等价)
2. 能把 JSON 响应 parse 出关键字段注入 prompt

## 最小接入与可选辅助流程

### Optional Codex orientation wrapper (Windows / Codex CLI)

The minimum integration is a direct HTTP route with one stable AgentId. It does
not require a dedicated ChatGPT/Codex task, a prior check-in, or a wrapper. If
the host project ships the helper scripts and the current task wants a combined
readiness, check-in, briefing, and route call, it may run:

```powershell
scripts\codex-3can.cmd bootstrap `
  -Role frontend `
  -Task "current task"
```

This optional convenience command:

- verifies the real 3CAN graph instead of trusting a responding `9700`
- returns typed `UNAVAILABLE` when the machine-owned Runtime is offline; it
  never starts or recovers backend/proxy
- checks in the agent with a stable `agent_id`
- fetches compressed briefing
- routes the current task before long file reads

Before mutating files, use:

```powershell
$prepareResult = scripts\codex-3can.cmd prepare `
  -TaskDescription "edit focused area" `
  -TargetFiles path/to/file `
  -ToolName apply_patch `
  -ToolInputSummary "edit focused area" | ConvertFrom-Json
$ticketId = $prepareResult.prepare.ticket_id
if (-not $ticketId) { throw "prepare did not return ticket_id" }
```

After the mutation, use
`scripts\codex-3can.cmd done -TicketId $ticketId -Detail "what changed and why"`.
Always pass the exact ID returned by that operation's `prepare`; never infer it
from wrapper state. The wrapper stores no ticket-selection state. Before
compacting, pass files explicitly; compact never imports files from ticket or
wrapper state.

### Optional agent check-in

The packaged helper derives one stable AgentId from the current execution. A
direct API client must still carry its own explicit unique AgentId on every
related call; AgentId is identity and correlation, not authority.

```bash
curl -X POST http://localhost:9700/api/agents/checkin \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "codex-cli-frontend-W1",
    "name": "Codex CLI (GPT-5.4)",
    "role": "coding agent",
    "current_task": "frontend scaffold from INTF contracts",
    "capabilities": ["code", "schema"]
  }'
```

### Direct route (minimum)

Codex 特别适合通过 INTF-* 节点写前端对接后端. Claude Code 建 INTF-* 描述 API 契约, Codex 查 INTF 写代码.

```bash
curl -X POST http://localhost:9700/api/route \
  -H "Content-Type: application/json" \
  -d '{
    "task": "create_node API schema 强制字段",
    "max_nodes": 5,
    "agent_id": "codex-cli-frontend-W1",
    "mode": "full"
  }' | jq '.activated_nodes[] | select(.id | startswith("INTF-"))'
```

`mode=full` 对 Codex 更合适 — 生成代码需要完整契约, slim 模式的 120 字符截断会丢字段定义.

### Optional task-update check-in

每次完成一个任务:

```bash
curl -X POST http://localhost:9700/api/agents/checkin \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "codex-cli-frontend-W1",
    "current_task": "wait / next task",
    "meta": {"last_completed": "frontend scaffold for INTF-db-node_weights"}
  }'
```

## Ticket Gate 兼容

如果 Codex CLI 想执行 mutating 操作 (写文件 / git commit), 且用户装了 3CAN PreToolUse hook, 也必须走 ticket:

```bash
# Step A: 拿 ticket
TICKET=$(curl -X POST http://localhost:9700/api/route/ticket \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "codex-cli-frontend-W1",
    "task_description": "write frontend form for create_node",
    "target_files": ["frontend/src/NodeCreateForm.tsx"]
  }' | jq -r '.ticket_id')

# Step B: 带 ticket 调用文件 tool (具体机制取决于 CLI 实现)
echo "Ticket for this task: $TICKET"
export THREECAN_TICKET_ID=$TICKET

# Step C: 执行 mutating task (Codex 的 write 命令 / 或手动 edit)
# ...

# Step D: 后续 PostToolUse hook (如果装了) 自动 POST /api/activity/log
```

Hook 能力取决于客户端和配置。Codex 项目可以使用 `.codex/hooks.json`；
没有 hook 的直接 HTTP 客户端仍可使用显式 wrapper，或在确有 guarded-write
要求时手动调用 ticket/activity 端点。无需为此另建 3CAN Session 或第二份状态。

## Codex 独有优势 (vs Claude Code)

Codex 作 **前端 / schema 对接** 场景:
- Claude Code 建 INTF-* 节点描述后端契约
- Codex 查 INTF → 生成前端 type 定义 + API 客户端
- 不需要 Claude Code 把完整后端代码传给 Codex
- One historical dogfood payload comparison observed approximately 600 tokens
  for an INTF full response versus 10K+ tokens of source context. These are
  approximate context payload sizes, not total token savings and not a
  cross-tool benchmark.

**实测**: S62c-era Codex-CLI (GPT-5.4) 通过纯 HTTP API 完成跨模块开发. 12 次 route 全部命中 INTF. **Caveat**: 单次 session 手记, 非 benchmark 协议, 无 baseline 对照. 见 EVIDENCE.md §7.

## Troubleshooting

**Codex 看不懂 3CAN 返回的 Chinese description**
- 3CAN 节点多数是中文. 如果 Codex/GPT 语言模型不够好, 考虑:
  - 用 `mode=full` 让 Codex 看到完整上下文, 自行理解
  - 未来: 加 `?lang=en` 参数让 3CAN 自动翻译描述 (当前未实现)

**Codex 没带 agent_id 调 /api/route**
- activity_log 会记 `agent_id: unknown`, 审计链损失可追溯性
- 每次 route 都应带本 session/workorder 的唯一 AgentId，例如
  `agent_id: codex-cli-frontend-W1`

**多 Codex client execution 冲突**
- 每个 agent execution/workorder 使用唯一 agent_id，例如
  `codex-cli-backend-W1`, `codex-cli-frontend-W2`, `codex-cli-test-W3`；通用
  `codex-main` 会被 wrapper 拒绝
- `GET /api/agents` 返回 heartbeat TTL 投影后的登记状态，不是进程清单；
  过期登记显示 `offline`
- `POST /api/handoff/create` 在 agents 间传任务状态

## 跨 agent 协作 (Claude Code + Codex + Gemini)

```
Claude Code ────► POST /api/nodes INTF-db-foo
                  ↓
                  graph_engine 广播 "node_created"
                  ↓
Codex ─────────► GET /api/route "INTF-db-foo schema"
                  → 直接拿 schema 写前端
Gemini ───────► GET /api/briefing
                  → 见最近 activity 知道 Claude Code + Codex 做了什么
                  → 不重复劳动
```

核心: **3CAN 是共享的项目现实层**, 不同 agent 跑不同 IDE, 通过 HTTP 拉取同一份真相.

## 完整 reference

- [AGENT_BINDING.md](../AGENT_BINDING.md) — 接入 8 环详细
- [EVIDENCE.md](../EVIDENCE.md) — §7 "不能声明" 黑名单, 12 次 route 全中的 caveat
- [API_USAGE.md](../API_USAGE.md) — 所有端点参数
- [CONTRACTS.md](../CONTRACTS.md) — 节点 / 边 / 状态机
