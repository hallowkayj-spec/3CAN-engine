# 3CAN Engine

**3CAN is a source-available, graph-backed project substrate for coding-agent
workflows.** It gives Codex, Claude Code, OpenCode, Gemini CLI, and other
HTTP-capable agents a shared project reality: what to read, what to avoid, what
changed, what failed before, and where durable work should be written back.

Status: **v0.2 release candidate (unreleased)**. This package is suitable for
controlled project-team use and cross-project testing. It is not a polished
enterprise memory platform and should not be described as OSI open source.

License: **PolyForm Noncommercial 1.0.0**. This is source-available, not
OSI-approved open source. Commercial use requires separate written permission.
See `LICENSE`, `NOTICE`, `LICENSING.md`, and `SECURITY.md`.

## What It Does

3CAN runs as a local HTTP sidecar, usually on `127.0.0.1`.

Core capabilities:

- **Project graph memory**: decisions, sessions, errors, interfaces, processes,
  feedback, and skills are stored as typed graph nodes and edges.
- **Context routing**: agents call `/api/route` to get a compact task-specific
  briefing instead of injecting all project history into every session.
- **Writeback discipline**: durable results can be recorded as activity,
  session notes, decision nodes, error lessons, or handoff nodes.
- **Optional governance adapters**: project kits include route/prepare/done,
  task-ledger, hook, and compact-handoff examples for projects that need them.
  They are not required for ordinary read-only routing.
- **Optional GitHub PR fallback**: the Codex project kit includes an
  approval-gated local REST adapter for environments where `gh` is absent or
  a connector cannot create a PR.
- **Token operations**: local token usage telemetry can be grouped by date,
  session, model, source, task, and agent; cache and fresh-input pressure are
  reported separately.
- **Multi-project isolation**: every project can run its own graph directory,
  port, token ledger, and bootstrap profile.
- **Project kits**: `examples/codex-cli-project-kit/` provides optional Codex
  wrappers, hooks, and a minimal template `AGENTS.md` for new projects.
- **Owner intent**: one project-root `3CAN.md` provides small, human-editable
  working defaults. Its compact project-bound projection augments route and
  briefing; it never replaces Git, CI, Runtime, Evidence, or hard gates.
- **Release hygiene**: `scripts/prerelease_scan.py --strict` blocks secrets,
  maintainer-local paths, and runtime graph artifacts before publishing.

3CAN is **not** an autonomous agent runtime, chat application, SaaS backend,
RPA crawler, IDE, or general-purpose long-term user-memory product. In the
operations-coach system, it acts as the substrate for task timing, evidence,
memory routing, agent coordination, and writeback. RPA adapters and business
logic live above it.

## Unreleased v0.2 Candidate

The v0.2 candidate turns error history into bounded, reusable knowledge:

- a first failure remains an occurrence; only a repeated deterministic
  fingerprint promotes an ErrorCase;
- ordinary routes exclude legacy and canonical `ERR-*` error knowledge, while
  explicit error routes return at most three applicable records;
- verified `done` writeback creates a solution and evidence chain and marks the
  case resolved;
- route-ticket authorization uses a process-safe SQLite/WAL ledger, scoped
  digests, append-only events, and a replay-safe completion journal;
- a reversible maintenance tool archives legacy one-off error nodes and removes
  their global mandatory-route edges.

See [`docs/ERROR_KNOWLEDGE_LIFECYCLE.md`](./docs/ERROR_KNOWLEDGE_LIFECYCLE.md)
and the `[Unreleased]` section in [`CHANGELOG.md`](./CHANGELOG.md).

## Secure Target and Evidence Configuration

Ticket target snapshots are restricted to `THREECAN_PROJECT_DIR` plus optional
absolute roots in `THREECAN_TARGET_ROOTS`. Multiple roots use the operating
system path separator (`;` on Windows, `:` on POSIX). Do not add broad roots
such as a whole user profile.

Automatic ErrorCase resolution is fail-closed:

- `THREECAN_EVIDENCE_ROOTS` must list the absolute directories from which
  evidence artifacts may be read. It has no implicit default.
- `THREECAN_EVIDENCE_HMAC_KEY` must be injected at runtime from a secret store
  and contain at least 32 random bytes. A 64-character hexadecimal encoding is
  one safe representation. The runtime rejects configured values shorter than
  32 characters. Never commit the real key; `.env.example` intentionally leaves
  it blank.
- `THREECAN_EVIDENCE_MAX_BYTES` defaults to `4194304` (4 MiB) per artifact.

