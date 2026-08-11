# 3CAN Engine — 11 项能力详解

每项含: **是什么 / 代码文件 / 实测数据 / 归属**。

## 1. 记忆精确指引 (Route + Hybrid Retrieval + Reranker)

**是什么**: 4-signal RRF + cross-encoder 精排, 把 agent 的自然语言 query 对应到 3CAN 图谱的 top-K 节点。

**代码**: `neural-memory/backend/graph_engine.py:683-880 route()`

**当前可复现证据**: 16-node public synthetic seed fixture 的内容寻址
46-query receipt: MRR 0.9783, R@1 0.8261, Hit@3 1.0。它只证明 release seed
graph，不代表真实 OPC 或 production graph。v9.2 的 MRR 0.9239 / P50 4.4s
属于缺少冻结私有 graph 与逐题 receipt 的历史记录，不是当前 PASS。

**归属**: RRF (Cormack 2009), BGE-M3 (BAAI 2024), bge-reranker-v2-m3 (BAAI 2024), FlashRank fallback (PrithivirajDamodaran)。 详见 [ATTRIBUTION.md](./ATTRIBUTION.md) A1-A3。

## 2. 项目协作管理 (Agent 注册 + Handoff + Activity Log)

**是什么**: agent checkin 注册 + 跨 session 的 handoff 通知 + 活动日志 (hash chain 审计)。

**代码**:
- `graph_engine.py:1774-1810 log_activity()` (v9.3 hash chain 版)
- `app.py:/api/agents/checkin /api/agents /api/handoff/*`
- `agents.json + activity_log.json` 持久化

**独特性**: Mem0/Letta/Zep 都不做 agent 注册表 + 活动审计。这是 3CAN 北极星 2 (项目协作管理) 的核心。

## 3. Token 整盘诊断 + 瘦身 (the maintainer 明确高权重)

**是什么**: CCR 3 档 + budget 硬限 + IDF 降权 + Leiden 减噪 + confidence fallback。

**实测** (query='RRF fusion', 6 节点):
- skeleton 574 tokens (vs full 4266, -86.5%)
- slim 796 tokens
- budget_tokens 可硬限总包

**代码**: `app.py:305-366`, `graph_engine._kw_idf()`, `tools/leiden_community.py`

**详见**: [TOKEN_OPTIMIZATION.md](./TOKEN_OPTIMIZATION.md) (单独分卷, 因为核心差异)

## 4. 错误+偏好记忆优化 (ERR-* / FEE-* / Observer Hook)

**是什么**: 错误教训和反馈规则作为**专类节点**存档, 配合 Claude Code 的 UserPromptSubmit hook, 在 the maintainer 纠错信号时主动提示 agent route 历史 ERR。

**代码**:
- `~/.claude/scripts/hooks/3can-prompt-observer.js` — 检测纠错信号 + 新概念
- `tools/observer_llm_analyzer.py` — async DeepSeek 分析日志, 生成 PROPOSED-* 节点
- 节点 ID 前缀: `ERR-*` (错误), `FEE-*` (feedback 规则)

**数据**: 当前 ERR 节点 33 个, FEE 节点 62 个 (2026-04-18 stats)

**独有提案** (PRD 原话): "注意力矫正是 3CAN 独有提案 — 没有任何现有工具将自己定位为 LLM 幻觉的外部校正层"

## 5. 双向 Skill 管理 (SKILL.md ↔ 3CAN 节点)

**是什么**: 扫 `~/.claude/skills/*/SKILL.md` 和项目 `.claude/skills/*/SKILL.md`, 同步到 3CAN 的 `SKILL-*` 节点 (NodeType.skill), 记录调用成功率。

**代码**:
- `tools/skill_sync.py` — 双向同步
- `app.py:/api/skills GET /api/skills/invoke POST` (v9.1)
- `models.py NodeType.skill` (v9.1 新增)

**现状**: 12 user-level skills 已入库 (SKILL-user-*)

**归属**: Claude Code SKILL.md 协议 (Anthropic 官方)

## 6. 生命周期自动衰减

**是什么**: 30 天未 route 命中的 active 节点 → dormant; 60 天 →
`status=archived`。普通 route 默认排除 archived；明确 history 查询仍可读取。
Archived 只有显式恢复为 active，普通读取不会静默复活。
尚未 supersede 的 `INTF` / `PROC` / `DEC` / `PRJ` current 节点不会仅因低活跃度
被自动衰减；已被 supersede 的受保护历史节点才进入该 lifecycle。

