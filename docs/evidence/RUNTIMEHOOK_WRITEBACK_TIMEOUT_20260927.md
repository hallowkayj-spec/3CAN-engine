# RuntimeHook writeback deep-readiness timeout

Status: source repair verified locally; installed/customer acceptance pending. Owner:
3CAN RuntimeHook. Workorder: `3CAN-RUNTIMEHOOK-WRITEBACK-TIMEOUT-20260927`.
Stable fault ID: `RUNTIMEHOOK-WRITEBACK-DEEP-READINESS-TIMEOUT`.

## Diagnosis and scope

The released adapter caps every request at two seconds, including
`GET /api/stats?deep=true`. The server explicitly forces a fresh readiness
snapshot for `deep=true`. A captured request through the pinned canonical
client timed out after 2102.549 ms. The socket audit recorded a direct loopback
connection, while bounded healthy controls took approximately 1.65–2.60 seconds.
Thus the request cap overlaps the observed healthy latency range. The adapter
also collapses transport failures to `RUNTIME_UNAVAILABLE`, discarding the
request phase and cause.

The received proposal and source/installed adapter hashes were verified before
editing. Twenty selected handoff/evidence files (68,402 bytes) were copied
without deleting or changing their sources. The private intake manifest and
original receipts are under `output/runtimehook-writeback-timeout-20260927/`.
The original candidate is retained as evidence, not installed or blindly applied.

One successful historical implementation delta was independently read back in
full. Three failed historical markers were absent during the diagnostic read.
Those older failures have no per-request trace: the new reproduction establishes
the same preflight failure mechanism, not the precise cause of every old event.

## Bounded decision and counterexamples

Reuse the existing adapter and canonical client. Give only the forced deep
readiness request a five-second cap; keep other requests at two seconds and
the existing ten-second remaining-budget calculation. Add fixed, sanitized
failure phase/cause/timing fields to the existing receipt. Preserve stable
error codes, identity, readiness, node/project scope, CAS, mutation count,
exact readback, previous notes, and zero automatic retries.

Rejected: shallow readiness, a larger overall budget, global network changes,
new configuration, a background queue, a fallback writer, and runtime restarts.
Five seconds is a bounded margin over the observed control, not a universal
latency guarantee. The canonical urllib socket timeout and OS scheduling are
not a hard process wall-clock deadline; this patch must not claim otherwise.

Before changing production code, add focused regressions for a healthy 2.6-second
deep check, failure diagnostics at each request phase, remaining-budget
exhaustion, and sanitized unknown transport errors. Retain existing gate,
idempotence, real-graph persistence, and offline native-callback tests.

## Executed regression and review

- Eighteen new regression cases failed against the unchanged adapter: the slow
  deep check was refused and failure diagnostics were absent.
- After the focused repair, all 36 writeback tests passed in 44.72 seconds,
  including the retained gate, idempotence, real-graph persistence, controller
  integration, and offline native-callback tests.
- The received candidate was extended only to report a pre-request exhausted
  budget as `phase=<pending phase>, cause=deadline`, with zero timeout/elapsed
  because that request did not run. The overall budget is unchanged.
- The diagnosis review actually called Jev once (request
  `9bbdc0cebaa171bf68c4ea4b1eba102a048b79a104f8d98e32b5a0f6c2c7f710`).
  Both narrow claims were SUPPORTED; criterion scores 1.46/1.33 and low
  confidence retained PARTIAL under the current policy. Implementation and
  counterexample proof were still pending then. The red/green regression now
  supplies that missing bounded proof; no repeated question or lowered threshold
  was used to seek approval. The later delivery review must use the actual new
  results and preserve remaining installation/customer gaps.
- That installed-controller diagnosis review's automatic writeback failed with
  `RUNTIME_UNAVAILABLE` after 2575.719 ms. Its receipt and packet are retained;
  its missing phase does not prove exactly which request failed. This observation
  is consistent with the diagnostic gap but is not a reconstructed network trace.
- The candidate source controller then wrote one error-progress delta under its
  own verified task binding. An independent GET matched the marker and complete
  packet. One explicit replay of this task's failed diagnosis delta also wrote
  and independently read back the original packet; it reused the same Jev
  opinion without another provider request. The failed original remains saved.
  No customer event was replayed. These are source-controller trials, not an
  installed-plugin or customer-native UAT claim.
- The local plugin/distribution test run reported 69 passed, 1 skipped, 2 failed.
  Both release-policy failures scanned preserved, ignored historical handoff
  files under `output/continuation-20260926/` (old numpy-load consumers and old
  license discussions). Those files are outside the Git release. Keep the
  failures and archives; validate a clean Git-built candidate instead of
  changing the scanner, deleting evidence, or claiming this run passed.

## Delivery and rollback boundary

Source and project-kit mirror must remain identical. Run relevant regression,
lint, and distribution checks, then report the exact reviewed commit and PR.
No installation, policy change, App restart, runtime lifecycle action, or
customer activation/lease adoption is authorized by this source-repair module.
The source plugin version is `0.1.13-rc.1+codex.20260927`; the installed version
remains `0.1.12-rc.1+codex.20260926`. Its adapter file is unchanged.
The prior Jev-role and Stop-feedback work remains a separate pending module.

An authorized installation must use the supported versioned plugin distribution
path, preserving the old compatible version and task receipts. Customer
acceptance requires that task's own native `review --summary`/error receipt and
an independent exact readback. Historical failed events require explicit,
idempotent replay under their owning task after diagnosis; do not adopt them.
Rollback selects the prior compatible plugin version and preserves all evidence
and remote history. No writeback-policy disable or shared-runtime restart is
part of rollback. Formal ErrorCase resolution remains on canonical verified
`done`; source tests alone do not resolve the live fault.
