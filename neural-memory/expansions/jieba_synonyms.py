"""Lightweight Chinese synonym expander — jieba 分词 + 手维护同义词表.

不需下大模型. jieba 约 50MB. 离线工作.

策略:
  1. jieba 切词
  2. 每个词去 SYNONYM_SEED 查同义词
  3. 回拼成 query 变体
  4. 若 query 整体在 SYNONYM_SEED 作为 key → 直接返回映射列表

SYNONYM_SEED 随领域术语积累. 建议用户本地 fork 后扩充, 或通过 ENV
THREECAN_SEED_FILE=/path/to/seed.json 指向外部 JSON (未实装, TODO).
"""
from __future__ import annotations


try:
    import jieba
    _JIEBA_OK = True
except ImportError:
    _JIEBA_OK = False

from expansions.base import ExpanderBase, ExpansionResult


# 手维护种子表. key = 术语, value = (同义词, 置信度) 列表.
# 置信度: 1.0 = 完全等价 / 0.8 = 强近义 / 0.6 = 弱近义
SYNONYM_SEED: dict[str, list[tuple[str, float]]] = {
    # —— 3CAN 项目高频术语 ——
    "武商端": [
        ("武汉商业端", 0.95),
        ("wu merchant", 0.9),
        ("商户端 SaaS", 0.85),
        ("店家端", 0.8),
        ("merchant saas", 0.85),
    ],
    "记忆": [("memory", 1.0), ("知识", 0.85), ("上下文", 0.75), ("context", 0.85)],
    "回写": [("writeback", 1.0), ("persist", 0.85), ("落盘", 0.9)],
    "盲覆盖": [("blind overwrite", 1.0), ("silent overwrite", 0.95), ("unvalidated write", 0.85)],
    "路由": [("route", 1.0), ("routing", 1.0), ("检索", 0.8), ("retrieve", 0.8)],
    "置信度": [("confidence", 1.0), ("conf", 0.95), ("score", 0.8)],
    "阈值": [("threshold", 1.0), ("cutoff", 0.9)],
    "近义词": [("synonym", 1.0), ("semantic similar", 0.85), ("同义词", 0.95)],

    # —— 技术/工程通用 ——
    "接口": [("api", 1.0), ("interface", 1.0), ("endpoint", 0.9)],
    "补丁": [("patch", 1.0), ("fix", 0.9), ("修复", 0.95)],
    "错误": [("error", 1.0), ("err", 0.95), ("bug", 0.9)],
    "依赖": [("dependency", 1.0), ("deps", 0.9), ("package", 0.75)],
    "部署": [("deploy", 1.0), ("deployment", 1.0), ("上线", 0.85)],

    # —— GitHub / Git ——
    "拉取请求": [("pr", 1.0), ("pull request", 1.0)],
    "提交": [("commit", 1.0), ("push", 0.8)],

    # —— 扩展方向 (中文 ↔ 英文等价) ——
    "向量": [("vector", 1.0), ("embedding", 0.9)],
    "图": [("graph", 1.0), ("knowledge graph", 0.85)],
    "节点": [("node", 1.0), ("entry", 0.8)],
}


def _lookup_whole(query: str) -> list[ExpansionResult]:
    """整 query 作 key 命中."""
    hits = SYNONYM_SEED.get(query.strip(), [])
    return [ExpansionResult(query=syn, score=score) for syn, score in hits]


def _lookup_word_level(query: str, top_k: int) -> list[ExpansionResult]:
    """jieba 切词后逐词找同义, 回拼. 仅替换一个词, 避免组合爆炸."""
    if not _JIEBA_OK:
        return []
    words = list(jieba.cut(query))
    out: list[ExpansionResult] = []
    for i, w in enumerate(words):
        syns = SYNONYM_SEED.get(w.strip(), [])
        for syn, score in syns:
            variant = "".join(words[:i] + [syn] + words[i + 1:])
            if variant != query:
                out.append(ExpansionResult(query=variant, score=score * 0.9))
            if len(out) >= top_k:
                return out
    return out


class JiebaSynonymsExpander(ExpanderBase):
    name = "jieba_syn"
    lang = "zh"

    def available(self) -> bool:
        return _JIEBA_OK  # jieba 未装时关闭, 但 seed whole-query lookup 仍可用
        # 注: 若想在无 jieba 时仍返回 whole-query 命中, 改为 return True
        # 并在 expand() 内 graceful degrade. 当前保守: 缺 jieba 整个 adapter 不启.

    def expand(self, query: str, top_k: int = 3) -> list[ExpansionResult]:
        results = _lookup_whole(query)
        if len(results) >= top_k:
            return results[:top_k]
        results.extend(_lookup_word_level(query, top_k - len(results)))
        # 去重 by query text, 保最高分
        seen: dict[str, float] = {}
        for r in results:
            seen[r.query] = max(seen.get(r.query, 0.0), r.score)
        return [ExpansionResult(query=q, score=s) for q, s in seen.items()][:top_k]


EXPANDER = JiebaSynonymsExpander()
