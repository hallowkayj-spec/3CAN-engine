"""Abstract base for query expansion adapters (S66h, 2026-04-23).

Adapter 契约:
  - 继承 ExpanderBase
  - 实装 available() 和 expand()
  - 暴露为模块级 `EXPANDER` 实例 (或 `Expander` class, host 会 instantiate)
  - import 失败时模块层面就 raise, host 会捕获跳过 — 不要 try/except 假装 OK

新增 adapter 模板:
  # expansions/my_adapter.py
  from expansions.base import ExpanderBase, ExpansionResult

  class MyAdapter(ExpanderBase):
      name = "my"
      lang = "zh"  # "zh" | "en" | "both"
      def available(self) -> bool:
          return True  # 或实测依赖可用性
      def expand(self, query, top_k=3):
          return [ExpansionResult(query="近义词", score=0.85)]

  EXPANDER = MyAdapter()
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ExpansionResult:
    """单个扩展结果. score 0..1, >0.5 建议采用."""

    query: str
    score: float


class ExpanderBase(ABC):
    name: str = "base"
    lang: str = "both"  # "zh" | "en" | "both"

    @abstractmethod
    def available(self) -> bool:
        """返回 False 则 host 跳过此 adapter (依赖缺失/模型未下)."""
        ...

    @abstractmethod
    def expand(self, query: str, top_k: int = 3) -> list[ExpansionResult]:
        """返回最多 top_k 个扩展. 不要包含原 query (host 会自动加)."""
        ...
