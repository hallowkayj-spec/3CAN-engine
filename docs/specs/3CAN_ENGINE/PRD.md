# 3CAN Engine — 产品需求文档 (PRD)

> 版本 v9.3 / 2026-04-18
>
> **历史快照说明**: 本文保留 2026-04 的产品设想，不是 v0.2 当前
> 验收表。当前发行分类为 **source-available under PolyForm Noncommercial
> License 1.0.0**, 不是 OSI open source；当前能力以根 README、
> CHANGELOG 和 STABILITY_TIERS 为准。

## 1. 产品定义

**3CAN** = **C**ustom · **C**ompact · **C**omposite · **A**ugmented · **N**eurosymbolic
(5C 缩写源自内部 PRD, 对内用; 对外 source-available 发布曾考虑
`3CAN-Graph` 副品牌, 详见 [NAMING.md](./NAMING.md))

**一句话**: 为个人/小团队开发者 (中等偏大项目规模) 提供**项目协同的 substrate** — 把知识图谱 + 多 agent 注册 + 活动审计 + 错误教训 + 双向 skill 四件事打包,供 Claude Code / Codex / Gemini CLI 等已有 agent 共享使用。

## 2. 目标用户

| 用户画像 | 具体描述 | 为什么需要 3CAN |
|---|---|---|
| 个人开发者 | 独立维护一个中大型 SaaS / 工具链 / 研究项目 | 单人记不住一年跨 60+ session 的所有决策 |
| 小团队 (2-5 人) | 每个人都在用不同 agent (Claude Code / Cursor / Copilot / Gemini CLI) | 不同 agent 间知识不通, 重复踩坑 |
| AI 工程师 | 做 agent 自动化 pipeline, 想让 agent 记错不再犯 | 每个 ERR-* 节点 + observer hook 自动警示 |

**不是目标用户**:
- 企业级多团队 (我们无权限体系)
- 普通消费者 (3CAN 是面向 Claude Code 等 agent 用户的, 不是面向终端 AI 聊天)
- 100+ 人的项目 (Leiden 聚类实测 1400 节点 OK, 未测 10K+)

## 3. 核心问题 (解决的痛点)

the maintainer 反复强调的 **北极星 6 条**:

1. **记忆精确指引** — agent 跨 session 要找"上次我做 X 的决策在哪", grep memory 目录烧 token, 3CAN route 20 token 返回 top-3 节点
2. **项目协作管理** — 多 agent 并行开发, 谁做了什么、handoff 传递、冲突预警, 都需要一个共享事件日志
3. **Token 整盘诊断** — 中等项目上下文容易爆, 3CAN 负责 agent 侧的 token 瘦身 + 诊断 (详见 [TOKEN_OPTIMIZATION.md](./TOKEN_OPTIMIZATION.md))
4. **错误+偏好记忆** — ERR-* (错误) 和 FEE-* (feedback 规则) 节点, 配合 observer hook, agent 在纠错信号下主动 route 历史教训
5. **双向 skill** — `~/.claude/skills/*/SKILL.md` ↔ 3CAN SKILL-* 节点双向同步, route 时可按 kind=skill 过滤
6. **自适应优化** — IDF 权重、activation 热度、lifecycle 衰减、Leiden community 自动重跑

## 4. 核心设计原则

### 4.1 北极星: **高精度项目索引优先于全盘记忆** (PRD 原话)

- 不追求"记住每一条对话"(Mem0 路线)
- 追求"agent 问 'S62c 的 route 优化决定是什么', 0.5 秒返回正确节点"
- 指标: MRR @ 0.9+, R@1 @ 0.76+ (46-query 内部 benchmark, 详见 [BENCHMARK.md](./BENCHMARK.md))

### 4.2 北极星: **3CAN 对全局 token 消耗负责**

- 3 档响应 (skeleton / slim / full), 可选 budget_tokens 硬限
- skeleton 模式测得 vs full **-86.5% token**
- IDF 自动降权热重 kw, Leiden 减少 sparse 误召
- 详见 [TOKEN_OPTIMIZATION.md](./TOKEN_OPTIMIZATION.md)

### 4.3 北极星: **PROPOSED 审批流**, 不让 LLM 自改节点

- LLM 产出的所有建议 (kw / edge / skill / short-code) 写入 `PROPOSED-*` 节点 (status=dormant)
- the maintainer 或其他 agent 审批后才转 active
- 和 EvoMap 自改代码 (Mad Dog Mode 默认) 的路线反着走 — 稳 > 快

### 4.4 反 Hermes 7 条 (token 治理)

基于 Hermes 社区实测数据反向总结 (非贬低 Hermes, 只是吸取经验):
1. 不做 session replay (3CAN 只存节点摘要 + 事件日志)
2. 不做平台级常开 (MCP 全关)
3. sub-agent 不传全 toolset (只传目标节点 ID)
4. runtime/core 硬分层 (agent 可换, 3CAN engine 不换)
5. 破坏性操作走 hook 审
6. skill 节点程序性记忆 (替代散落 40 tools)
7. 保持协同层定位 (不做 agent runtime)

