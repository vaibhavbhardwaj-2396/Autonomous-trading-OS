# Final runtime-unblock production report

Captured: 26 September 2026, 21:04 IST. Baseline: `969b7d7`, API v1.4.0.
Verified implementation: `6ce7bb368a0c5769582a8d8bad3075c77cff4316`, API v1.5.0.

## Outcome

The unsafe runtime behavior is fixed, deployed and verified. An experiment that crosses its
deadline now actually stops. Its child process group is terminated, its temporary result file
and database connection are released, the worker becomes available again, the contract is
marked `ABANDONED` with `TIMEOUT`, and no scientific evidence is created. Startup reconciliation
also converts a stale `RUNNING` contract with no surviving child into an audited
`ABANDONED/WORKER_CRASH` outcome without retrying it.

The research throughput blocker is not fully removed. The production smoke workload is a
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
verification). The scientific funnel remained 109 hypotheses, 5 reported experiments, zero
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
- `EXPERIMENTS`: `PAUSED`, manual resume, production replay still exceeds 180 seconds.
- Telegram milestone delivery: successful (`sent`).
- Phase 11: `LOCKED`; explicit human decision remains required.
- Live orders submitted during this work: **0**.

## Remaining blockers

1. Rework the broad Nifty 500 replay into a bounded bulk/columnar execution path, or introduce a
   deterministic pre-test compute-size rejection/partition rule. Do not raise the timeout as a
   substitute for measurement.
2. Install `OPENAI_API_KEY` only in the protected VPS environment, perform one bounded structured
   provider proof, and record actual model, trace, usage, latency and cost before activating AI.
3. Only after a normal experiment completes inside the operational bound should recurring
   experiments be re-enabled. Evidence, strategy and paper progression must then follow the
   existing gates without manual promotion or threshold weakening.
