# Foundation convergence: upstream comparison and engineering decisions

Observed: 2026-09-10. Scope: capability/source comparison, **not a head-to-head
performance benchmark**. Public main, open PRs, installed Skills and the selected
production release were different versions. Reusing already implemented work
and aligning installation is preferable to a new memory/orchestration engine.

## Module decisions

| 3CAN module | Relevant maintained alternative / primary evidence | Adopt here | Do not infer or build |
| --- | --- | --- | --- |
| Durable memory and current/history lineage | [Graphiti](https://github.com/getzep/graphiti) models episodic knowledge and temporal relationships; its [edge implementation](https://github.com/getzep/graphiti/blob/main/graphiti_core/edges.py) separates creation, validity and expiration. | Keep provenance, supersession and lifecycle evidence distinct from a current-answer projection. Retain existing 3CAN current/history and project-scope implementation. | No migration to a second temporal database without a demonstrated local retrieval failure and migration test. Managed Zep capabilities are not identical to OSS Graphiti. |
| Memory ingestion / writeback | [Mem0's current OSS migration guide](https://docs.mem0.ai/migration/oss-v2-to-v3) describes append-only automatic extraction and removal of graph memory from OSS; explicit update/delete remain separate operations. | Avoid automatic semantic deduplication as the authority for destructive consolidation. Meaningful bounded writeback, exact identity and explicit supersession stay in the canonical owner. | Do not compare a managed graph feature with an OSS default, or assume extraction is factual because an agent stated it. |
| Graph-assisted retrieval | [GraphRAG dataflow](https://microsoft.github.io/graphrag/index/default_dataflow/) uses graph/community processing for document retrieval; [LightRAG](https://github.com/HKUDS/LightRAG) supports graph/vector retrieval and source-aware maintenance. | Retain route, precise retrieval, compact/full representations and explicit evidence references. Integrate existing ErrorKnowledge and project-orientation fixes rather than add another indexer. | No claims of superior recall/latency without identical corpus, questions, resources and evaluator. 3CAN is not automatically a document-ingestion substitute for these products. |
| Embeddings | [BGE-M3 model card](https://huggingface.co/BAAI/bge-m3) documents dense, sparse and multivector modes and 1024 dimensions. | Keep the verified pinned local dense backend and synchronized cache; preserve production/development distinction. | Using BGE-M3 through sentence-transformers does not mean every BGE-M3 retrieval mode is implemented. Do not enable reranking or a new model without local relevance/latency evidence. |
| ErrorKnowledge / consolidation | Graphiti's temporal provenance and LightRAG's source-aware maintenance are useful comparisons, not interchangeable error-resolution protocols. | Integrate existing error-family projection, preserved occurrences, scoped resolution and counterexample tests. Keep exact evidence and rollback; no bulk business-graph cleanup in this release. | Similar text is not necessarily the same root cause. An old error with no resolution evidence is not a verified fix. |
| Research | [Open Deep Research](https://github.com/langchain-ai/open_deep_research) retains research briefs and source material; its [research loop](https://github.com/langchain-ai/open_deep_research/blob/main/src/open_deep_research/deep_researcher.py) has bounded termination. | One existing decision note: failure/question → opened evidence → local constraints → chosen/rejected approach → executable probe → actual result. Keep bounded collection and turn bindings. | A stopped loop, source quota, score or supplied `pass` string is not evidence that the engineering problem is solved. Do not install another research runtime. |
| Semantic supervision | [Codex native hooks](https://learn.chatgpt.com/docs/hooks) expose lifecycle events; [pi extensions](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/extensions.md) distinguish lifecycle/settling events. | Use native lifecycle delivery and the existing RuntimeHook boundary/Git-currentness implementation. Reinforce actual module-output review and unmet criteria in the existing reference. | Installation, hook execution and semantic review quality are three separate claims. No second selector, proof lifecycle, daemon or task queue. Hosted tool/event coverage must be checked, not assumed. |
| Graph navigation | [Cytoscape.js](https://js.cytoscape.org/) offers graph selection/traversal/filtering; [Obsidian graph view](https://obsidian.md/help/plugins/graph) is a useful navigation reference. | Keep the existing renderer and add visible-scope counts, selection focus and fit/reset; repair proven graph reentry and composable-filter bugs. Lazy-load the full graph and stop animation when hidden. | A replacement renderer would not solve stale facts or missing edges. Large vendor graph counts do not prove local browser performance. |
| Concurrency / authorization | Native Git worktrees and the existing 3CAN ticket/SQLite owner are the actual local boundary. | Keep separate physical writers, exact project/workspace binding and just-in-time tickets only for governed operations. Research guidance does not authorize external effects. | No second lease, broad regex mutation policy or global shutdown on memory-service failure. |

## Local evidence and counterexamples

- Canonical 9700 startup verification: 2,655 nodes / 1,035 edges, production
  deep-ready, synchronized BGE-M3 cache. This is service integrity, not quality.
- Separate 9711: 19 nodes / 10 edges, development-ready and not production-ready.
  Startup used the existing no-seed path. Neither count is a client hardcode.
- Browser test replaced only the candidate HTML document; API reads reached
  the existing production owner. No second backend or business-graph copy/write.
- Before the fix, graph → overview → graph removed its only canvas:
  `GRAPH_REENTRY: before=1, after=0`. After the fix, reentry, combined interface /
  dormant filtering, endpoint-consistent links and selection focus were exercised
  on the actual graph. Desktop and 390px overview screenshots stayed private.
- Initial focused research/PR/frontend tests: 39 passed. Full regression and
  deployment are tracked separately in `../FOUNDATION_CONVERGENCE.md`.

## Acceptance and remaining limits

Source collection may establish structural readiness for review, never semantic
or implementation correctness. No amount of research guarantees zero defects.
The local validation plan is: focused failure counterexamples; native launcher
and lifecycle smoke tests; clean public seed benchmark; full test suite; one
independent review; immutable release rehearsal; supported Supervisor cutover
with exact rollback; live identity, route/retrieval and bounded writeback readback.

This release does not prove a 72-hour RPA repair, an improved video, unlimited
context retention or a cross-product benchmark win. Those need their own actual
business acceptance. No external code or new runtime dependency was copied in
this convergence. The existing source-available license is unchanged; do not
describe PolyForm Noncommercial as an OSI-approved open-source license.
