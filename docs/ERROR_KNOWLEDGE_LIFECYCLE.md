# Error Knowledge Lifecycle

> Status: **v0.2 implementation contract and release candidate**
>
> Target: the 3CAN-engine v0.2 development line
>
> Implementation status: **the stdlib core, bounded production route,
> SQLite/WAL ticket and occurrence ledgers, evidence-backed replay-safe `done`,
> migration tooling, focused tests, and benchmark fixtures are staged; the
> candidate is not yet deployed, tagged, or publicly released**
>
> License: this document is part of the 3CAN-engine source-available
> distribution under the **PolyForm Noncommercial License 1.0.0**. PolyForm Noncommercial is
> not an OSI-approved open-source license. See `LICENSE` and `LICENSING.en.md`.

## 1. Purpose

3CAN records failures so that a later agent can retrieve a verified solution,
not merely rediscover that a failure happened.

The v0.1 model uses typed `ERR-*` nodes and route-time warnings. That model
demonstrated the value of cross-session error recall, but it does not yet
separate:

- a single failed attempt;
- a canonical recurring problem;
- a tested resolution;
- and a versioned policy decision.

When those concerns share one durable node type, repeated attempts can create
high-cardinality clusters, route packs can become noisy, and an unresolved
historical record can be mistaken for a reason to stop unrelated work.

This contract defines a four-layer lifecycle for error knowledge, explicit
solution writeback through `done`, bounded route behavior, simplified ticket
leases and receipts, reversible migration, and benchmark gates.

Normative words such as **MUST**, **MUST NOT**, **SHOULD**, and **MAY** describe
the v0.2 contract. A normative target is not a shipped claim unless the
changelog and test evidence identify its implementation.

## 2. Privacy-safe motivation

A recent internal dogfood snapshot showed that error-and-lesson records had
grown to a majority share of graph nodes. The same review found repeated
variants of operational failures and cases where mandatory context injection
could exceed the route budget requested by the caller.

These observations are directional, not public benchmark results. Raw counts,
node identifiers, local paths, stack traces, prompts, credentials, user data,
and project payloads are intentionally omitted. Public evidence for this work
MUST use only:

- aggregate ratios or scale bands;
- synthetic or irreversibly redacted examples;
- documented design thresholds;
- and reproducible fixtures that contain no private project material.

## 3. Design goals and non-goals

### 3.1 Goals

The v0.2 design aims to:

1. preserve immutable evidence without turning every retry into a graph node;
2. group equivalent failures using deterministic fingerprints;
3. make verified resolutions first-class, routeable knowledge;
4. reopen resolved cases when a compatible failure regresses;
5. keep route output inside the caller's declared token budget;
6. reserve hard blocking for a concrete, versioned safety policy;
7. make prepare, retry, and completion idempotent;
8. support migration and rollback without destructive in-place rewriting;
9. retain provenance while removing private or high-volume payloads from the
   hot graph;
10. measure recovery quality, false blocks, and graph health separately.

### 3.2 Non-goals

This contract does not:

- replace the activity log with an LLM-generated summary;
- permit an LLM to delete, merge, or resolve authoritative records by itself;
- claim that semantic similarity proves two failures have the same cause;
- require a graph database, workflow service, policy engine, or generative LLM;
- make every warning a gate denial;
- publish internal error payloads as benchmark fixtures;
- or claim a deployment, tag, or public release before those actions occur.

## 4. Four-layer error knowledge model

### 4.1 `ErrorOccurrence`: immutable execution evidence

An `ErrorOccurrence` represents one observed failure or abnormal outcome.
Occurrences SHOULD live in the append-only activity/evidence store. They SHOULD
NOT become durable graph nodes by default.

The v0.2 implementation profile records this normalized envelope:

```json
{
  "occurrence_id": "occ_...",
  "fingerprint_version": "ek2",
  "fingerprint": "ek2:<sha256>",
  "project_id": "portable project scope",
  "operation": "normalized operation",
  "component": "normalized adapter or subsystem",
  "error_type": "stable machine-readable type",
  "error": "redacted current symptom",
  "root_cause": "redacted current diagnosis",
  "occurred_at": "RFC3339 timestamp",
  "agent_id": "optional agent identity",
  "context": {}
}
```

Raw stack traces, screenshots, request bodies, and command output MUST NOT be
copied into the hot graph. They MAY be retained in an access-controlled cold
store according to a configurable retention policy. The graph stores only the
minimum redacted summary, hash, and evidence reference.

