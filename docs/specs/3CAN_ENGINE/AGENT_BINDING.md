# 3CAN Agent Binding 架构衔接规范

> Agent (Claude Code / Codex / Gemini CLI / Cursor / 自写 agent) 接入 3CAN 的标准规范. 与 [PROTOCOL.yaml](./PROTOCOL.yaml) + [CONTRACTS.md](./CONTRACTS.md) 配套.

## 1. 最小可用接入 (3 步)

```
Step 1: agent 启动时 POST /api/agents/checkin
Step 2: 每次 query 走 POST /api/route 或 GET /api/route/simple
Step 3: 阶段成果走 POST /api/writeback 或 /api/nodes
```

3 步齐了, agent 就接入了。

## 2. 完整接入 (8 个环节)

### 2.1 Agent 注册 (一次性 + 每次 session 复检)

```http
POST /api/agents/checkin
{
  "agent_id": "opus-brain-main",       // 必须, kebab-case, ≤30 字符
  "name": "Opus 主脑",                  // 可选, 展示用
  "role": "Strategy + Dispatch",        // 可选
  "current_task": "3CAN v9.4 基座修复",  // 推荐每次 session 更新
  "session_id": "session-abc-123",      // 可选, 跨 session 追溯用
  "capabilities": ["code", "strategy"], // 可选, 自我描述
  "meta": {"priority": "high"}          // 开放
}
```

**规则**:
- `agent_id` 全局唯一. 冲突时旧记录被覆盖 (建议不同 agent 不同 id 前缀: opus-xxx / codex-xxx / gemini-xxx)
- 每次新 session 应重新 checkin 更新 `current_task`
- `last_checkin` 由 3CAN 自动记录, 是判断 "agent 是否活跃" 的依据

### 2.2 冷启动 briefing (可选但强推荐)

```http
GET /api/briefing
→ {
  "agents_active": [...],                // 最近活跃 5 agents + 各自任务
  "recent_activity": [...],              // 最近 5 条 activity
  "err_warnings_7d": [...],              // 最近 7 天 ERR 警示 top 3
  "project_state": {...}                 // 图谱整体状态
}
```

Agent 新 session 第一次用 3CAN 时调一次, ~400 token 拿全局, 省下自己 4-5 次 route 查。

### 2.3 Route 查询 (主流量)

见 [API_USAGE.md](./API_USAGE.md). 关键:
- **每次带 `agent_id`** — 否则 activity_log 记 "unknown", 审计链断
- 选对 `mode`: 日常 `slim`, benchmark 用 `full`, 先扫用 `skeleton`
- 看 `confidence` — `low` 时触发 fallback (skill / WebSearch), 不要硬答

### 2.4 Skill 调用追踪 (自动 or 手动)

**推荐**: 装 PostToolUse hook (`3can-post-tool-capture.js`), SlashCommand 工具自动 POST 到 `/api/skills/invoke`。

**手动**: 如果 agent 直接执行了 skill 脚本不通过 Claude Code:
```http
POST /api/skills/invoke
{
  "skill_id": "SKILL-user-dev-browser",
  "agent_id": "opus-brain-main",
  "outcome": "success",             // or "fail"
  "duration_s": 42.5,
  "notes": "可选"
}
```

### 2.5 Writeback (阶段成果回写)

**三触发原则** ([01-core.md](../../.claude/rules/01-core.md) §4):
- T1: 用户显式要求
- T2: 上下文累积 ≥ 50% session
- T3: 阶段节点 (bug 修复 / 决策 / 接口 / 交接 / 新规则)

**反面清单 (永不 writeback)**:
- 中间参数 / 探索查询 / LLM 草稿 / 子步骤

### 2.6 建新节点 (遵守 R1 查重)

```python
# R1 先查
check = route(task=new_node_name, mode="skeleton", max_nodes=3)
top1 = max(check["scores"].values()) if check["scores"] else 0
if top1 >= 0.045:
    # 拒建, 考虑 PUT 更新已有
    return
# 真新, 走 POST /api/nodes
post("/api/nodes", json={...})
```

必填字段: `id` (带 prefix) / `name` (≥10 字符) / `cluster` / `type` / `activation_keywords` (≥5 个中英双份) / `primary_author` (agent_id)

### 2.7 订阅实时事件 (可选, 推荐多 agent 场景)

```python
import websockets

async with websockets.connect("ws://localhost:9700/ws") as ws:
    while True:
        msg = await ws.recv()
        event = json.loads(msg)
        # event = {"event": "node_created|node_updated|writeback|agent_checkin|...", ...}
        if event["event"] == "writeback":
            # 其他 agent 刚回写了节点, 我是不是要刷新 context
            ...
```

**事件列表**: 见 [CONTRACTS.md §6](./CONTRACTS.md)

**当前限制**: 无 filter, 订阅即收全部事件. 如需按 agent 过滤, 自写 filter 层。

### 2.8 生命周期感知

**强制**: agent 不需要关心生命周期 (3CAN 自动 lifecycle_sweep).
**但不要**直接改 `status` 字段. 要归档就通过 /api/nodes/batch-dormant 或手动 deprecate。

## 3. Hooks 集成 (Claude Code / Codex)

如果用 Claude Code, 装这 4 个 hook 到 `~/.claude/scripts/hooks/` 并注册 `~/.claude/settings.json`:

