# Implementation roadmap and continuation record

Updated: 2026-09-19. User authorized implementing, testing, merging and
deploying the agreed roadmap, with routine engineering decisions delegated.
This is software delivery authorization, not authorization to override trading
guardrails, change capital, resume live trading, or place orders.

## Completion criteria

Each increment needs relevant tests, green CI for its exact SHA, a clean VPS
deployment, activation of changed services, and a check of the serving route.
Frontend publication is separate from copying files to the VPS: verify the
actual hosted asset before claiming a dashboard change is live.
Preserve unrelated changes and never stage the local untracked AGENTS.md.

## Approved phases

| Phase | Deliverable / acceptance boundary | Status |
|---|---|---|
| 0 | Operating contract, inventory, baseline and delivery workflow | Delivery workflow exists; baseline audit still open |
| 1 | Bounded research throughput, downstream queues and discovery admission | Initial backpressure shipped in ea3c6ca; broader pipeline remains |
| 2 | Observable worker/data/paper health, freshness, failures and UI | Worker status API/UI source shipped in de28ff0; activation and broader health remain |
| 3 | Reproducible isolated tests, release gates, rollback, browser checks | Python CI exists; frontend CI being added |
| 4 | Market-data quality, point-in-time coverage, freshness, provenance | Pending |
| 5 | Typed strategy DSL with validation and deterministic compilation | Pending audit of existing strategy foundation |
| 6 | Evidence-to-strategy factory with immutable provenance | Pending |
| 7 | Realistic backtests, cost/slippage assumptions, holdout controls | Pending audit of existing backtest engine |
| 8 | Paper factory with reliable scheduling and reconciliation | Pending audit of existing paper engine |
| 9 | Portfolio/risk integration within protected guardrails | Pending |
| 10 | Secure administration, complete UI workflows and operations | Existing control UI; remaining scope pending |
| 11 | Human-gated live readiness and operational handoff | Pending; human gate remains required |

These are phases, not assertions that an entire phase is done after one commit.
Existing modules must be inspected and reused before adding new implementations.

## Next work

1. Complete activation and serving checks for the worker-status API.
2. Verify dashboard publication independently; correct unknown admission display.
3. Add bounded telemetry reads and explicit missing/corrupt/stale diagnostics.
4. Make tests use isolated state; remove environment-dependent resource assumptions.
5. Audit data quality and the strategy/backtest/paper integration against the master spec.

## Unattended continuation

No recurring Codex task is active as of this checkpoint. This session exposes
no scheduling tool, and computer control of the desktop Codex application was
denied by the tool. A saved prompt does not start a scheduler. A local scheduled
task also requires the computer awake and the desktop application running.

Suggested task: "Living Quant roadmap", hourly, this project directory,
using the continuation prompt in ROADMAP_CONTINUATION.md. Before enabling,
ensure runs cannot overlap work on the same checkout. Keep the existing runtime
approval and sandbox settings; a blocked deployment must be reported honestly.