A verification artifact is JSON with
`schema_version: "3can.verification-attestation/v1"` and these signed fields:
`kind`, `verifier`, `ticket_id`, `target_digest`, `scope_digest`, `command`,
integer `exit_code`, and `outcome`. A passing attestation requires
`exit_code: 0` and `outcome` equal to `pass`, `passed`, or `success`.
Canonicalize every field except `signature` as UTF-8 JSON with keys sorted,
Unicode preserved, and separators `,` and `:` with no extra whitespace. Sign
those bytes with HMAC-SHA256 and write
`signature: "hmac-sha256:<64 lowercase hex characters>"`.

The evidence receipt must also contain the SHA-256 digest of the complete
attestation file, and its `kind`/`verifier` must match the signed values. A
client-provided `verified: true`, an activity self-hash, a file outside the
allowed roots, a missing/short key, a digest mismatch, or an invalid signature
cannot resolve an ErrorCase. The completion remains `review_required`.

## Current Update, 2026-08-09

- `/api/stats` now derives `healthy` from the canonical readiness contract.
  A verified deep result can be reused while the graph/embedding fingerprint is
  unchanged; the response says whether evidence is `verified`,
  `cached_verified`, `stale_verified`, or `deep_required`.
- Agent wrappers only observe runtime readiness; they never spawn, terminate,
  or request recovery of the shared production runtime. Offline route, ticket,
  and writeback become typed `UNAVAILABLE`, while local Git, coding, builds,
  and offline tests continue. Lifecycle remains an explicit machine
  operator/service-manager action.
- Route responses publish `3can.route-response/v1`, retain mandatory and
  temporal metadata during budget compaction, and expose both
  `response_tokens` and the compatibility alias `post_budget_tokens`.
- Public route/substrate benchmarks declare their required seed-graph node IDs
  and fail as `INVALID_GRAPH_BINDING` before scoring the wrong graph.
- A reproducible public seed-graph receipt records route MRR `0.9783`, exact
  top-1 `0.8261`, query-level Hit@3 `1.0`, substrate top-1 `1.0`, and mean
  top-3 recall `0.8167`. It is candidate evidence for the synthetic hashing
  profile, not a production-profile claim; see
  `docs/evidence/SEED_GRAPH_BENCHMARK_20260809.json`.
- Release CI runs Ruff across all shipped Python paths, the full test suite, strict
  isolation scanning, and a typed development-readiness smoke check.

## Previous Update, 2026-05-08

This release staging package now includes the lessons from real multi-project
dogfood testing:

- The release graph is intentionally empty. Runtime graph files are generated
  per project and are not committed.
- `seed_nodes.py` creates a generic 16-node base graph and is idempotent.
- `init-project.ps1` and `init-project.sh` initialize isolated project graphs
  and bind `THREECAN_*` environment variables.
- Token monitoring imports Codex runtime status when available and exposes
  importer state in `/api/token-usage/health` and the dashboard.
- The GitHub PR harness is packaged as an ERR-backed hook so repeated `gh` or
  connector PR failures route to the local REST fallback path.
- Release scanning blocks graph databases, embeddings, activity logs, node
  JSON, secrets, and maintainer-local path leaks.
- CI smoke now uses a temporary graph and checks both `/api/stats` and
  `/api/route`.

See:

- `docs/CAPABILITY_MATRIX_20260508.md`
- `docs/RELEASE_READINESS_SCORECARD_20260508.md`
- `docs/GITHUB_PR_HARNESS.md`
- `docs/PROJECT_KIT.md`

## Quick Start

### Minimal Install

```bash
bash install.sh
```

By default this installs the minimal dependency set and seeds a local project
graph. Use the full profile when you want heavier semantic dependencies:

```bash
THREECAN_INSTALL_PROFILE=full bash install.sh
```

### Start Backend

```bash
export THREECAN_READINESS_MODE=development
python neural-memory/backend/app.py --port 9711 --host 127.0.0.1
```

Open:

- Gateway: `http://127.0.0.1:9711`
- Token dashboard: `http://127.0.0.1:9711/static/token_usage.html`

### Verify

```bash
python scripts/verify_project.py --base-url http://127.0.0.1:9711 --min-nodes 10
```

Expected result: liveness passes, the deep stats snapshot is internally
consistent, the node count is above threshold, route returns at least one node,
and readiness is typed as either `VERIFIED_PRODUCTION` or
`DEVELOPMENT_ONLY`. Add `--require-production-ready` for a pinned production
profile.

## Project Sidecar Setup

For Windows PowerShell from a target project:

```powershell
..\3CAN-engine\scripts\init-project.ps1 -ProjectDir . -Port 9711 -StartServer
python ..\3CAN-engine\scripts\verify_project.py --base-url http://127.0.0.1:9711 --min-nodes 10
```

For Linux/macOS/Git Bash:

```bash
../3CAN-engine/scripts/init-project.sh --project . --port 9711 --start-server
python ../3CAN-engine/scripts/verify_project.py --base-url http://127.0.0.1:9711 --min-nodes 10
```

