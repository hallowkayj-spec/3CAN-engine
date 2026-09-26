# RuntimeHook global binding repair — 2026-09-26

## Scope and root cause

Owner requested globally usable RuntimeHook/Jev without requiring directory
cleanup first. A globally installed plugin still skipped supervision when a
task's saved native cwd differed from its verified development worktree:

- The controller treated the native cwd's Git root as the only permitted target.
- Both launchers skipped non-SessionStart events without state under that cwd.
- Historical 3CAN path disagreement also suppressed checkpoints and Jev even
  when the host/target relationship had been explicitly verified.

This is a plugin compatibility defect, not an OpenAI requirement that every
tool workdir equal the saved App cwd. Official Hook inputs provide session ID
and cwd; subagent events may carry the parent ID:
[native input contract](https://learn.chatgpt.com/docs/hooks#common-input-fields).

## Small repair, unchanged owners

Scope cache v2 retains the existing per-task JSON and separately records the
observed host anchor and verified Git target. `bind-scope` is explicit, uses
actual host metadata and current target Intent evidence, and does not change
semantic state or the host cwd. No automatic foreign activation adoption.
Git-host subdirectories remain supported; a non-Git host matches its exact
observed cwd. Target Git marker and activation must still match. Legacy v1
bindings keep their former same-worktree behavior until explicitly rebound.

Launchers defer to the single controller whenever scope caches exist, instead
of duplicating JSON/identity logic in another shell. Inactive events may then
incur interpreter startup; the no-state/no-cache fast path remains. Neither
callback nor relationship lookup performs network requests or calls Jev.

3CAN retains durable project meaning and handoff evidence. An absent record is
`UNVERIFIED`; a historical disagreement is reported as `CONTRADICTS`, not used
to replace a verified current task mapping or skip mandatory Jev. The Agent
updates durable meaning at normal handoff/closeout, never from each event.
No graph execution-state mirror, daemon, project migration, or second owner.

## Verification and objections

- Before the repair, targeted tests reproduced the mismatch and historical
  disagreement suppression. After the repair: 13 focused tests passed.
- One complete Windows four-module run: 147 passed, 2 skipped, 2 failed in
  566.32 seconds. Both failures exposed the same missing-target-Git-marker
  exception type during stale-scope/online-currentness checks. A narrow fix
  translates this filesystem failure to the existing typed mismatch; it
  neither accepts the missing target nor retries the provider.
- Failure/currentness-focused retest after the typed-error correction:
  **11 passed, 62 deselected**. Release-manifest tests: **17 passed**. Repository
  and plugin Ruff checks passed. The original non-green full run is preserved,
  not renamed an all-green run; current-commit CI remains a separate receipt.
- Installed Windows launcher simulation: all four events plus continued Stop
  passed with a non-Git host and a separate Git target. Peer bytes unchanged;
  no host-directory state created; temporary fixtures removed. A due review
  requests one continuation, not an infinite block.
- Local binding lookup, 1,000 samples: p50 **1.272 ms**, p95 **1.984 ms**;
  whole launcher callbacks were **1.192–1.646 s**. The Python/shell/Git and safe
  state checks are not part of the millisecond-only relationship lookup claim.
- Real cross-directory `checkpoint -> review` obtained Jev responses. The
  first packet had over-summarized code evidence; that record was retained.
  One new packet supplied actual code excerpts and the observed provider
  rejection. Jev still returned low claim confidence / unsupported judgments,
  despite checkpoint-quality scores of 1.97 and 1.92 out of 2. No thresholds,
  prompts or model were changed to obtain acceptance; no same-input reroll.
  This review remains `PARTIAL`, not an approved development milestone.

Jev opinion does not authenticate supplied evidence. Component tests do not
prove actual App event loading or business/UAT quality. A claimed successful
cross-directory launcher simulation must not be presented as a real App event.

## Deployment and rollback boundary

Candidate plugin: `0.1.10-rc.1+codex.20260926`. Local installation uses the native
plugin installer and preserves previous version files for already-running
tasks. Host config and the required-Jev policy must remain unchanged. Only
this task's verified mapping is rebound; other task states are untouched.
The installation receipt verified **13/13** source hashes, retained **six**
prior version directories, and verified unchanged host config and policy.
One operator helper was initially launched with Windows PowerShell 5 using a
UTF-8/no-BOM non-ASCII local path; it failed before the installer ran. Running
the same helper in the current UTF-8-capable shell completed installation.

Reload/restart the desktop App at a safe boundary, then confirm its actual
Hook target and activation. Do not require folder migration or repeat plugin
installation in every repository. Agents still establish the correct current
task mapping and task-specific checkpoints; global installation is not global
shared Intent or permission to upload any task's private contents.

No 9700/9711 lifecycle action is required or authorized by this repair. No
public main update, release publication, or PR merge is implied. For rollback,
retain v2 scope and v3 checkpoint evidence; explicitly verify and restore the
affected task's previous binding when using an older controller. Do not bulk
rewrite active-task caches or downgrade semantic state to silence warnings.
