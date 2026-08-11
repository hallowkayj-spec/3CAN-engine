# 3CAN Prior-Art and OPC Convergence

Status: `PARTIAL`

Observed: 2026-08-11

Public baseline: `cb532daee7c17954b2cdf93b536f65683db4c9a2`

This report records a build-vs-buy review and a bounded convergence change. It
does not claim production deployment, complete OPC utility proof, or token
savings. External projects supplied design and test ideas only; no external
source code was copied.

## A. Current 3CAN baseline

The detailed inventory is in
[CURRENT_3CAN_CAPABILITY_BASELINE.md](CURRENT_3CAN_CAPABILITY_BASELINE.md).

3CAN already had the important product substrate: durable INTF/PROC/DEC and
ErrorKnowledge nodes, BGE-M3 hybrid route, graph traversal, skeleton/slim/full
budgets, project/worktree/ticket identity, current/history projection,
supersession edges, deep readiness, writeback, handoff/session archaeology,
and multi-project primitives. Git, CI, Runtime, and providers remain their own
sources of truth; 3CAN compiles project cognition and provenance rather than
replacing those systems.

The real baseline gaps were narrower:

1. Archive, current truth, and supersession were not uniformly enforced by the
   canonical mutation and route owners.
2. Durable-current writes and persistent route learning needed risk-matched
   provenance without adding an approval platform.
3. A successful write did not prove that a clean Agent could recover the
   milestone's critical facts and evidence.
4. Public contract, release-manifest, and source-boundary evidence could drift.
5. Real-graph route output was not deterministic across processes, and an
   existing low-fan-out code signal was ignored inside natural-language tasks.

## B. Prior-art matrix

The evidence, exact commits, verdicts, and license boundaries are in
[PRIOR_ART_CAPABILITY_MATRIX.md](PRIOR_ART_CAPABILITY_MATRIX.md).

The review classified almost all external surface as `ALREADY_HAVE`,
`HARDEN_EXISTING`, `ADAPT`, `REJECT`, or `DEFER`. No new database, retrieval
engine, daemon, wiki compiler, approval service, vector store, or MCP server was
justified.

## C. Real gaps found

- **Lifecycle truth:** the documented archived state was not a real persisted
  status, while dormant/archived/superseded facts could still influence normal
  indexes, fallback results, or protected current nodes.
- **Canonical mutation boundary:** generic CRUD, merge, edge deletion, feedback,
  and lifecycle paths did not all share durable authority, project identity,
  atomic validation, and lineage rules.
- **Recovery evidence:** route rank and an HTTP success were insufficient to
  prove exact critical-fact and evidence recovery after a serious milestone.
- **Contract truth:** implemented parameters, status values, error responses,
  current production topology, and mandatory release artifacts had drifted
  from public documentation.
- **Route reproducibility:** equal-frequency co-occurrence candidates and some
  graph/code-index iteration order were process-dependent. Natural-language
  queries containing a discriminative existing code did not reuse the existing
  code index.

## D. Implemented convergence

### 1. Truthful archive and controlled supersession

`NodeStatus`, lifecycle sweep, route/index filters, edge mutation, merge, and
generic delete now share the existing graph owner. Archived and superseded
facts stay out of ordinary retrieval and return only for explicit historical
intent. Unsuperseded protected current truth is not aged out automatically.

### 2. Risk-matched durable authority and one feedback owner

The existing writeback and node metadata carry a small typed authority receipt:
machine-verifiable, user-authoritative, or untrusted/inferred. User authorization
is an auditable assertion, not cryptographic authentication. Project, tenant,
credential, writer, destructive, and repository boundaries remain hard gates.
Route feedback now validates and records through one GraphEngine owner; protected
durable families cannot acquire keyword or short-code promotion from unauthorised
feedback.

### 3. Serious-milestone recovery probe

A bounded, explicitly invoked standard-library client composes existing deep
readiness, route, and exact read operations. It binds graph identity, reads only
declared expected nodes, verifies trusted leaf values and evidence digests, and
fails closed on missing, too-short, or malformed facts. Probe literals must be
discriminative. It creates no durable knowledge, ticket, owner, or background
task; ordinary route telemetry and activity still apply. It is not an automatic
hook.

### 4. Deterministic release and contract truth

The public protocol now describes the implemented routes, identity parameters,
status values, correlation rules, and bounded writeback responses. The release
manifest includes the convergence evidence and probe. Production-source wording
now distinguishes current parity, candidate changes, and deployment truth.

### 5. Deterministic bounded route reuse

Co-occurrence, code-index, and score ties now have stable ordering; graph
traversal uses one fixed bounded anchor count rather than the requested output
size. Natural-language queries may reuse the existing code index only when an
embedded code and its merged candidate set are low-fan-out. Broad labels are
ignored, so this does not become a second keyword engine or a hard-coded alias
table.

## E. Reused external capability

No external implementation was copied and no new attribution notice is required
for code. The clean-room adaptations were:

