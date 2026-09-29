# RuntimeHook writeback deep-readiness timeout

Status: the September 27 five-second mitigation recurred on September 29; see
the corrective follow-up below. Owner:
3CAN RuntimeHook. Workorder: `3CAN-RUNTIMEHOOK-WRITEBACK-TIMEOUT-20260927`.
Stable fault ID: `RUNTIMEHOOK-WRITEBACK-DEEP-READINESS-TIMEOUT`.

## September 29: reuse canonical verified readiness

Two reported receipts identify the failing request as readiness, with a five-second
socket limit and elapsed times of 5862.833 and 5257.411 ms. This governance task
then reproduced the same failure at 5151.431 ms; its marker was independently
absent from the knowledge node. The failure occurred before any write request.

A sequential read-only measurement found ordinary stats at 59.669 ms, forced
deep stats at 2295.286 ms, and the canonical client's `_probe_stats` at 60.139 ms.
All completed measurements passed the canonical identity/readiness validator.
The canonical client's `prepare` operation instead calls route-ticket and consume
endpoints, so its success does not prove that the same readiness probe completed.

The adapter unnecessarily forced a full deep check for every write. It now uses
the canonical probe policy: validate `/api/stats` first (four seconds), and perform
one deep refresh only for `production_not_ready` (30 seconds). The existing
server cache validates graph/profile/embedding fingerprints before reusing deep
evidence. Wrong identity, a missing readiness contract, transport failure, or a
failed refresh never permits writing or triggers a retry. No client-side readiness
cache, new service, retry queue, or backend deployment is introduced.

The overall budget is 40 seconds, allowing the existing canonical 30-second deep
probe plus normal requests; each other request remains capped at two seconds and
all requests use the remaining budget. Deadline exhaustion and unknown write or
readback outcomes stay typed failures. This is a bounded request budget, not an
operating-system wall-clock guarantee.

Eight regression cases failed before the repair. The repaired writeback and
readiness suites passed 65 tests, including a required 5.9-second deep refresh,
cached verification, invalid identities, deep failure without writes/retries,
CAS/effect/readback rejection, and exhausted readback budget. A real candidate
error write used ordinary stats in 285.465 ms and completed exact readback. Both
successful diagnostic deltas were independently read back exactly once. The
previously failed **own** review was explicitly replayed after the diagnosis and
repair; it succeeded while reusing the existing Jev opinion. No other task's
identity, Hook state, or failed event was replayed.

The candidate plugin is `0.1.15-rc.1+codex.20260929`. Local receipts, request
timings, focused tests and installation evidence belong under
`output/runtimehook-readiness-regression-20260929/`. The original global-session
acceptance and non-Git direct-writeback limitation are separate and remain open.
Jev's low-confidence design assessment is retained; no threshold or policy was
changed and this repair does not register the overall goal as PASS.

Rollback restores the exact saved prior plugin with the official manager while
retaining task state and receipts. The shared Runtime, graph, selector and proxy
lifecycle are unchanged. The older diagnosis below is historical evidence, not
the current request-budget contract.

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

## Follow-up root-cause investigation

The Owner challenged stopping at a tested source candidate. Fresh bounded
component probes measured liveness at 294.492 ms, embedding deep verification
at 271.107 ms, complete readiness at 4554.545 ms, two deep stats calls at
2271.408/2085.957 ms, and cached verified stats at 65.797 ms. All completed
health checks were production-ready. The five-second client cap therefore has
less margin than the initial controls suggested; it is not a latency guarantee.

Git identifies the blanket two-second cap in the new writeback adapter's
introduction, `719912b` (2026-09-26). The canonical client's existing
`_probe_stats` first validates cached stats and only when needed requests a
fresh deep readiness check with a thirty-second timeout. The adapter's new
forced-deep request and shorter cap did not follow that operating behavior.
The endpoint is a complete graph verification, not merely a loopback ping.
No network, graph corruption or service outage was established by these probes.

Read-only function profiling, without creating a GraphEngine or second runtime,
used the actual graph's 2,696 nodes and 1,035 edges. It found 299,256 recursive
normalizer calls, including traversal of model JSON already normalized by the
existing serializer. The smallest backend repair returns that serializer's
JSON-mode result directly. Generic non-model normalization and the existing
compatibility path remain unchanged; no new cache or validation bypass is added.

All 3,731 live node/edge model outputs compared equal before and after that
change. Three alternating before/after complete-readiness comparisons were
production-ready and returned identical result objects. The graph file
signature remained unchanged throughout:

| Pair | Previous function (ms) | Candidate function (ms) |
| --- | ---: | ---: |
| 1 | 827.666 | 406.417 |
| 2 | 671.673 | 394.379 |
| 3 | 700.318 | 354.980 |

Median function time fell from 700.318 to 394.379 ms (about 44%). These are
read-only local function comparisons, not measurements of a deployed server.
The full readiness suite passed 22 tests, including a new counterexample that
accepts equivalent nested datetime/enum/tuple serialization and rejects a changed
nested value on disk. Existing corruption, identity, profile, embedding, edge,
cache invalidation and forced-refresh checks remain covered.

The App-bundled plugin manager confirms that the installed 0.1.12 plugin and
its configured marketplace both still resolve to the previous 4b8ea7c source.
Updating this PR cannot update that installed source automatically. The supported
plugin manager can select a pinned candidate, but installation and actual native
loading are separate evidence steps. The global npm CLI was incompatible with
the App's current configuration schema; its read-only listing failed. The App's
own native binary successfully listed the exact installed plugin. No global
configuration was edited to work around the unrelated CLI mismatch.

Private component timings, function profiles, equality comparisons and native
plugin listing are retained beside the original receipts. This follow-up does
not replace those failures or retroactively label them successful.

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
