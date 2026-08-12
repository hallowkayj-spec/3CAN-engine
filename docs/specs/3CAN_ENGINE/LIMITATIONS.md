# 3CAN Engine — 局限与诚实评估

> **the maintainer 要求**: 严格自评, 留余量。此文档是外审第二个必读 (第一个 [ATTRIBUTION.md](./ATTRIBUTION.md))。
> **原则**: 宁肯低估自己 1-2 分, 也不夸一分。
> **发行分类**: source-available under PolyForm Noncommercial License 1.0.0,
> 不是 OSI open source。

## 0. 我们明确不做的 (和其他工具的硬边界)

the maintainer 的原话精神:
> "我们就是围绕小项目团队或个人, 去优化了开发环境、使用成本, 并尽可能在 vibecoding 过程中, **以人为纽带**, 去精细化的、有效的、记忆化的、协同化的管理项目, 并真实做到 vibecoding 的降本增效。至于是否能真的创收和进化, 需要实测, 也要让各位开发者自己注意的"

这段是 3CAN 的**核心立场**, 比功能清单更重要。展开来讲:

### 0.1 我们**没有** agent orchestrator / 编排器
- OpenClaw 可自行调度多种 tools 完成任务
- Hermes Agent 6 个 terminal backends (local/Docker/SSH/Daytona/Singularity/Modal) 自动执行
- LangGraph / CrewAI / AutoGen 都有 orchestrator
- **3CAN 无任何 orchestrator** — 我们不决定 "下一步 agent 该做什么"。那是 Claude Code / Codex 自己的事, 3CAN 只提供知识和记忆

### 0.2 我们**不让** agent 自动化 (全自主)
- EvoMap Evolver 有 Self-Repair Mode (自动分析 log 改代码)
- OpenClaw / Hermes 可以自己连续执行 5-20 步任务不问你
- **3CAN 刻意不做** — 所有 LLM 生成的建议都进 `PROPOSED-*` (status=dormant), **等人审批** 才转 active
- 原因: the maintainer 明确 "错误不犯" 比 "速度快" 更重要; EvoMap 开发者都在吐槽 Mad Dog Mode

### 0.3 我们**不是** vibecoding 工具本身
- Cursor / Copilot / Zed / Continue.dev / Codeium 都是 IDE 级 vibecoding 工具
- **3CAN 是 vibecoding 工具的"背后大脑"** — 给 Claude Code / Codex 这些 vibecoding agent 提供共享记忆, 不自己做 IDE
- 类比: PostgreSQL 不是 Web 应用, 但每个 Web 应用都用它

### 0.4 我们**不承诺** 自动创收 / 自动进化
- 不像 EvoMap 宣传 "agents 能力能跨项目进化复用"
- 不像 OpenClaw 宣传 "让 AI 替你做工 / 赚钱"
- **3CAN 的收益靠开发者自己**: 你省了多少 token、跨 session 少踩几次坑、多 agent 协作多高效 — 这是开发者自己的感知, 3CAN 只是工具
- 真创收 / 真进化需要**实测**, 不是我们承诺

### 0.5 我们**不替代** RAG / 向量库
- 我们用 BGE-M3 + bge-reranker-v2-m3, 这是标准 RAG 组件
- 3CAN 不是 "更好的 RAG", 是 "RAG 上面的项目管理层"
- 对 the maintainer 的意思: 你的 agent 如果想做代码补全、文档理解这种 RAG 任务, 用 Cursor / RAG 工具就行, 3CAN 不竞争那个场景

### 0.6 定位总结 (用大白话)

**3CAN 做的**:
- 给你 (人 / 开发者) 一个**精细化、带记忆、多 agent 协同**的项目知识底座
- 在 vibecoding 的过程中, **以人为核心**, 让不同 agent 协作不冲突、不重复犯错、不烧 token
- 目标是**真实降本增效**, 不是花架子

