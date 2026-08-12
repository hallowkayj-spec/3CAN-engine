"""Small 3CAN domain alias expander.

This adapter is intentionally dependency-free. It covers high-frequency project
language that generic embedding fallback can miss: route, token, RPA, KB,
advisor, semantic retrieval, and PR/runtime wording.
"""
from __future__ import annotations

import re

from expansions.base import ExpanderBase, ExpansionResult


ALIASES: dict[str, list[tuple[str, float]]] = {
    "route": [
        ("routing retrieval recall context engine route precision", 0.92),
        ("node route semantic recall confidence rerank", 0.88),
    ],
    "routing": [
        ("route retrieval recall context engine", 0.9),
    ],
    "token": [
        ("fresh input cached input context budget token impact", 0.94),
        ("token usage runtime status cost monitor", 0.86),
    ],
    "impact": [
        ("3can impact avoided context fresh input route budget", 0.9),
    ],
    "rpa": [
        ("browser automation playwright crawler evidence artifact data collection", 0.92),
        ("kb pipeline rpa artifact promotion gate data quality", 0.9),
    ],
    "kb": [
        ("knowledge base data cleaning annotation promotion gate evidence", 0.9),
    ],
    "advisor": [
        ("store coach operation coach opc backend action card", 0.9),
    ],
    "semantic": [
        ("embedding vector bge reranker hybrid retrieval query expansion", 0.94),
    ],
    "retrieval": [
        ("route recall rerank hybrid search embedding", 0.9),
    ],
    "mimo": [
        ("mimo sidecar provider benchmark token runtime", 0.86),
    ],
    "pr": [
        ("github pull request branch review merge readiness", 0.86),
    ],
}


_ASCII_ALIAS_RE = re.compile(r"[a-z0-9]+")


def _alias_matches(alias: str, query: str) -> bool:
    """Match ASCII aliases as tokens so short forms do not leak across words."""
    if _ASCII_ALIAS_RE.fullmatch(alias):
        return re.search(
            rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])",
            query,
        ) is not None
    return alias in query


class DomainAliasExpander(ExpanderBase):
    name = "domain_alias"
    lang = "mixed"

    def available(self) -> bool:
        return True

    def expand(self, query: str, top_k: int = 3) -> list[ExpansionResult]:
        q = query.lower()
        results: list[ExpansionResult] = []
        for key, aliases in ALIASES.items():
            if _alias_matches(key, q):
                for alias, score in aliases:
                    if alias not in q:
                        results.append(ExpansionResult(query=alias, score=score))
                    if len(results) >= top_k:
                        return results
        return results


EXPANDER = DomainAliasExpander()
