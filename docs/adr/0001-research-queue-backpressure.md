# ADR-0001: Apply backpressure before research discovery

**Status:** Accepted  
**Date:** 2026-09-18

## Context

The continuous research worker can discover hypotheses faster than the system
can promote, test, evaluate, and review them. An unbounded draft or runnable
experiment queue obscures the evidence pipeline and spends Research AI budget
on work that cannot yet be acted upon.

## Decision

Use a pure, observable queue policy before admitting a `DISCOVER` action. By
default discovery is paused when there are at least three pending drafts or
two locked runnable experiments. Promotion and experiment execution remain
eligible, so the backlog can drain. The policy snapshot and decision are
included in worker telemetry. Thresholds are bounded configuration values in
`WorkerLimits`, with environment overrides.

## Consequences

- Discovery resumes automatically when both queues are below their limits.
- Existing research is prioritized without changing hypothesis, evaluation,
  portfolio, broker, or live-trading controls.
- Queue health is independently testable and visible in each worker run.
- A later control-plane phase can replace the static thresholds with measured
  service-level targets without changing the worker's admission boundary.
