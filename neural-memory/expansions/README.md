# 3CAN Query Expansion — Optional Plugins

Optional semantic expansion layer. Core 3CAN doesn't depend on any of these — install only what matches your language / workload.

## Quick start (Chinese, lightest)

```bash
pip install jieba
```

Then in `config.json` (repo root):
```json
{
  "query_expansion": {
    "enabled": true,
    "backends": ["jieba_synonyms"],
    "min_score": 0.5,
    "max_expansions": 5
  }
}
```

Restart `backend/app.py`. Done.

---

## Available backends (by language)

### 🇨🇳 Chinese

| Adapter | File | Deps | Install | Size | Latency | When to use |
|---|---|---|---|---|---|---|
| **jieba_synonyms** ✅ shipped | `jieba_synonyms.py` | `jieba` + hand-curated seed | `pip install jieba` | 50MB | <1ms | default for all CN users |
| cn_cilin optional shipped | `cn_cilin.py` | HIT Cilin-style local synonym file | download dictionary, set `CILIN_PATH` | ~30MB | <5ms | precise Chinese synonyms |
| tencent_vectors (planned) | `cn_tencent_vectors.py` | 腾讯 AI Lab 800 万词向量 | download txt, set `TENCENT_VECTOR_PATH` | 2GB | <10ms | semantic similarity on short/compound words |
| hanlp (planned) | `cn_hanlp.py` | `hanlp>=2.1` | `pip install hanlp` | 1GB | 30ms | NER + segmentation needed |

### 🇬🇧 English

| Adapter | File | Deps | Install | When to use |
|---|---|---|---|---|
| wordnet (planned) | `en_wordnet.py` | `nltk` + wordnet corpus | `pip install nltk && python -m nltk.downloader wordnet` | default for EN users |
| fasttext (planned) | `en_fasttext.py` | `fasttext` + wiki-news-300d-1M.bin | `pip install fasttext` | compound words (DevOps, CI-gate) |
| sentence_transformers (planned) | `en_sbert.py` | `sentence-transformers` | `pip install sentence-transformers` | long queries |
| spacy_lg (planned) | `en_spacy.py` | `spacy` + `en_core_web_lg` | `pip install spacy && python -m spacy download en_core_web_lg` | technical-term-dense domains |

Status:
- ✅ shipped = implemented, tested
- planned = skeleton / stub only, PR welcome

---

## How it works

`backend/query_expander.py` is the plugin host. On startup it scans `expansions/*.py`:
- imports each module
- reads `module.EXPANDER` (instance) or `module.Expander` (class, auto-instantiated)
- calls `adapter.available()` — if False, skip silently
- collects available adapters into `QueryExpander._adapters`

At query time, `/api/route` calls `get_default_expander().expand(query)` and gets back:
```python
[("武商端", 1.0), ("武汉商业端", 0.95), ("wu merchant", 0.9), ...]
```

Then (TODO, Phase 2) `/api/route` runs `engine.route(q)` for each and merges via RRF.

---

## Writing a new adapter

Copy `expansions/jieba_synonyms.py` as template. Contract:

```python
from expansions.base import ExpanderBase, ExpansionResult

class MyAdapter(ExpanderBase):
    name = "my_adapter"
    lang = "zh"  # "zh" | "en" | "both"

    def available(self) -> bool:
        # test if deps importable / model loadable
        return True

    def expand(self, query: str, top_k: int = 3) -> list[ExpansionResult]:
        return [ExpansionResult(query="syn1", score=0.85)]

EXPANDER = MyAdapter()   # ← host looks for this
```

Rules:
- Do NOT import heavy deps at module top-level if they might be missing. Use try/import inside `available()`.
- Do NOT include the original query in your result list — host adds it at score=1.0.
- Keep expansions short (1-5 words ideal). Long rewrites degrade route precision.

---

## Tuning

`config.json`:
- `min_score` (default 0.5): drop adapter results below this confidence
- `max_expansions` (default 5): cap total expansions per query
- `backends`: explicit list — if omitted, all `expansions/*.py` auto-loaded

Measure before tuning: see `benchmark/3can_ablation.md` for the A/B design.

---

## Not optimal?

Pure-LLM expansion (calling Claude/GPT/DeepSeek to generate synonyms) is the fallback for rare/novel queries. It's **not** the default because:
- +100-200 tokens per route (×N routes/day = real money)
- +500-1000ms latency
- non-deterministic (A/B tests get noisy)

The offline dictionary+embedding combos above hit 80%+ of expansion value at <1% the cost. LLM is the last resort, wired as Phase 3.
