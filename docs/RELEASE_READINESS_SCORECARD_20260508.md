# Release Readiness Scorecard, 2026-05-08

This scorecard is for the released 3CAN engine package. It does not score the
private 3CAN dogfood graph or the operations-coach SaaS.

Scale:

- `0`: absent
- `1`: sketch
- `2`: partially implemented
- `3`: usable with manual care
- `4`: repeatable preview
- `5`: release-grade for the stated scope

| Dimension | Score | Evidence | Remaining Work |
| --- | ---: | --- | --- |
| Runtime graph isolation | 4 | Release graph is empty; init scripts bind `THREECAN_GRAPH_DIR`; scanner blocks graph artifacts | Add central port/profile registry |
| Startup and bootstrap | 4 | `init-project.ps1/sh` seed a 14-node base graph and support project-local ports | Add friendlier `doctor` wrapper output |
| Route functionality | 4 | Unit tests and HTTP smoke cover stats and route; route supports budgeted skeleton mode | Broaden real-world query corpus |
| Writeback workflow | 3 | Codex wrappers support prepare/done/compact and activity logs | Native enforcement depends on shell/runtime |
| Harness engineering | 3 | Standing orders, task ledger, approval gate, loop detection helpers are packaged | Need more end-to-end examples and docs |
| GitHub PR workflow guard | 4 | Project kit includes PR harness, PreToolUse hook, ERR seed node, and tests for approval/token/fallback behavior | Needs real-world reports across more private repositories and credential helpers |
| Token monitoring | 3 | Dashboard groups by date/session/model/source/task/agent and imports Codex status | Billing accuracy still depends on provider telemetry |
| Multi-project use | 3 | Tested with another local project and separate ports; docs include sidecar setup | Add automatic port collision detection |
| Release hygiene | 4 | Strict scan blocks secrets, local paths, DB/NPZ/log/node graph artifacts | Add CI on the real split-out repository |
| Documentation clarity | 3 | README, project kit, capability matrix, and scorecard now define scope | Some deep historical docs still need UTF-8 cleanup |
| Security posture | 3 | Localhost-first warning and strict no-secret rule | No auth layer; LAN exposure remains unsafe without reverse proxy |
| Public validation | 2 | Dogfood evidence and smoke tests exist | Need third-party install reports and issue feedback |
| Dependency ergonomics | 3 | Minimal and full dependency files split install burden | Need Windows/WSL matrix and model-cache docs |

## Summary Scores

- **Internal project-team preview**: 4.0 / 5
- **Cross-project local dogfood**: 3.7 / 5
- **Public source-available developer preview**: 3.3 / 5
- **Production/enterprise product**: 2.0 / 5

Interpretation: the package is ready to merge as release-staging hardening and
to test in small projects. It is not ready for claims of public benchmark
leadership or enterprise readiness.

## Must Not Claim

- "Open source" in the OSI sense.
- "Autonomous agent OS".
- "RPA automation platform".
- "Guaranteed token reduction".
- "Production-grade security".
- "Publicly benchmarked best-in-class memory".

## Safe Public Wording

Use:

> 3CAN is a source-available developer-preview project substrate for
> multi-agent coding workflows. It provides graph-backed route, writeback,
> task-memory, release hygiene, and token-observability primitives for local
> development teams.

## Next Release Gates

1. Add root CI once the package is split into its own repository.
2. Collect at least three clean fresh-machine install reports.
3. Add port/profile registry for multi-project local development.
4. Normalize remaining historical docs to UTF-8.
5. Add a small public route benchmark that does not depend on private node IDs.
6. Test the PR harness across Windows Git Credential Manager, WSL, and PAT env
   setups.