**3CAN 不做**:
- 不替你决定 agent 该执行什么 (orchestrator 是你自己的事)
- 不让 agent 自己进化 (我们相信审批的力量)
- 不保证创收 (那是你的业务)
- 不是 IDE 也不是 agent runtime (那是 Cursor / OpenClaw 的事)

**3CAN 适合**:
- 个人开发者维护中等偏大项目 (52 文件 / 700+ 函数 / 30+ API)
- 2-5 人小团队不同人用不同 agent (Claude Code + Cursor + Gemini CLI)
- 关心 token 花费 + 错误不重犯 + 跨 session 连续性
- 愿意接受"PROPOSED 审批"这种稳慢路线, 不追 "全自动闭环"

**3CAN 不适合**:
- 追求 "一键让 AI 替我做事" (用 OpenClaw / Hermes)
- 零代码产品经理想管 AI 流程 (用 n8n / LangFlow)
- 企业多部门权限管控 (我们无权限体系)
- 10K+ 节点规模 (numpy 矩阵到瓶颈)

## 1. 功能上的真空白

### 1.1 Temporal Validity (bi-temporal 事实有效期)
- **缺什么**: 节点没有 `valid_from` / `valid_until` / `invalidated_by_contradiction` 字段
- **影响**: query "the maintainer 现在用什么 GPU" 时, 如果节点 "the maintainer 用 RTX 4090" (6 个月前) 和 "the maintainer 用 RTX 5090" (1 个月前) 都存在, 3CAN 无法知道后者才是现状
- **Zep / Graphiti 的核心护城河**, 我们明确未上
- **影响域**: LongMemEval 的 `temporal-reasoning` 类题目。早期内部
  10 题 pilot 不可复现，也不能与其他项目的公开结果直接比较

### 1.2 BGE-M3 原生 sparse + ColBERT 多向量
- **缺什么**: 只用了 BGE-M3 的 dense 通道, 没用原生 sparse (lexical_weights 线性头) 和 multi-vector (ColBERT-style token-level embeddings)
- **原因**: `FlagEmbedding==1.3.5` 在 Python 3.14 + transformers 5.x 下导入失败 (decoder_only reranker 模块链 `is_torch_fx_available` 缺失)
- **替代**: 我们自写 IDF-kw sparse 信号, 功能类似但不是 BAAI 官方训练的 sparse 表示
- **代价**: sparse 信号质量可能低于官方；当前发布包没有冻结的 v0.2
  benchmark 证明其质量已足够

### 1.3 pgvector / HNSW 索引
- **现状**: numpy 矩阵暴力余弦。私有图曾记录约 1,385 节点、
  P50 4.4s，但缺冻结环境与原始 receipt，不是 v0.2 验收值
- **未上原因**: 当前规模够快, 没必要上依赖
- **扩展临界点**: 10K+ 节点时 numpy 矩阵会超 100MB, 余弦计算 >2s, 那时必上 pgvector HNSW

### 1.4 Hierarchical Leiden Community
- **现状**: 单层 Leiden, 721 节点 → 287 社区, 长尾严重 (最大社区只占 5.4%)
- **未上**: 分层 Leiden (子社区 → 父社区 → 全图) 能做多粒度摘要, Microsoft GraphRAG 已证有效
- **原因**: 工程量, 优先级低

### 1.5 Online IDF 重算
- **现状**: `_kw_df` 在 engine 启动时计算一次, 节点 kws 变化后不动
- **未上**: 生产级 ES / Solr 会在 index 变更时 online 增量重算
- **影响**: 大量 kw 改动后需要重启 engine 才能看到新 IDF 权重 (本 session 改完 kw 后直接重启生效)

### 1.6 Cross-session Token Ledger
- **现状**: 3CAN 不知道 agent 累计花了多少 token
- **未上原因**: 需要接 Claude API billing endpoint 或 LangSmith 类工具
- **替代**: `benchmark` 内建 latency + grep_replacement_ratio 指标供 agent 估算

