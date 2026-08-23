# ErrorFamily isolated candidate — 2026-08-23

Status: **VALIDATING (recall and isolation gates pass; latency is not comparable)**.

This is internal production-derived evidence, not a public benchmark or a
deployment receipt. Query text, project-specific node IDs, local paths, and
decision manifests stay outside the release. The live 9700 graph and runtime
were not mutated, stopped, or restarted.

## Governance result

- source graph: 2,568-node production snapshot copied read-only into an
  isolated graph; no candidate server or second production owner was started
- reviewed ErrorCases: 145 total
- deterministic complete candidates: 16 cases in 14 families
- incomplete legacy cases: 129, all retained as `review_required`
- automatic semantic merges: 0
- graph business-node or edge mutations: 0
- active candidate: 16 reviewed sidecar assignments in 14 families
- dense embedding rebuild: not required; aliases are sparse/route-only

The candidate set drifted once while the shared graph received an external
write. Compilation rejected the stale snapshot. A fresh plan contained the
same 16 candidate identities, no additions or removals, and no identity
changes; the reviewed decisions were then rebound to the new snapshot.

## Same-suite result

The suite and independent node-ID ground truth are identical to
`ERROR_KNOWLEDGE_QUERY_BENCHMARK_20260823.md`.

| Gate | Production baseline | Isolated candidate | Threshold | State |
|---|---:|---:|---:|---|
| Top-1 recall | 0.50 | 1.00 | >= 0.80 | pass |
| Top-3 recall | 0.50 | 1.00 | >= 0.95 | pass |
| Incorrect-family pollution | 0.00 | 0.00 | <= 0.01 | pass |
| Ordinary-query false error route | 0.00 | 0.00 | <= 0.01 | pass |
| Latency p95 | 264.1 ms over HTTP | 2,981.4 ms direct | <= 10% regression | validating |

The latency values are not an apples-to-apples comparison: the baseline
includes the production HTTP path, while the candidate ran through a fresh
isolated direct GraphEngine process. The receipt therefore overrides no
threshold and remains typed `VALIDATING`.

## Adversarial boundaries

- Exact ek2 identity is still the strongest and only blocking ErrorCase
  identity.
- Proposed/default aliases cannot activate the ranking promotion.
- Reviewer aliases promote only on an explicit operational-error route and
  only when all matched unique aliases resolve to one case.
- A shared or multi-case alias yields no promotion.
- Ordinary development queries still exclude ErrorKnowledge artifacts.
- ErrorFamily never inherits a resolution and never changes ErrorCase nodes or
  graph edges.
- Production activation still requires reviewed PR state, a maintenance
  window, the canonical Supervisor owner, and a matching HTTP benchmark.

## Internal receipt integrity

- suite SHA-256:
  `412e19df066d7fca46159578de8fed7bc9c441c333f137e353d31e92cfee919e`
- candidate manifest file SHA-256:
  `e266be0c8e99c11e839e025ee3fdb5734c959ab1c79cb1fd1415fcbb58117f7d`
- decision manifest file SHA-256:
  `25086c1e91190c78916ab14b988576fcd6be545b46b0461a9e9088a8d076d6d7`
- compiled active candidate file SHA-256:
  `383c454eaa0e7cc0db7c784f3359b6a0ec6805a04c3902efc84929a6c40dffcc`
- isolated candidate receipt SHA-256:
  `a257607613b50698e832a51bc12e24424b8fdbf3f21afefbe2a4b1fb324514ee`

These hashes identify local private evidence only. They do not make that
evidence part of the public release.
