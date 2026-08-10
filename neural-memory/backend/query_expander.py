"""3CAN Query Expander — optional semantic expansion layer (S66h, 2026-04-23).

设计目标: 在不强迫用户装重依赖的前提下, 让 route 对"武商端 / 武汉商业端 / wu
merchant / 商户端 SaaS"这类近义查询召回一致. 核心不依赖, 用户按需装 adapter.

架构:
  /api/route(q)  ──>  QueryExpander.expand(q) ──> [(q, 1.0), (syn1, 0.9), ...]
                         └─ loads adapters from expansions/ dir, silent-skip
                            any that can't import (missing package).

不破坏原行为: 若 config.query_expansion.enabled=false 或 adapter 全不可用,
返回只含原 query 的列表 — 跟现在 baseline 等价.

当前接入: GraphEngine.route() 在 embedding/keyword/RRF 主链路前调用 expand(),
并以有界 weighted query 集合参与现有融合；没有第二套路由器。

Config (config.json):
{
  "query_expansion": {
    "enabled": true,
    "backends": ["jieba_syn"],
    "min_score": 0.5,
    "max_expansions": 5
  }
}
"""
from __future__ import annotations

import importlib
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from expansions.base import ExpanderBase

log = logging.getLogger("3can.query_expander")

REPO_ROOT = Path(__file__).resolve().parent.parent
EXPANSIONS_DIR = REPO_ROOT / "expansions"
CONFIG_FILE = REPO_ROOT / "config.json"


def _load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8")).get("query_expansion", {})
    except Exception as e:
        log.warning(f"config.json parse failed: {e}")
        return {}


def _discover_adapters() -> list[str]:
    if not EXPANSIONS_DIR.exists():
        return []
    return sorted(
        f.stem for f in EXPANSIONS_DIR.glob("*.py")
        if f.stem not in ("base", "__init__")
    )


class QueryExpander:
    """Plugin host. 初始化时按 config 或全量加载 adapters, 失败静默跳过."""

    def __init__(self, backends: list[str] | None = None):
        import sys
        if str(REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(REPO_ROOT))

        cfg = _load_config()
        self.enabled = cfg.get("enabled", True)
        self.min_score = float(cfg.get("min_score", 0.5))
        self.max_expansions = int(cfg.get("max_expansions", 5))

        wanted = backends if backends is not None else cfg.get("backends", _discover_adapters())
        self._adapters: list[ExpanderBase] = []
        for name in wanted:
            try:
                mod = importlib.import_module(f"expansions.{name}")
                adapter = getattr(mod, "EXPANDER", None)
                if adapter is None:
                    cls = getattr(mod, "Expander", None)
                    if cls is not None:
                        adapter = cls()
                if adapter is None:
                    log.debug(f"adapter {name} has no EXPANDER/Expander export, skip")
                    continue
                if not adapter.available():
                    log.info(f"adapter {name} reported unavailable, skip")
                    continue
                self._adapters.append(adapter)
                log.info(f"adapter {name} loaded (lang={adapter.lang})")
            except Exception as e:
                log.warning(f"adapter {name} load failed: {e}")

    @property
    def active_adapters(self) -> list[str]:
        return [a.name for a in self._adapters]

    def expand(self, query: str, top_k: int | None = None) -> list[tuple[str, float]]:
        """返回 [(query, 1.0), (syn1, 0.xx), ...]. 原 query 永远 index 0, score 1.0.

        disabled 或 adapter 全空 → 返回 [(query, 1.0)] 向后兼容.
        """
        if not self.enabled or not self._adapters:
            return [(query, 1.0)]

        limit = top_k if top_k is not None else self.max_expansions
        out: dict[str, float] = {query: 1.0}
        for adapter in self._adapters:
            try:
                for result in adapter.expand(query, top_k=limit):
                    if result.score < self.min_score:
                        continue
                    q_norm = result.query.strip()
                    if not q_norm or q_norm == query:
                        continue
                    out[q_norm] = max(out.get(q_norm, 0.0), result.score)
            except Exception as e:
                log.warning(f"adapter {adapter.name} expand() raised: {e}")

        ranked = sorted(out.items(), key=lambda kv: -kv[1])
        return ranked[: 1 + limit]


_default_cache: QueryExpander | None = None


def get_default_expander() -> QueryExpander:
    global _default_cache
    if _default_cache is None:
        _default_cache = QueryExpander()
    return _default_cache
