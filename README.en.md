# 3CAN

**A source-available graph-backed project substrate prototype for coding-agent workflows, with explicit routing, writeback, and harness-level evaluation.**

Designed for OPC (One-Person Company) and small teams working on medium-sized projects running multiple AI coding agents (Claude Code, Codex CLI, Gemini CLI, or any HTTP-capable agent) over weeks or months.

## Status

**v0.2 release candidate — unreleased.** Built with heavy AI-agent assistance
during authorship. The candidate adds deterministic ErrorCase promotion,
bounded error retrieval, evidence-backed solution writeback, durable ticket
receipts, ordinary-route suppression of legacy and canonical `ERR-*` records,
reversible legacy-cluster migration, canonical readiness, stable route-response
fields, and graph-bound benchmarks. Agent wrappers no longer launch or
terminate runtime processes. Hard criticism, corrections, and PRs are warmly
welcome.

It is not deployed, tagged, or publicly released yet. See
[ERROR_KNOWLEDGE_LIFECYCLE.md](./docs/ERROR_KNOWLEDGE_LIFECYCLE.md) and the
[changelog](./CHANGELOG.md) for the implemented and remaining gates.

## Secure target and evidence configuration

Ticket snapshots are limited to `THREECAN_PROJECT_DIR` and any additional
absolute roots in `THREECAN_TARGET_ROOTS` (OS-path-separator delimited).
Automatic ErrorCase resolution also requires explicit
`THREECAN_EVIDENCE_ROOTS` and a runtime-injected
`THREECAN_EVIDENCE_HMAC_KEY` containing at least 32 random bytes; the example
environment file leaves the secret blank. Evidence outside those roots, or a
missing/short key, remains `review_required`.

The signed artifact schema is `3can.verification-attestation/v1`. Its signed
payload contains `kind`, `verifier`, `ticket_id`, `target_digest`,
`scope_digest`, `command`, integer `exit_code`, and `outcome`. Exclude
`signature`, serialize the remaining object as UTF-8 JSON with
`ensure_ascii=false`, sorted keys, and compact `,`/`:` separators, then sign it
with HMAC-SHA256. Store the result as
`hmac-sha256:<64 lowercase hex characters>`. The evidence receipt separately
supplies the SHA-256 digest of the complete attestation file. A claimed
`verified: true` or activity self-hash is not resolution authority.

## License

Source-available under **PolyForm Noncommercial License 1.0.0**. Not
OSI-approved open source.

- Noncommercial personal use, research, learning, modification, and sharing: allowed under the license terms
- Forks, experiments, PR contributions: welcome
- Company-internal, client, SaaS, paid-product, and other commercial use: separate written permission required

See `LICENSE`, `NOTICE`, and `LICENSING.md` for full terms.

## What 3CAN is (plain description)

A local HTTP service (`localhost:9700`) that agents query to:

- Find the right place in a project (shared graph of decisions, sessions, errors, interfaces, feedback)
- Avoid repeating historical mistakes (typed ERR nodes; hooks are optional)
- Restore context across sessions at low token cost (briefing endpoint + skeleton pack mode)
- Share project state across multiple agents (agent registry + hash-chained activity log)

Not a chat-memory tool, not an autonomous agent runtime, not an IDE replacement.

## One project steering file

Each project may keep one root `3CAN.md`. Its small flat front matter describes
Owner working defaults such as caution, autonomy, external-change confirmation,
context size, history, review, and meaningful writeback. The project kit binds
the file to `.agents/project.json`, caches it by file stat, and sends only a
compact digest-backed projection to route/briefing. The Markdown body, local
path, and unrelated project defaults are never injected.

Shared-authority projections are typed as `client_asserted`; only a file read
by the serving process is `server_local_file`. Owner defaults are preferences,
not authentication or objective evidence.

Current explicit Owner instructions take precedence only for governable task
preferences. Git/CI/Runtime/Provider/Evidence truth, project isolation,
credentials, destructive-production protections, and durable provenance remain
hard boundaries. `3CAN.md` is not a policy engine or a second state database.

## Quickstart

```bash
git clone https://github.com/hallowkayj-spec/3CAN-engine.git
cd 3CAN-engine
bash install.sh
export THREECAN_READINESS_MODE=development
python neural-memory/backend/app.py --port 9700 --host 127.0.0.1 &
curl http://127.0.0.1:9700/api/stats

# Fresh-graph verification (the generic seed graph starts small)
python scripts/verify_project.py \
  --base-url http://127.0.0.1:9700 \
  --min-nodes 10
```

The verifier performs liveness, deep stats/readiness, route, and token-health
checks. Development readiness is accepted for a fresh local graph; use
`--require-production-ready` when a pinned production profile is required.

The Claude Code behavioral gate is an optional integration example. If a
project enables it, it can deny selected mutations while the bound graph is
offline. Ordinary routing, the engine, and other agents do not depend on that
hook. The older `engine_liveness.py` harness uses mature-dogfood thresholds
(`1000` nodes / `500` edges); it is not the clean-clone verifier.

