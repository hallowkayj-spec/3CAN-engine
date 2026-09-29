# Convergence Hook Efficiency Evidence — 2026-08-24

Status: `PARTIAL / GLOBAL_ROLLOUT_BLOCKED`

Strict reference: PR #15 commit
`5995c3f6d694297f8994de252371fdaca7664cd4`.

This evidence separates two different costs:

- foundation engineering time: implementation, adversarial review, CI, and
  release work for the Hook itself; and
- business-session Harness cost: extra elapsed time paid by another Session
  while completing a real task with the Harness enabled.

The first cost is not an input to the second. Four hours spent building and
reviewing the foundation does not prove that a one-hour downstream task becomes
four hours. Conversely, if a real one-hour task becomes three or four hours,
the Harness has failed even if every correctness check passes.

## Current hot-path change

The default project kit no longer starts convergence on `PreToolUse`. It keeps
convergence at `SessionStart` and `Stop`; the PR adapter matcher is restricted
to Bash and create-pull-request tool names. Therefore ordinary `apply_patch`,
Edit, and Write calls match zero default convergence handlers.

This is a configuration reduction, not a new first-write gate. The rejected
`--require-task-for-edit` prototype and its Git-head checkpoint logic are not
part of the public implementation. The existing strict Task Oracle at the
reference commit remains the independently runnable correctness baseline.

## Local absolute-latency sample

Method used an isolated copied project kit on this Windows host, initialized
and committed outside the timed region, with one cold and 30 warm handler calls.
The disposable fixture was removed afterward. No 3CAN Runtime, port 9700/9711,
or graph was changed.

| Boundary | cold ms | warm p50 ms | warm p95 ms | max ms |
| --- | ---: | ---: | ---: | ---: |
| PR adapter, ordinary Bash read/no-op | 142.7 | 142.8 | 155.2 | 157.0 |
| active strict convergence `SessionStart` restore | 785.2 | 645.3 | 704.9 | 723.8 |

These measurements time the local Python handler process, not Codex model time,
task authoring, application tests, final review, or an outer PowerShell wrapper.
They establish only bounded local process cost. They do not prove total Harness
cost, cross-machine performance, or a Harness Tax Ratio.

## Required boundary-level telemetry

A future FAST/EPISODIC experiment should record only:

- task wall time;
- Hook, review, and Oracle runtime;
- first-gate blocks and false blocks;
- review rounds and repair time;
- full-suite runs; and
- manual interventions.

Do not add per-tool tracing. If no historical or paired comparable baseline
exists, report these absolute costs and do not invent the time the task “should”
have taken.

Only with a real comparable baseline may a report calculate:

```text
Harness Tax Ratio =
  (hooked task time - comparable baseline time) / comparable baseline time
```

## Dogfood and equivalence gates

Global rollout remains blocked until:

1. a separate Lite implementation catches the same stale-evidence, hidden
   fallback, mutable-hardcoding, goal-drift, unrequested-behavior, old-candidate,
   and review-reuse fixtures as the strict reference;
2. a 10--20 minute code task, a UI/SaaS task, and a video-lineage task report
   absolute Harness cost, escape, and false-block evidence; and
3. Harness Tax Ratio is calculated only where a truthful comparable baseline
   exists.

The current status is `NOT_RUN` for Lite equivalence and real-task dogfood. A
quality `CONVERGED` result is not an efficiency claim. See
[`ADAPTIVE_REVIEW_HARNESS.md`](../ADAPTIVE_REVIEW_HARNESS.md) for the design-only
experiment and preserved guarantees.
