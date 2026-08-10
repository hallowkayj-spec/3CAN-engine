# Chinese Route Semantics / 中文路由语义栈

This document records the Chinese-first route stack used by 3CAN. It is written
in English and Chinese because 3CAN's dogfood graph is bilingual, with a large
amount of Chinese project memory.

本文记录 3CAN 的中文优先路由语义栈。由于 3CAN 的 dogfood 图谱是中英混合，
且大量项目记忆为中文，核心说明必须中英双语保留。

## Current Online Stack / 当前已在线能力

1. **Dense semantic recall: `BAAI/bge-m3`**
   3CAN uses `SentenceTransformer("BAAI/bge-m3")` in `backend/graph_engine.py`
   for multilingual dense embeddings. This is the primary semantic recall layer
   and works for Chinese, English, and mixed-language queries.

   **稠密语义召回：`BAAI/bge-m3`**
   3CAN 在 `backend/graph_engine.py` 中使用
   `SentenceTransformer("BAAI/bge-m3")` 生成多语言 embedding。这是第一层语义召回，
   支持中文、英文和中英混合 query。

2. **Chinese-native rerank: `BAAI/bge-reranker-v2-m3`**
   Route Step 4 prefers `CrossEncoder("BAAI/bge-reranker-v2-m3")` for
   cross-encoder reranking. The implementation fuses RRF and reranker scores
   instead of letting the reranker fully override graph evidence.

   **中文原生精排：`BAAI/bge-reranker-v2-m3`**
   route 第 4 步优先使用 `CrossEncoder("BAAI/bge-reranker-v2-m3")` 做精排。
   实现上采用 RRF + reranker 融合，不让 reranker 完全覆盖图谱证据。

3. **Graph co-occurrence expansion**
   `GraphEngine._expand_query()` expands a query with co-occurring keywords
   derived from current graph nodes. This is the currently wired expansion path
   inside `GraphEngine.route()`.

   **图谱共现词扩展**
   `GraphEngine._expand_query()` 会用当前图谱节点中的共现关键词扩展 query。
   这是目前已经接入 `GraphEngine.route()` 的在线扩展路径。

4. **Chinese-friendly GET route**
   `/api/route/simple` exists to avoid shell quoting and JSON escaping problems
   with Chinese queries.

   **中文友好的 GET route**
   `/api/route/simple` 用于避免中文 query 在 shell `curl -d` 和 JSON 引号转义中的
   兼容问题。

## Optional / Not Fully Wired Yet / 可选但尚未完整接入

`backend/query_expander.py` and `expansions/jieba_synonyms.py` provide a plugin
host and a lightweight Chinese synonym adapter. The design is valid, and the
files exist, but the plugin host is not the current primary route path in
`GraphEngine.route()`. Treat it as a prepared extension point unless a future
commit explicitly wires it into route fusion.

`backend/query_expander.py` 与 `expansions/jieba_synonyms.py` 提供了可插拔扩展
host 和轻量中文同义词 adapter。设计已经存在，文件也在，但当前
`GraphEngine.route()` 主路径并没有以它作为主要扩展入口。除非后续提交明确把它接入
route fusion，否则应把它描述为“已准备的扩展点”，不要写成“已全量上线能力”。

Planned or documented adapters include:

计划中或文档中提到的 adapter 包括：

- `jieba_synonyms` (shipped adapter file; requires `jieba`)
- `cilin` / 哈工大同义词词林 (planned)
- `tencent_vectors` / 腾讯 AI Lab 词向量 (planned)
- `hanlp` (planned)

## Documentation Rule / 文档规则

When documenting 3CAN's Chinese retrieval capability, distinguish:

文档描述 3CAN 中文检索能力时，必须区分：

- **online route stack**: BGE-M3 dense embedding, bge-reranker-v2-m3 rerank,
  graph co-occurrence expansion, `/api/route/simple`
- **prepared extension points**: `query_expander.py`, `jieba_synonyms.py`, Cilin,
  Tencent vectors, HanLP

- **已在线 route 栈**：BGE-M3 稠密 embedding、bge-reranker-v2-m3 精排、
  图谱共现词扩展、`/api/route/simple`
- **预留扩展点**：`query_expander.py`、`jieba_synonyms.py`、词林、腾讯词向量、HanLP

Do not claim the plugin expansion host is fully wired into `/api/route` unless
code and tests prove it.

除非代码和测试证明，否则不要声称 plugin expansion host 已完整接入 `/api/route`。
