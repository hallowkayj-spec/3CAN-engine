# 3CAN Current Production Pointer

Pointer refreshed from the read-only observation at `2026-08-11T17:15:40Z`.
This file records immutable selector/release identity only. Live readiness,
node/edge counts, embedding alignment, listener identity, claim/permit, and
runtime/graph path identity must be read from the runtime endpoint and bound
operator receipts; this Markdown file is not their live authority.

## Current production

- selector generation: `15`
- release SHA-256: `5cd41ca96bf945440d7d376292ca74ce1c67f021b4b0299b07da727b3a9790e6`
- runtime manifest SHA-256: `7bd56658a2de3006cf4c78f5c82af71a5a88e96a30c17e4736b3b1433dd938c3`
- runtime tree SHA-256: `0d5a7cf8a096fbb54f6ea4358f0ac5fc3dfae029be87e523b51c27cd3e970cf6`
- selector SHA-256: `33d25e9066cfe6b0d25d2d3ef4c6e471f32c3897e43a567080605c953bb5e7e0`

The immutable runtime manifest does not contain a Git commit. A normalized
source audit found its public semantic core content-equivalent to checkpoint
`cb532daee7c17954b2cdf93b536f65683db4c9a2`, but source-to-release Git binding
therefore remains `PARTIAL`, not cryptographically proven.

## Evidence lineage

The immutable `3can.runtime-ticket-fix-cutover/v1` receipt for run
`codex-3can-worktree-ticket-fix-20260811T031700Z` records the generation-15
cutover and substantiates the generation-15 / `cb532da` production boundary.
[`PRODUCTION_SOURCE_BOUNDARY_20260810.md`](PRODUCTION_SOURCE_BOUNDARY_20260810.md)
remains the historical boundary showing that later `380804c` changes were
candidate-only. PR #4 repaired the project-identity pair gate at `b5abff7`;
PR #5 adds its runtime engine at `8cef3c4`, repairs the public MCP boundary at
`a366f14`, and closes two-dimensional verifier coverage at `452c104` with
refreshed evidence at `8050d4c`.
Neither candidate was retroactively treated as deployed.

## New candidate

- engine source commit: `8cef3c4ba36c833341cd423b0626aff4f4d75e32`
- public client/verifier repair commit: `a366f14209aa474b0e3d4f1171fe2b42ecd66846`
- two-dimensional verifier commit: `452c1042c14974606533e0451984a6e42857fdec`
- benchmark receipt and clean-clone source commit: `8050d4cae4688701942e52be70845e7ff3dcdce5`
- reviewed predecessor: `b53cd959420612a2c11fb831300ef9afce0f2ff8`
- candidate deployed: `false`
- candidate runtime release: `03cf214a6158bb34bb37e5dbc3831269ee453a90f5ed491c3bb1ba912ae21dff`
- candidate runtime tree: `a4c48044c89f568939b4c863847d50492dcc68ffca69fac0e016769a4411521c`
- candidate manifest: `ab5fa950418db3af19ca0735343a0b125792f9796a2585fc2803070d628a6a54`
- isolated UAT: port `9701`, `development_ready=true`, `production_ready=false`, Owner Intent loaded from the server-local project file, partial project identity returned `422`, listener removed after test
- clean-clone acceptance: fresh minimal venv at `8050d4c`, non-equal `public-rc` / `public-scope` identity plus independent project-only and namespace-only mismatch fixtures, write isolation, writeback, ErrorKnowledge, project kit, and clean stop all passed
- candidate `backend/app.py`: `b9d6a7094805de7cd6981d2d8279020f7b23e53ca88709f0dabec581a91496cf`
- candidate `backend/graph_engine.py`: `c07caec569d410dd30686d380db892d4f57c2c54506ba802e18694aa729a628c`
- candidate `backend/models.py`: `a39efbf537a0b57b61adc45a67abcd3d17f12989159458d40ffa9d050b84f6b2`
- candidate `backend/owner_intent.py`: `9a4fe56a1416b34f3819e896df88b05fe70f4054ce9276a6e152cd87696733f5`

The MCP and generic verifier repair is outside the 24-file immutable runtime
payload. The content-addressed runtime therefore remains bound to `8cef3c4`;
the complete public package, focused tests, full suite, seed benchmark, and
fresh clean clone bind the later public repairs. This is a scope distinction,
not a claim that `8050d4c` is deployed.

Candidate validation is summarized in
[`RELEASE_VALIDATION_20260809.md`](RELEASE_VALIDATION_20260809.md). Production
remains on generation 15 until a separately authorized cutover.
