# 3CAN Engine — 借鉴来源归属清单 + 感谢

> **原则 (the maintainer 明确要求)**: 借鉴就是借鉴, 不抄袭, 不乱说. 每条标明**借鉴了什么 + 怎么用到 3CAN**, 不延伸算法细节 (各原项目文档更权威). 开源后此文档是社区判断 3CAN 诚信的第一张纸.
>
> **向所有下列项目作者, 论文作者, 开源社区贡献者**: 感谢你们把这些思想 / 实现 / 数据做成公开资源, 让 3CAN 这种小团队项目能在前人工作基础上搭建起来. 没有你们就没有 3CAN.

## A. 算法层 (有明确论文 / 库)

### A1. RRF (Reciprocal Rank Fusion)
- **出处**: Cormack, Clarke, Büttcher 2009 "Reciprocal Rank Fusion outperforms Condorcet and Individual Rank Learning Methods" (SIGIR)
- **标准实现**: Elasticsearch RRF, Hybrid 搜索教材
- **我们怎么用**: `graph_engine.route()` Step 2 - 3 signal (emb/kw/hybrid) + 第 4 路 code_index 的 RRF 融合。公式 `score = Σ w_i / (K + rank_i)`, K=60 (Elasticsearch 默认)
- **代码**: `neural-memory/backend/graph_engine.py:802-813`
- **没借鉴**: ES 的完整检索栈, 我们自己实现 numpy 矩阵版

### A2. BGE-M3 Multilingual Dense Embedding
- **出处**: BAAI 2024, arxiv 2402.03216 "BGE M3-Embedding"
- **仓库**: `FlagOpen/FlagEmbedding`, HF `BAAI/bge-m3`
- **许可**: MIT
- **我们怎么用**: route Signal 1 主干, 通过 `sentence_transformers` 加载 (`graph_engine._get_model()`), 1024-d 向量余弦
- **没借鉴**: BGE-M3 原生 sparse (lexical_weights) 和 multi-vector (ColBERT), 因为 FlagEmbedding 在 Python 3.14 + transformers 5.x 下导入失败 (decoder_only 模块链断); 我们的 sparse 是自写 IDF-kw

### A3. bge-reranker-v2-m3
- **出处**: BAAI 2024
- **仓库**: HF `BAAI/bge-reranker-v2-m3`, 568M 参数
- **我们怎么用**: route Step 4 cross-encoder 精排, top-15 → top-K。`alpha=0.5` 融合 RRF 和 reranker 分数
- **代码**: `graph_engine.py:828-858`
- **fallback**: FlashRank (PrithivirajDamodaran/FlashRank) 的 `ms-marco-MiniLM-L-12-v2`

### A4. IDF / TF-IDF
- **出处**: Spärck Jones 1972 "A Statistical Interpretation of Term Specificity"
- **我们怎么用**: `graph_engine._kw_idf(kw)` — `log((N+1)/(df+1)) + 1`, 封顶 [0.2, 3.0]
- **代码**: `graph_engine.py:_build_kw_df()` + `_kw_idf()`
- **v9.0 Path B2 落地**

### A5. Leiden Community Detection
- **出处**: Traag, Waltman, van Eck 2019 "From Louvain to Leiden: guaranteeing well-connected communities" (Scientific Reports)
- **实现库**: `leidenalg` + `python-igraph` (pip install leidenalg python-igraph)
- **我们怎么用**: `tools/leiden_community.py` 离线跑 → 721 active 节点聚成 287 社区, modularity 0.9189
- **route Step 3.5** (v9.2 Path 2): same-community boost +0.002
- **没借鉴**: 层次社区 (hierarchical Leiden), 我们只做一层

### A6. SHA256 Hash Chain
- **出处**: Merkle 1988 / 区块链常识
- **我们怎么用**: `ActivityEntry` 加 `prev_hash` + `self_hash`, `engine._compute_entry_hash()` 算 sha256(timestamp+agent_id+action+detail+affected_nodes+meta+prev_hash)
- **端点**: `GET /api/audit/verify` 返回链完整性
- **代码**: `graph_engine.py:1774-1810` (v9.3 b 落地)
- **没做**: Merkle Tree, 默克尔证明, 区块链共识 — 用不上, 单机自审计足够

