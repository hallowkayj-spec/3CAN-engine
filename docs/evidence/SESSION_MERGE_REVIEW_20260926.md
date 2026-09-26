# Session cumulative merge review — 2026-09-26

Follow-up: the Owner did not authorize a merge exception for low-confidence Jev.
The initial integration candidate `fbf2567` completed all five CI jobs in
[run 36233075830](https://github.com/hallowkayj-spec/3CAN-engine/actions/runs/36233075830)
(772 passed, 7 skipped). Subsequent scoped Jev adapter changes, fixed-case real
provider comparisons and their unresolved false rejections are documented in
[the decision-scope review](JEV_DECISION_SCOPE_REVIEW_20260926.md).
Those later changes require their own CI. Cumulative merge and global rollout
remain unaccepted; the initial code-review result below is not model-efficacy
acceptance or approval of a changed candidate.

## Scope and authority

The Owner requested review and repair of this Session's accumulated development,
then commit, PR and merge of the reviewed result. This does not authorize changes
to other tasks, worktrees, credentials, shared runtime ownership or graph storage.

The reviewed source baseline is `3ea5e355eb2619875b9d6d346f32dd68e85bd277` on
PR #17, against `main` at `78e748bfa095063eda4a170fe856678ca5c3ef95`.
The baseline contains the exact heads of PRs #8, #12, #13, #14, #15 and #16.
PR #11's download/share documentation was not included; this change merges its
head `e7f21050a05471789505bedeb183046370751a0c` and corrects release wording.
Merge-commit ancestry, rather than a squash or an assertion about similar code,
preserves those development histories. The final merge is conditional on the
current candidate's CI, not the older green baseline.

## Findings fixed

1. **Incompatible rollback guidance.** Jev documentation still suggested a
   no-migration return to RuntimeHook 0.1.7. That controller accepts v1/v2 state,
   whereas checkpoint-enabled tasks use v3. Both distributed reference copies
   now require a state-compatible backed-up controller, preserving failures and
   receipts. Optional `assess` and Owner-required review are distinguished;
   `off` is not a bypass for the latter.
2. **Historical status presented without a date boundary.** The adaptive harness
   design document began with `GLOBAL_ROLLOUT_BLOCKED` and a one-state-file
   description. It now identifies its historical baseline and points to the
   current RuntimeHook guide. Historical dogfood is not relabeled as current UAT.
3. **Missing release-sharing branch and ambiguous download wording.** PR #11's
   links are incorporated. The existing `v0.2.0-rc.1` archive is explicitly an
   August 20 snapshot, not a distribution of every later Hook/Jev change.
   Current source installation and the noncommercial license remain explicit.

No production code or policy change was justified by these findings. No new
state machine, queue, compatibility fallback or runtime instance was added.

## Risk-based code review coverage

| Area | Source and counterexamples inspected |
| --- | --- |
| Native hooks and scope | Four event dispatchers, launcher argument handling, native cwd versus physical worktree, activation/session binding, advisory mismatch and non-adoption of foreign state. |
| Semantic supervision | Git review currentness, episode boundaries, temporary-task completion/cancellation, and separation from independent Stop gates. |
| Checkpoints and Jev | Required-point coverage, cached single-attempt decisions, schema/model/usage validation, low-confidence and low-score rejection, and failure states that cannot become PASS. |
| Automatic writeback | Verified project/Agent/Workorder binding, existing-node scope, canonical client, CAS append, idempotence, exact readback, bounded failures, and error progress without fabricated ErrorCase resolution. |
| Evidence kernel | Candidate/receipt currentness and Task Oracle completion checks remain in the existing substrate, not a second RuntimeHook kernel. Missing optional contracts are not invented. |
| Graph and errors | Ticket/projection recovery, sanitized issue observation, graph locking, reviewed ErrorFamily aliases and migration/rollback boundaries. |
| Research and UI | Research evidence/decision linkage and advisory limitations; graph loading, stale-state/readiness presentation, filters and escaping. |
| Distribution | Release manifest, source mirrors, Windows/POSIX launchers, privacy/isolation scans and CI paths. |

This was a focused source/dataflow review plus regression verification, not an
independent exhaustive proof of every line or every business workflow. No new
P0/P1 was found in the inspected paths. Model opinion is supplementary evidence,
not proof that no defect exists.

## Executed validation

- Windows focused regression on RuntimeHook writeback, checkpoints, Jev and
  plugin wiring: **145 passed, 1 skipped, 284.54 seconds**. The skipped case is
  the POSIX launcher contract, which is not executable on this Windows host.
- The baseline's GitHub run
  [36223418137](https://github.com/hallowkayj-spec/3CAN-engine/actions/runs/36223418137)
  had all five jobs successful; its full Linux suite reported **772 passed,
  7 skipped**. This is baseline evidence, not the final candidate's result.
- Skill validation initially failed because the standalone validator read
  Chinese UTF-8 as Windows GBK. Running that same validator with `-X utf8`
  succeeded. No shared validator or global encoding setting was changed.
  The observed failure and verified local remedy were recorded with one fault
  identity; that is not a formal ErrorCase closure.
- `git diff --check` passed for the documentation changes.
- An initial local release-test run reported **54 passed, 2 failed**: the new
  report/spec were not staged yet, and a whole-working-directory fixture copied
  ignored private operator outputs into its purported release. Those results
  are retained, not relabeled green. Stage the required public files and verify
  the actual Git-built archive and clean-clone CI; do not weaken the scanner or
  publish the working directory to conceal this distinction.
- Final candidate acceptance requires the PR's five current CI jobs, including
  the full suite, extracted-release scan and clean-clone Windows/Linux checks.
  Results and exact merge SHA belong in the
  [PR #17 checks and merge receipt](https://github.com/hallowkayj-spec/3CAN-engine/pull/17),
  rather than guessing the not-yet-created containing commit's SHA here.

## Installed and live boundaries

The restarted native Session received the current RuntimeHook context. The
installed 0.1.11 controller and four supporting script payloads matched this
source baseline byte-for-byte. This is evidence of the current installation and
this Session, not proof that every existing peer has rebound its task correctly.
Each participating task must retain its own verified scope and connect binding.

The canonical production 9700 read-only checks returned live/healthy,
`production_ready=true`, `development_ready=false`, and verified deep cache
evidence. Deployed app, graph engine, ticket ledger, ErrorFamily and frontend
source matched the candidate after newline normalization. This documentation
integration introduces no runtime-code deployment and needs no 9700/9711 restart.

Real connect and error-progress writebacks were remotely read back during this
review. This is not a claim that all Agents, every platform or all business UAT
have passed. Shared services, other worktrees and other tasks were not changed.

## Remaining acceptance limits

- Real business UAT, visual quality, agent attention and Jev's empirical accuracy
  remain task-specific. Tests and a model score cannot certify them globally.
  Earlier low-confidence Jev observations remain in their original evidence;
  this integration review does not retroactively turn those tasks into PASS.
- An existing task may still require explicit verified scope/activation/connect
  setup. Global plugin availability does not grant authority to adopt peer state.
- Existing release assets are unchanged; merging current source does not publish
  a new binary/archive release or change the license.
- Online outages, insufficient evidence and failed writeback remain typed
  `UNAVAILABLE`/`PARTIAL`; independent safe work can continue without false PASS.

The accompanying two-point checkpoint spec covers this review and its delivery.
Private machine paths, keys, raw provider payloads, graph contents and unrelated
task history are deliberately excluded from this public report.
