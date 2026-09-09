# Paper / Shadow Trading — Slice AA

This document covers the paper/shadow execution engine added in Slice AA: what it is, how
it differs from live trading, where its state lives, how to run and inspect it, and the
safety boundary that keeps it from ever touching real money.

**Scope note, up front:** Slice AA stops here on purpose. It does not implement AB
(portfolio-level allocation across strategies) or AC (the live promotion gate). A
StrategyVersion that trades cleanly in paper mode has demonstrated nothing about live
readiness — see "What paper results do and do not mean" below.

## 1. What paper/shadow mode is

Paper (shadow) trading runs an approved `StrategyVersion` (see `strategies/core.py`)
against real market data on a schedule, generates `Signal`s exactly as it would for a
live or backtest run, and then simulates the resulting order, fill, position, and P&L
entirely inside a separate paper ledger — no broker is ever involved.

The pipeline, end to end:

```
StrategyVersion (registered) -> paper-eligible (explicitly approved)
  -> signal generation (strategies.core.Strategy, unmodified interface)
  -> paper order (simulated)
  -> paper fill (same-bar close, no future data)
  -> paper position / paper P&L (paper/store.py)
  -> paper trade journal (immutable, append-only)
  -> dashboard / API (read-only)
  -> Telegram notification (best-effort, unmistakably labeled)
```

This is the second stage of a four-stage lifecycle: Research → StrategyVersion →
**Paper/Shadow (AA, this slice)** → Portfolio (AB, not built) → Promotion Gate → Live
Execution (AC, not built).

## 2. How paper differs from live — the safety boundary

**AA must never submit a real broker order, and cannot by construction.** This is
enforced at several levels, not just by convention:

- **No import path to the broker.** `paper/` never imports `engine.execute`,
  `engine.guardrails`, `engine.journal`, `engine.broker`, `engine.broker_kite`, or
  `engine.broker_indstocks`. The only `engine/` imports anywhere in the package are
  `engine.market_data.get_history` (read-only daily OHLCV, the same function
  `engine/screener.py` and `engine/regime.py` already use), `engine.costs`
  (pure cost-model math, no I/O), and `engine.watchlist` (a static, hardcoded symbol
  list, no I/O). This is checked mechanically, not just documented — see
  `tests/test_paper_isolation.py`.
- **A separate store.** Paper state lives in its own SQLite database
  (`paper/paper_shadow.db` by default), entirely distinct from `memory/state.json`
  (live) and `research/market_memory.db` (research). Nothing in `paper/` opens,
  reads, or writes either of those.
- **No live-side mutation, proven at runtime.** `tests/test_paper_isolation.py` monkeypatches
  `engine.execute.propose_trade` / `close_position` / `sync_from_broker`,
  `engine.broker.get_broker`, `engine.guardrails.save_state` / `validate_order`, and
  `engine.journal.add_position` / `close_position` / `log_trade` to raise
  `AssertionError` if called at all, then runs full paper cycles and asserts zero calls.
  It also takes sha256 fingerprints of `memory/state.json`, `memory/trades.jsonl`,
  `memory/regime_log.jsonl`, `memory/portfolio_state.md`, `engine/guardrails.py`, and
  `engine/execute.py` before and after, and asserts they are byte-identical.
- **Read-only API.** The `/paper/*` endpoints are all `GET`; there is no write endpoint
  anywhere in Slice AA (see §7).
- **Separate, disableable cron.** Nothing in this slice touches `run_cycle.sh` or its
  existing crontab entries. The paper cadence is its own cron line, added or removed
  independently (see §9).

Everything else about paper mode is a deliberate simplification versus live: no broker
sync, no drawdown ladder, no Telegram commands, no options — just BUY/SELL signals,
simulated fills, and simulated accounting.

## 3. Where paper state lives

| Path | What it is | Live/research equivalent |
|---|---|---|
| `paper/paper_shadow.db` | SQLite: account, lots, orders, trades, marks, cycle ledger | `memory/state.json` (live) |
| `paper/eligibility/eligibility_log.jsonl` | Append-only APPROVE/REVOKE audit log | *(no live equivalent — new concept)* |

