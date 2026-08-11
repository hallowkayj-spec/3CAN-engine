# 3CAN 契约层

> 与 [PROTOCOL.yaml](./PROTOCOL.yaml) 配套. Protocol 定"端点/方法", Contracts 定"数据结构/状态机".

## 1. 数据结构 Schemas

位于 `schemas/` 子目录, JSON Schema Draft 2020-12.

- [node.schema.json](./schemas/node.schema.json) — Node 节点
- [edge.schema.json](./schemas/edge.schema.json) — Edge 边
- [activity.schema.json](./schemas/activity.schema.json) — ActivityEntry 事件

## 2. 运行时类型与语义 ID family

这两个维度不得混用：

- `Node.type` 是粗粒度的存储/行为分类，例如 `knowledge`、`process`、
  `session`、`decision`。它决定运行时如何处理节点。
- 节点 ID 的首段是项目语义角色，例如 `INTF`、`PROC`、`DEC`、`SES`、
  `HO`。需要语义角色时统一调用 `models.semantic_id_family(node_id)`。

一个语义 family 可以映射到多个 runtime type；现有节点无需迁移，也不新增
第二个 type 字段。下表的“常见 runtime type”只是惯例，不是等价关系。

| 前缀 | 语义 | 常见 runtime type | 例 |
|---|---|---|---|
| DEC | 架构决策 | decision | DEC-3can-route-opt-s66c |
| DOC | 文档 / 项目文档 | reference / knowledge | DOC-3can-routing-dimensions-s66d |
| FEE | feedback 规则 | feedback | FEE-3can-test-lag-architecture-first |
| ERR | 错误教训 | feedback / knowledge | ERR-longmemeval-runner-slim-mode |
| SES | 会话记录 | session | SES-20260418-S66d-base-gap-fix |
| HO | handoff 交接 | session | HO-2026-04-18-S66d-3can-deep-opt |
| INTF | 接口契约 (独有) | knowledge | INTF-scripts-build-flux-train-v3 |
| MOD | 项目模块 | knowledge | MOD-advisor / MOD-kb |
| SEC | 密钥凭证引用名 | secret | SEC-deepseek / SEC-autodl |
| MCP | MCP 工具节点 | tool | MCP-firecrawl / MCP-playwright |
| MEM | memory 文件引用 | reference | MEM-feedback-gpt54-pricing |
| RES | 本地资源 | reference | RES-hf_hub-bge-m3 |
| ARCH | 架构历史 | knowledge | ARCH-3can-memory-tier-4-layer |
| SKILL | SKILL.md 映射 | skill | SKILL-user-dev-browser |
| PROPOSED | 待审批 | (任意, status=dormant) | PROPOSED-FEE-xxx-20260418 |
| STR | 策略 / 战略 | knowledge | STR-platform-strategy |
| AGT | agent 关联记录 | knowledge | AGT-opus-brain-main |
| TASK | 任务列表节点 | process | TASK-s66-project-roadmap |
| PRO | 协议/流程 | process | PRO-3can-protocol |

## 2.1 项目现实与执行绑定

Durable project identity 由 tracked `.agents/project.json` 的
`normalized Git repository + project_id + project_namespace` 构成。
Git common-dir hash 只标识本机 clone/worktree family；physical worktree hash
只绑定当前 writer。两者组合为不含绝对路径的 `workspace_id`，不能代替跨机器
项目身份。

Agent check-in、route、ticket 与 writeback 复用同一解析结果。已有 WorkorderId
才传播；不存在时保持缺失，ticket ledger 的兼容投影可显示 `unspecified`，但不
制造一个历史 Workorder。Endpoint 由 `THREECAN_URL` 等运行环境配置提供，
不写入 tracked project capsule。

## 2.2 当前现实、历史与 supersession

- current/canonical/owner/path 查询优先 durable semantic families，并后置
  `SES`/`HO`；普通 history/continuation/handoff 查询保持原行为。历史查询若明确
  要求 evidence/source/boundary，则先给 durable source pointer，再保留历史叙述。
- `status=deprecated`、`content.extra.invalidated_by/superseded_by/replaced_by`
  或被 `new --supersedes--> old` 指向的节点，不再参与当前现实竞争。
- project-scoped Core Memory 只有在 metadata 证明 project/namespace 匹配时
  才可 `must_consume`；未知适用性退回可选语义召回。
- Git branch、worktree、PR、CI、Agent 与 Workorder 是外部可变事实。3CAN 可
  返回 source pointer，但必须同时声明需要实时外部核验。
