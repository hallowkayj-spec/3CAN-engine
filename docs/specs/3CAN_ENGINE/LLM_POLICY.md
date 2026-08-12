# 3CAN LLM Integration Policy

> Version: 2026-04-28 session synthesis.
> Scope: release-facing 3CAN engine docs, not this repository's private dogfood graph.

3CAN should be understood as a graph-backed project substrate with optional LLM
assistance at specific points. The engine must stay provider-neutral and must
degrade cleanly when a user has no LLM API key.

This file records where LLMs are used, where they are planned, and how future
sessions should reason about BYOK, token management, graph route quality, and
open-source release boundaries.

## 1. Three Model Layers

3CAN uses the word "model" in three different ways. Mixing them creates design
confusion, so release docs and implementation should keep them separate.

| Layer | Role | Default expectation | LLM API key needed |
|---|---|---|---|
| Retrieval models | BGE-M3 dense embedding, local reranker, lexical/graph signals | Core route quality layer | No, if local models are bundled or installed |
| Tokenizers / counters | Provider-specific token counting and budget estimates | Budget accuracy layer | No, but provider-specific packages may be needed |
| Generative LLM tools | Diagnosis, enrichment, judge, keyword/alias repair, bootstrap | Optional enhancement and maintenance layer | Yes, unless local `llama.cpp` or equivalent is configured |

The route/backend/proxy/writeback core must not require a generative LLM to
start. Generative LLMs can improve quality, but their outputs are advisory and
must not silently mutate the graph.

## 2. Current Capability Map

This table reflects the current audited state: the Desktop `neural-memory`
engine contains more 3CAN tools than this application repository. Release docs
must not claim a tool is shipped in the public package until it is present in
the release tree and covered by a smoke test.

| Module | Purpose | Current status | Trigger / frequency | Degradation |
|---|---|---|---|---|
| BGE-M3 embedding | Chinese/English semantic recall | shipped in engine runtime | every route / indexing refresh | no good no-model fallback; route becomes keyword-only |
| bge reranker / rerank stage | top-K precision after recall | shipped or configured per runtime | every route if enabled | skip rerank, use RRF/weighted fusion only |
| Query expansion / alias layer | expand local terms like "武商端" into project vocabulary | partial / extension point | low-confidence route, alias-miss, scheduled curation | original query only |
| Keyword precision audit | find over-hot, weak, or misleading keywords | engine tool, release status must be checked | occasional maintenance | keep existing keywords |
| Short-code curator | disambiguate project shorthand and session codes | engine tool, release status must be checked | after short-code route failures | require explicit alias nodes |
| Edge inferrer | propose missing semantic graph edges | engine tool, release status must be checked | orphan-node maintenance | graph stays sparser |
| Summary enrichment | fill concise L2 summaries for better skeleton/slim packs | engine tool, release status must be checked | after bulk import or bootstrap | keep original description |
| LLM-guided health | semantic health check for stale, duplicate, or low-value nodes | engine tool, release status must be checked | scheduled weekly/monthly or before release | use static housekeeping metrics |
| Observer analyzer | convert repeated agent failures into proposed ERR/FEE/DEC nodes | engine tool, release status must be checked | after multi-session work or incident | manual writeback only |
| Project bootstrapper | diagnose a new project and propose seed nodes | engine tool, release status must be checked | first deployment and large import | manual seed-node creation |
| Content / behavior gate | check risky claims or policy-sensitive actions | basic shipped in harness docs, runtime varies | before guarded tool actions | structural ticket gate only |
| Benchmark judge | evaluate LongMemEval or other QA-style tasks | benchmark-only | explicit benchmark runs | no judge score; use deterministic metrics only |
| Token diagnosis | estimate context cost, pack size, and truncation risk | partial; route has budget modes | every route pack, plus optional project audit | rough character-based estimates |
| Provider abstraction | BYOK routing across DeepSeek/OpenAI/Anthropic/Gemini/local | app repo has `tools/llm/provider.py`; engine release needs alignment | each LLM tool call | provider-specific scripts or disabled tools |

## 3. Initial Deployment With User-Owned LLM

For open-source or source-available users, the expected first-run flow should be:

1. Install/start 3CAN with an empty graph.
2. Configure a preferred provider or choose no external LLM.
3. Run a bootstrap diagnosis against selected project files, not the entire disk.
4. Generate `PROPOSED-*` seed nodes: `DEC-*`, `INTF-*`, `ERR-*`, `PRO-*`, `SES-*`.
5. Show token-cost estimate and privacy warning before sending content to any
   external API.
6. Require user approval before promoted nodes enter the live graph.

This is where LLMs are most valuable for new users: project diagnosis, seed-node
quality, token-management advice, and suggested route vocabulary. It should not
be a hidden always-on agent.

## 4. Route-Time LLM Use

Generative LLMs should not run on every `/api/route` request by default. Route
is frequent and latency-sensitive.

Recommended route strategy:

| Situation | LLM involvement |
|---|---|
| Normal route with clear confidence | no generative LLM; use embedding + lexical + graph + rerank |
| Low confidence / flat distribution | optional LLM query expansion and alias suggestion |
| Local shorthand miss | optional LLM maps phrase to known project vocabulary, then stores reviewed alias |
| Repeated route failure | propose `ERR-*` or `FEE-*` lesson after review |
| Batch maintenance | LLM audits keywords, aliases, summaries, and edge candidates |

