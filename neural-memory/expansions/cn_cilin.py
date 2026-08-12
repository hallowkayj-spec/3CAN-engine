"""Optional Chinese Cilin synonym expander.

Set CILIN_PATH to a local HIT Cilin-style synonym file. Typical rows look like:

    Aa01A01= word1 word2 word3

The adapter never downloads data and silently disables itself when the file is
missing. This keeps 3CAN route startup deterministic while allowing stronger
Chinese synonym expansion on machines that have the dictionary locally.
"""
from __future__ import annotations

import os
from pathlib import Path

from expansions.base import ExpanderBase, ExpansionResult

try:
    import jieba
    _JIEBA_OK = True
except Exception:
    _JIEBA_OK = False


class CilinExpander(ExpanderBase):
    name = "cn_cilin"
    lang = "zh"

    def __init__(self, path: str | None = None) -> None:
        self.path = Path(path or os.environ.get("CILIN_PATH", "")).expanduser()
        self._loaded = False
        self._synonyms: dict[str, list[str]] = {}

    def available(self) -> bool:
        return bool(str(self.path)) and self.path.exists() and self.path.is_file()

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.available():
            return
        groups: list[list[str]] = []
        for raw in self.path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) < 3:
                continue
            words = [part.strip() for part in parts[1:] if part.strip()]
            if len(words) >= 2:
                groups.append(words)
        mapping: dict[str, set[str]] = {}
        for words in groups:
            for word in words:
                bucket = mapping.setdefault(word, set())
                bucket.update(other for other in words if other != word)
        self._synonyms = {word: sorted(values) for word, values in mapping.items()}

    def expand(self, query: str, top_k: int = 3) -> list[ExpansionResult]:
        self._load()
        if not self._synonyms:
            return []
        normalized_query = query.strip()
        terms = [normalized_query]
        if _JIEBA_OK:
            terms.extend(part.strip() for part in jieba.cut(query) if part.strip())
        # ``jieba`` is deliberately optional in the minimal install.  Match
        # dictionary terms from query substrings as a deterministic fallback
        # instead of silently disabling Cilin expansion without the tokenizer.
        # Enumerating query substrings keeps the cost bounded by query length,
        # rather than scanning a potentially large synonym dictionary.
        matched_terms = {
            normalized_query[start:end]
            for start in range(len(normalized_query))
            for end in range(start + 1, len(normalized_query) + 1)
            if normalized_query[start:end] in self._synonyms
        }
        terms.extend(
            term
            for term in sorted(matched_terms, key=lambda value: (-len(value), value))
            if term not in terms
        )
        seen: dict[str, float] = {}
        for term in terms:
            for synonym in self._synonyms.get(term, [])[:top_k]:
                if term == query:
                    candidate = synonym
                else:
                    candidate = query.replace(term, synonym, 1)
                if candidate and candidate != query:
                    seen[candidate] = max(seen.get(candidate, 0.0), 0.92)
                if len(seen) >= top_k:
                    break
            if len(seen) >= top_k:
                break
        return [ExpansionResult(query=query, score=score) for query, score in seen.items()]


EXPANDER = CilinExpander()