- 历史 ErrorKnowledge 继续通过 explicit-error intent 精确召回，不进入普通
  current route 或 hot-topology 权重。

## 3. 节点状态机

```
           ┌────────────────────────────────────────┐
           │                                        │
           │  dormant → active (近期明确使用后复活)   │
           ▼                                        │
  ┌────► active ───30d 未命中───► dormant ───60d───► archived
  │         │                         │                  │
  │         └── 人工 deprecate ───────┴──────►  deprecated
  │                                                      │
  │                                                      ▼
  │                                                    (status=deprecated
  │                                                     不会复活, 视为历史节点)
  └── blocked (人工设, 跟 active 一样但禁止某些 action)
```

**关键规则**:
- **lifecycle sweep 永不自动删除**. `archived` 是图内真实状态；默认 route 排除，
  明确历史查询和 `GET /api/nodes/{id}` 仍可读取。manual public DELETE 只适用于
  unprotected、且不属于 supersession lineage 的节点。
- **复活**: dormant 节点在带 route correlation 的 exact read 记录
  `last_accessed_at` 后可由 sweep 恢复；archived 节点必须
  显式更新 `status=active`，普通读取不会静默把历史重新变成 current。
- **PROPOSED 审批流**:
  ```
  LLM 生成建议 (kw/edge/skill/short-code)
     ↓
  POST /api/nodes force=true id=PROPOSED-*, status=dormant
     ↓
  the maintainer 审阅:
    - 通过 → PUT /api/nodes/{id} status=active + 改 id 去 PROPOSED- 前缀 (or 直接保留)
    - 拒绝 → PUT status=deprecated (不删, 留痕)
  ```

## 3.1 Durable-current authority

`POST /api/writeback` 对 `INTF` / `PROC` / `DEC` / `PRJ` 的
`current_state`、`description`、`status`、`blockers`、`tech_stack` 使用三类
来源声明：

- `machine_verifiable`: 必须同时有 `verification_state=verified` 和至少一个
  `evidence_refs` source pointer；该字段表示调用者声明该证据可独立复核，不表示
  3CAN 已替调用者完成密码学验证；
- `user_authoritative`: 必须有 `authorized_by=user`；这是 provenance/audit
  assertion，不是 cryptographic authentication 或 security boundary；
- `untrusted_inferred`（也包括缺失声明）: 不得直接修改 durable current。

上述两类 metadata 都是 provenance/audit declaration，不是 credential、RBAC 或
approval security boundary。`notes` 和 `last_session` 仍可低 ceremony 回写；它们
不是 current authority。

受保护 family 的公共 `POST /api/nodes` 与 `PUT /api/nodes/{id}` 继续可用，但
`content.extra` 必须携带完整 project ID、namespace 与同一 `durable_authority`
回执。公共 DELETE 不允许删除这些 durable-current 节点；状态变更走 authorized
writeback，替代关系走 `supersedes`。这保留完整 NodeCreate/NodeUpdate 能力，且
不新增第二套审批协议。

项目已有 identity 时，durable-current writeback 必须携带并匹配 project ID 与
namespace。合法来源回执记录在既有 `content.extra.durable_authority`，不新建
审批数据库。writeback 不会把已有 unscoped/global 节点自动占为某个项目。
通用 `supersedes` 只允许同 semantic family；任一端已有 project identity 时，
双方必须完整且精确匹配。受保护 family 的 replacement source 还必须 active 并
带合法 durable authority。`supersedes` 边不能从公共 DELETE 移除，受保护 family
也不能通过 generic merge 绕过该契约。普通 route 默认排除 superseded target，
只有明确 history 查询可恢复；supersedes 任一端点也不能通过 generic node DELETE
抹去 lineage。lifecycle sweep 不会仅因低活跃度衰减尚未 supersede 的受保护 current
节点。

## 3.2 Serious-milestone recovery

HTTP write success 不等于下一 Agent 可恢复。重要 durable milestone 应显式运行
`neural-memory/benchmark/milestone_recovery_probe.py`：先绑定 deep readiness
schema/mode 与 `expected_graph_root_sha256`，再用新的 AgentId 执行现有 skeleton
route。它只对成功路由的 expected node 做不带 feedback correlation 的精确读取；
每个 critical/evidence fact 必须以 `node_id` 绑定一个 expected node，不能跨节点
拼接凑出 PASS。匹配范围只包含节点身份与 durable content 字段；低 ceremony 的
`notes`、`last_session` 和学习关键词不能构成事实证明。任一缺失返回 `PARTIAL`；
该工具不引入第二存储或后台任务。