For the "武商端" example, 3CAN should not solve it by storing all project text in
the graph. The healthier design is:

- alias node or vocabulary entry: `武商端 -> 武侧 / 武引擎 / 运营教练后端`
- enriched activation keywords on the relevant architecture nodes
- graph edges from business shorthand to backend contracts and state-machine nodes
- route feedback that records the miss as a reusable lesson

## 5. RAG And Keyword Management

3CAN is not meant to become an HTTP dump of the whole project. It should be a
high-quality index and coordination substrate that tells the agent what to read.

LLM assistance belongs in:

- keyword cleanup: remove misleading or over-broad keywords
- alias expansion: convert local shorthand into canonical vocabulary
- summary enrichment: make skeleton/slim route packs readable
- source selection: recommend which files or handoffs should be expanded next
- node split/merge proposals: identify bloated or duplicate nodes
- stale-node review: propose archive/dormant status, never hard-delete

All such outputs should be proposed changes until reviewed. The graph is memory,
not a place for unverified LLM drafts.

## 6. Token Management

Token management has two layers:

| Layer | Current / intended behavior |
|---|---|
| Route pack budget | `skeleton`, `slim`, `full`, and `budget_tokens` decide what the agent receives |
| Provider token ledger | planned: count actual usage per agent/provider/session where APIs expose it |

The engine should support provider-specific tokenizers:

- OpenAI-compatible models: `tiktoken` or provider-compatible tokenizer
- Anthropic: official/token-count API or package when available
- Local models: tokenizer shipped with the model, `tokenizer.json`, or sentencepiece
- Fallback: rough character estimate with an explicit low-confidence flag

LLM-based project diagnosis should include a token map: noisy docs, large
handoffs, repeated reads, candidate 3CAN nodes, and recommended route-first
workflow. This is a major public-use case for 3CAN.

## 7. Provider And BYOK Policy

3CAN should be BYOK and provider-neutral.

Supported or target provider classes:

- DeepSeek: current preferred low-cost Chinese-capable provider in dogfood usage
- OpenAI-compatible endpoints
- Anthropic-compatible endpoints
- Gemini-compatible endpoints
- Volcengine / Doubao where project tasks require it
- local `llama.cpp` or other OpenAI-compatible local servers for sensitive projects

Configuration priority should be explicit:

```text
CLI option > task-specific environment variable > global environment variable > secrets file > disabled
```

Provider abstraction should live in one shared module. Current app-side code has
`tools/llm/provider.py` and a legacy wrapper `tools/llm_provider.py`; the 3CAN
release tree must align its engine tools with that shared abstraction before
public release.

## 8. Privacy And Safety

External LLM calls may send node names, summaries, descriptions, keywords,
selected file excerpts, and activity summaries. They must not send API keys,
passwords, secret files, full private chat logs, or entire project directories by
default.

Required guardrails:

- dry-run / cost-estimate mode for every batch LLM tool
- explicit provider shown before first call
- path allowlist for bootstrap scans
- `PROPOSED-*` outputs for node creation, merge, delete, or archive suggestions
- no automatic hard delete
- no publication of dogfood runtime state in the release package
- audit log of LLM tool calls: provider, model, input estimate, output estimate,
  timestamp, and affected proposed nodes

## 9. Open-Source Release Implications

Before wider release, the public package should include:

- this LLM integration map
- a minimal provider config guide with no real keys
- no project-private graph, runtime artifacts, logs, or embeddings
- sample graph fixtures that demonstrate LLM-assisted bootstrap safely
- smoke tests for no-key mode and one BYOK provider mode
- a release checklist item verifying which tools are truly shipped

Project-group prerelease in May 2026 should deliberately test three user modes:

- no LLM key: route/writeback still works, quality lower but usable
- DeepSeek-compatible key: low-cost Chinese project workflow
- alternate provider or local model: checks provider-neutral design and privacy path

## 10. Known Gaps

- The public release docs still mix shipped, partial, and planned LLM tools.
- Some engine tools exist in Desktop `neural-memory` but are not obviously present
  in this application repository.
- `--estimate-cost` is not consistently implemented across all LLM tools.
- Route-time generative query expansion is not yet a stable default path.
- Provider-specific token counting is not yet a complete cross-agent ledger.
- LLM-as-judge benchmark results require caveats: judge model, self-judge bias,
  prompt, dataset slice, and whether the measured layer is route/retrieval only.

## 11. Future Work

1. Align the 3CAN release tree with one shared provider abstraction.
2. Add a standard `--estimate-cost` and `--dry-run` option to every LLM tool.
3. Build a reviewed alias/keyword proposal workflow for Chinese project shorthand.
4. Add low-confidence route feedback that can propose query-expansion rules.
5. Add provider-specific token counting and a per-session/per-agent usage ledger.
6. Add open fixtures for first-run project diagnosis without private data.
7. Update README, USER_GUIDE, benchmark docs, and graph PNG explanation to point
   to this map.
