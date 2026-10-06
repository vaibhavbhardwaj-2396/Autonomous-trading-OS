# Research governance and capacity calibration

## Finding

`MAX_LOCKS_PER_PERIOD = 5` and `LOCK_BUDGET_PERIOD_DAYS = 7` were introduced
as a deliberately conservative placeholder for a human-paced review workflow.
The source documentation explicitly says the value was not production-tuned.
It is therefore an operational commitment throttle—not a statistically valid
multiple-testing correction, AI budget, compute budget, or trading-risk limit.

The initial release preserved the limit during the audit. Existing drafts were not
discarded and no budget override is used. Compute, runtime, queue depth and AI
spend continue to have their own independent controls.

## October 2026 calibration

The observation window supplied the missing production evidence: 11 of 11 locked
experiments reached `REPORTED`; the latest five all produced evidence; the optimized
replay stayed inside its deadline; and the one-vCPU VPS remained `HEALTHY`/`LOW_LOAD`
(21% load, 76% memory available during the decision audit). The five-lock limit then
left 94 reviewed drafts idle and every ten-minute heartbeat returning `skipped_budget`.

The operational ceiling is therefore recalibrated to **20 locks per rolling seven
days**. Execution remains serial, with at most one promotion and one experiment per
worker heartbeat, a 180-second experiment deadline, hard cancellation, duplicate
rejection and resource admission. The new ceiling provides 15 immediately available
slots from the observed 5/20 state without authorizing an unbounded queue drain.

### Confirmation reserve

The first calibrated production run exposed a separate structural bottleneck: all
legacy autonomous contracts had discovery data only. They could become `PROMISING`,
but `ROBUST` requires a positive, structurally linked validation/holdout sibling, so
the lifecycle had no legal next action. At the same time, the general 20-lock capacity
was full.

The general lane therefore remains **20 locks per rolling seven days**, while a total
ceiling of **25** reserves five additional locks exclusively for genuine
validation/holdout siblings. This is not a budget override: a confirmation must carry
`split_of`/`split` lineage, use the ordinary immutable lock primitive, and enter the
same multiple-testing family. `ROBUST` additionally requires that exact sibling to be
positive, sufficiently sampled, economically meaningful and statistically significant
at the family-adjusted threshold. A merely positive noisy holdout remains `PROMISING`.
Ordinary discovery and parameter variants remain blocked at 20.

New AI ResearchPacket proposals must pre-register a non-overlapping validation or
holdout window and evaluate discovery first. For legacy discovery-only parents, the
control plane may derive exactly one fixed-rule test on the project-wide reserved
holdout `2025-01-01..2026-06-30`, but only when the parent is reported and positive,
the archive covers the entire window, no equivalent OOS contract already exists, and
the window begins after discovery. It can never retry the same holdout until it passes.

## Scientific control

Evidence now records an explicit research family, family hypothesis count,
scored attempt count, unique rule variants, retests, parameter searches,
variant lineage, raw alpha, adjusted alpha and adjusted t threshold. The
deterministic comparison applies conservative Bonferroni family-wise error
control over the explicit research-area family; an untagged hypothesis is its
own family. Raw verdict statistics are preserved.

This prevents one nominal result among many related attempts from becoming
`PROMISING` merely because operational throughput increased. Exact duplicate
contracts remain `REDUNDANT`, not independent replication. Robust/strategy
promotion still depends on deterministic gates and cannot be overridden by an
AI response.

## Operational control

The calibrated throttle may be revisited only from measured queue, compute and
scientific-completion data. It must never be described as protection against
p-hacking. The independent controls are:

- worker runtime and per-action bounds;
- experiment deadline, process cancellation and resource governor;
- draft/runnable queue backpressure;
- AI call/token budgets;
- family-wise multiple-testing correction and immutable experiment contracts.

Paper eligibility remains a separate, explicit and attributed event. Neither the
confirmation reserve nor a `ROBUST` label can silently enroll a strategy in paper, and
no paper outcome authorizes live execution.