Both paths are overridable via environment variables (`PAPER_DB_PATH`,
`PAPER_ELIGIBILITY_DIR`) and are deliberately **not** named anything resembling
`state.json`, to make it impossible to confuse a paper file with the live one at a
glance — see `paper/config.py` and `paper/__init__.py`.

The database schema (`paper/schema.sql`) has six tables:

- `paper_account` — singleton row: cash, capital, as_of.
- `paper_lots` — one row per opened position lot (mutable: OPEN → CLOSED).
- `paper_orders` — append-only; every simulated order, filled or rejected.
- `paper_trades` — append-only; every closed round trip, with gross/costs/net P&L.
- `paper_marks` — latest mark price per symbol (for unrealized P&L / dashboard).
- `paper_cycles` — idempotency ledger: one row per `cycle_id`, RUNNING/COMPLETED/FAILED.

`paper_orders` and `paper_trades` enforce append-only at the database level (SQLite
triggers that `RAISE(ABORT, ...)` on `UPDATE`/`DELETE`), the same discipline
`research/store.py` uses for its bitemporal history — a paper trade is never rewritten
in place, only ever inserted.

## 4. Strategy eligibility — explicit and auditable

Registering a `StrategyVersion` in `strategies/registry/` means only "this exact
algorithm + parameters + source combination is defined." It does **not** make it
paper-eligible. AA never infers approval from registration, backtest results, or
anything else — an eligibility decision is always an explicit, attributed event:

```bash
python3 -m paper.eligibility approve \
    --version-id <version_id> --strategy-id momentum \
    --by Vaibhav --reason "promising backtest, want shadow tracking"

python3 -m paper.eligibility revoke \
    --version-id <version_id> --strategy-id momentum \
    --by Vaibhav --reason "backtest degraded"

python3 -m paper.eligibility list
```

Each call appends an event (`APPROVE` or `REVOKE`) to
`paper/eligibility/eligibility_log.jsonl` — never a single mutable flag — so the full
history of who approved or revoked a version, when, and why is always recoverable
(`paper.eligibility.list_events()`), even after a later revoke. `mark_paper_eligible()`
fails closed: it refuses to record an event for a `version_id` that isn't actually in
the registry, or whose registered `strategy_id` doesn't match what was passed.

Only the strategy versions whose *most recent* event is `APPROVE` are picked up by the
runner each cycle (`paper.eligibility.list_paper_eligible()`).

## 5. Fill model and cost model

A paper order fills at the **close of the most recent daily bar** returned by the same
`context.history()` call the strategy used to generate its signal — see
`paper/fills.py` and `paper/context.py`. This is leak-free by construction: the fill
price and the signal are read from the identical historical data, so there is no code
path by which a fill can see a price the signal itself couldn't have seen. There is no
broker/live-quote dependency at all (`engine.market_data.get_live_quotes`/`get_ltp` are
never imported anywhere in `paper/`).

If the history data is missing, empty, or malformed (`NaN`/non-positive/non-numeric
close), `compute_fill()` returns `None` and the order is rejected with
`REJECT_NO_PRICE_DATA` — never a fabricated price.

Costs are computed with the existing `engine.costs.equity_round_trip()` — the same
brokerage/STT/exchange/stamp/SEBI/DP/GST model live and backtest trades use — so gross
P&L, total costs, and net P&L are recorded separately and are directly comparable to
live/backtest numbers.

## 6. Portfolio accounting — the lot model

Each `(strategy_version_id, symbol)` position is tracked as a FIFO list of **lots**:

- **BUY, no open lot** → opens a new lot (a standard entry).
- **BUY, lot(s) already open** → opens an *additional* lot against the same position
  (pyramiding is allowed — see `PAPER_MAX_LOTS_PER_POSITION` below for the cap). This is
  a paper-mode design choice, not a live-mode assumption: it exists so that "increase an
  existing position" and "reduce/close a position" are both real, independently testable
  code paths, per the AA spec.
- **SELL, no open lot** → rejected with `REJECT_NO_OPEN_POSITION`. There is no shorting
  in AA.
