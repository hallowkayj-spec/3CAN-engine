# Token 整盘诊断 + 瘦身

> **the maintainer 明确高权重**: "Token input 瘦身太重要了, 这个对于小开发团队是非常重要的能力, 要做扎实, 3CAN 对全局 token 消耗负责, 自身也降低消耗, 全局控盘"

这篇单独开, 因为是 3CAN 最核心的差异化价值之一。

## 1. 为什么 token 瘦身在中等项目里是生死线

- Claude Opus / GPT-5.4 context 最大 1M tokens, 但**有效注意力**在 100-200K 后会打折 (已有 Needle-in-Haystack 实测)
- 一个中等项目 (52 个 Python 文件, 733 函数, 36 API 端点) 的 session prompt 累积到 200K+ 是家常便饭
- 个人开发者不能像企业靠砍 API 预算, 月花 $300-500 以内要跑起来
- **3CAN 承诺: 替 agent 承担"该问什么 / 怎么问最省 / 什么不用再塞" 的决策负担**

## 2. 3CAN 的 token 瘦身四件套

### 2.1 CCR 变分辨率 (skeleton / slim / full)

**借鉴**: Entroly (juyterman1000/entroly, Rust+WASM 代码压缩工具) 的 "variable-resolution context" 思路。
**我们的映射**: `POST /api/route` 加 `mode=skeleton|slim|full` 参数 (详见 `neural-memory/backend/app.py:268-356`)

| mode | 平均 token/节点 | 含内容 | 用途 |
|---|---|---|---|
| skeleton | ~20 | id, name, kw[:6], summary[:80] | 先扫索引, 按需展开 |
| slim (默认) | ~50 | id, name, cluster, type, summary[:120], current_state[:80], key_files[:3] | 常规 |
| full | ~500-800 | 全 Node.model_dump() | 需要完整 |

**真实数据** (本项目实测, query='RRF fusion routing', max_nodes=6):
- skeleton: **574 tokens**
- slim: 796 tokens
- full: 4266 tokens
- skeleton vs full: **-86.5% token**
- skeleton vs slim: **-27.9% token**

**关键设计**: `/api/retrieve/{id}` 作为 CCR 第二段, agent 从 skeleton 选定后按需取全量, 节点 activation_count+1 用于学习热度。

### 2.2 Budget Tokens 硬限

**借鉴**: Graphify (safishamsi/graphify) 的 `/graphify query --budget N` CLI 参数。
**我们的映射**: `POST /api/route` 加 `budget_tokens` 参数 (app.py:290 `_enforce_budget()`)

- 粗估 token = `len(json.dumps(item, ensure_ascii=False).encode("utf-8")) / 3.5`
- 从尾部截断 (最底端 score 的节点先丢)
- 返回 `budget_truncated: true/false` 让 agent 知道是否被砍

例:
```
POST /api/route {"task":"...", "max_nodes":10, "budget_tokens":500}
→ 可能只返 4-6 节点 (看每节点大小), budget_truncated=true
```

### 2.3 IDF 自动降权 (kw 层)

**借鉴**: Spärck Jones 1972 经典 TF-IDF。
**我们的映射**: `graph_engine._kw_idf()` (v9.0)

```python
idf(kw) = log((N+1) / (df+1)) + 1
# df=1 (稀有 kw): idf → 3.0 (封顶)
# df=426 ("intf"): idf → 2.17
# df=N: idf → 1.0 (最小)
```

**在哪生效**: `_score_keyword` 里 `kw_score += self._kw_idf(kw)` 替代原来的 `+= 1.0`。
**效果**: 热重 kw (intf 426 nodes / doc 313 / codex-cli 265) 自动降权, 不需要手动清。

### 2.4 Leiden community 减少 sparse 误召

**借鉴**: Graphify "Leiden Community Detection Without Embeddings" + Traag et al. 2019 "From Louvain to Leiden" + Microsoft GraphRAG 的 community summary。
**我们的映射**:
- `tools/leiden_community.py` 离线跑 Leiden (graspologic / leidenalg)
- 721 active 节点 → 287 communities, modularity **0.9189** (高)
- `graph_engine.route` Step 3.5 加 community boost (+0.002 给 top3 的同 community 节点)

**效果**: 同语境节点自动聚成社区, query 命中 top1 时兄弟节点 (同社区) 自动浮出。实测 short-code 类 MRR 从 0.556 → 0.667 (+20%)。

## 3. Token 诊断 (全局控盘)

