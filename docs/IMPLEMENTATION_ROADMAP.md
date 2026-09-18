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
| 10 | Secure administration, complete UI workflows and operations | **Complete** — authenticated control plane, explicit CORS, audit history, resource/AI controls and operational UI |
| 11 | Human-gated live readiness and operational handoff | Pending; human gate remains required |

These are phases, not assertions that an entire phase is done after one commit.
Existing modules must be inspected and reused before adding new implementations.

## Next work

Phase 11 only: human-gated live readiness, shadow-performance review, explicit
operator approval, canary sizing and rollback rehearsal. Completion of phases
0–10 does **not** authorize a live order or remove any existing live pause.

## Unattended continuation

The project itself is scheduled on the VPS through bounded cron/systemd entry
points. Codex/ChatGPT desktop scheduling is not a production runtime dependency.
See `docs/PHASE_1_10_ACCEPTANCE.md` for the release evidence and Phase 11 boundary.