### A7. HNSW (Hierarchical Navigable Small World)
- **出处**: Malkov, Yashunin 2016
- **状态**: **未用**, 3CAN 1385 节点用 numpy @ 矩阵够快。未来扩到 10K+ 节点时考虑迁 pgvector + HNSW (详见 [LIMITATIONS.md](./LIMITATIONS.md))

## B. 产品设计借鉴 (开源项目思路, 非代码抄袭)

### B1. Graphify (safishamsi/graphify)
- **仓库**: https://github.com/safishamsi/graphify
- **项目性质**: AI-coding-assistant skill (Claude Code / Cursor / OpenClaw 等平台插件), 把代码库/文档/视频转成 NetworkX 知识图谱
- **许可**: 未发现明确许可证；默认版权法适用，仅作公开思想研究，不复制代码或受保护表达
- **借鉴了什么**:
  - (1) `--budget N` token 硬限 CLI 参数的思路
  - (2) Leiden topology-based 聚类不用 embedding 的思路
- **我们的应用**: `budget_tokens` 参数 + 单独离线跑 Leiden 工具
- **代码对应**: `app.py:290-302 _enforce_budget()` + `tools/leiden_community.py`
- **没借鉴**: tree-sitter AST 解析 (我们不做代码索引), video/audio whisper 转录 (N/A), graph.html 可视化 (我们已有 3d-force-graph), SHA256 文件缓存 (我们是节点 ID-based cache)
- **差异原因**: Graphify 是"代码 → 图谱"的构造工具, 我们是"知识决策 → 图谱"的协同 substrate, 场景不同

### B2. Entroly (juyterman1000/entroly)
- **仓库**: https://github.com/juyterman1000/entroly
- **项目性质**: Rust + WASM 代码库上下文压缩 (70-95% token 节省), 给 Cursor/Copilot/Claude 提供代码压缩视图
- **许可**: MIT
- **借鉴了什么**: CCR (Compression-Compression-Retrieval) 变分辨率两段式 — 先给 skeleton, agent 按需取详细
- **我们的应用**: `mode=skeleton|slim|full` 三档 + `GET /api/retrieve/{id}` 端点
- **代码对应**: `app.py:268-366`
- **没借鉴**: Rust + WASM 性能实现 (我们 Python 够用), 任意压缩率 (我们只 3 档), MCP server 常开 (我们按 the maintainer 规则 MCP 默认关), GitHub Action cost-check (未上)
- **差异原因**: Entroly 专注代码压缩交给 Cursor; 我们专注知识节点压缩交给 Claude Code agent 做多 session 项目管理

### B3. EvoMap / GEP (https://github.com/EvoMap/evolver)
- **仓库**: EvoMap/evolver + evomap.ai
- **项目性质**: AI agent 自进化引擎, Genome Evolution Protocol 让 agent 共享能力
- **许可**: MIT → GPL-3.0 (自 Hermes 抄袭案后改)
- **借鉴了什么**:
  - (1) 5 维资产打分 GDI (Genetic Diversity Index: 结构/语义/独特/实用/验证)
  - (2) Events append-only 不可变日志的设计哲学 (促成我们 v9.3 hash chain)
- **我们的应用**: `tools/node_gdi_scorer.py` 给每节点 5 维打分 + `quality_score` 综合; hash chain 落在 activity_log
- **代码对应**: `tools/node_gdi_scorer.py` + `graph_engine.py:1774-1810`
- **没借鉴** (明确不要):
  - Genes (原子能力包) — 我们单项目, 不搞跨 agent 能力市场
  - Capsules (任务路径打包) — 我们 handoff 文档够用
  - Self-Repair Mode (自动改代码) — **明确拒绝**, the maintainer 规则 "错误不犯" 走人工审批, EvoMap 开发者也吐槽 "Mad Dog Mode 是默认 / 自改是灾难"
  - 70/30 规则 / Innovation Mandate — 我们没有"30% 探索"预算, 小团队工程纪律要求 100% 稳
  - 全球 agent 市场 / GEP 协议本体 — 跨 agent 能力共享不是 3CAN 赛道