## 4. R1 查重协议 (创节点前先查)

**Hard Rule (CLAUDE.md / 01-core.md §4)**: 建节点前必须先 POST /api/route, 判断:

```python
existing = route(task=new_node_name + " " + description, mode="skeleton", max_nodes=3)
top1 = max(existing.scores.values()) if existing.scores else 0
if top1 >= 0.045 and top1 显著高于 top3:
    # 拒建, 返 409
    return {"error": "R1 duplicate detected", "hint": "考虑 PUT 更新已有节点", "top1_node": ...}
# 否则允许建
```

用 `force=true` 可绕过 (仅供工具批量同步, 如 skill_sync / session_aggregator)。

## 5. Hash Chain 验证协议

每条 `ActivityEntry`:
```
prev_hash = 前一条的 self_hash (链首=64 个 '0')
self_hash = sha256(f"{timestamp}|{agent_id}|{action}|{detail}|{','.join(sorted(affected_nodes))}|{json.dumps(meta, sort_keys=True)}|{prev_hash}")
```

**校验**:
```python
GET /api/audit/verify
→ {
    "valid": true/false,
    "n_entries": <int>,
    "breaks": [
      {"idx": <i>, "ts": <timestamp>, "reason": "self_hash mismatch | prev_hash mismatch"}
    ]
  }
```

**截断策略**: `activity_log.json` 保留最近 500 条, 截断时断链 (老 hash 从文件开头失去前向链). 开源后可加 `activity_chain.jsonl` append-only 永久日志, 现未做。

## 6. WebSocket 事件协议 (v9.4)

连接: `WS /ws`

事件 payload:
```json
{
  "event": "node_created" | "node_updated" | "node_deleted" | "edge_created" | "edge_deleted" | "handoff_created" | "writeback" | "preference_learned" | "graph_reload_started" | "graph_reloaded" | "nodes_merged" | "batch_dormant" | "agent_checkin" | "agent_task_update" | "activity_log_appended",
  "node": { ... } | "node_id": "...",
  "timestamp": "..."
}
```

Agent 订阅后可实时感知其他 agent 动作 (基座#6)。订阅 filter 未实现, 当前广播所有事件。

## 7. Agent Binding 协议 (参见 [AGENT_BINDING.md](./AGENT_BINDING.md))

Agent 接入 3CAN 最低要求:
1. 唯一 agent_id (kebab-case, ≤30 字符)
2. 启动时 POST /api/agents/checkin 注册
3. 每次 route 传 agent_id (用于 activity_log 追溯)
4. Writeback 时 primary_author=agent_id
5. (推荐) 订阅 `WS /ws` 感知其他 agent 动作
6. (推荐) 启动时调 GET /api/briefing 拉冷启动摘要

## 8. 版本兼容性承诺

| 变更类型 | 版本号 | 向后兼容? |
|---|---|---|
| 新端点 / 新字段 | MINOR (9.3→9.4) | ✅ |
| 删端点 / 改字段语义 | MAJOR (9→10) | ❌ (破坏性) |
| bug 修复 | PATCH | ✅ |

当前版本 **v9.4** (2026-04-18 稳定).

## 8.1 Readiness 的两种证据

`physical_integrity` 继续由 immutable runtime identity、graph/engine hash、
embedding 对齐与 deep readiness 决定，并且是 `production_ready` 的唯一输入。

`effective_project_reality` 是只读诊断投影，分别报告 raw graph、historical
archive、ordinary hot nodes、durable-current candidates、SES/HO 与 hot/history
relations。它的 semantic quality 在有固定 gold 的真实查询基准完成前保持
`validating`，不是生产 hard gate，也不能用节点数量冒充项目理解质量。

## 9. 错误码语义

| HTTP | code | 含义 | agent 应对 |
|---|---|---|---|
| 400 | invalid_request | body/params 不符 schema | 看错误消息修改请求 |
| 404 | not_found | node_id / agent_id 不存在 | 先查 stats / 确认 id |
| 409 | r1_duplicate | 查重拒绝建节点 | 改用 PUT 更新 existing |
| 422 | quality_warning | strict=true 时节点质量不达标 | 补齐 description / kws |
| 429 | writeback_rate_limited | 同一 agent/node 在 60 秒内已完成 5 次成功 mutation | 等待窗口；非法请求、同批重复 node 和 no-op 不计数 |
| 503 | backend_unavailable | proxy 转发失败或 single-writer 切换窗口 | 保留请求上下文并重试；不要假设自动 failover |