| Hook | 事件 | 作用 |
|---|---|---|
| `3can-cold-start.js` | SessionStart | 启动时注入 /api/briefing 摘要到 agent |
| `3can-prompt-observer.js` | UserPromptSubmit | 检测 the maintainer 纠错 / 新概念, 提醒 agent 走 WebSearch |
| `3can-post-tool-capture.js` | PostToolUse | 自动抓捕 SlashCommand / Edit / Write, 回写 skill invoke + file change |
| `3can-pre-compact-writeback.js` | PreCompact | compact 前扫本 session 新文件, 建 DOC-autowrite-* 节点 |

Codex 用户: Codex 还不支持 Claude Code 那种原生 hook (检查官方更新). 当前推荐用
Codex-side wrapper 模拟 hook/harness:

```powershell
scripts\codex-3can.cmd bootstrap -Role frontend -Task "current task"
$prepareResult = scripts\codex-3can.cmd prepare -TaskDescription "edit focused area" -TargetFiles path/to/file -ToolName apply_patch -ToolInputSummary "edit focused area" | ConvertFrom-Json
$ticketId = $prepareResult.prepare.ticket_id
if (-not $ticketId) { throw "prepare did not return ticket_id" }
scripts\codex-3can.cmd done -TicketId $ticketId -Detail "what changed and why"
scripts\codex-3can.cmd compact -TaskSummary "continuation state" -TargetFiles path/to/file
```

This is not automatic interception, but it gives Codex the same operational
shape: SessionStart, PreToolUse ticket, PostToolUse activity, and PreCompact
continuation. `done` must receive the exact `ticket_id` returned by its own
`prepare`. The helper derives one stable execution-specific AgentId; the wrapper
stores no ticket-selection state and never adds compact files implicitly.

## 4. 自定义 Agent 最小代码示例

```python
# my_agent.py
import requests, json

AGENT_ID = "my-custom-agent"
BASE = "http://localhost:9700"

def checkin(task: str):
    requests.post(f"{BASE}/api/agents/checkin",
                  json={"agent_id": AGENT_ID, "current_task": task})

def ask(query: str, mode="slim"):
    r = requests.post(f"{BASE}/api/route", json={
        "task": query, "max_nodes": 5, "agent_id": AGENT_ID, "mode": mode
    })
    data = r.json()
    if data.get("confidence") == "low":
        print(f"Low confidence, hint: {data.get('fallback_hint')}")
    return data["nodes"]

def remember(decision: str, details: str):
    # R1 先查
    check = requests.post(f"{BASE}/api/route",
                          json={"task": decision, "max_nodes": 3,
                                "agent_id": AGENT_ID, "mode": "skeleton"})
    top1 = max(check.json().get("scores", {0: 0}).values())
    if top1 >= 0.045:
        return None  # 已有类似, 不建
    slug = decision.lower().replace(" ", "-")[:40]
    return requests.post(f"{BASE}/api/nodes", json={
        "id": f"DEC-{slug}-{int(time.time())}",
        "name": decision,
        "cluster": "架构设计",
        "type": "decision",
        "content": {"description": details, "notes": details},
        "activation_keywords": [slug] + decision.split(),
        "primary_author": AGENT_ID
    }).json()

# 使用
checkin("实现用户登录")
nodes = ask("之前有没有处理过 OAuth?", mode="skeleton")
remember("选择 Google OAuth 而非 GitHub", "因为目标用户多为设计师, Google 账户渗透率高")
```

## 5. 多 Agent 协作模式 (the maintainer 核心场景)

**场景**: Claude Code (opus-brain-main) 出主脑, Codex (codex-cli) 做前端, Gemini CLI (gemini-cli) 做数据分析. 三者共享 3CAN.

**推荐**:
- 每 agent 唯一 agent_id (opus-brain-main / codex-cli / gemini-cli)
- 每 agent 注册时 `role` 标记角色 (strategy / frontend / data)
- 共享同一套 rules (`.claude/rules/01-core.md`)
- 订阅 WS 实时感知彼此动作
- 跨 agent 任务交接通过 handoff 节点 (POST /api/handoff/create)

**冲突处理**:
- 当前 3CAN 无锁. 两 agent 同时改同节点 = 最后写的赢
- 建议不同 agent 负责不同节点前缀 (opus 写 DEC/SES/HO, codex 写 INTF, gemini 写 RES/MEM)
- 真正的 multi-agent 一致性是未来工作 (distributed lock / CRDT)

## 6. 最低兼容性矩阵

| Agent / Runtime | 测过 | 接入难度 |
|---|---|---|
| Claude Code (Anthropic) | ✅ 生产 | 极低 (hook 原生) |
| Codex (OpenAI) | ⚠️ 部分测过 | 低 (HTTP 直接用, 但 hook 支持待确认) |
| Gemini CLI (Google) | ❌ 未测 | 中 (HTTP 可用, hook 需适配) |
| Cursor | ❌ 未测 | 中-高 (需 shell 扩展启 hook) |
| Aider / Goose / OpenCoder | ❌ 未测 | 理论可用 |
| 自写 Python/JS agent | ✅ 样例跑过 | 低 (HTTP 直接用) |

## 7. 常见错误模式

1. **不传 agent_id** → activity_log 全是 "unknown", 审计追溯断, 热度统计混乱
2. **忘了 R1 查重** → 节点重复爆炸, 图谱污染
3. **用 skeleton 喂 benchmark answer LLM** → DeepSeek 拿残缺 context 说 "I don't know" (LongMemEval pilot 23% 的真实教训)
4. **不订阅 WS** → 其他 agent 改了节点, 自己 agent 不知情, 重复劳动
5. **直接 PUT 修改 status** → 绕过 lifecycle_sweep, 可能丢活跃度; 应该让 3CAN 自动判
