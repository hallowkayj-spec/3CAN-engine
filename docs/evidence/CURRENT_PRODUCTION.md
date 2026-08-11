# 3CAN Current Production Pointer

Observed at `2026-08-11T13:39:00Z`. This is the small current-state pointer;
older evidence remains immutable and is not rewritten to predict later events.

## Current production

- selector generation: `15`
- release SHA-256: `5cd41ca96bf945440d7d376292ca74ce1c67f021b4b0299b07da727b3a9790e6`
- runtime manifest SHA-256: `7bd56658a2de3006cf4c78f5c82af71a5a88e96a30c17e4736b3b1433dd938c3`
- runtime tree SHA-256: `0d5a7cf8a096fbb54f6ea4358f0ac5fc3dfae029be87e523b51c27cd3e970cf6`
- selector SHA-256: `33d25e9066cfe6b0d25d2d3ef4c6e471f32c3897e43a567080605c953bb5e7e0`
- deep readiness: `production_ready=true`, `verification_state=verified`
- graph observation: `5412` nodes, `6916` edges
- embedding observation: BGE-M3, `5412 x 1024`, synchronized and non-degraded
- runtime identity: engine-path `b7a5127742727e6028dbb31c6288642c855d84702442f2784485550e10fc0711`, graph-path `c14d3d2e9dd7df2b3fddd734816a29599c00f8432e3168b0bbc5b387e36aa8bb`

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
candidate-only. This pointer adds the newer `f712563` candidate without
retroactively claiming either candidate was deployed.

## New candidate

- engine source commit: `f712563743ab39d7891a1b3ff99d70c0b5ad89af`
- parent reviewed source: `380804c040e8600288c68fe1878499f7cefe9609`
- candidate deployed: `false`
- candidate `backend/app.py`: `3570ae548ecc1e54bdcdba38366f860f5b4e94ef2542b5c0831d5cc0dc0e35e4`
- candidate `backend/graph_engine.py`: `343b2d81e0ac554ecb2af47ad873e73f6fbca95901676282c1a36e486269298a`
- candidate `backend/models.py`: `66cfb869fc3e1c74ce9556b012d6832881e89549d8e408dd922644b640b4509a`

Candidate validation is summarized in
[`RELEASE_VALIDATION_20260809.md`](RELEASE_VALIDATION_20260809.md). Production
remains on generation 15 until a separately authorized cutover.