## 2. Benchmark / 数据上的诚实

### 2.1 46-query 分数是不可复现的历史内部记录

- 2026-04-15 的私有图记录写下 MRR 0.9239 / R@1 0.7826，但发布包
  不含当次冻结 graph、逐题响应和原始 receipt。
- 当前 46-query 文件已替换为只引用通用 seed graph 的
  `synthetic_public` fixture，不能重现或验证旧分数。
- 任何新分数必须绑定 commit、fixture hash、模型/降级模式、环境与原始
  结果票据；旧分数不能写成 v0.2 当前能力。

### 2.2 LongMemEval 记录不是当前发布证据

- 早期 10 题 pilot 与后续 oracle balanced-60 内部运行采用了不同 runner
  条件，不能混为同一结果。
- balanced-60 笔记记录过 `0.2333 → 0.7500`，但冻结输入、环境和逐题
  receipt 未随包提供，且同一模型参与回答与判定。
- 因此不发布当前 LongMemEval 分数，也不与论文或其他产品做横向比较。

### 2.3 跨工具自评分已撤回

- 早期内部加权排名由项目自己定权重、出题与打分，没有 apples-to-apples
  运行其他工具。
- “综合评分”“Top N”“beats X”以及 stars 增长预测都不是发布证据。

### 2.4 Token 数字仅是历史单次内部切片

- 86.5% 来自一次 skeleton 与 full pack 的内部载荷比较，不是总成本，
  也不是跨工具 benchmark。
- 没有冻结输入、tokenizer 和 raw receipt 时，只能作为历史设计线索，
  不能承诺用户将节省固定比例。

## 2bis. 历史 gate / runner 教训

- `mode=slim` 会截断描述，不适合需要完整事实的 benchmark；这类任务应
  显式使用 `full` 并冻结 runner 配置。
- 安装 hook 不等于 hook 实际进入运行回路。验收必须同时覆盖有效票据
  allow、无票据/过期/scope mismatch deny、writeback 和审计日志。
- 历史结果若没有可发布的 ErrorCase、解决方案和验证 receipt，只能保留
  为内部调试线索，不能当作“错误不重犯”能力证明。

## 3. 工程上的脆弱点

### 3.1 Windows 兼容性
- 本机 Windows 11 + Python 3.14 + BGE-M3 CPU, 冷启动 embedding 1385 节点要 4-20 分钟 (内部多次实测波动大)
- EvoMap Evolver 官方承认 Windows 不兼容 (pgrep/ps aux 用不了), 我们在 Windows 做通了, 但**Windows 性能差于 Linux/Mac**
- 未测 WSL2 (理论上会好)

### 3.2 蓝绿部署 proxy_state.json 陈旧问题
- proxy 不主动探活 (只在 admin/state?live=true 才探), 多次观察到 state.json 显示 `status: starting/offline` 但实际 backend 跑着
- 本 session 多次碰到旧 pid (PID 4000 活了 10:16 → 12:xx) 被 Stop-Process 过滤器漏掉, 需手动 taskkill

### 3.3 Node 文件读写无锁
- 多 agent 同时 PUT /api/nodes/{id} 可能互相覆盖 (最后写的赢)
- 本项目单开发者无此问题, 开源多人协作时需加锁

### 3.4 Hash chain 截断策略
- `activity_log` 只保留最近 500 条, 截断时前序 hash chain 断
- 如果严格审计要求全量保留, 需开辅助 `activity_chain.jsonl` append-only 日志 (未实现)

### 3.5 Embedding Cache Invalidation
- 节点 kws 变化时, embedding 基于**新 kws** 的 text 重算 (好)
- 但如果手动直接改 JSON 文件 (不经过 /api/nodes PUT), engine 需 /api/reload 才能感知, 且 reload 如果 IDs 相同不重算 embedding → **embedding 和当前节点 kws 可能不一致**
- 本 session 跑 kw_audit 直写文件后就遇到此 (需手动删 embeddings.npz 强制 rebuild)

