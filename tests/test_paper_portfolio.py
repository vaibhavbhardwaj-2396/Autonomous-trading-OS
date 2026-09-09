"""
tests/test_paper_portfolio.py — paper/portfolio.py's accounting: BUY
open/increase, SELL reduce/close (FIFO), no-shorting, simulation risk
limits, cost integration (engine.costs, never reimplemented), and
determinism.

Run with:  python -m tests.test_paper_portfolio
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.paper_fixtures import PaperTestEnv, fixed_history_df, FIXED_NOW  # noqa: E402
from paper.portfolio import PaperPortfolio, REJECT_NO_OPEN_POSITION  # noqa: E402
from paper.portfolio import REJECT_MAX_LOTS, REJECT_MAX_CONCURRENT, REJECT_INSUFFICIENT_CASH  # noqa: E402
from paper.store import PaperStore  # noqa: E402
from strategies.core import Signal  # noqa: E402
from engine.costs import equity_round_trip  # noqa: E402

PASSED, FAILED = 0, 0


def check(name, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  ✓ {name}")
    else:
        FAILED += 1
        print(f"  ✗ {name}")
        if detail:
            print(f"      {detail}")


NOW = FIXED_NOW.isoformat(timespec="seconds")


def buy(symbol="INFY", version="v1", at="2026-01-05T16:00:00+05:30"):
    return Signal(strategy_id="s1", strategy_version_id=version, symbol=symbol,
                 action="BUY", generated_at=at)


def sell(symbol="INFY", version="v1", at="2026-01-05T16:00:01+05:30"):
    return Signal(strategy_id="s1", strategy_version_id=version, symbol=symbol,
                 action="SELL", generated_at=at)


# ---------------------------------------------------------------------------
print("\n--- BUY: opening and increasing a position ---")
# ---------------------------------------------------------------------------

with PaperTestEnv(position_notional=10_000.0) as env:
    pf = PaperPortfolio(env.store)
    df = fixed_history_df([100.0])

    out1 = pf.process_signal(buy(at="t1"), df, cycle_id="c1", now=NOW)
    check("a BUY with no existing position OPENS a new lot, status FILLED",
          out1.order["status"] == "FILLED" and out1.is_new)
    positions = env.store.list_positions()
    check("after one BUY, exactly one lot is open at the fill price",
          len(positions) == 1 and positions[0]["avg_entry_price"] == 100.0
          and positions[0]["lot_count"] == 1)

    df2 = fixed_history_df([120.0])
    out2 = pf.process_signal(buy(at="t2"), df2, cycle_id="c1", now=NOW)
    check("a SECOND BUY for the same (version, symbol) INCREASES the position "
          "(a second lot), never ignored the way the backtest's no-pyramiding "
          "v0 model does", out2.order["status"] == "FILLED")
    positions = env.store.list_positions()
    check("after two BUYs, the position has 2 lots and a quantity-weighted avg entry price",
          positions[0]["lot_count"] == 2
          and positions[0]["avg_entry_price"] not in (100.0, 120.0))


# ---------------------------------------------------------------------------
print("\n--- SELL: reducing (FIFO) and fully closing ---")
# ---------------------------------------------------------------------------

with PaperTestEnv(position_notional=10_000.0, max_lots_per_position=5) as env:
    pf = PaperPortfolio(env.store)

    pf.process_signal(buy(at="t1"), fixed_history_df([100.0]), cycle_id="c1", now=NOW)
    pf.process_signal(buy(at="t2"), fixed_history_df([100.0]), cycle_id="c1", now=NOW)
    lots_before = env.store.list_open_lots(strategy_version_id="v1", symbol="INFY")
    check("two BUYs open two distinct lots", len(lots_before) == 2)
    oldest_lot_id = lots_before[0]["id"]

    out = pf.process_signal(sell(at="t3"), fixed_history_df([110.0]), cycle_id="c1", now=NOW)
    check("a SELL with two open lots closes the OLDEST one FIFO, and REDUCES "
          "the position rather than fully closing it",
          out.order["status"] == "FILLED" and out.trade["lot_id"] == oldest_lot_id)
    check("exactly one lot remains open after the partial reduction",
          len(env.store.list_open_lots(strategy_version_id="v1", symbol="INFY")) == 1)

    out2 = pf.process_signal(sell(at="t4"), fixed_history_df([115.0]), cycle_id="c1", now=NOW)
    check("a further SELL with exactly one lot open CLOSES the position entirely",
          out2.order["status"] == "FILLED")
    check("no lots remain open — the position is fully closed",
          env.store.list_positions() == [])

    out3 = pf.process_signal(sell(at="t5"), fixed_history_df([120.0]), cycle_id="c1", now=NOW)
    check("a SELL with NO open position is rejected with reason=no_open_position "
          "(no shorting is ever created)",
          out3.order["status"] == "REJECTED" and out3.order["reason"] == REJECT_NO_OPEN_POSITION)
    check("the rejected SELL created no lot and no trade",
          out3.trade is None)


# ---------------------------------------------------------------------------
print("\n--- costs: reused from engine.costs, never reimplemented ---")
# ---------------------------------------------------------------------------

with PaperTestEnv(position_notional=10_000.0) as env:
    pf = PaperPortfolio(env.store)
    pf.process_signal(buy(at="t1"), fixed_history_df([100.0]), cycle_id="c1", now=NOW)
    lot = env.store.list_open_lots(strategy_version_id="v1", symbol="INFY")[0]
    out = pf.process_signal(sell(at="t2"), fixed_history_df([105.0]), cycle_id="c1", now=NOW)

    expected_costs = equity_round_trip(lot["entry_price"], lot["quantity"], intraday=False).total
    check("the recorded trade cost is EXACTLY engine.costs.equity_round_trip()'s own "
          "figure — never a separately reimplemented estimate",
          abs(out.trade["costs"] - expected_costs) < 1e-9)

    expected_gross = (105.0 - lot["entry_price"]) * lot["quantity"]
    check("gross_pnl is (exit - entry) * quantity, exactly",
          abs(out.trade["gross_pnl"] - expected_gross) < 1e-9)
    check("net_pnl is gross_pnl - costs, exactly",
          abs(out.trade["net_pnl"] - (out.trade["gross_pnl"] - out.trade["costs"])) < 1e-9)


# ---------------------------------------------------------------------------
print("\n--- simulation-only risk limits (never engine/guardrails.py's limits) ---")
# ---------------------------------------------------------------------------

with PaperTestEnv(position_notional=10_000.0, max_lots_per_position=2) as env:
    pf = PaperPortfolio(env.store)
    pf.process_signal(buy(at="t1"), fixed_history_df([100.0]), cycle_id="c1", now=NOW)
    pf.process_signal(buy(at="t2"), fixed_history_df([100.0]), cycle_id="c1", now=NOW)
    out3 = pf.process_signal(buy(at="t3"), fixed_history_df([100.0]), cycle_id="c1", now=NOW)
    check("a BUY beyond paper.config.max_lots_per_position() is rejected",
          out3.order["status"] == "REJECTED" and out3.order["reason"] == REJECT_MAX_LOTS)

with PaperTestEnv(position_notional=10_000.0, max_concurrent_positions=1) as env:
    pf = PaperPortfolio(env.store)
    out1 = pf.process_signal(buy(symbol="INFY", at="t1"), fixed_history_df([100.0]),
                             cycle_id="c1", now=NOW)
    check("the first NEW position is accepted", out1.order["status"] == "FILLED")
    out2 = pf.process_signal(buy(symbol="TCS", at="t2"), fixed_history_df([100.0]),
                             cycle_id="c1", now=NOW)
    check("a SECOND, genuinely new position beyond max_concurrent_positions is rejected",
          out2.order["status"] == "REJECTED" and out2.order["reason"] == REJECT_MAX_CONCURRENT)
    # Increasing the ALREADY-held position is not blocked by the concurrent-position cap.
    out3 = pf.process_signal(buy(symbol="INFY", at="t3"), fixed_history_df([100.0]),
                             cycle_id="c1", now=NOW)
    check("pyramiding an EXISTING position is not blocked by max_concurrent_positions "
          "(that cap is about distinct symbols/positions, not lot count)",
          out3.order["status"] == "FILLED")

with PaperTestEnv(initial_capital=1_000.0, position_notional=10_000.0,
                  max_lots_per_position=10) as env:
    pf = PaperPortfolio(env.store)
    out = pf.process_signal(buy(at="t1"), fixed_history_df([100.0]), cycle_id="c1", now=NOW)
    check("a BUY that would exceed available paper cash is SIZED DOWN rather than "
          "opening a lot the account cannot afford",
          out.order["status"] == "FILLED" and out.order["requested_quantity"] <= 10)
    acct = env.store.get_account()
    check("paper cash never goes negative from a sized-down BUY", acct["cash"] >= 0)

with PaperTestEnv(initial_capital=1.0, position_notional=10_000.0) as env:
    pf = PaperPortfolio(env.store)
    out = pf.process_signal(buy(at="t1"), fixed_history_df([1_000_000.0]),
                            cycle_id="c1", now=NOW)
    check("a BUY that cannot afford even ONE share is rejected as insufficient_paper_cash",
          out.order["status"] == "REJECTED" and out.order["reason"] == REJECT_INSUFFICIENT_CASH)


# ---------------------------------------------------------------------------
print("\n--- idempotency at the portfolio layer ---")
# ---------------------------------------------------------------------------

with PaperTestEnv(position_notional=10_000.0) as env:
    pf = PaperPortfolio(env.store)
    sig = buy(at="same-signal-time")
    out1 = pf.process_signal(sig, fixed_history_df([100.0]), cycle_id="c1", now=NOW)
    out2 = pf.process_signal(sig, fixed_history_df([100.0]), cycle_id="c1", now=NOW)
    check("processing the IDENTICAL signal twice in the SAME cycle_id opens only ONE lot",
          out1.is_new and not out2.is_new
          and len(env.store.list_open_lots(strategy_version_id="v1", symbol="INFY")) == 1)


# ---------------------------------------------------------------------------
print("\n--- determinism: identical inputs -> identical outputs ---")
# ---------------------------------------------------------------------------

def _run_once(tmp_env: PaperTestEnv) -> dict:
    pf = PaperPortfolio(tmp_env.store)
    pf.process_signal(buy(at="t1"), fixed_history_df([100.0, 101.0]), cycle_id="c1", now=NOW)
    pf.process_signal(buy(at="t2"), fixed_history_df([100.0, 103.0]), cycle_id="c1", now=NOW)
    out = pf.process_signal(sell(at="t3"), fixed_history_df([100.0, 108.0]), cycle_id="c1", now=NOW)
    return tmp_env.store.get_account(), out.trade


with PaperTestEnv(position_notional=10_000.0) as env_a, \
     PaperTestEnv(position_notional=10_000.0) as env_b:
    account_a, trade_a = _run_once(env_a)
    account_b, trade_b = _run_once(env_b)
    check("two structurally-identical runs (same signals, same price data, same "
          "config, same starting capital) produce byte-identical account state",
          account_a["cash"] == account_b["cash"]
          and account_a["realized_pnl_alltime"] == account_b["realized_pnl_alltime"])
    check("...and byte-identical trade records (P&L, prices, quantities)",
          trade_a["net_pnl"] == trade_b["net_pnl"]
          and trade_a["gross_pnl"] == trade_b["gross_pnl"]
          and trade_a["quantity"] == trade_b["quantity"])
    check("...and identical deterministic ids (paper_order_id / paper_trade_id "
          "are derived from business fields, not randomness)",
          trade_a["paper_trade_id"] == trade_b["paper_trade_id"])


# ---------------------------------------------------------------------------
print("\n--- performance_summary() ---")
# ---------------------------------------------------------------------------

with PaperTestEnv(initial_capital=100_000.0, position_notional=10_000.0) as env:
    pf = PaperPortfolio(env.store)
    perf0 = pf.performance_summary()
    check("performance_summary() on a fresh account: n_trades=0, win_rate=None "
          "(never a divide-by-zero 0.0)", perf0["n_trades"] == 0 and perf0["win_rate"] is None)
    check("a fresh account's current_equity equals its starting_capital",
          perf0["current_equity"] == perf0["starting_capital"])

    pf.process_signal(buy(at="t1"), fixed_history_df([100.0]), cycle_id="c1", now=NOW)
    pf.process_signal(sell(at="t2"), fixed_history_df([120.0]), cycle_id="c1", now=NOW)
    perf1 = pf.performance_summary()
    check("after one profitable round trip, n_trades=1 and win_count=1",
          perf1["n_trades"] == 1 and perf1["win_count"] == 1 and perf1["loss_count"] == 0)
    check("win_rate is exactly 1.0 after one win and zero losses",
          perf1["win_rate"] == 1.0)
    check("total_net_pnl equals realized_pnl (to the 2dp performance_summary() rounds "
          "to) when nothing is currently open",
          abs(perf1["total_net_pnl"] - round(perf1["realized_pnl"], 2)) < 1e-9
          and perf1["unrealized_pnl"] == 0.0)


print(f"\n{'=' * 52}")
print(f"  {PASSED} passed, {FAILED} failed")
print(f"{'=' * 52}\n")
sys.exit(1 if FAILED else 0)
