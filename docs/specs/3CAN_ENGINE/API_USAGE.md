# 3CAN API 使用指南 (避免 runner 类误用)

> the maintainer 明确要求 (基座#5): 暴露"为什么 LongMemEval 用 slim 喂就错"的文档缺失, 须给每个场景推荐 mode / 参数配比.

## 核心选择: Mode = skeleton / slim / full / detail=true

| 场景 | 推荐 mode | 理由 |
|---|---|---|
| **agent 先扫相关节点, 按需展开** | `skeleton` + `/api/retrieve/{id}` | 省 80%+ token, 支持两段式决策 |
| **普通 route 回答问题** | `slim` (默认) | 平衡 token 和信息量, ~50 token/节点 |
| **benchmark / 评测 / 长上下文 QA** | `mode=full` 或 `detail=true` | 评测 LLM 答题需要**完整 content**, slim 截断会导致 "I don't know" |
| **前端可视化 / 完整节点查看** | `/api/nodes/{id}` (直 GET full) | 独立端点, 不走 route |
| **预算硬限 (agent 容量有限)** | 任意 mode + `budget_tokens=N` | 尾部截断, 返 `budget_truncated` 让 agent 感知 |

### 反例: LongMemEval 错用 slim (2026-04-18 session 实测)

```python
# ❌ 错: 用 slim 喂 benchmark
requests.post("/api/route", json={
    "task": "What was my first issue after car service?",
    "max_nodes": 10,
    "mode": "slim",  # summary 只截到 120 字符, LongMemEval 的 haystack turn 原文被丢
})
# → DeepSeek 拿到残缺 context, 只能答 "I don't know", benchmark 23%

# ✅ 对: 用 full 喂 benchmark
requests.post("/api/route", json={
    "task": "...",
    "max_nodes": 10,
    "mode": "full",  # 返完整 Node.model_dump(), description+notes 都在
})
```

## Endpoints 逐条说明 + 推荐参数

### POST /api/route/ticket (v9.5 S66g — 可选 guarded-write 适配)

**用途**: 项目为高风险写操作启用 ticket gate 时，先申请有 scope 的授权/证据票据。返回的 err_warnings / intf_anchors / api_usage_hints 可作为前置上下文。普通 read-only route/retrieve 不需要 ticket；未安装对应 PreToolUse hook 的项目也不依赖它。

**Body**:
```json
{
  "agent_id": "opus-main",
  "task_description": "修 runner 的 slim mode bug",
  "target_files": ["neural-memory/benchmark/longmemeval_runner.py"],
  "scope_keywords": ["runner", "benchmark", "slim mode"],
  "task_type": "Edit"
}
```

**Response**:
```json
{
  "ticket_id": "rt_abc123...",
  "issued_at": "2026-04-19T...",
  "ttl_sec": 900,
  "scope": {
    "target_files": [...],
    "scope_keywords": [...],
    "related_node_ids": ["ERR-...", "INTF-...", ...]
  },
  "err_warnings": [ {"id": "ERR-...", "name": "...", "summary": "..."} ],
  "intf_anchors": [...],
  "api_usage_hints": [...]
}
```

**使用流程**:
1. POST ticket，读取与当前 scope 精确相关的 ErrorCase / INTF / API 提示。
2. 调用 `POST /api/route/ticket/{id}/consume`，同时提交 agent、
   target digest 和 scope digest；仅同一授权意图可消费。
3. 修改完成后以相同 ticket 调用 `done`。服务端把完成请求 hash
   与 ticket 绑定；重复请求返回同一结果，不会重复创建 solution 或 evidence。
4. 只有服务端实际返回的 `resolved_errors` 才能在客户端标记为 resolved。

票据和事件存放在 SQLite/WAL 账本中，后端重启后仍可核验。旧
`route_tickets.json` / `route_ticket_receipts.jsonl` 只做一次性只读导入。
过期、agent 不同、target/scope digest 不同或 completion payload 变化时，
服务端明确拒绝并要求重新 prepare；不会用陈旧票据猜测放行。

### POST /api/activity/log (v9.5 S66g 新增 — PostToolUse 回写入口)

**用途**: 供 PostToolUse hook / agent 手动回写 "我刚做了什么". 写 hash-chained activity_log.

**Body**:
```json
{
  "agent_id": "opus-main",
  "action": "file_change",           // or bash_mutating / web_search / skill_invoke_unmatched
  "detail": "edited longmemeval_runner.py",
  "affected_nodes": ["SES-..."],
  "meta": {"tool_name": "Edit", "file_path": "..."},
  "ticket_id": "rt_abc123..."        // optional back-reference
}
```

**Response**: `{"ok": true, "timestamp": "...", "self_hash": "..."}`

### POST /api/route (主检索)

| 参数 | 类型 | 默认 | 推荐设置 |
|---|---|---|---|
| `task` | str | — | 中文/英文自然语言 query |
| `max_nodes` | int | 10 | 日常 3-6; benchmark 8-15 |
| `agent_id` | str | "unknown" | 每个 agent 起独立 id, 便于 activity_log 追溯 |
| `mode` | str | "slim" | 见上表 |
| `budget_tokens` | int | None | 400-1200 常见; 0 / 不传 = 不限 |
| `include_edges` | bool | True | 禁用可稍省 token |

**Response 关键字段**:
- `confidence`: `high` / `medium` / `low` — **low 时 agent 应 fallback 调 skill 或 WebSearch, 不要硬上**
- `fallback_hint`: 低置信时给具体建议
- `scores`: 各节点的 RRF 融合分 (仅供参考, 不作判断依据)
- `budget_truncated`: `true` 时意味着 top-K 被截, 可能漏关键节点

### GET /api/route/simple (curl / 中文 query 友好)

与 POST 语义等价, 用 query string 避免 body 引号逃逸。中文 query 直接 URL-encode, 不用考虑 JSON 转义。

```bash
curl 'http://localhost:9700/api/route/simple?q=RRF%20fusion&max_nodes=4&mode=skeleton'
```

### GET /api/retrieve/{node_id} (CCR 第二段)

**用途**: agent 从 skeleton 模式选定目标后, 单独取完整 content。

```python
# Step 1: skeleton
resp = route(task, mode="skeleton", max_nodes=6)
# Step 2: agent 判断要不要展开
if "DOC-3can-routing-dimensions-s66d" in {n["id"] for n in resp["nodes"]}:
    full = requests.get("/api/retrieve/DOC-3can-routing-dimensions-s66d?agent_id=me").json()
    # 完整 Node.model_dump()
```

**agent_id 参数**: 传 agent 自己的 id, 让 3CAN 记录"某 agent expanded 此节点", 用于热度统计。

### POST /api/nodes (建节点)

**R1 查重硬规则** (默认 `force=False`):
- 先 route 查 top1 score
- 若 ≥ 0.045 且比 top3 显著高, 返 409 拒建, 提示"考虑 PUT 更新已有节点"
- agent 确实要强建 → `?force=true`

```python
# 推荐
requests.post("/api/nodes", json={
    "id": "DEC-my-decision-xxx",   # 必须 prefix + slug
    "name": ">=10 字概括, 不要省略",
    "cluster": "架构设计",         # 手动分类
    "type": "decision",            # 9 类枚举之一
    "content": {
        "description": "60-150 字摘要 (L2, skeleton 模式返这层)",
        "notes": "详细内容 (L3, 最长 1000 字)",
        "extra": {
            "project_id": "project-id",
            "project_namespace": "project-namespace",
            "durable_authority": {
                "source_authority": "user_authoritative",
                "verification_state": "unverified",
                "evidence_refs": [],
                "authorized_by": "user"  # audit assertion, 不是身份认证
            }
        },
        ...
    },
    "activation_keywords": [...8-15 个中英双份...],
    "primary_author": "agent-id",  # 必填, audit 用
})
```

上例的 `DEC` 属于受保护 durable family，因此必须带完整 project identity 与
authority 回执；`INTF` / `PROC` / `PRJ` 相同。它们的 `PUT` 更新也必须在
`content.extra` 继续携带该回执。公共 DELETE 被拒绝；请用 authorized writeback
更新状态，或创建 replacement 后添加 `supersedes`。

### POST /api/writeback (批量字段更新)

**三触发原则** (agent 应遵守, 别乱写):
- T1: 用户显式要求
- T2: 上下文已累积 ≥ 50% session
- T3: 阶段节点 (bug修复 / 决策 / 接口 / 交接 / 新规则)

反面清单: 中间参数 / 探索查询 / LLM 草稿 / 子步骤 → 永不 writeback。

Durable `INTF` / `PROC` / `DEC` / `PRJ` 的 current 字段必须声明来源。
用户明确方向不需要 approval subsystem：

```python
requests.post("/api/writeback", json={
    "agent_id": "codex-project-W1",
    "project_id": "project-id",
    "project_namespace": "project-namespace",
    "source_authority": "user_authoritative",
    "authorized_by": "user",  # audit assertion, 不是身份认证
    "changes": [{
        "node_id": "DEC-project-current-owner",
        "field": "current_state",
        "value": "用户明确确认的新方向",
    }],
})
```

Git/CI/runtime 等机器事实使用 `source_authority=machine_verifiable`、
`verification_state=verified` 和至少一个非敏感 `evidence_refs`。未经验证的
网页、Agent 推断、activity/done 不得直接成为 durable current。这里的
`machine_verifiable` 同样是可审计声明，不是 3CAN 的认证或签名验证结论；引用的
receipt 必须能由后续 Reviewer 独立复核。

### POST /api/skills/invoke (skill 执行记录)

**通常由 PostToolUse hook 自动触发**, agent 手动调场景:
- agent 直接调某 skill 外的脚本, 想记录下来

```python
requests.post("/api/skills/invoke", json={
    "skill_id": "SKILL-user-playwright-skill",
    "agent_id": "opus-brain-main",
    "outcome": "success",  # or "fail"
    "duration_s": 42.5,
    "notes": "optional context",
})
```

### GET /api/audit/verify

开源后第三方审计用, 校验 activity_log hash chain 完整性。返回 `{valid: bool, n_entries, breaks}`。

### GET /api/briefing

**新 session 冷启动专用**。返回一个压缩 briefing, agent 一次拿到:
- 最活跃 5 agents + 任务
- 最近 5 活动
- 最近 7 天 ERR 警示 top 3

比 agent 自己 4-5 次 route 查省 80% token。Claude Code `SessionStart` hook 自动调这个。

## 典型 Agent Code Pattern

### Pattern A: 先 skeleton 扫, 再按需 retrieve

```python
# agent 不确定 context 重不重, 先窄查
resp = route(task="implement route hardening", mode="skeleton", max_nodes=4)
# agent 判断: 需要哪个深入
for n in resp["nodes"]:
    if "hardening" in n["name"] or "DEC" in n["id"]:
        full = retrieve(n["id"])  # 只展开 1-2 个, 不是全部 4 个
        break
# 省 80% token vs 一上来就 full
```

### Pattern B: confidence-gated fallback

```python
resp = route(task, mode="slim")
if resp["confidence"] == "low":
    # fallback: 试 skill 或 WebSearch
    skills = requests.get("/api/skills").json()
    # 找匹配 skill 调用, 或 WebSearch
else:
    # 正常处理 resp["nodes"]
```

### Pattern C: 建节点前 R1 查重

```python
# 先 route 看同类节点存不存在
check = route(task=new_node_description, mode="skeleton", max_nodes=3)
top1_score = max(check["scores"].values()) if check["scores"] else 0
if top1_score >= 0.045:
    # 有同类, 考虑 PUT 更新已有
    ...
else:
    # 真新, POST /api/nodes (default force=False 走 R1)
    requests.post("/api/nodes", json=...)
```

### Pattern D: Budget-constrained 路由

```python
# agent context 剩 500 token 了, 硬限
resp = route(task, mode="slim", budget_tokens=500)
if resp["budget_truncated"]:
    # 可能漏了重要节点, 记日志, 下次减少 max_nodes 或升级 mode=skeleton
```

## 容易踩的坑

1. **slim/skeleton 喂 benchmark answer LLM** → I don't know 陷阱 (实测 LongMemEval 23%, 应用 full)
2. **force=true 滥用** → 跳过 R1 查重, 节点重复爆炸
3. **agent_id 不传** → activity_log 都写 "unknown", audit 链断
4. **节点 id 不带 prefix** → route 匹配不到意图分类 (INTF-/DEC-/FEE-/ERR-/SES-/HO- 都有意义)
5. **description 留空或 <30 字符** → route 返 skeleton 时 summary 为空, agent 看不懂是什么
6. **大量 force=true 导入后不 /api/reload** → embedding cache 和节点文件不同步
7. **budget_tokens 设太小** (<80 字符) → 整个 top-K 全被截空, 等于没检索