- **SELL, lot(s) open** → closes the *oldest* open lot (FIFO). If other lots remain open
  afterward, this is a partial reduce; if it was the last lot, the position is fully
  closed and a `paper_trade` row is written with gross/costs/net P&L.

`avg_entry_price` shown on `/paper/positions` is always computed live as a
quantity-weighted average over currently open lots — it is never a separately stored,
independently driftable field.

Simulation-only risk limits (not a second copy of `engine/guardrails.py`, see §2 and
`paper/config.py`'s own module docstring):

| Env var | Default | Meaning |
|---|---|---|
| `PAPER_INITIAL_CAPITAL` | ₹100,000 | Starting paper cash. A round, obviously-synthetic figure — never derived from `memory/state.json`'s real `allocated_capital`. |
| `PAPER_POSITION_NOTIONAL` | ₹10,000 | Target notional per opened lot. |
| `PAPER_MAX_CONCURRENT_POSITIONS` | 10 | Max distinct symbols with an open position at once. Does not block pyramiding an *existing* position. |
| `PAPER_MAX_LOTS_PER_POSITION` | 5 | Max lots a single `(strategy_version_id, symbol)` may pyramid to. |
| `PAPER_MIN_CASH_BUFFER` | 0 | A BUY that would take paper cash below this is rejected outright, or sized down to what's affordable (down to a minimum of 1 share) if only partially affordable. |

## 7. Idempotency

Idempotency is enforced at two independent layers:

1. **Cycle-level.** Every run is identified by a `cycle_id` (default:
   `paper-<label>-<YYYY-MM-DD>`, one cycle per label per day). `run_paper_cycle()` checks
   `paper_cycles` first — if `cycle_id` is already `COMPLETED`, it returns the stored
   summary immediately (`idempotent_replay: true` in the response) without touching a
   Strategy, the market data, or the ledger again.
2. **Order-level.** Even within one cycle, `paper_orders.dedupe_key` is a `UNIQUE`
   column derived from `cycle_id + strategy_version_id + symbol + side + signal timestamp`.
   `insert_order()` uses `INSERT OR IGNORE`; a duplicate insert returns `None`, and the
   portfolio layer skips all lot/cash mutation for that signal entirely.

Both layers are covered explicitly in `tests/test_paper_runner.py` (run-once vs.
run-twice vs. run-three-times on the same `cycle_id`) and `tests/test_paper_store.py`
(direct `dedupe_key` collision tests), rather than assumed from code review.

## 8. Telegram notifications

`paper/notify.py` reuses `scripts/telegram_notify.send_message()` verbatim — the same
function `run_cycle.sh`'s live cycles already call — so there is exactly one Telegram
sending mechanism in this codebase, not two. Every paper message is prefixed and
explicitly labeled so it can never be mistaken for a live trade:

```
🟡 PAPER TRADE — simulation only, no real order was placed

Strategy: momentum
Version: 3f1a9c2b7e6d1a05
Symbol: RELIANCE
Action: BUY
Qty: 10
Paper Fill: ₹2,845.60
```

Other message kinds: `🟡 PAPER ORDER REJECTED`, `🟡 PAPER CYCLE SUMMARY`, and
`🔴 PAPER CYCLE FAILED` (all similarly labeled "simulation only").

Notifications are strictly best-effort: `paper/store.py` writes are always committed
*before* a notification is attempted, every call in `paper/notify.py` swallows its own
exceptions, and a Telegram outage can never lose paper state or crash a cycle — verified
in `tests/test_paper_runner.py` by monkeypatching the send function to raise and
confirming the cycle still completes and persists correctly.

## 9. Running a paper cycle manually

```bash
# From the project root, with the same environment (.env) as the live engine:
python3 -m paper.runner run

# With an explicit cycle id (useful for testing / re-running the same label twice):
python3 -m paper.runner run --cycle-id paper-manual-2026-09-09

# Suppress Telegram for a test run:
python3 -m paper.runner run --no-notify
```

Each call prints a JSON summary to stdout: `as_of`, `eligible_version_count`,
`skipped_versions` (with reasons), `signals_evaluated`, `orders_filled`,
`orders_rejected`, and the performance fields from §11.

On production (VPS), the equivalent is:

```bash
cd /root/trading-agent && venv/bin/python -m paper.runner run --cycle-label scheduled
```

## 10. Enabling / disabling the paper cadence

**Not installed by this slice** — the AA spec explicitly calls for this to be
documented, not deployed. To enable it later, add one line to the crontab
(`crontab -e` on the VPS), well after the live evening cycle (18:30 IST, see
`docs/ENGINE_DEPLOY.md`) so the day's final daily bar is settled, and clearly separated
from the existing live block:

```cron
# ---- Paper/shadow strategy engine (Slice AA) — simulation only, never
# ---- touches the live account. Independent of the trading-cycle block above;
# ---- disable by commenting out or removing this one line.
0 19 * * 1-5  cd /root/trading-agent && venv/bin/python -m paper.runner run --cycle-label scheduled >> /root/trading-agent/logs/paper_cycle.log 2>&1
```

To disable: comment out or delete that one line (`crontab -e`). Nothing else needs to
change — the paper engine has no daemon, no queue, and no state that depends on the cron
job continuing to run. To disable at the application level instead (keep the cron job
but make it a no-op), revoke eligibility for every StrategyVersion
(`python -m paper.eligibility revoke ...` for each); a cycle with zero eligible versions
completes immediately with an empty summary.

This line is additive only — it does not modify, reorder, or depend on any existing
entry in the crontab, and removing it has zero effect on `run_cycle.sh`'s live cycles.

## 11. Inspecting paper state

Via the dashboard: open the **Paper** tab (Netlify dashboard) — shows paper capital,
equity, P&L, open positions, active strategies, orders, and trades, all clearly marked
"PAPER"/"SHADOW", in a section entirely separate from the live **Trading** tab.

Via the API (same bearer-token auth as the rest of the API — see
`docs/DEPLOYMENT.md`):

```
GET /paper/account      — cash, capital, equity, as_of
GET /paper/positions    — open positions (aggregated across lots), avg entry price
GET /paper/orders       — all simulated orders (filled + rejected), most recent first
GET /paper/trades       — all closed round trips, with gross/costs/net P&L
GET /paper/strategies   — currently paper-eligible StrategyVersions + approval metadata
GET /paper/performance  — the summary described in §11 below
```

All six are `GET`-only; there are no `/paper/*` write endpoints, and they are handled by
a dedicated `api/paper_data.py` module with its own error type (`PaperDataSourceError`
→ 503) — entirely separate from `api/data.py`'s live data path.

Via SQLite directly (read-only inspection, e.g. for debugging):

```bash
sqlite3 paper/paper_shadow.db "select * from paper_account;"
sqlite3 paper/paper_shadow.db "select * from paper_trades order by closed_at desc limit 10;"
```

## 12. Performance summary fields

`GET /paper/performance` (and the `perf` fields folded into every `run_paper_cycle()`
JSON summary) reports, deterministically from the ledger:

- `starting_capital`, `cash`, `equity`
- `realized_pnl`, `unrealized_pnl`, `total_net_pnl`
- `trade_count`, `win_count`, `loss_count`, `win_rate` (`None` when `trade_count == 0`,
  never divide-by-zero)
- `open_position_count`

Deliberately excluded from AA: CAGR, Sharpe, drawdown curves, or any other
time-series-derived analytics. These belong to a later slice, once there's enough paper
history to make them meaningful.

## 13. Recovery after restart

All paper state is committed to `paper/paper_shadow.db` synchronously as each order/lot
is processed — nothing is held only in memory across a cycle. A process restart (VPS
reboot, systemd restart of the API, etc.) loses nothing: the next `python -m paper.runner
run` call simply reopens the same database and continues from the last committed state.
`tests/test_paper_store.py` and `tests/test_paper_runner.py` cover this directly
(closing and reopening a store mid-test, confirming state survives).

## 14. Determinism

Given the same strategy code, the same `StrategyVersion`, the same historical data, the
same starting capital/config, and the same paper-store state, a cycle produces byte-
identical signals, orders, fills, positions, and P&L across independent runs —
including the `paper_order_id`/`paper_trade_id` values themselves, which are derived via
`sha256` of the order/trade's business fields (`paper/store.py`'s `_short_hash()`), not
random UUIDs. `tests/test_paper_portfolio.py` and `tests/test_paper_runner.py` both
include an explicit "run the same scenario in two independent environments, assert
identical output including IDs" test.