For first-time import from an existing project (with `CLAUDE.md`, `handoffs/`, `memory/*.md`):

```bash
python neural-memory/tools/project_bootstrapper.py --project-dir .
```

See [docs/specs/3CAN_ENGINE/recipes/](./docs/specs/3CAN_ENGINE/recipes/) for Claude Code and Codex CLI integration walkthroughs.

## Documentation

| Read first | Purpose |
|---|---|
| [docs/specs/3CAN_ENGINE/README.md](./docs/specs/3CAN_ENGINE/README.md) | Full project index and TL;DR |
| [PRD.md](./docs/specs/3CAN_ENGINE/PRD.md) | Product definition, target users, 6 north stars |
| [EVIDENCE.md](./docs/specs/3CAN_ENGINE/EVIDENCE.md) | Hard facts, 46-query MRR, LongMemEval ablation, dogfood observations |
| [LIMITATIONS.md](./docs/specs/3CAN_ENGINE/LIMITATIONS.md) | Known gaps, honest self-audit, things we don't do |
| [ATTRIBUTION.md](./docs/specs/3CAN_ENGINE/ATTRIBUTION.md) | Every external idea we borrowed, with thanks |

| Reference | Purpose |
|---|---|
| [ARCHITECTURE.md](./docs/specs/3CAN_ENGINE/ARCHITECTURE.md) | Engine structure, 4-step route pipeline, node/edge model |
| [API_USAGE.md](./docs/specs/3CAN_ENGINE/API_USAGE.md) | Endpoint-by-endpoint usage guide |
| [LLM_POLICY.md](./docs/specs/3CAN_ENGINE/LLM_POLICY.md) | Where LLM integrates, BYOK configuration, provider-neutral design |
| [BENCHMARK_POLICY.md](./docs/specs/3CAN_ENGINE/BENCHMARK_POLICY.md) | 3-layer evaluation: memory / substrate / harness (not one aggregate score) |
| [STABILITY_TIERS.md](./docs/specs/3CAN_ENGINE/STABILITY_TIERS.md) | What is stable, what is experimental, what is research-stage |
| [DEPLOYMENT.md](./docs/specs/3CAN_ENGINE/DEPLOYMENT.md) | 5 components + setup + known gotchas |
| [SECURITY.md](./SECURITY.md) | Security policy, threat model, how to report |
| [CONTRIBUTING.md](./CONTRIBUTING.md) | How to contribute, style, license note |
| [CHANGELOG.md](./CHANGELOG.md) | Release history |

## Evaluation status (3-layer, not aggregated)

Per `BENCHMARK_POLICY.md`, we do not publish a single "overall score". Each layer stands alone:

- **Memory / Retrieval**: the reproducible 46-query public seed-graph run scored
  MRR 0.9783, exact top-1 0.8261, and query-level Hit@3 1.0. The
  content-addressed receipt is
  `docs/evidence/SEED_GRAPH_BENCHMARK_20260809.json`. It proves only the
  synthetic seed fixture with the hashing fallback, not a private corpus or a
  pinned production embedding profile.
- **Project substrate**: the reproducible 10-case public seed-graph run scored
  top-1 accuracy 1.0 and mean top-3 recall 0.8167 in the same receipt.
- **Harness / governance**: an older denial-biased pilot note recorded 8/8,
  but it did not cover a valid-ticket allow path. It is not a current v0.2
  acceptance result.
- **Real UAT**: the recorder exists, but this package ships no current
  project-specific production acceptance receipt.

We do not compare 3CAN to specific competitor products by name. Different tools optimize for different things; side-by-side "beats X" claims require apples-to-apples cross-tool benchmarking we have not performed.

## About the author

3CAN is authored by the original maintainer, a vibecoding developer . Claude Code and other AI coding agents were used heavily throughout the project. There are bugs, non-idiomatic patterns, and architectural choices that a professional software engineer will spot.

Pointing these out is the single most useful contribution. See [CONTRIBUTING.md](./CONTRIBUTING.md).

## Chinese-friendly

Most node content in the dogfood graph is in Chinese. BGE-M3 is multilingual; queries and storage support Chinese natively. English-only projects also work. Mixed-language is the common case.

## Source-available release reality

3CAN ships as an engine + tools + docs, not as a pre-populated graph. A project
with existing handoffs and contracts can seed useful nodes sooner than a fresh
project. The old percentage-based recovery curves were subjective dogfood
estimates, not a measured release guarantee, so they are not used as v0.2
evidence.

## What 3CAN is NOT

- Not a chat memory or personal assistant memory
- Not a replacement for Claude Code / Codex CLI / Gemini CLI / any IDE
- Not a full autonomous agent runtime
- Not an enterprise platform (no RBAC, no multi-tenant isolation)
- Not release-ready; v0.2 is an unreleased source-available candidate
