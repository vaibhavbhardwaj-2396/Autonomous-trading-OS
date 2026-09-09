"""
Tests for the guardrail enforcement layer.

These matter more than any other tests in the project: this is the code standing between
an over-confident agent and a blown-up account. Every limit in memory/guardrails.md should
have a test proving it actually blocks.

Run with:  python -m tests.test_guardrails
"""

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from engine import guardrails as gr  # noqa: E402

BASE = {
    "capital": 10000.0,
    "peak_capital": 10000.0,
    "cash_available": 10000.0,
    "realized_pnl_alltime": 0.0,
    "total_costs_alltime": 0.0,
    "risk_per_trade_pct": 0.02,
    "consecutive_losing_days": 0,
    "trading_paused": False,
    "pause_reason": None,
    "awaiting_human_ack": False,
    "drawdown_level": "NORMAL",
    "day": {"date": "2026-09-02", "starting_capital": 10000.0, "realized_pnl": 0.0,
            "trades_taken": 0, "process_grade": None},
    "week": {"start_date": "2026-08-31", "starting_capital": 10000.0},
    "open_positions": [],
}

PASSED, FAILED = 0, 0


def check(name, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  ✓ {name}")
    else:
        FAILED += 1
        print(f"  ✗ {name}  {detail}")


def s(**overrides):
    st = copy.deepcopy(BASE)
    st.update(overrides)
    return st


print("\n--- Drawdown ladder ---")
check("normal at no drawdown", gr.drawdown_level(s()) == "NORMAL")
check("amber at 10%", gr.drawdown_level(s(capital=9000.0)) == "AMBER")
check("amber at 12%", gr.drawdown_level(s(capital=8800.0)) == "AMBER")
check("orange at 15%", gr.drawdown_level(s(capital=8500.0)) == "ORANGE")
check("red at 20%", gr.drawdown_level(s(capital=8000.0)) == "RED")
check("red below 20%", gr.drawdown_level(s(capital=7000.0)) == "RED")
check("risk halved at orange",
      gr.current_risk_per_trade(s(capital=8500.0)) == gr.RISK_PER_TRADE_DERISKED)
check("risk normal at no drawdown",
      gr.current_risk_per_trade(s()) == gr.RISK_PER_TRADE)

print("\n--- Gate: blocks new positions ---")
check("allowed when clean", gr.check_can_open_positions(s()).allowed)
check("blocked at amber", not gr.check_can_open_positions(s(capital=9000.0)).allowed)
check("blocked at red", not gr.check_can_open_positions(s(capital=8000.0)).allowed)
check("blocked when paused",
      not gr.check_can_open_positions(s(trading_paused=True, pause_reason="test")).allowed)
check("blocked awaiting ack",
      not gr.check_can_open_positions(s(awaiting_human_ack=True)).allowed)
check("blocked on 2 losing days",
      not gr.check_can_open_positions(s(consecutive_losing_days=2)).allowed)

daily_hit = s()
daily_hit["day"]["realized_pnl"] = -500.0  # exactly 5%
check("blocked at daily loss cap", not gr.check_can_open_positions(daily_hit).allowed)

daily_near = s()
daily_near["day"]["realized_pnl"] = -400.0  # 4%
check("allowed just under daily cap", gr.check_can_open_positions(daily_near).allowed)

weekly_hit = s(capital=9000.0)
weekly_hit["week"] = {"start_date": "2026-08-31", "starting_capital": 10000.0}
check("blocked at weekly loss cap", not gr.check_can_open_positions(weekly_hit).allowed)

three_pos = s(open_positions=[
    {"symbol": "A", "open_risk": 50}, {"symbol": "B", "open_risk": 50},
    {"symbol": "C", "open_risk": 50},
])
check("blocked at max positions", not gr.check_can_open_positions(three_pos).allowed)

high_risk = s(open_positions=[{"symbol": "A", "open_risk": 620}])  # >6% of 10000
check("blocked at max aggregate risk", not gr.check_can_open_positions(high_risk).allowed)

print("\n--- Instrument tiers ---")
check("equity allowed at 10k", gr.check_instrument_permitted("equity_cash", s()).allowed)
check("options BLOCKED at 10k", not gr.check_instrument_permitted("long_options", s()).allowed)
check("options still blocked at 25k (a Nifty lot is >25% of capital there)",
      not gr.check_instrument_permitted("long_options", s(capital=25000.0)).allowed)
check("options allowed at 50k",
      gr.check_instrument_permitted("long_options", s(capital=50000.0)).allowed)
check("spreads blocked at 50k",
      not gr.check_instrument_permitted("option_spreads", s(capital=50000.0)).allowed)
check("spreads allowed at 1.5L",
      gr.check_instrument_permitted("option_spreads", s(capital=150000.0)).allowed)
check("futures blocked even at 5L",
      not gr.check_instrument_permitted("futures", s(capital=500000.0)).allowed)
check("writing blocked even at 5L",
      not gr.check_instrument_permitted("option_writing", s(capital=500000.0)).allowed)

print("\n--- Position sizing ---")
# 2% of 10000 = 200 risk. Stop distance 10 → qty 20. Cost 20*200 = 4000 (40% cap = 4000, ok)
r = gr.size_position(entry_price=200.0, stop_price=190.0, target_price=230.0, state=s())
check("sizes correctly", r.approved and r.quantity == 20, f"got qty={r.quantity} {r.reasons}")
check("risk equals budget", r.approved and abs(r.risk_amount - 200.0) < 0.01,
      f"risk={r.risk_amount}")

r = gr.size_position(200.0, 190.0, 205.0, s())
check("rejects R:R below 1.5", not r.approved, str(r.reasons))

r = gr.size_position(200.0, 199.99, 250.0, s())
check("rejects when qty would be enormous but cost cap binds or buffer fails",
      not r.approved or r.position_cost <= 10000 * gr.MAX_POSITION_PCT,
      f"cost={r.position_cost}")

r = gr.size_position(5000.0, 4000.0, 8000.0, s())
check("rejects when qty computes to 0", not r.approved and r.quantity == 0, str(r.reasons))

r = gr.size_position(200.0, 190.0, 230.0, s(capital=8500.0))
check("smaller size when de-risked at orange",
      r.approved and r.quantity == 8, f"qty={r.quantity} {r.reasons}")

r = gr.size_position(100.0, 99.0, 100.4, s())
check("rejects when target does not clear costs", not r.approved, str(r.reasons))

r = gr.size_position(200.0, 210.0, 230.0, s())
check("rejects long with stop above entry", not r.approved, str(r.reasons))

print("\n--- Full order validation ---")
gate, sizing = gr.validate_order("INFY", "NSE", "BUY", 200.0, 190.0, 230.0, state=s())
check("valid order passes", gate.allowed and sizing.approved,
      f"{gate.reasons} {sizing.reasons}")

gate, sizing = gr.validate_order(
    "INFY", "NSE", "BUY", 200.0, 190.0, 230.0, instrument_type="long_options", state=s())
check("option order blocked at tier 0", not gate.allowed, str(gate.reasons))

dup = s(open_positions=[{"symbol": "INFY", "open_risk": 100}])
gate, sizing = gr.validate_order("INFY", "NSE", "BUY", 200.0, 190.0, 230.0, state=dup)
check("blocks averaging into existing position", not gate.allowed, str(gate.reasons))

gate, sizing = gr.validate_order("INFY", "MCX", "BUY", 200.0, 190.0, 230.0, state=s())
check("blocks wrong exchange for equity", not gate.allowed, str(gate.reasons))

gate, sizing = gr.validate_order(
    "INFY", "NSE", "BUY", 200.0, 190.0, 230.0, state=s(capital=8000.0))
check("blocks everything at red drawdown", not gate.allowed, str(gate.reasons))

print("\n--- Mandate: personal holdings are off-limits ---")
# Regression for the first live run: the account holds 21 personal positions worth
# ₹5.7 lakh. Those are not the agent's capital and must not be tradeable.
personal = s()
personal["broker_snapshot"] = {
    "total_account_value": 570447.95,
    "free_cash": -88.50,
    "unmanaged_symbols": ["INFY", "ITC", "MARUTI", "BRITANNIA", "SGBAUG28V-GB"],
}
gate, sizing = gr.validate_order("INFY", "NSE", "BUY", 200.0, 190.0, 230.0, state=personal)
check("blocks trading a personal holding", not gate.allowed, str(gate.reasons))
check("block names commingling as the reason",
      any("commingle" in r for r in gate.reasons), str(gate.reasons))

gate, sizing = gr.validate_order("TCS", "NSE", "BUY", 200.0, 190.0, 230.0, state=personal)
check("still allows a symbol outside the personal book", gate.allowed and sizing.approved,
      f"{gate.reasons} {sizing.reasons}")

approved = copy.deepcopy(personal)
approved["overlap_approved_symbols"] = ["INFY"]
gate, sizing = gr.validate_order("INFY", "NSE", "BUY", 200.0, 190.0, 230.0, state=approved)
check("allows an explicitly approved overlap", gate.allowed and sizing.approved,
      f"{gate.reasons} {sizing.reasons}")

gate, sizing = gr.validate_order("ITC", "NSE", "BUY", 200.0, 190.0, 230.0, state=approved)
check("approval is per-symbol, not blanket", not gate.allowed, str(gate.reasons))

check("capital stays the allocation, not the account value",
      personal["capital"] == 10000.0)
check("sizing uses allocation not account balance",
      gr.size_position(200.0, 190.0, 230.0, personal).quantity == 20,
      "would be ~1140 if it sized against ₹5.7 lakh")

print("\n--- Transaction costs (the DP-charge bug) ---")
from engine import costs as ct  # noqa: E402

cb = ct.equity_round_trip(500.0, 13)   # ₹6,500 delivery position
check("DP charge is included at all", cb.dp > 0, f"dp={cb.dp}")
check("round trip on ₹6,500 is ~0.79%, not the old 0.22%",
      0.7 < cb.pct_of_turnover < 0.9, f"got {cb.pct_of_turnover:.3f}%")
check("DP fee is a large share of small-trade cost",
      cb.dp / cb.total > 0.3, f"dp is {cb.dp / cb.total:.0%} of total")

cb_intra = ct.equity_round_trip(500.0, 13, intraday=True)
check("intraday has no DP charge", cb_intra.dp == 0.0)
check("intraday is cheaper than delivery", cb_intra.total < cb.total)

big = ct.equity_round_trip(500.0, 333)  # ₹166,500
check("cost % falls with size (flat fees amortise)",
      big.pct_of_turnover < cb.pct_of_turnover,
      f"{big.pct_of_turnover:.3f}% vs {cb.pct_of_turnover:.3f}%")

opt = ct.options_round_trip(premium=120.0, lot_size=65)
check("options carry no DP charge", opt.dp == 0.0)
check("flat ₹20/order dominates small option trades",
      opt.brokerage == 40.0, f"got {opt.brokerage}")

check("nonsense input fails the clearance test safely",
      ct.cost_as_pct(0.0, 0) == 1.0)

# A tight-stop trade at minimum 1.5:1 makes very little per share, so once the flat DP
# fee and ₹20 brokerage cap are priced in it cannot pay for itself. This is exactly the
# class of trade the old 0.22% estimate waved through.
marginal = gr.size_position(entry_price=500.0, stop_price=495.0, target_price=507.5, state=s())
check("marginal trade rejected once real charges are priced",
      not marginal.approved and any("charges" in r for r in marginal.reasons),
      f"approved={marginal.approved} qty={marginal.quantity} {marginal.reasons}")

# ...while a trade with a proper move still passes.
healthy = gr.size_position(entry_price=500.0, stop_price=485.0, target_price=530.0, state=s())
check("trade with a real move still passes the cost hurdle",
      healthy.approved, f"{healthy.reasons}")

print("\n--- Status summary ---")
st = gr.status_summary(s())
check("summary reports tier 0", st["tier"] == 0)
check("summary allows opening when clean", st["can_open_new_positions"])
check("summary risk budget is 200", abs(st["risk_budget_per_trade"] - 200.0) < 0.01)
check("summary ladder thresholds correct",
      st["ladder_thresholds"]["red"] == 8000.0, str(st["ladder_thresholds"]))

print(f"\n{'=' * 46}")
print(f"  {PASSED} passed, {FAILED} failed")
print(f"{'=' * 46}\n")
sys.exit(1 if FAILED else 0)