## 15. Fail-safe behavior

| Situation | Behavior |
|---|---|
| Strategy referenced by an eligibility event is missing/deleted from the registry | Skipped, reason recorded in the cycle summary's `skipped_versions` |
| A Strategy's `generate_signal()` raises | Caught, skipped, reason recorded — the rest of the cycle continues |
| Signal for a symbol outside the resolved universe | Rejected (`symbol_outside_universe`), recorded as a rejected order |
| Missing/malformed price data | No fill — rejected with `REJECT_NO_PRICE_DATA` |
| Insufficient cash | Sized down if partially affordable (min 1 share), else rejected (`REJECT_INSUFFICIENT_CASH`) |
| Duplicate signal (same cycle or a re-run of the same `cycle_id`) | No-op — idempotent, see §7 |
| Telegram send fails | Swallowed; paper state already committed; cycle result unaffected |
| API/dashboard unavailable | The paper engine (cron-driven) runs independently either way — it never calls the API |

Nothing in the runner or portfolio layer partially writes a trade: `open_lot()` and
`close_lot()` in `paper/store.py` are each wrapped in an explicit SQLite transaction
(`BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK`), so a failure mid-write rolls back cleanly
rather than leaving a half-updated lot or account row.

## 16. What Slice AA does NOT do

- Does not place, modify, or cancel any real broker order, under any circumstance.
- Does not implement AB (cross-strategy paper portfolio allocation).
- Does not implement AC (the live promotion gate, or any automatic path from paper
  results to live capital).