Expected, handled, or transient conditions MAY remain telemetry-only. A single
retry MUST NOT create a new canonical case unless its signature or impact is
materially different.

The canonical server observes typed exception, validation, and unhandled
failure paths that it returns to clients.
Expected gates become rate-bounded, sanitized `3can_issue_observed` Activity;
only stable protocol, identity-binding, integrity, and server failures enter
this occurrence ledger. The original HTTP response is unchanged, and observer
failure never replaces or delays the original error. Replays bound to the same
authoritative ticket or the same caller-supplied `X-Request-ID` are idempotent;
without either
correlation, separate failed requests remain separate occurrences, and a second
occurrence is required before graph projection. The request-ID value is hashed
for correlation and is never stored verbatim.
Expected gates remain bounded Activity observations and do not become blocking
ErrorCases. The observer never records request bodies, headers, raw exception
text, unmatched URL paths, or failures from `/api/errors/*`, health, or stats
routes, and it does not enable the public unticketed occurrence endpoint.
Endpoints that intentionally return an error-shaped `JSONResponse` without
raising are outside this observer boundary.

This boundary covers errors that reach 3CAN through MCP, the project kit,
PowerShell, or direct HTTP. Local validation and transport failures that occur
before a request reaches the server remain `UNAVAILABLE` until an authorized
ticketed delivery can succeed; the server MUST NOT claim to have observed them.

### 4.2 `ErrorCase`: canonical recurring problem

An `ErrorCase` groups compatible occurrences. The SQLite ledger is
authoritative; its graph node is a rebuildable projection used for routing. It
answers: “What problem is this?”

The v0.2 implementation profile is:

```json
{
  "schema_version": "3can.error-case/v1",
  "case_id": "ERR-case-<first 24 fingerprint hex>",
  "fingerprint_version": "ek2",
  "fingerprint": "ek2:<sha256>",
  "project_id": "portable project scope",
  "operation": "normalized operation",
  "component": "normalized adapter or subsystem",
  "error_type": "stable machine-readable type",
  "error": "redacted current symptom",
  "root_cause": "redacted current diagnosis",
  "occurrence_count": 2,
  "state": "observed|diagnosed|mitigated|resolved|regressed|superseded",
  "active_resolution": null,
  "resolution_history": []
}
```

Fingerprinting MUST be deterministic before any semantic comparison. `ek2`
identity is exactly the canonicalized `project_id + operation + component +
error_type`. The mutable symptom text, root-cause diagnosis, evidence,
timestamps, retry IDs, and resolution are deliberately excluded. Improving a
diagnosis therefore updates one case instead of splitting its identity.

Normalization MUST remove secrets, local paths, user identifiers, request IDs,
timestamps, random values, and other high-cardinality data. Project and
workspace scope remain explicit routing dimensions. Cross-project reuse occurs
through applicability metadata and verified resolutions, never by silently
dropping the project identity from an existing fingerprint version.

Semantic similarity MAY propose a merge, but ambiguous merges MUST remain
`review_required`. Frequency alone MUST NOT establish common root cause.

### 4.3 `Resolution`: verified reusable knowledge

A `Resolution` answers: “What fixed or safely mitigated this problem, under
which conditions, and how was that claim verified?”

Minimum fields:

```json
{
  "resolution_id": "FIX-<content digest>",
  "case_id": "ERR-case-<fingerprint digest>",
  "root_cause": "supported explanation or review_required",
  "solution_summary": "actionable fix or mitigation",
  "evidence_id": "EVD-<content digest>",
  "fixed_in": "portable file, commit, or release reference",
  "resolved_by": "agent identity",
  "resolved_at": "RFC3339 timestamp",
  "ticket_id": "rt_..."
}
```

A resolution MUST NOT be marked `verified` from prose alone. It requires at
least one durable receipt tied to a test, audit, replay, target digest, or other
task-appropriate check. `not_reproduced` and `unresolved` MUST NOT transition a
case to `resolved`.

When a newer resolution invalidates an older one, the older record remains
available through `supersedes_resolution_id`; it is not silently overwritten.

### 4.4 `PolicyProcedure`: versioned control memory

A `PolicyProcedure` describes a stable safety rule or execution protocol. It
answers: “What is allowed, warned, reviewed, or blocked?”

It is separate from both failures and resolutions. Minimum fields:

```json
{
  "policy_id": "policy_...",
  "policy_version": "semver or immutable digest",
  "applies_to": ["operation or scope class"],
  "decision": "allow|warn|manual_review|block",
  "reason_code": "stable machine-readable code",
  "required_evidence": ["condition"],
  "effective_at": "RFC3339 timestamp",
  "supersedes_policy_id": null
}
```

An unresolved error case MUST NOT become an implicit policy. A policy change
requires an explicit version, provenance, tests, and rollback path.

## 5. Relationships and lifecycle

The proposed graph relationships are:

```text
ErrorOccurrence --GROUPED_IN--> ErrorCase
Resolution      --resolves------> ErrorCase
Resolution      --verified_by---> Evidence
Resolution      --supersedes----> Resolution
ErrorCase       --regressed_from-> Resolution
PolicyProcedure --applies_to----> Operation or Scope
ErrorCase       --related_to----> Decision, Interface, or Component
```

An occurrence may be represented by an activity-store reference instead of a
graph node. The durable graph SHOULD normally contain one canonical case and
its small set of versioned resolutions.

Case transitions:

```text
observed -> diagnosed -> mitigated -> resolved
        \-------------------------> resolved
any non-superseded state          -> superseded
resolved + compatible new occurrence -> regressed
regressed + verified new resolution   -> resolved
```

All transitions MUST be append-audited. A case status change MUST NOT erase its
prior state, evidence hashes, or resolution lineage.

## 6. Explicit solution writeback through `done`

In v0.2, `POST /api/activity/done` supports optional structured resolution
fields. Completion and solution writeback are related but not identical:

- a task can complete without resolving an encountered error;
- an error can be mitigated without being permanently fixed;
- and a transport-level success does not prove durable graph mutation.

Implemented request fragment:

```json
{
  "agent_id": "codex-public-fixture",
  "ticket_id": "rt_...",
  "detail": "public-safe completion summary",
  "affected_nodes": [],
  "resolved_errors": ["ERR-case-<fingerprint digest>"],
  "root_cause": "supported, redacted explanation",
  "solution_summary": "actionable, redacted fix",
  "verification_evidence": [
    {
      "kind": "test",
      "ref": "artifacts/public-fixture-result.json",
      "summary": "focused regression passed",
      "verified": true,
      "verifier": "pytest",
      "digest": "sha256:<64 hex>"
    }
  ],
  "fixed_in": "portable source reference"
}
```

The server MUST recoverably:

1. validate the consumed ticket, agent, completion hash, and
   ticket-bound `allowed_error_ids`;
2. verify each typed evidence receipt by recomputing an artifact digest inside
   an allowed evidence root; an activity self-hash is an audit reference only
   and MUST NOT authorize automatic resolution;
3. upsert Evidence, Resolution, and graph edges through the completion journal;
4. transition the canonical case only after evidence verification;
5. append or confirm the completion activity;
6. return durable object IDs, the completion hash, and the activity self-hash.

The implemented response includes:

```json
{
  "ok": true,
  "ticket_id": "rt_...",
  "ticket_state": "completed",
  "completion_request_hash": "<sha256>",
  "self_hash": "<activity sha256>",
  "resolution_outcome": "resolved|review_required",
  "resolved_errors": [
    {
      "error_id": "ERR-case-<fingerprint digest>",
      "resolution_id": "FIX-...",
      "evidence_id": "EVD-...",
      "case_status": "resolved"
    }
  ]
}
```

HTTP success without an activity self-hash, completion request hash, and the
server's actual `resolved_errors` result MUST NOT be presented as successful
solution writeback. An unverified evidence claim produces `review_required`,
not `resolved`.

Repeated `done` calls with the same receipt MUST be idempotent. They return the
same durable IDs and MUST NOT increment occurrence or resolution counts.

## 7. Route, warn, review, and block semantics

### 7.1 Route is retrieval, not enforcement

`route` returns relevant project context. It MUST NOT block an operation merely
because it retrieved an unresolved, frequent, or high-degree error case.
An ordinary non-error route excludes both legacy and canonical `ERR-*` records
from semantic ranking and global memory injection. They remain available by
direct retrieval and through an explicit error/case query.

The recommended order is:

1. exact signature or protocol-code match;
2. hard filters for component, operation, version, status, and authorized
   scope;
3. verified-resolution quality and applicability;
4. lexical, vector, and graph-neighborhood ranking;
5. deterministic packing inside the declared token budget.

A normal error route SHOULD return no more than:

- one best canonical case;
- one best applicable resolution;
- and one raw-evidence reference when authorized.

Additional candidates MAY be returned as compact identifiers, not full node
payloads.

### 7.2 Decision meanings

| Decision | Meaning | May stop the operation? |
|---|---|---|
| `allow` | No applicable guard condition was found. | No |
| `warn` | Relevant history or a recoverable concern exists. | No |
| `manual_review` | Evidence is ambiguous or authority is required. | Only at an explicit human/authority gate |
| `block` | A concrete, versioned policy condition proves the requested action unsafe or out of scope. | Yes |

Every non-`allow` decision MUST include:

```json
{
  "decision": "warn",
  "reason_code": "stable_code",
  "policy_version": "immutable version",
  "decision_id": "decision_...",
  "evidence_refs": ["opaque reference"],
  "remediation": "bounded next action"
}
```

Cluster size, route similarity, a stale historical failure, or absence of a
known resolution is insufficient for `block`.

Hard blocks are reserved for explicit safety boundaries such as destructive
target mismatch, protected-scope mutation, secret exposure, or invalid
authority. Advisory research and read-only diagnostics SHOULD fail open with a
warning when the knowledge service is degraded. Mutations covered by an
explicit fail-closed policy MUST return a short policy decision, not inject a
large error cluster.

### 7.3 Budget behavior

`budget_tokens` is a hard route-pack ceiling. Mandatory knowledge MUST be
summarized or replaced by stable references to fit within it. The engine MUST
NOT silently exceed the caller's budget to inject arbitrary full nodes.

A constant-size safety decision envelope MAY be returned separately from the
knowledge pack. If this envelope is outside the requested budget, the response
must report its size explicitly.

## 8. Ticket lease, attempts, and receipts

### 8.1 Stable intent identity

Prepare SHOULD calculate a stable intent key from:

```text
project scope
workspace identity
workorder identity
normalized operation
ordered target digest
```

The intent key is not the ticket ID. Repeating the same prepare request while a
compatible lease is active SHOULD return the same live ticket.

The release-candidate implementation stores tickets, indexed append-only
events, completion request hashes, and the recovery journal in SQLite with WAL,
`BEGIN IMMEDIATE`, and a busy timeout. Legacy ticket JSON/JSONL files are
read-only import sources and are never rewritten.

### 8.2 Separate ticket and attempt

The proposed state model is:

```text
NEW -> PREPARED -> EXECUTING -> DONE
                           \-> FAILED
PREPARED or EXECUTING      \-> EXPIRED
```

- `ticket_id` identifies the bounded mutation intent recorded as evidence.
- `attempt_id` identifies one execution attempt.
- `lease_expires_at` bounds exclusive use.
- `target_digest` proves the target state on which the decision was made.
- `policy_version` proves which guard policy was evaluated.

A transient retry creates a new attempt under the same compatible ticket. It
does not create a new error case. Permanent validation and policy failures are
non-retryable unless their inputs or authority change.

### 8.3 Lease refresh

If a lease expires but the intent, target digest, and policy version remain
compatible, prepare SHOULD refresh the lease rather than emit a new error or
force a complete reroute.

If the target digest or authorized scope changed, refresh MUST fail with a
specific reason code and require a new ticket. “Stale ticket” is operational
telemetry; it is not automatically a durable graph case.

A ticket consumed while its lease was valid MAY finish the exact,
hash-identical completion after expiry. It cannot authorize another mutation,
change its target/scope, or resolve an ErrorCase outside the ticket's
`allowed_error_ids`.

### 8.4 Consumption receipt

Ticket consumption SHOULD return a signed or hash-chained receipt:

```json
{
  "receipt_id": "receipt_...",
  "ticket_id": "ticket_...",
  "attempt_id": "attempt_...",
  "target_digest": "sha256:...",
  "policy_version": "immutable version",
  "decision_id": "decision_...",
  "consumed_at": "RFC3339 timestamp"
}
```

`done` uses this receipt in a compare-and-set transition. Replays return the
same result; incompatible second completions fail without corrupting the first
completion.

## 9. Retention and graph pruning

Pruning is a storage and retrieval policy, not deletion of inconvenient truth.

The engine SHOULD:

- aggregate duplicate occurrences into case counters and time buckets;
- keep raw payloads out of the hot graph;
- use configurable hot, cold, and deletion retention periods;
- suppress routine handled conditions from canonical case creation;
- retain evidence hashes and migration provenance;
- preserve rare critical, recently regressed, and verified-resolution cases;
- and generate community summaries for observability, not enforcement.