## 5. 与同类产品的关系 (赛道说明)

详见 [ATTRIBUTION.md](./ATTRIBUTION.md) 全表。简版:

| 层 | 我们做 | 不做 |
|---|---|---|
| **agent runtime** | — | OpenClaw / Hermes Agent (小龙虾赛道) |
| **memory layer** | 部分做 (记忆指引) | Mem0 / Letta 主要做这个 |
| **temporal KG** | 部分做 (lifecycle 衰减) | Zep / Graphiti 的 bi-temporal 事实有效期我们**没做** |
| **agent capability market** | — | EvoMap GEP |
| **codebase indexer** | — | Graphify / Entroly |
| **project substrate** | ✅ **这是我们的赛道** | 不声明“无直接对手”；缺少同协议横向评测 |

## 6. 跨领域架构假设 (不是验证)

早期 PRD 曾把项目协同图谱类比到另一个业务领域。该私有产品映射不属于
3CAN 发布包，也没有同协议、冻结数据和 UAT 证明跨领域有效。

对外只能说 typed graph、route、writeback、lifecycle 等机制可能适用于多个
领域。是否具备跨领域迁移能力仍是待验证假设，不能从私有项目类比推导。

## 7. 路线图 (S66g 更新 2026-04-19)

| 项 | 状态 | 备注 |
|---|---|---|
| Path 0 回退 layer weighting | ✅ v9.2 完成 | 错误的 prefix/suffix 重复撤销 |
| Path 2 Leiden community boost | ✅ v9.2 完成 | modularity 0.9189 |
| Path 4 Agentic fallback | ✅ v9.2 完成 | confidence/fallback_hint |
| L2 summary 补填 | ✅ v9.2 完成 | 99 节点 DeepSeek 生成 |
| Skill 双向同步 | ✅ v9.2 完成 | 12 user skills 入节点 |
| Hash chain audit | ✅ v9.3 完成 | SHA256 不可篡改 |
| GDI 5 维资产打分 | ✅ v9.3 完成 | 721 节点打分, 留作 housekeeping/tiebreaker |
| LongMemEval 60 题均衡 + 拆层评分 | ✅ v9.5 S66g | balanced 60 跑完; 见 BENCHMARK.md §2.1 (caveats 齐); Ablation P0.1b 进行中 |
| Route Ticket 硬 Gate (PreToolUse) | ✅ v9.5 S66g | /api/route/ticket + 3can-behavioral-gate.js 重写; sentinel bootstrap 文档化 |
| PostToolUse writeback 强制 | ✅ v9.5 S66g | /api/activity/log + 3can-post-tool-capture.js 重写; 失败日志 |
| BENCHMARK_POLICY 三层 (L1/L2/L3) | ✅ v1.0 S66g | 单分撤回, 按层权重 0.22/0.48/0.30 + UAT 封顶 6 |
| 20 维 self-audit 按层重排 | ✅ v9.5 S66g | 新 overall ≤ 5.0/10 (有封顶门槛) |
| 全仓 lint (ruff + eslint) | ⏳ P0.4 | 本 session 进行中 |
| substrate-bench (L2 自建) | ❌ P1 | route / briefing / INTF / ERR 命中 |
| harness-bench (L3 自建) | ❌ P1 | gate 真触发率 / writeback 自动化率 / skills 动态调用 |
| Real UAT 3-5 场景 | ❌ P1 | the maintainer 最看重 (REAL_UAT_PLAN §7) |
| Temporal validity (Graphiti 路线) | ❌ P3 | valid_from/until 真空白, LongMemEval temporal 已 0.9 但底层模型仍弱 |
| 仓库结构整理 + README 中英双版 | ❌ P2 | 不大搬家, 保留 neural-memory/ |
| Source-available License | ✅ **已定** | **PolyForm Noncommercial License 1.0.0；不是 OSI open source** |

## 8. 期望使用节奏 (诚实预期)

3CAN 不是"插件式立即见效", 但也**不是 2-4 周才用**。真实节奏:

- **Day 1 开箱**: 部署 engine + hooks + rules, 跑 bootstrap_check → 可以开始用, 初始节点从 project_bootstrapper 抽种子来
- **Day 1-3 高密度使用**: 真实项目任务里 agent 频繁 route/writeback, 问题暴露, 用户可自行调参数 (关掉不喜欢的 hook / 改 IDF 阈值 / 调 Leiden 参数)
- **Day 3+ 持续优化**: 节点数累积到 100+, community 结构成型, confidence 开始稳定报告, ERR/FEE 节点触发反幻觉
- **小白用户**: 跟官方更新 (我们自身迭代节奏 3-4 天从 v9.0→v9.4, 开发者用户跟同节奏即可)

期待 "Day 1 立竿见影" 或 "Day 30 才可用" 都不符合实际。**1-3 天高密度开发即可用 + 自优化**, 是核心用户 (vibecoding 开发者) 的典型节奏。