**代码**: `graph_engine` lifecycle_sweep + `app.py:/api/lifecycle/sweep /api/lifecycle/stats`

**历史数据**: 721 active / 664 dormant (2026-04-18，不是当前 release 验收)

**归属**: Zep / Graphiti 的 activation decay 思路 (详见 ATTRIBUTION B4)

## 7. 注意力矫正 / 反幻觉 (3CAN 独有提案)

**是什么**:
- Observer hook 检测 the maintainer 纠错 + 新概念 (2026 后产品 / 工具), 强制 agent WebSearch 核验再答
- PROPOSED-* 人工审批流 (LLM 自动建议节点均 status=dormant, 待 the maintainer 审)
- `ERR-gemma4-not-verified-2026-04-17` 先例节点, 防止重复犯错

**代码**:
- `~/.claude/scripts/hooks/3can-prompt-observer.js`
- `tools/observer_llm_analyzer.py`

**独有性**: PRD 原话 "现有工具都是帮 LLM 记住, 3CAN 额外做到帮 LLM 不编"

## 8. INTF 契约节点

**是什么**: 接口契约 (函数签名 / API 路由 / schema) 作为一等公民节点 (`INTF-*` 前缀), 供 Codex 前端对接直接 route。

**现状**: 484 个 INTF-* 节点 (占全图 35%, 最多的前缀)

**独有性**: Mem0 / Letta / Zep / EvoMap / Graphify 都无此抽象

## 9. 单写代理与可验证进程所有权

**是什么**: Proxy 9700 提供稳定入口，green/blue 是可轮换端口，但共享图
目录一次只允许一个 writable backend。`/api/admin/deploy /switch /retire
/recover-active` 都必须验证持久化 identity、实时 OS process identity 与
监听端口。

**代码**: `neural-memory/proxy/server.py`

**安全边界**: 独占 `GraphRuntimeLock` 与同时健康的 writable standby 不兼容。
v0.2 因而禁用自动 failover，并让普通 inactive deploy 在 spawn 前返回
`409 single_writer_graph_requires_cutover`。只有 OS 明确证明旧 writer 已退出、
端口空闲后，受控切换才可继续；检查 unavailable 一律 fail closed。

**未宣称能力**: 当前没有 per-slot immutable release root、自动图快照回滚或
零停机切换。历史“多次无停机蓝绿切换”记录不足以证明共享图单写约束下的
能力，已从当前能力说明撤回。

## 10. Confidence Gating + Fallback (Agentic RAG 路线)

**是什么**: route 返回 `confidence: high|medium|low` + `fallback_hint`, low 时 agent 应调 skill / WebSearch。

**阈值**:
- `LOW_CONF_THRESHOLD = 0.045` (top1 score)
- `SPARSE_TOP_GAP = 1.15` (top1/top3 比)

**代码**: `app.py:305-346 _compute_confidence()`

**归属**: Agentic RAG 社区 (详见 ATTRIBUTION B8)

## 11. Hash Chain Audit (v9.3 新增)

**是什么**: activity_log 每条 entry 带 `prev_hash` + `self_hash` (SHA256), 形成不可篡改审计链。

**端点**: `GET /api/audit/verify` 返回 `{valid, n_entries, breaks}`

**代码**: `graph_engine.py:1769-1810` + `app.py:/api/audit/verify`

**实测**: 本 session 活动日志 500 条, `valid=True breaks=0`

**用途**:
- 开源保护: 第三方要主张"我早于你实现某功能"时, hash chain 提供数学时间线证据 (EvoMap 刚踩过)
- 多 agent 并发审计: 冲突时可复核事件顺序

## 12 (延伸). 同构验证方法论

**是什么**: 同一架构 (Compound AI + Neurosymbolic 图谱 + 多 agent 协同) 既用于管 3CAN 本身开发, 又用于 Zeven SaaS 运营教练 — 同一方法两个领域双重验证。

**PRD 原话**: "如果我们为电商运营教练设计的架构是有效的, 那么将同一架构反向应用于管理自身开发过程, 应该同样有效"

**独有性**: the maintainer 原创方法论, 未见其他项目

---

**未实现 / 真空白**:
- Temporal validity (bi-temporal 事实有效期) — Zep/Graphiti 核心, 我们没上
- BGE-M3 原生 sparse + multi-vector — FlagEmbedding 库兼容性阻塞
- Hierarchical Leiden — 单层够用
- pgvector + HNSW — 1385 节点 numpy 够快

详见 [LIMITATIONS.md](./LIMITATIONS.md)