Node degree, cluster size, age, or frequency MAY nominate records for review.
None of those metrics alone may delete a case or resolution.

LLM-generated consolidation is advisory. Deterministic rules and evidence
receipts remain authoritative. Raw episodes MUST remain recoverable until their
retention policy expires, even when a summary exists.

## 10. Reversible migration from v0.1 `ERR-*`

The migration MUST be resumable, idempotent, and reversible.

### Phase 0: snapshot and classify

1. Freeze a content-addressed snapshot and integrity manifest.
2. Record aggregate health statistics without publishing raw content.
3. Derive deterministic signatures from redacted fields.
4. Classify legacy records as occurrence evidence, canonical case,
   candidate resolution, policy/procedure, or `review_required`.

No legacy node is deleted or rewritten during this phase.

### Current v0.2 RC maintenance boundary

The staged `migrate_legacy_errors.py` utility is a conservative cleanup and
normalization step, not an assertion that the later shadow, canary, and cutover
phases below have run on a private graph. Its dry run:

1. archives a low-value legacy record when its occurrence count is missing or
   at most one and it has no recurrence, explicit promotion, solution, or
   resolution edge; diagnosis text alone does not retain a node;
2. preserves unknown-count records only when recurrence, explicit promotion,
   a solution, or a resolution edge supplies reusable evidence;
3. removes registry-to-error `requires` edges that could turn historical
   errors into unrelated task gates; and
4. bounds node/edge lists in the public manifest while reporting an exact
   `<field>_count` and `<field>_truncated` flag. The content-addressed rollback
   backup and JSONL archive are not truncated.

Apply still requires an explicit stopped-engine confirmation and a reviewed
dry-run result. Every removed record remains recoverable from the complete
backup/archive.

### Phase 1: shadow model

1. Create v0.2 shadow objects in a separate namespace or store.
2. Maintain an immutable `legacy_id -> new_object_ids` mapping.
3. Quarantine ambiguous merges for review.
4. Run route comparisons against recorded, redacted queries.

The v0.1 route remains authoritative.

### Phase 2: dual read and canary

1. Enable v0.2 retrieval behind a feature flag.
2. Compare route relevance, pack size, decisions, and latency.
3. Keep writes single-authoritative or use an audited outbox; do not perform
   uncoordinated dual writes.
4. Stop the canary automatically on a benchmark-gate regression.

### Phase 3: cutover

Cutover requires a reviewed migration manifest, passing benchmark gates,
successful rollback rehearsal, and explicit maintainer approval. Legacy nodes
remain read-only for a configurable retention window.

### Rollback

Rollback switches the route adapter to v0.1, disables v0.2 writes, and replays
only committed outbox events. It MUST NOT require reconstructing deleted legacy
nodes. Physical pruning is permitted only after the retention window and a
second integrity snapshot.

## 11. Proposed benchmark and release gates

The following are candidate v0.2 gates. They are deliberately separate rather
than combined into one marketing score. Thresholds MUST remain versioned policy
data and SHOULD be recalibrated on a representative, redacted corpus before
release.

| Gate | Draft threshold |
|---|---:|
| Canonical-case deduplication | At least 80% of known duplicate occurrences grouped correctly |
| Incorrect merge rate | At most 1% on reviewed benchmark pairs |
| Resolution route recall | Top-1 at least 0.80; top-3 at least 0.95 |
| Route payload bound | 100% of cases respect declared pack budget |
| Normal route breadth | Median at most 3 full error-knowledge objects |
| False block rate | At most 1% on authorized non-destructive tasks |
| Safety regression | Zero known destructive-policy bypasses in the gate suite |
| Verified resolution integrity | 100% include a valid receipt and evidence hash |
| Idempotent `done` replay | 100% produce no duplicate durable mutation |
| Compatible stale-ticket recovery | At least 95% refresh without duplicate live tickets |
| Incompatible target change | 100% reject or require a new ticket |
| Migration determinism | 100% identical mapping hashes across two clean runs |
| Rollback rehearsal | 100% restored v0.1 route authority with no lost legacy record |
| Public fixture privacy | Zero raw paths, secrets, credentials, or identifiable payloads |
| Route latency | p95 no more than 10% slower than the declared v0.1 baseline |

The benchmark SHOULD include:

