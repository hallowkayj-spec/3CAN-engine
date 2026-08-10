# 3CAN Engine — 架构文档

## 1. 整体结构

```
 ┌─────────────────────────────────────────────────────┐
 │  Agents (Claude Code / Codex / Gemini CLI / ...)   │   ← 不同 runtime
 └─────────────────────┬───────────────────────────────┘
                       │ HTTP (localhost:9700)
                       │
 ┌─────────────────────▼────────────────────────────┐
 │  Proxy (neural-memory/proxy/server.py)           │
 │  - Port 9700 唯一对外入口                         │
 │  - 单写 slot: green=9701 / blue=9702（一次仅一个）│
 │  - /api/admin/deploy /switch /retire /recover     │
 │  - 共享图锁下禁用自动 failover；故障时明确 503     │
 └─────────────────────┬────────────────────────────┘
                       │
 ┌─────────────────────▼────────────────────────────┐
 │  Backend (neural-memory/backend/app.py)          │
 │  - FastAPI 35+ 端点                               │
 │  - GraphEngine 实例 (graph_engine.py, 1900 LOC)   │
 └─────────────────────┬────────────────────────────┘
                       │
 ┌─────────────────────▼────────────────────────────┐
 │  GraphEngine 核心                                │
 │  - 1385 节点 / 1015 边 / 9 类节点类型             │
 │  - BGE-M3 dense embedding (1024-d)                │
 │  - bge-reranker-v2-m3 cross-encoder               │
 │  - 4-signal RRF fusion (Cormack 2009)             │
 │  - Leiden community detection (Traag 2019)        │
 │  - Activation heat + lifecycle decay              │
 └─────────────────────┬────────────────────────────┘
                       │ 持久化
 ┌─────────────────────▼────────────────────────────┐
 │  graph/                                          │
 │  ├── nodes/*.json        节点本体 (每节点一个文件)│
 │  ├── edges.json          边列表                   │
 │  ├── embeddings.npz      BGE-M3 向量缓存          │
 │  ├── agents.json         agent 注册表             │
 │  ├── activity_log.json   事件日志 (hash chain)    │
 │  ├── click_log.json      route outcome 反馈       │
 │  └── pending_keywords.json  Miss Healer 缓冲      │
 └──────────────────────────────────────────────────┘
```

## 2. 节点数据模型

```python
# neural-memory/backend/models.py
class Node(BaseModel):
    id: str                        # prefix + slug, 如 "SES-20260415-S62"
    name: str                      # 节点概括, 10+ 字符
    cluster: str                   # 手动分类 (架构设计/项目交接/...)
    layer: str = "L0"              # L0-L5 (C³AN 层级)
    type: NodeType                 # 9 类枚举
    status: NodeStatus             # active/dormant/blocked/deprecated
    content: NodeContent           # 详见下
    activation_keywords: list[str] # 5-15 个 kw, 中英双份
    priority: Priority             # critical/high/medium/low
    activation_count: int          # 使用热度
    created_at / updated_at
    primary_author / contributors
```

**9 类 NodeType**:
- `knowledge` — 领域知识
- `process` — 流程/管线
- `tool` — 工具/技术栈
- `config` — 配置/环境
- `reference` — 外部引用
- `secret` — 密钥/凭证 (仅存引用名)
- `session` — 会话记录
- `decision` — 架构决策
- `feedback` — 用户反馈/规则
- `skill` — SKILL.md 对应 (v9.1 新增)

**NodeContent 字段**:
- `description` — L2 摘要 (60-150 字), route skeleton 模式返这层
- `current_state` — 当前状态
- `notes` — L3 细节 (最长 1000 字)
- `key_files` / `api_refs` / `decisions` / `tech_stack`
- `extra` — 开放字段, 存 `community_id` / `quality_score` / `gdi` / skill metadata 等

## 3. 边模型

```python
class Edge(BaseModel):
    source: str                    # node_id
    target: str
    type: EdgeType                 # 14 类；v0.2 增加错误分组、解决、验证、适用、取代和复发关系
    weight: float = 1.0
    description: str = ""
```