- **差异原因**: EvoMap 是"让 agent 自己进化", 3CAN 是"让开发者控制 agent 的记忆和协同" — 价值观相反 (自动 vs 审批)

### B4. Zep / Graphiti (getzep/graphiti)
- **仓库**: https://github.com/getzep/graphiti
- **项目性质**: 时序知识图谱 + 事实有效期窗口
- **借鉴了什么**: (1) activation 衰减概念 (2) 节点 lifecycle 状态 (dormant/archive)
- **我们的应用**: 30d 未命中 → dormant, 60d → archive
- **没借鉴 (真空白)**: bi-temporal 事实有效期 (valid_from / valid_until / invalidated_by_contradiction) — 这是 Zep 核心护城河, 我们**明确识别为未来可补**, 现在没做
- **差异原因**: 我们优先级是项目协同, 没精力同时做时序事实管理

### B5. Letta / MemGPT (letta-ai/letta, 前 MemGPT)
- **仓库**: https://github.com/letta-ai/letta
- **借鉴了什么**: OS-style 三层记忆概念 (core / archival / recall)
- **我们的应用**: status 多态 (active/dormant/blocked/deprecated/archive) — 概念近似, 实现不同
- **没借鉴**: self-editing memory API (agent 自改记忆), Conversations REST API (我们走 HTTP 节点 CRUD)
- **差异原因**: Letta 是 agent runtime (类 OpenClaw), 3CAN 是 substrate, 不让 agent 自改

### B6. Claude Code / Anthropic SKILL.md 协议
- **来源**: Anthropic 官方, https://code.claude.com/docs/en/skills + https://github.com/anthropics/skills
- **借鉴了什么**: SKILL.md YAML frontmatter 协议 + `/skill-name` 手动触发 + 自动描述匹配 auto-invoke
- **我们的应用**: `tools/skill_sync.py` 扫 `~/.claude/skills/*/SKILL.md` 和项目 `.claude/skills/*/SKILL.md` → 建 3CAN SKILL-* 节点 + 使用统计 (`/api/skills/invoke`)
- **代码对应**: `tools/skill_sync.py` + `app.py:267-320`
- **v9.1 P2 落地**
- **没借鉴**: Plugins 体系, Managed Agents 平台 (商业层, 非开源必需)

### B7. Microsoft GraphRAG
- **仓库**: https://github.com/microsoft/graphrag, paper arxiv 2404.16130
- **借鉴了什么**: Community summary retriever — 按社区边界返回全局性回答
- **我们的应用**: route Step 3.5 same-community boost (top1 的 community 伙伴 +0.002), 实测 short-code 类 MRR +20%
- **没借鉴**: Hierarchical community summaries (分层摘要), Local-to-Global 两阶段问答 — 未来可扩

### B8. Agentic RAG 社区
- **来源**: arxiv 2501.09136 "Agentic RAG Survey" + Haystack (deepset) + Dify blog + arxiv 2602.03442 "A-RAG"
- **借鉴了什么**: low-confidence 时条件路由 fallback (换 query / 调 skill / WebSearch)
- **我们的应用**: route 返回 `confidence: high|medium|low` + `fallback_hint`
- **代码对应**: `app.py:305-346 _compute_confidence()`
- **v9.2 Path 4 落地**
- **没借鉴**: 完整 Reflection-Planning 循环 (还要多轮 agent 自我批判), 实现复杂度高

### B9. Token 治理 7 条 (公开社区实测数据)

