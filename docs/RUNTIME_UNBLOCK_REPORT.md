# Runtime-unblock production report

Captured: 26 September 2026, 21:04 IST. Baseline: `969b7d7`, API v1.4.0.
Verified implementation: `6ce7bb368a0c5769582a8d8bad3075c77cff4316`, API v1.5.0.

## Performance resolution — 26 September 2026

The throughput blocker described in the original incident record below is resolved. Profiling
identified per-session/per-symbol SQLite price-window reads as the dominant N+1 path. Commit
`ab665dc294f6c4b9912eaafc7e514c5ddfb2187a` replaced that path with a bounded point-in-time bulk
load and array feature computation. It preserves Contract, universe, as-of, cost and portfolio
semantics and falls back to the exact legacy engine whenever equivalence is not guaranteed.

The production three-year Nifty 500 benchmark completed in 16.359 seconds with 459,325 price
rows, 630 symbols, 739 sessions, five SQL queries and 392.2 MiB peak RSS. The matching Q1 sample
was 18.61 times faster and produced identical ordered trades and exact P&L. `EXPERIMENTS` was
resumed only after CI and the full production benchmark passed. The next governed Contract,
`EXP-162BB2-G`, completed in 10.064 seconds, persisted 2,061 simulated trades, reached
`REPORTED`, and created one legitimate evidence update. No live orders were submitted.

The control plane now also provides calibrated workload admission, explicit compute classes,
read-only execution telemetry in the API and existing dashboard screens, and automatic
fail-closed pausing after two consecutive production timeouts. The hard deadline remains 180
seconds. These controls were committed as `881e249`. See
[Experiment engine performance](EXPERIMENT_ENGINE_PERFORMANCE.md) for the complete profile,
scaling decision and operator commands.

## Original cancellation-safety outcome

The unsafe runtime behavior is fixed, deployed and verified. An experiment that crosses its
deadline now actually stops. Its child process group is terminated, its temporary result file
and database connection are released, the worker becomes available again, the contract is
marked `ABANDONED` with `TIMEOUT`, and no scientific evidence is created. Startup reconciliation
also converts a stale `RUNNING` contract with no surviving child into an audited
`ABANDONED/WORKER_CRASH` outcome without retrying it.

At this point in the incident, the research throughput blocker was not yet removed. The production smoke workload is a
three-year Nifty 500 replay with multiple point-in-time conditions. It still exceeds the
180-second limit on the one-vCPU VPS after two targeted improvements: an ordered replay lookup
index and reuse of the largest price window within each immutable as-of session. Two post-fix
controlled runs were cancelled at 180 seconds, and a scheduled run already admitted before the
component was paused was cancelled by the same supervisor. There were no orphan research
processes at final audit. `EXPERIMENTS` is therefore `PAUSED`, not falsely labelled active.

## Production proof

| Item | Result |
|---|---|
| CI | Python and frontend jobs passed for `6ce7bb3` (run `36252254285`) |
| VPS SHA | `6ce7bb368a0c5769582a8d8bad3075c77cff4316` |
| API | healthy on the internal listener; v1.5.0 |
| Safety smoke | 62 passed, 0 failed during deployment |
| Supervisor tests on VPS | 7 passed, 0 failed |
| Timeout lifecycle | `QUEUED → RUNNING → CANCEL_REQUESTED → ABANDONED` |
| Timeout result | `TIMEOUT`; child group terminated; zero evidence |
| Restart recovery | stale `RUNNING` work becomes `WORKER_CRASH`; no silent duplicate |
| Concurrency | one experiment maximum |
| Resource governor | `HEALTHY` / `NORMAL`; heavy lane bounded to one |
| Orphan check | no worker or experiment-supervisor process present |
| Live orders | **0** |

## Backlog and scientific state

All 103 original drafts were processed in six bounded batches (20, 20, 20, 20, 20, 3). Every
review was persisted in `research_draft_review`; actual deterministic outcome was 103 `READY`,
with supported structure, resolvable universe and production price coverage. `READY` is a
pre-test result, not evidence and not permission to trade.

Three drafts entered the governed autonomous promotion/execution path during controlled and
scheduled verification and were operationally abandoned on timeout. Final registry state was
100 draft, 5 reported and 4 abandoned contracts (one abandoned contract predated this final
verification). At that snapshot, the scientific funnel remained 109 hypotheses, 5 reported experiments, zero
evidence artifacts, zero StrategyVersions, zero eligible paper strategies and zero paper trades.
No operational failure was converted into negative or positive scientific evidence.

## AI and cost

`OPENAI_API_KEY` is absent on the VPS. No provider call was attempted, so `RESEARCH_AI` remains
`PAUSED / OPENAI_PROVIDER_NOT_CONFIGURED`. Actual production AI usage is zero calls, zero measured
input tokens, zero measured output tokens and zero monetary cost. Model routing is configured in
the provider-neutral gateway, but there is no honest “actual model used” until a credentialed
call succeeds. Consumer ChatGPT authentication is not used as a substitute.

## Final controls

- `DATA`, `PAPER`, `BROKER_SYNC`, `VALIDATION`, `NOTIFICATIONS` and `WATCHDOG`: `RUNNING`.
- `RESEARCH_AI`: `PAUSED`, manual resume, missing developer API credential.
- `EXPERIMENTS`: was `PAUSED` at the incident snapshot; resumed after the measured bulk replay
  and governed production completion passed.
- Telegram milestone delivery: successful (`sent`).
- Phase 11: `LOCKED`; explicit human decision remains required.
- Live orders submitted during this work: **0**.

## Remaining external blocker

1. Install `OPENAI_API_KEY` only in the protected VPS environment, perform one bounded structured
   provider proof, and record actual model, trace, usage, latency and cost before activating AI.

The experiment-engine runtime blocker is no longer open. Evidence, strategy and paper
progression remain governed by their existing scientific gates; Phase 11 remains locked.
