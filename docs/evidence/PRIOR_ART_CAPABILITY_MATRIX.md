# 3CAN Prior-Art Capability Matrix

Observation date: 2026-08-11

Research mode: static, disposable, no external code execution or dependency
installation

This is a decision record, not a feature backlog. The selected work adapts
invariants and tests to existing 3CAN owners; no external implementation was
copied.

## Mandatory corpus

| Prior art | Capability examined | Existing 3CAN equivalent | Gap | Verdict | License boundary |
|---|---|---|---|---|---|
| [Andrej Karpathy LLM Wiki idea](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) | raw source vs compiled knowledge, ingest/query/lint | Git/Runtime/provider truth plus INTF/PROC/DEC/ErrorKnowledge, route and writeback | serious milestones lacked recovery acceptance | `ADAPT` concept only | no LICENSE at commit `ac46de1ad27f92b28ac95459c782c07f6b8c964a`; no text/code copied |
| [WeAgentAI LLM-Wiki](https://github.com/WeAgentAI/LLM-Wiki) / Retrieval as Reasoning | search -> read -> link -> sufficiency, Error Book | BGE-M3 hybrid route, retrieve, graph edges, bounded modes, ErrorKnowledge | ranking was not evidence of clean-agent recovery | `HARDEN_EXISTING`; reject wiki/BM25/ingest/Error Book | repo MIT at `79a5a2d5fb9e07c7e5d21fe704b4eca0c27452ef`; paper distributed through arXiv, concept only |
| [WiCER](https://arxiv.org/abs/2605.07068) | compile -> evaluate probes -> focused refine | durable nodes, route/readback and OPC receipts | write success did not prove critical-fact retention | `ADAPT` test pattern | no canonical code release found; paper CC BY-NC-ND 4.0 |
| [WikiKV](https://github.com/WeAgentAI/WikiKV) | expected hash/CAS, object-before-pointer, readback, budgeted navigation | node hashes, atomic writes, rebuildable index and route budgets | authority/readback and contract drift | `HARDEN_EXISTING`; reject KV/storage rewrite | repo MIT at `b45aae773d5284f97c2f07d4da9e6bd7f18e7919`; paper distributed through arXiv, concept only |
| [`nvk/llm-wiki`](https://github.com/nvk/llm-wiki) | query-lite read-only, explicit promotion tests, active/archive | read-only route, SES/HO sources, durable families | authority and archive behavior were inconsistent | `HARDEN_EXISTING`; reject plugin/wiki/session stack | MIT, commit `b6a72bfd3df52e8f5fd0ae13851c6e4f250540f3` |
| [`Ss1024sS/LLM-wiki`](https://github.com/Ss1024sS/LLM-wiki) | requested stale/manifest/delta patterns | runtime/graph hashes, release manifest, scanner | canonical artifacts could drift or disappear | `ADAPT` concept only | canonical repository unavailable on 2026-08-11; no reuse |
| [`lucasastorian/llmwiki`](https://github.com/lucasastorian/llmwiki) | durable source before derived index, deterministic lint, workspace isolation | durable graph JSON, rebuildable embeddings, project capsule | release/protocol lint coverage was incomplete | `HARDEN_EXISTING`; reject VaultFS/DB/watcher/MCP | Apache-2.0, commit `aac3e6493306b7fff3b09cb181ca93918e93d0e6` |
| [`gowtham0992/link`](https://github.com/gowtham0992/link) | active/archive/stale, proposals, supersession, poisoning tests | statuses, current/history, supersedes, writeback and OPC scorer | archive wording and generic authority could poison current truth | `ADAPT` lifecycle/authority tests; defer as-of recall | MIT, commit `643e208adbbe2dfd1c91bf9e8305e6dec2b037a6` |
| [`nashsu/llm_wiki`](https://github.com/nashsu/llm_wiki) | staged compile, cache completeness, graph relevance | BGE-M3 + lexical/exact RRF, graph traversal and bounded modes | post-write recovery acceptance | retrieval `ALREADY_HAVE`; compile/probe `ADAPT`; rest `REJECT` | GPL-3.0, commit `fa2652eb8186635c2b251007d0a46b0614528a7d`; no code copied |

## Reuse-first decisions

### Truthful lifecycle and controlled supersession

- Real failure: old reality could stay active or incorrectly hide current truth.
- Existing owner: `NodeStatus`, lifecycle sweep, route filter, edge mutation.
- Adaptation: one archived value plus checks/tests in those owners.
- Added surface: no service, database, daemon, dependency, or migration system.

### Durable authority and feedback consolidation

- Real failure: inferred/session/query material could influence durable current
  facts or persistent keywords with weaker provenance than ErrorKnowledge.
- Existing owner: writeback, project identity, `record_outcome`/keyword healer.
- Adaptation: a small receipt in existing node metadata and one feedback owner.
- Explicit user authorization remains an audit assertion, not authentication.

### Serious-milestone recovery probe

- Real failure: an HTTP 200 did not prove that the next clean Agent could recover
  owner, status, evidence, and replacement lineage.
- Existing owner: route, exact read and evidence references.
- Adaptation: one bounded, explicitly invoked CLI probe; it creates no durable
  knowledge, ticket, owner, or background task, while ordinary route telemetry
  and activity still apply.

### Deterministic release and route truth

- Real failure: contracts could drift while scans passed; real route output also
  varied across processes because equal-frequency co-occurrence terms and graph
  traversal anchors were not stable.
- Existing owner: manifest/scanner and `GraphEngine.route`.
- Adaptation: extend those owners, use deterministic tie-breaking, keep one
  fixed bounded traversal seed, and reuse the existing code index only for
  discriminative low-fan-out codes embedded in natural-language queries.

## Deliberate rejections and deferrals

- `REJECT`: Markdown wiki, WikiKV/Redis/HDFS backend, second vector database,
  second Error Book, second memory engine, temporal database, another MCP,
  deep-research orchestrator, watcher, daemon, or cross-3CAN federation.
- `DEFER`: point-in-time/as-of recall until current lineage plus explicit history
  fails a real OPC case.
- `DEFER`: adaptive multi-step search/read/sufficiency orchestration until one
  route plus bounded retrieve fails a real utility benchmark.
- `REJECT`: direct external code reuse in this round. License-compatible sources
  informed invariants and test shapes only.

## Counterfactual

Without the selected convergence, archived reality would remain contradictory,
weak-provenance material could affect current truth, milestone completion would
remain unrecoverable to a clean Agent, and release/route results could pass by
accident. The rejected components add more state and ceremony without evidence
that they reduce OPC correction, search, file reads, tokens, or wall-clock.