基础关系仍为 `depends_on` / `feeds_into` / `informs` / `updates` /
`validates` / `triggers` / `requires` / `blocks`。v0.2 错误知识关系为
`grouped_in` / `resolves` / `verified_by` / `applies_to` / `supersedes` /
`regressed_from`。其中 `resolves` 必须配套 `verified_by` 证据；单独存在的
历史错误边不能成为通用任务的全局阻塞器。

## 4. Route 管线 (核心检索流程, graph_engine.route())

```
Query "RRF fusion routing"
       │
       ├─ Step 0: Query 分析
       │   - _classify_intent() → ('status', ['MOD-', 'SEC-', 'PROG-'])  MAGMA 风格意图
       │   - _expand_query() → 加 3-5 个共现词 (高区分度)
       │   - is_short_code 判定 (S62/KB4 类 ≤8 字符 字母+数字)
       │
       ├─ Step 0.5: 短代码 code_index 解析
       │   - 自动从节点 ID+name+desc+kws 抽短码, 建反向索引
       │   - query 是短码 → 直接返候选 (RRF 第 4 信号)
       │
       ├─ Step 1: 3-signal 打分 (每节点 3 路独立)
       │   ├─ Signal 1 — emb: BGE-M3 余弦相似 + priority_bonus
       │   ├─ Signal 2 — kw:  kw_idf(kw) 匹配 (v9.0 IDF 加权) + tier_boost + intent_score*0.5
       │   └─ Signal 3 — hybrid: 自适应加权融合 (w_emb/w_kw/w_int 按 kw_ratio 动态)
       │
       ├─ Step 2: RRF Fusion (Cormack 2009)
       │   - 各 signal 独立排序 → 1/(K+rank), K=60
       │   - code_index 作为 Signal 4 注入 (短码 query 时)
       │   - rrf_scores = Σ w_i / (60 + rank_i)
       │
       ├─ Step 3: Graph Traversal Boost
       │   - top-3 节点的 edge 邻居 +0.001*weight
       │
       ├─ Step 3.5 (v9.2): Leiden Community Boost
       │   - top-3 的 community_id 集合
       │   - 同 community 的其他节点 +0.002
       │   - 主要涨 R@3 (兄弟节点一起浮出)
       │
       ├─ Step 4: Cross-encoder Re-ranking (top-15 → top-K)
       │   - 优先 bge-reranker-v2-m3 (中文原生)
       │   - fallback FlashRank
       │   - alpha=0.5: 0.5*RRF + 0.5*reranker (_minmax 归一)
       │
       ├─ Step 5: dormant 补全 (如 top-K 不足, 从 dormant 池按 hybrid 分取前 N)
       │
       └─ Step 6: 永不返回空 (最后兜底返最近更新 K 个 active 节点)

       ▼ 返回
 RoutingResponse(activated_nodes, relevant_edges, scores) + v9.2 加 confidence/confidence_meta/fallback_hint
```

## 5. 响应模式 (v9.0 Entroly 风格 CCR)

| mode | 每节点 token | 字段 | 场景 |
|---|---|---|---|
| `skeleton` | ~20 | id, name, kw[:6], summary[:80] | agent 先看索引, 按需 /api/retrieve/{id} 取全量 |
| `slim` (默认) | ~50 | id, name, cluster, type, summary[:120], current_state[:80], key_files[:3] | 常规 route |
| `full` (或 detail=true) | ~500-800 | 全量 Node.model_dump() | 需要完整上下文 |

Response 含 `confidence` ('high'/'medium'/'low') + `fallback_hint` (低置信触发 skill/WebSearch)。

## 6. 关键端点清单