- fingerprint equivalence and counterexamples;
- repeated transient attempts;
- resolved-case regression;
- version-incompatible resolutions;
- full, succinct, and no-history recovery variants;
- ticket expiry with unchanged and changed target digests;
- warn versus block counterexamples;
- route-budget pressure;
- migration interruption and resume;
- and rollback after partial canary traffic.

All reported results MUST identify dataset version, judge method, route mode,
policy version, hardware class, and known caveats. Internal-only datasets MUST
be labeled as such. 3CAN MUST NOT claim that it “beats” another product without
an apples-to-apples independent evaluation.

## 12. External patterns and license boundaries

The design review was refreshed on 2026-07-29. Repository licenses and upstream
terms must be rechecked before vendoring or pinning any dependency.

| Reference | License/status observed during review | Pattern adopted | Boundary or rejected pattern |
|---|---|---|---|
| [Mem0](https://github.com/mem0ai/mem0) | Apache-2.0 | Explicit memory CRUD, scoped retrieval, semantic update candidates | No LLM-only destructive merge or deletion |
| [Letta / MemGPT](https://github.com/letta-ai/letta-code) | Apache-2.0 | Working/file/archival memory tiers; versioned memory inspection | No unbounded always-visible memory; no uncontrolled shared last-write-wins |
| [LangGraph](https://github.com/langchain-ai/langgraph) and [LangMem](https://github.com/langchain-ai/langmem) | MIT | Checkpoint versus cross-thread store; semantic, episodic, and procedural separation; background consolidation | Embedding-only recall and an LLM memory manager are not authoritative truth |
| [Microsoft GraphRAG](https://github.com/microsoft/graphrag) | MIT | Community summaries, hybrid query modes, graph metrics as pruning candidates | No wholesale indexing pipeline requirement; no degree-only deletion |
| [Graphiti](https://github.com/getzep/graphiti) | Apache-2.0 | Episode provenance, temporal invalidation, hybrid lexical/vector/graph retrieval | No mandatory graph-database or LLM-extraction dependency in v0.2 core |
| [Hugging Face smolagents](https://github.com/huggingface/smolagents) | Apache-2.0 | Errors attached to execution steps, replay, succinct/full views, pruning of heavy historical payloads | Per-run agent memory is not a cross-session knowledge base |
| [Temporal Python SDK](https://github.com/temporalio/sdk-python) | MIT | Stable workflow intent, attempt separation, idempotency, retry classification | No Temporal service dependency; protocol semantics only |
| [Open Policy Agent](https://github.com/open-policy-agent/opa) | Apache-2.0 | Versioned decisions and auditable decision IDs | Rego/OPA remains an optional adapter, not a v0.2 core requirement |
| [OpenTelemetry semantic conventions](https://github.com/open-telemetry/semantic-conventions) | Apache-2.0 | Stable error type and severity fields; lower-cardinality telemetry | Telemetry records do not automatically become graph knowledge |
| [Sentry issue grouping](https://docs.sentry.io/product/issues/) | Current Sentry server code is source-available under FSL terms | Fingerprinted issue lifecycle, regression reopen, release-linked resolution | Product behavior is referenced conceptually; no FSL-covered code is copied |
| [Google SRE postmortem culture](https://sre.google/workbook/postmortem-culture/) | Documentation reference | Searchable concise postmortems, evidence links, owned action items | Conceptual attribution only; not a code dependency |
| [Letta Recovery-Bench](https://github.com/letta-ai/recovery-bench) | No clear repository license located during review | Replay a failed trajectory and compare full, summarized, and absent history | No code or fixtures may be copied unless licensing is clarified |

No third-party source code is added by this work. Conceptual references
do not relicense 3CAN-engine or any upstream work. Any future dependency MUST
receive a separate license, security, maintenance, and provenance review.

## 13. Public release requirements

Before any v0.2 source-available release claim:

- implementation and schema versions must match this document;
- migration and rollback must be exercised on sanitized fixtures;
- all benchmark gates must be reported with pass/fail evidence;
- unresolved gates must remain explicitly `PARTIAL`, `BLOCKED`, or
  `review_required`;
- release notes must say **source-available under PolyForm Noncommercial 1.0.0** and
  MUST NOT call the repository OSI open source;
- public artifacts must pass secret, path, credential, and personal-data scans;
- third-party notices and license boundaries must be reviewed;
- and an independent maintainer review must confirm that no design-only item is
  described as shipped.

Until those conditions are met, this document remains a release-candidate
contract; target-only items remain unshipped.