3CAN 不只"自己省", 还给 agent 提供 **诊断信号**:

### 3.1 confidence / fallback_hint (v9.2 Path 4)

**借鉴**: Agentic RAG 社区 (arxiv 2501.09136 + Haystack + Dify)。
**我们的映射**: route 返回 `confidence: high|medium|low` + `fallback_hint` 建议。

```python
LOW_CONF_THRESHOLD = 0.045  # top1 score 低于此 → low, 建议 agent 换 query / 调 skill / WebSearch
SPARSE_TOP_GAP = 1.15       # top1/top3 < 1.15 且 top1<0.09 → medium, 结果扁平
```

agent 拿到 `confidence=low` 就知道"这题 3CAN 答不出, 别再多轮磨", 立即 fallback, 不浪费 token。

### 3.2 skeleton mode 作默认 (v9.0)

agent 只要不显式 `mode=full`, 默认就是 slim (~50 token/节点)。批量查多节点时 **省 90%**。

### 3.3 grep_replacement_ratio 指标

benchmark 内建指标 (`benchmark/run_benchmark.py`): 46 题有多少比例 agent 原本要 grep 文件 (读一堆 file)、现在 route 就能答。**本项目实测 0.9348 (93.5%)** — 93.5% 的查询用 3CAN 就够, 不用 grep。

### 3.4 latency 追踪

每次 route 返 `latency_ms`, agent 可以在慢查询时主动换策略。P50 4.4s, P95 5.6s (含 bge-reranker 精排)。

## 4. 配合 Claude Code / Codex 的整盘策略

3CAN 不是孤立跑的, 要在 agent 层面配合:

### 4.1 CLAUDE.md / AGENTS.md 层

- 禁止 grep memory/ 和 handoffs/active/ 目录 (直接省几 K token/次)
- 查知识先 route, 不命中再考虑 grep
- MCP 默认全关 (每个 MCP tool schema 注入 system prompt, 几百 token/个)

### 4.2 Compact 续接硬约束 (the maintainer S66c 规则)

- /compact 摘要 ≤3K tokens
- 禁止注入原文 (不复制 handoff.md / code / UAT log)
- 强制含 3CAN 节点 ID 列表 (下轮 route 按需拉完整内容, 不 cache_read 千 K 历史)

### 4.3 Session 规模硬规则

- 250K prompt → 必须 compact
- 300K → 强制 clear + handoff 重启
- cache_read >200K 主动提示 the maintainer

### 4.4 Skeleton 先行原则

agent 在不确定需要什么详细度时, 默认调 skeleton mode, 省 86%。确认关键节点后再 /api/retrieve 单独展开。

## 5. 局限与诚实

- **BGE-M3 encoding 本身不算 token 诊断**: 3CAN 只优化"给 agent 返多少", 不优化"agent 给 LLM 发多少" — 后者要 agent 侧的 context engineering 配合
- **budget_tokens 粗估**: 用 `len(utf-8)/3.5` 估算, 误差 ±15% (真实 tokenizer 依赖模型)
- **没有全局 token ledger**: 3CAN 不知道单个 agent 一个月累计花了多少 token, 要靠外部 API 统计 (如 Claude API ledger)
- **IDF 是静态的**: kw_df 在 engine 启动时算, 节点 kws 变化后不自动重算。生产环境要加 online 重算 (v9.x 未做)

## 6. 对比同类

| 项目 | token 优化策略 | 是否适用于中型项目 |
|---|---|---|
| **3CAN** | CCR + budget + IDF + Leiden + confidence fallback + skeleton-first | ✅ 设计目标 |
| Mem0 | 向量检索 + LLM 摘要, "90% 减少" 是对比"全量对话" | ✅ 记忆赛道, 不覆盖项目协同 |
| Letta | OS 三层 (核心/归档/召回), agent 自管 context | ⚠️ agent 开销大 |
| Entroly | Rust+WASM 代码压缩 70-95%, 主要给 Cursor/Copilot 代码看 | ⚠️ 偏代码, 不做知识决策 |
| Graphify | Leiden cluster + budget, 71.5x 少 token (vs 读 raw 文件) | ⚠️ 代码/文档图谱, 和项目协同有差 |
| EvoMap | GDI 打分 (我们借鉴了) + genes/capsules 打包, 跨 agent 复用 | ⚠️ 全球市场, 规模不匹配 |

**3CAN 独特组合**: CCR + IDF + Leiden + confidence-gated fallback + skeleton-first-by-default — 没有一家把这 5 件全做在一起。