- Does not automatically infer eligibility from a backtest score, registration, or
  anything else — every eligibility decision is a manual, attributed CLI call.
- Does not support shorting or any order type beyond BUY-to-open/increase and
  SELL-to-reduce/close.
- Does not compute CAGR, Sharpe, or other advanced analytics.
- Does not touch `run_cycle.sh`, the existing live crontab, `engine/guardrails.py`, or
  `engine/execute.py`.
- Is not deployed to the VPS by this slice (see §10) — deployment is documented, not
  performed.

### What paper results do and do not mean

**A StrategyVersion trading cleanly in paper mode is evidence, not approval.** It says
signal generation didn't crash, the strategy respected the universe, and the simulated
accounting was internally consistent — nothing about it constitutes, implies, or
automatically triggers a decision to risk real capital. Any future promotion from paper
to live (AC, not part of this slice) is a separate, explicit, human-gated decision.

## 17. Known limitations

- Daily-bar granularity only (one fill opportunity per symbol per day, at the prior
  close) — there is no intraday paper execution in AA.
- `PAPER_MIN_CASH_BUFFER`-driven rejection only sizes a BUY *down*; it never queues or
  retries a rejected signal on a later cycle.
- No shorting, no options, no order types beyond plain BUY/SELL — matches the live
  system's `Signal` model exactly (`strategies/core.py`), not a limitation specific to
  paper mode.
- `paper/algorithms.py` keeps its own algorithm registry, independent of
  `research/experiments/strategy_backtest.py`'s — a strategy class must be registered in
  both places to be usable for both backtesting and paper trading (deliberate: avoids
  pulling `research/` into `paper/`'s import graph; see `paper/algorithms.py`'s module
  docstring).
- No automated promotion path exists yet (by design — that's AC).

## 18. Files

New package: `paper/` (`__init__.py`, `clock.py`, `config.py`, `schema.sql`, `store.py`,
`eligibility.py`, `context.py`, `fills.py`, `algorithms.py`, `portfolio.py`, `notify.py`,
`runner.py`). New API module: `api/paper_data.py`. Modified: `api/app.py` (6 new routes),
`frontend/{index.html,css/style.css,js/{views.js,app.js}}` (Paper tab). New tests:
`tests/{paper_fixtures,test_paper_isolation,test_paper_store,test_paper_eligibility,
test_paper_fills,test_paper_portfolio,test_paper_runner,test_paper_api}.py`.