| 端点 | 方法 | 用途 |
|---|---|---|
| `/api/route` | POST | 主检索, body: `{task, max_nodes, mode, budget_tokens, agent_id}` |
| `/api/route/simple` | GET | 旁路 (curl 友好), 相同参数 query string |
| `/api/retrieve/{id}` | GET | CCR 第二段, 取 full Node + 记 expand 活动 |
| `/api/nodes/{id}` | GET | 获取节点全文 + Miss Healer outcome |
| `/api/nodes` | POST | 建节点 (自动 R1 查重, 除非 force=true) |
| `/api/nodes/{id}` | PUT | 更新节点 (增量 embedding) |
| `/api/edges` | POST/GET/DELETE | 边 CRUD |
| `/api/writeback` | POST | agent 批量回写节点字段 |
| `/api/route/outcome` | POST | agent 反馈 "此 route 有效/无效" 给 Miss Healer |
| `/api/route/feedback` | POST | 更细粒度的反馈 (hit/miss/wrong) |
| `/api/agents/checkin` | POST | agent 注册 + 当前任务 |
| `/api/agents` | GET | agent 注册表 |
| `/api/briefing` | GET | 冷启动 briefing, agent 第一次接入用 |
| `/api/activity` | GET | 查活动日志 (近 500 条) |
| `/api/skills` | GET | 列 SKILL-* 节点 (v9.1) |
| `/api/skills/invoke` | POST | 记 skill 调用 outcome (v9.1) |
| `/api/audit/verify` | GET | 校验 activity_log hash chain 完整性 (v9.3) |
| `/api/stats` | GET | 全局统计 |
| `/api/health/scan` | GET | 孤立节点 / 零激活 / prefix 分布 |
| `/api/admin/state` / `deploy` / `switch` / `retire` / `recover-active` | - | 单写 slot 状态、部署、切换、回收与显式 active 恢复；不能并行运行两个共享图 writer |

## 7. 性能数据 (1385 节点, Windows + CPU + BGE-M3)

- 冷启动: 4-20 min (BGE-M3 encoding 1385 节点, Windows Python 3.14 CPU 单机)
- 启动后 cache 载入: 15-30 秒
- 单次 route: P50 4.4s / P95 5.6s (含 bge-reranker crossencoder)
- skeleton mode token: ~20/节点 (vs full ~500, -96%)
- 内存占用: ~2 GB (BGE-M3 + reranker + 节点 cache)

## 8. 部署拓扑

```
开发者机器:
├─ C:\Python314\python.exe  (系统 Python, 3CAN 用)
├─ neural-memory/
│  ├─ proxy/server.py        : 端口 9700 (唯一入口)
│  ├─ backend/app.py         : 端口 9701 (green) 或 9702 (blue)
│  ├─ frontend/index.html    : 3d-force-graph 可视化
│  └─ graph/                 : 数据持久化
└─ ~/.claude/
   ├─ secrets.json            : API keys (DeepSeek 等)
   ├─ skills/*/SKILL.md       : 用户级 skill, 3CAN 自动同步
   └─ scripts/hooks/         : UserPromptSubmit / SessionStart hook
```

冷启命令:
```bash
# 启动 backend (green)
cd neural-memory/backend && python app.py --port 9701

# 启动 proxy
cd neural-memory/proxy && python server.py   # 绑定 9700

# v0.2 共享图目录受独占锁保护，不能同时启动另一个 writable standby。
# 更新必须走受控单写切换：精确确认旧 writer 退出后再部署另一 slot；
# 切换窗口允许 9700 短暂返回 503。普通 deploy 会以 409 拒绝危险并发。
```

### 8.1 单写切换边界

`GraphEngine` 初始化即取得共享图目录的独占 runtime lock。因此 green 与
blue 是两个可轮换端口，不是两个可同时健康的 writer。代理必须满足：

- active 未经 OS identity 与退出确认前，不得 spawn target；
- inactive stale PID 只有在 identity 完整、PID 明确不存在、端口明确空闲时
  才能回收；检查 unavailable、PID 复用或端口被占用都必须 fail closed；
- target 的 `/api/stats` 必须返回合法、非负的 node/edge 数且达到部署阈值
  后，才能修改 `state.active`；
- 中断后仅可用显式 `confirm: "recover-stopped-active"` 恢复当前 active，
  且仍须重新证明旧 PID 不存在、端口为空，再写入新 managed identity；
- single-writer 模式禁用自动 failover；active 不可用时返回明确 503；
- 当前 release 没有 per-slot immutable release root，也没有自动代码回滚。
  因此它不宣称零停机升级；完整 transactional cutover 是后续独立能力。
