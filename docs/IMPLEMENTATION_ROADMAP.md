# Implementation roadmap and continuation record

Updated: 2026-09-25. User authorized implementing, testing, merging and
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
| 0 | Operating contract, inventory, baseline and delivery workflow | **Complete** — delivery workflow, safety boundaries and deployment contract are documented and tested |
| 1 | Bounded research throughput, downstream queues and discovery admission | **Complete** — bounded worker, queue policy, lifecycle and backpressure are operational |
| 2 | Observable worker/data/paper health, freshness, failures and UI | **Complete** — worker, recorder, paper and data readiness are exposed through authenticated APIs and UI |
| 3 | Reproducible isolated tests, release gates, rollback, browser checks | **Complete** — Python/frontend CI, guarded deploy and rollback checks gate releases |
| 4 | Market-data quality, point-in-time coverage, freshness, provenance | **Complete** — knowledge-time-gated quality inventory, explicit freshness states and append-only integrity checks |
| 5 | Typed strategy DSL with validation and deterministic compilation | **Complete** — validated closed rule vocabulary and source-locked deterministic compiler |
| 6 | Evidence-to-strategy factory with immutable provenance | **Complete** — ROBUST-only, idempotent promotion with hypothesis/contract/version provenance |
| 7 | Realistic backtests, cost/slippage assumptions, holdout controls | **Complete** — point-in-time replay, holdout evidence gate, costs, two-sided slippage and stop/target/time exits |
| 8 | Paper factory with reliable scheduling and reconciliation | **Complete** — registered algorithms, bounded/idempotent cycles, locking, accounting and typed exit reconciliation |
| 9 | Portfolio/risk integration within protected guardrails | **Complete** — isolated paper portfolio plus unchanged, tested live guardrails; live activation remains Phase 11 |
| 10 | Secure administration, complete UI workflows and operations | **Complete** — distinct observer/admin authentication, explicit CORS, audit history, resource/AI controls, broker health and interactive operational UI |
| 10.5 | Autonomous validation campaign | **Active** — capacity planner, durable funnel ledger, provider/broker/news extension, validation API/UI and architecture are implemented; longitudinal evidence collection continues |
| 11 | Human-gated live readiness and operational handoff | Pending; human gate remains required |

These are phases, not assertions that an entire phase is done after one commit.
Existing modules must be inspected and reused before adding new implementations.

## Phase 10.5 evidence campaign

The software boundary is documented in `docs/PHASE_10_5_VALIDATION.md`.
`research.validation` records append-only snapshots of observations through
paper results, conversion rates, AI economics, capacity allocation and exact
blockers. The system must now accumulate real longitudinal evidence; software
completion is not evidence that a strategy is robust.

Paper remains correctly blocked until an immutable StrategyVersion earns an
explicit paper-eligibility event. Phase 11 remains locked regardless of Phase
10.5 results.

## Remaining plan (Phase 11)

1. Restore bounded research/paper schedules only when the external AI budget is
   intentionally available; today those token-consuming jobs remain paused.
2. Repair optional/stale market-data sources until readiness is READY rather
   than DEGRADED.
3. Collect enough out-of-sample and paper observations for statistical review;
   no strategy may skip this evidence gate.
4. Retain continuous broker reconciliation and daily authentication evidence.
5. Drill expired auth, quote omission, API outage, process crash, state
   mismatch, stop failure and deployment rollback.
6. Prepare one canary strategy/version with explicit risk and rollback limits.
7. Require separate human approval for that exact canary, then observe it
   before any expansion.

Completion of phases 0–10 does **not** authorize a live order or remove any
existing live pause.

## Unattended continuation

The project itself is scheduled on the VPS through bounded cron/systemd entry
points. Codex/ChatGPT desktop scheduling is not a production runtime dependency.
See `docs/PHASE_1_10_ACCEPTANCE.md` for the release evidence and Phase 11 boundary.