## 4. 生态上的弱势

### 4.1 社区 / stars
- v0.2 仍是未发布的 source-available 候选，没有可引用的公开采用率、
  外部维护者规模或长期升级证据。
- 不使用会快速过期的第三方 stars 数量推导质量或市场位置。

### 4.2 许可证
- 已采用 **PolyForm Noncommercial License 1.0.0**
- 这是 **source-available** 许可，不是 OSI 认可的 open-source 许可
- `LICENSE` 原文是权威条款；本项目没有 GPL-3.0/MPL-2.0 双重许可

### 4.3 产品化水平
- 前端只有一个 `3d-force-graph` 可视化
- 无用户管理, 无多项目隔离, 无 SaaS 层
- 纯**开发者 CLI 级工具**, 不适合非工程师

## 5. 方法论上的风险

### 5.1 benchmark 驱动 vs 真实使用
- 46-query 是我们出的题, 跑通不代表真实使用体感好
- the maintainer 明确要求 "中期开始就是任务实际开发来测试", 这步还没做
- 目前只有方法论 (benchmark) 没有 UAT (用户实测)

### 5.2 LLM-as-judge 偏差
- the maintainer 审 the maintainer 出题, DeepSeek 判 DeepSeek 答, **自判循环有偏**
- 独立 judge (GPT-4o) 或人工判更可信

### 5.3 同构验证方法是 "类比", 不是 "证明"
- typed graph / route / writeback 机制可能迁移到其他领域，但类比不构成
  跨领域有效性的证明
- 发布包没有另一个领域的冻结数据、同协议 UAT 或结果 receipt

## 6. 对外承诺的边界 (我们能说 / 不能说)

**能说**:
- “2026-04-15 私有图内部记录写下 MRR 0.9239 / R@1 0.7826，
  但缺冻结图谱与原始 receipt，不能从发布包复现，也不是 v0.2 PASS。”
- “一次历史内部切片记录 skeleton 比 full 载荷少 86.5%，但没有当前
  可复现票据，不构成固定节省承诺。”
- “3CAN 组合了 typed project nodes、graph route、agent registry 与
  bounded hash-chain audit；各部分来源和限制见 ATTRIBUTION/EVIDENCE。”
- "借鉴了 Graphify / Entroly / EvoMap / Zep / Letta / Microsoft GraphRAG / Agentic RAG 的思路, 详见 ATTRIBUTION"

**不能说**:
- ❌ "3CAN 比 Mem0 / Zep / Letta 强" (没跑过对比)
- ❌ "3CAN 业界第一" (任何"第一/最强"都不说)
- ❌ "v0.2 在 LongMemEval 得 X 分" (发布包缺冻结输入与原始 receipt)
- ❌ "综合评分 8.6" (自评撤回)
- ❌ "Token 节省 90%+" (应说 "skeleton vs full 节省 86.5%, 跨工具对比未跑")

## 7. 对 GPT-5.4 外审的提问

在你审阅此文档时, 特别关注:
1. **归属是否漏了**: ATTRIBUTION.md 里有没有夸大"独创", 漏了哪些借鉴源
2. **benchmark 是否夸大**: 有没有我们"假涨" 的声称 (比如 step 2 MRR 0.9268 其实是样本偏差)
3. **温度**: 语气是否太吹, 或哪些数字需要更保守表述
4. **缺口**: LIMITATIONS 里漏了什么我们自己没意识到的弱点
5. **名称**: 3CAN (5C 缩写) 对外是否合适, 是否需要改名 (见 NAMING.md)
6. **发行策略**: 已选 PolyForm Noncommercial 1.0.0；请审查 source-available
   话术、NOTICE 与分发边界是否一致

反馈格式: markdown 批注 + 具体行号。