- **来源**: Nous Research (Hermes Agent) 社区公开 issue / benchmark — 感谢他们公开这些数据, 让开源生态对 "大型 autonomous agent runtime 的 token 开销结构" 有了量化认知.
- **借鉴了什么**: 公开数据显示的**可量化 token 开销点** (每轮固定 overhead / 工具定义注入 / session replay 成本). 这些数据为 3CAN 设计反向策略提供了参照.
- **我们的应用**: 7 条治理规则写进 `.claude/rules/01-core.md` §3.5. **不是对这个项目本身的评价**, 只是基于公开数据做的架构选择差异化.
- **没借鉴**: 项目本体代码 / 架构 (不是 3CAN 对标赛道).

### B10. Obsidian (Human-readable knowledge workflow 灵感)

- **来源**: Obsidian Team, https://obsidian.md — 感谢他们把 local-first + plain text + 反链 + graph view 做成了开源社区标杆
- **借鉴了什么**: 节点 per-file JSON 接近 Obsidian 的 plain-text vault 理念; 未来可加 markdown 导出 adapter
- **没借鉴**: Markdown 语法本体 (3CAN 用 JSON 结构化节点)

## C. 基准 / 评测借鉴

### C1. LongMemEval (Wu et al. 2024)
- **出处**: arxiv 2410.10813, MIT license, HF `xiaowu0162/longmemeval`
- **我们怎么用**: 跑 LongMemEval-oracle 60 题均衡 (每类 10 题) 作为对外可比基准, judge 用 DeepSeek-V3.2 (**明标不是 GPT-4o, 不同 judge 不完全可比**)
- **代码对应**: `benchmark/longmemeval_runner.py`

### C2. LoCoMo (Maharana et al. 2024)
- **引用作参考**, 未实跑。社区数据: Zep 75.1 (争议降到 58.44), Mem0 ~66, Letta 83.2, SuperLocalMemory 87.7

### C3. LLM-as-Judge 方法学
- **出处**: arxiv 2506.13639 "An Empirical Study of LLM-as-a-Judge"
- **结论**: judge 可靠性主要靠 prompt 设计 (+11.9pt accuracy) 非 judge 模型 — 给我们用 DeepSeek-V3.2 而非 GPT-4o 作 judge 提供理论支撑

## D. 未借鉴但放到未来的 (诚实标注)

| 未来可能引入 | 来源 | 目前不做的原因 |
|---|---|---|
| pgvector + HNSW 替代 numpy @ | PostgreSQL 18 + pgvector 2026 生产标配 | 1385 节点 numpy 足够, 10K+ 再考虑 |
| Graphiti bi-temporal 事实 | Zep 核心功能 | 优先级低于项目协同 |
| BGE-M3 原生 sparse + ColBERT | BAAI FlagEmbedding | Python 3.14 + transformers 5.x 兼容性破损 |
| Hierarchical Leiden | GraphRAG 分层 | 单层够用 |
| Online IDF 重算 | 生产 ES | 静态版本够 the maintainer 用 |
| Self-repair (agent 改自己代码) | EvoMap Mad Dog Mode | **明确拒绝**, the maintainer 原则 |

## E. 当前许可证策略

- **已采用**: **PolyForm Noncommercial License 1.0.0**。`LICENSE` 原文是唯一权威许可文本。
- **准确分类**: 本发布是 **source-available (源码可见)**，**不是 OSI 认可的 open source (开源软件)**。
- **没有 GPL/MPL 双重许可**: 文档中对 GPL-3.0、MPL-2.0 或其他项目许可证的介绍只说明相应上游项目的许可，不改变 3CAN 的许可。
- **归属不等于再许可**: 本清单记录思想来源、论文和依赖；任何第三方代码或依赖继续遵守其自己的许可证。
- **未来不作暗示**: 若维护者将来改变许可，必须通过新的明确版本和许可证文件完成；当前文档不承诺自动转为 OSI 许可证。

---

此文档是**动态**的, 每加一个新借鉴来源都要更新。对外分发时应与 README、LICENSE 和 NOTICE 一起提供。
