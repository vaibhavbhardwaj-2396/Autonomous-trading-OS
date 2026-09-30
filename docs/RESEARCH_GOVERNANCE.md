# Research governance and the five-lock audit

## Finding

`MAX_LOCKS_PER_PERIOD = 5` and `LOCK_BUDGET_PERIOD_DAYS = 7` were introduced
as a deliberately conservative placeholder for a human-paced review workflow.
The source documentation explicitly says the value was not production-tuned.
It is therefore an operational commitment throttle—not a statistically valid
multiple-testing correction, AI budget, compute budget, or trading-risk limit.

The release preserves the limit during the audit. Existing drafts are not
discarded and no budget override is used. Compute, runtime, queue depth and AI
spend continue to have their own independent controls.

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

The five-lock throttle remains unchanged until an observation window supplies
measured queue, compute and scientific-completion data. It may later be
recalibrated as an operational capacity limit, but it must never be described
as protection against p-hacking. The independent controls are:

- worker runtime and per-action bounds;
- experiment deadline, process cancellation and resource governor;
- draft/runnable queue backpressure;
- AI call/token budgets;
- family-wise multiple-testing correction and immutable experiment contracts.