The setup binds:

- `THREECAN_ENGINE_ROOT`
- `THREECAN_GRAPH_DIR`
- `THREECAN_PROJECT_DIR`
- `THREECAN_BASE_URL`
- `THREECAN_MIN_NODES`

New projects should usually start with `THREECAN_MIN_NODES=10`; a full dogfood
graph may contain thousands of nodes, but a fresh graph should not be judged by
that threshold.

## Agent Integration

Codex CLI:

1. Copy `examples/codex-cli-project-kit/` into your project.
2. Merge `.gitignore.template` into the target project's `.gitignore`; it
   excludes local tickets, error outboxes, route state, and test receipts.
3. Rename `AGENTS.template.md` to `AGENTS.md`.
4. Copy this repository's root `3CAN.md` to the target project root and edit
   only its supported flat front matter. It is the single Owner steering file;
   do not create a policy directory or separate preferences file.
5. Before any mutation, rename
   `.agents/project.template.json` to `.agents/project.json`, fill the project
   ID/namespace/name, and set `git_repository` to the normalized origin
   (`github.com/owner/repository`). Do not put ports or runtime paths in this
   durable project capsule. This is especially important for a shared 3CAN
   authority or cross-worktree mutation.
6. Run `python scripts/3can_codex.py doctor` and require
   `project_identity.status=pass` before requesting a mutation ticket.
7. Adjust the runtime port and graph path through environment configuration.
8. If you use the wrapper workflow, bootstrap the session with
   `scripts/codex-3can.cmd bootstrap ...`.

The capsule is optional only for read-only routing. Mutation commands require it
so the authority can bind the write to a repository and physical Git worktree,
including when the runtime is a project-owned sidecar.

`3CAN.md` is bound to the same `project_id` and `project_namespace`. The helper
parses it once and reuses a stat-keyed in-process cache until the file changes.
Normal requests carry only the seven effective defaults plus a digest—never the
Markdown body or an absolute path. Explicit current Owner instructions may
override a governable default for that task; evidence truth and hard safety do
not become configurable. A shared-authority request is reported as
`client_asserted`; only a file read by that server is `server_local_file`.
Neither label is authentication or objective evidence.

Route tickets, prepare/done wrappers, and hooks are optional policy adapters.
Use them for a specific guarded-write or evidence requirement, not as a
universal concurrency mechanism. The wrapper never directly launches or stops
the engine.

Claude Code:

- See `examples/claude-code-hooks/`.
- Hooks are examples, not mandatory runtime dependencies.
- Treat any tool that can delete files, alter schemas, publish content, spend
  API credits, or write real store data as an approval-gated action.

Any HTTP-capable agent:

- `GET /api/stats`
- `POST /api/route`
- `GET /api/nodes/{node_id}`
- `POST /api/activity/log`
- `GET /api/token-usage/overview`

## Release Boundary

The release package must not contain:

- `.env` files or raw API keys
- cookies, passwords, recovery codes, or secret JSON files
- maintainer-local absolute paths
- dogfood graph nodes, activity logs, token databases, embeddings, or agent
  runtime state
- project-specific RPA data or private business logs

Run before publishing or copying to another project:

```bash
python scripts/prerelease_scan.py --strict
```

## Honest Limitations

- No auth layer. Keep it on `127.0.0.1` unless you add your own protection.
- Not a replacement for careful engineering review.
- Semantic route quality depends on graph quality, seed quality, and query
  expansion quality.
- Token dashboards combine actual runtime telemetry and local estimates; the
  UI labels sources explicitly.
- External third-party validation is still limited. Dogfood evidence is useful
  but not a public benchmark.

## Documentation Map

- `docs/PROJECT_KIT.md`: project-sidecar setup and Codex kit usage
- `docs/GITHUB_PR_HARNESS.md`: local GitHub PR fallback guard and hook behavior
- `docs/CAPABILITY_MATRIX_20260508.md`: capability status and boundaries
- `docs/RELEASE_READINESS_SCORECARD_20260508.md`: release readiness scoring
- `docs/USER_GUIDE.md`: broader user guide
- `docs/specs/3CAN_ENGINE/ARCHITECTURE.md`: architecture notes
- `docs/specs/3CAN_ENGINE/SECURITY.md`: security posture
- `docs/specs/3CAN_ENGINE/LIMITATIONS.md`: known limits
- `docs/specs/3CAN_ENGINE/ATTRIBUTION.md`: attribution and inspirations

## Maintainer Note

3CAN was built through heavy dogfood usage with AI coding agents. That is a
strength for practical workflow discovery and a risk for consistency. The
project welcomes hard review, failing tests, reproducible bug reports, and
plain corrections to docs, contracts, and release hygiene.
