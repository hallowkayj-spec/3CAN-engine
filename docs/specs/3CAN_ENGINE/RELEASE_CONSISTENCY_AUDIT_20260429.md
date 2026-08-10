# 3CAN Release Consistency Audit 2026-04-29

> Purpose: close the current 3CAN release-doc optimization round before returning
> to product mainline work.

## Scope

Audited the release-facing 3CAN docs for mismatches between:

- canonical docs under `docs/specs/3CAN_ENGINE/`
- nested docs under `_release_staging_3CAN-engine/`
- the real Desktop `neural-memory` tool inventory
- open-source/source-available release isolation rules

This is a documentation and release-governance pass only. It does not claim new
engine features were implemented.

## Decisions

1. **LLM wording must be conservative.**
   3CAN core route/writeback/audit should run without a generative LLM. Retrieval
   models such as BGE-M3 and rerankers are core route dependencies; generative
   LLM tools are optional enhancement or maintenance tools.

2. **Release docs must label status explicitly.**
   Any LLM or graph-maintenance tool must be described as `shipped`, `partial`,
   or `planned v0.1.x`. Public docs must not say "shipped 6, planned 2" unless
   the exact release package contains and smoke-tests those tools.

3. **Staging docs must not drift from canonical docs.**
   The nested release-staging copies of `README.md`, `LLM_POLICY.md`, and
   `OPEN_SOURCE_CHECKLIST.md` were mechanically synced from canonical docs.

4. **Runtime artifacts are important but private dogfood state is not shipped.**
   3CAN runtime sequencing, tickets, activity logs, and graph activation are
   core design concepts. The public package should ship the mechanism and
   sanitized fixtures, not the maintainer's live graph, embeddings, logs, agents, or secrets.

5. **Open-source users need three smoke modes.**
   Release testing should cover:
   - no LLM key: route/writeback still works with lower quality
   - DeepSeek-compatible BYOK: low-cost Chinese/mixed-language workflow
   - local/OpenAI-compatible model: privacy-oriented path

## Findings

| Area | Finding | Action |
|---|---|---|
| Canonical LLM policy | Now separates retrieval models, tokenizers, and generative LLM tools | Keep as source of truth |
| Staging nested docs | Had old `shipped 6, planned 2` and hard-dependency wording | Synced from canonical docs |
| Top-level staging README / USER_GUIDE | Still mention "7 points" in user-facing shorthand | Adjusted to point at the conservative map |
| Checklist | Had outdated "LLM hard integration 7 points" summary | Updated to release-facing wording |
| Release isolation | Already has a clear runtime-vs-state boundary | Keep as hard gate |
| Tool inventory | Desktop `neural-memory` has tools not obviously present in this app repo | Must verify before final public package |

## Remaining Gates

- Verify the release artifact contains no live `graph/nodes`, `activity_log.json`,
  `agents.json`, `embeddings.npz`, observer logs, `.env`, or `secrets.json`.
- Verify which Desktop `neural-memory/tools/*.py` LLM tools are actually copied
  into the release package.
- Add or document smoke tests for no-key, DeepSeek/BYOK, and local-model modes.
- Keep `LLM_POLICY.md` as the source of truth for BYOK, route-time LLM use, token
  diagnosis, and degradation behavior.

## Handoff

Future sessions continuing 3CAN release work should route:

```text
3CAN release consistency LLM_POLICY BYOK no-key smoke release isolation staging docs
```

Then read this file, `LLM_POLICY.md`, `OPEN_SOURCE_CHECKLIST.md`, and
`RELEASE_ISOLATION_POLICY.md` before changing release artifacts.
