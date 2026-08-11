# 3CAN Current Production Pointer

Observed at `2026-08-11T17:15:40Z`. This is the small current-state pointer;
older evidence remains immutable and is not rewritten to predict later events.

## Current production

- selector generation: `15`
- release SHA-256: `5cd41ca96bf945440d7d376292ca74ce1c67f021b4b0299b07da727b3a9790e6`
- runtime manifest SHA-256: `7bd56658a2de3006cf4c78f5c82af71a5a88e96a30c17e4736b3b1433dd938c3`
- runtime tree SHA-256: `0d5a7cf8a096fbb54f6ea4358f0ac5fc3dfae029be87e523b51c27cd3e970cf6`
- selector SHA-256: `33d25e9066cfe6b0d25d2d3ef4c6e471f32c3897e43a567080605c953bb5e7e0`
- deep readiness: `production_ready=true`, `verification_state=verified`
- graph observation: `5429` nodes, `6916` edges
- embedding observation: BGE-M3, `5429 x 1024`, synchronized and non-degraded
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
candidate-only. This pointer adds the repaired `b5abff7` candidate without
retroactively claiming any candidate was deployed.

## New candidate

- engine source commit: `b5abff7eaba5955a2928ea612d8f73e572fed78a`
- reviewed predecessor: `16f5f585df13259ec421d71a38c91aaa2c4487ce`
- candidate deployed: `false`
- candidate runtime release: `b0cd06015388acf8ea0abf5bda62eb9b31ad62d832a459d9107e37f54a8d5a97`
- isolated UAT: port `9701`, `development_ready=true`, `production_ready=false`, listener removed after test
- candidate `backend/app.py`: `3570ae548ecc1e54bdcdba38366f860f5b4e94ef2542b5c0831d5cc0dc0e35e4`
- candidate `backend/graph_engine.py`: `44cef0046c3feb48c77a0c9bff3ac15f3b7a02419348b5dec62672b3cae8fa6b`
- candidate `backend/models.py`: `76c3d2ad1c7083ebd0c0647c388646af3b92213189bbc5aef30fbce29c789d8d`

Candidate validation is summarized in
[`RELEASE_VALIDATION_20260809.md`](RELEASE_VALIDATION_20260809.md). Production
remains on generation 15 until a separately authorized cutover.