| Source | Commit / publication | License boundary | Adapted idea |
|---|---|---|---|
| [Karpathy LLM Wiki idea](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) | `ac46de1ad27f92b28ac95459c782c07f6b8c964a` | no LICENSE; concept only | compiled project reality instead of repeated rediscovery |
| [WeAgentAI LLM-Wiki](https://github.com/WeAgentAI/LLM-Wiki) | `79a5a2d5fb9e07c7e5d21fe704b4eca0c27452ef` | repo MIT; paper distributed through arXiv, concept only | search/read/link/sufficiency as an acceptance shape |
| [WiCER](https://arxiv.org/abs/2605.07068) | arXiv 2605.07068 | paper CC BY-NC-ND; no canonical code found | compile -> probe -> focused refine |
| [WikiKV](https://github.com/WeAgentAI/WikiKV) | `b45aae773d5284f97c2f07d4da9e6bd7f18e7919` | repo MIT; paper distributed through arXiv, concept only | expected hashes and readback before acceptance |
| [`nvk/llm-wiki`](https://github.com/nvk/llm-wiki) | `b6a72bfd3df52e8f5fd0ae13851c6e4f250540f3` | MIT | active/archive query exclusion and explicit promotion tests |
| [`lucasastorian/llmwiki`](https://github.com/lucasastorian/llmwiki) | `aac3e6493306b7fff3b09cb181ca93918e93d0e6` | Apache-2.0 | durable source before rebuildable derived index and lint |
| [`gowtham0992/link`](https://github.com/gowtham0992/link) | `643e208adbbe2dfd1c91bf9e8305e6dec2b037a6` | MIT | risk-matched authority and supersession acceptance |
| [`nashsu/llm_wiki`](https://github.com/nashsu/llm_wiki) | `fa2652eb8186635c2b251007d0a46b0614528a7d` | GPL-3.0; study only | staged compile and graph-relevance test ideas |

The requested `Ss1024sS/LLM-wiki` canonical repository was unavailable during
the review; it remained concept-only and supplied no code.

## F. Before/after benchmark

### Frozen real-graph route regression

Twelve sanitized real project-recovery queries were run against isolated graph
copies with the same 5,405-node / 6,916-edge source snapshot, the same frozen
BGE-M3 cache and revision, `PYTHONHASHSEED=0`, and reranking disabled. Query text
and graph content remain private; only aggregate results are published.

| Metric | Production-source baseline | Candidate | Delta |
|---|---:|---:|---:|
| Hit@1 | 0.5000 | 0.5000 | 0.0000 |
| Hit@3 | 0.5000 | 0.6667 | +0.1667 |
| Hit@10 | 0.8333 | 0.8333 | 0.0000 |
| MRR | 0.5694 | 0.6139 | +0.0444 |
| Query p50 | 664 ms | 986 ms | +48.5% |
| Query p95 | 1,943 ms | 2,896 ms | +49.0% |

One short-code recovery case improved from rank 6 to rank 2 and one embedding
cache recovery case improved from rank 4 to rank 2. One AI45 network-recovery
case moved from rank 4 to rank 5; seven cases were stable and two missed in both
lanes. Independent-process checks produced stable expanded queries and result
ordering after the deterministic fix. Correctness improved on this small set,
but latency regressed in this single local observation and is not accepted as a
performance improvement.

### Public synthetic seed

The final frozen candidate was also rerun against the public 16-node / 12-edge
hashing fixture: 46 route queries produced MRR 0.9783, Hit@3 1.0, p95 35 ms;
the 10-case substrate check produced top-1 1.0, mean top-3 recall 0.8167,
ErrorKnowledge proactive@3 1.0, p95 31 ms. The bound receipt is
[SEED_GRAPH_BENCHMARK_20260809.json](SEED_GRAPH_BENCHMARK_20260809.json).
This proves only the public fixture, not real OPC utility.

The full paired OPC utility/token benchmark was **not run**. OPC usefulness is
`PARTIAL`; token savings is `UNAVAILABLE`.

## G. Complexity delta

Final candidate scope relative to the public baseline:

- files changed/added: 27
- source and documentation delta: `+3,464 / -310` lines
- dependencies: +0
- persistent state stores: +0
- services/daemons/ports: +0
- runtime configuration or environment variables: +0
- new routing modes or public protocols: +0

The change extends existing graph, writeback, feedback, route, MCP, protocol,
and release owners. Duplicate feedback orchestration and speculative mutation
owners were removed during review. There is no compatibility fallback, second
canonical owner, background worker, automatic milestone hook, or approval DB.

## H. What was deliberately rejected

- WikiKV/Redis/HDFS or temporal backend migration
- a second Error Book or second memory engine
- a Markdown Wiki as project truth
- another vector database or BGE-M3 replacement
- cross-project/3CAN federation
- another MCP server or retrieval protocol
- deep-search orchestration, watcher, janitor, or daemon
- blanket human approval for machine-verifiable writes
- direct reuse of GPL, unlicensed, unavailable, or unnecessary external code

These options add state, deployment surface, or user ceremony without evidence
that they reduce OPC correction, search, file reads, tokens, or wall-clock.

## I. Research cleanup proof

The disposable research root was outside every release/project repository. It
contained 2,399 files (108,033,458 bytes), eight shallow Git checkouts, and three
paper archives before cleanup.

```text
Research scratch root = C:\CodexResearch\3can-prior-art-20260811T090000Z
Deleted = true

Cloned repos remaining = 0
Temporary venv remaining = 0
Temporary node_modules remaining = 0
Research processes remaining = 0
Research listeners remaining = 0
Global packages installed = 0
Production services restarted = 0
External repositories copied into release repo = 0
```

No external repository was modified, executed, added to `PYTHONPATH`, or left
inside the public worktree.

## J. Remaining blockers

1. The full paired OPC utility benchmark, including file reads, search calls,
   wrong-path work, corrections, tokens/cache, and total cost, remains
   `UNAVAILABLE`.
2. The real regression contains only 12 private cases; two missed in both lanes,
   and one locally observed candidate latency run was materially slower.
3. The candidate has not been deployed to production 9700. Production parity
   applies to the baseline semantic core, not to these unshipped changes.
4. GitHub CI and external review must run on the final Draft PR commit before a
   merge, release, or production cutover decision.

Accordingly, the candidate is ready for Draft PR review, not for automatic
merge, deployment, graph migration, or a new architecture phase.
