"""
THE ENFORCEMENT LAYER.

Every rule in memory/guardrails.md is implemented here as code. Nothing else in this
project is allowed to place an order — order placement goes through engine/execute.py,
which calls into this module and refuses anything that fails.

This is deliberately NOT left to the agent's judgment. The agent reasons about markets;
this file decides what is permitted. A model that becomes convinced a trade is brilliant
still cannot get past these functions.

If you change a limit here, change memory/guardrails.md to match — and only Vaibhav may
authorise either.
"""

from __future__ import annotations

import json
import math
import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).parent.parent
STATE_FILE = PROJECT_ROOT / "memory" / "state.json"

# ---------------------------------------------------------------------------
# Limits — these mirror memory/guardrails.md exactly.
# ---------------------------------------------------------------------------

RISK_PER_TRADE = 0.02          # 2% of capital risked per trade (1% when de-risked)
RISK_PER_TRADE_DERISKED = 0.01

DAILY_LOSS_CAP = 0.05          # 5% of the day's starting capital
WEEKLY_LOSS_CAP = 0.10         # 10% of the week's starting capital

DD_AMBER = 0.10                # pause + diagnostic
DD_ORANGE = 0.15               # pause + audit + risk halved, human ack required
DD_RED = 0.20                  # full stop, human decision required
DD_RECOVERY = 0.05             # back within 5% of peak → restore normal risk

MAX_OPEN_POSITIONS = 3
MAX_TOTAL_OPEN_RISK = 0.06     # sum of open risk across all positions, as % of capital
MAX_POSITION_PCT = 0.40        # no single position > 40% of capital
MIN_CASH_BUFFER_ABS = 1000.0
MIN_CASH_BUFFER_PCT = 0.10
MIN_REWARD_RISK = 1.5
COST_CLEARANCE_MULTIPLE = 3.0  # expected move must be >= 3x round-trip cost

# Capital tiers — which instruments are permitted at what account size.
#
# Tier 1 was raised from ₹25,000 to ₹50,000 once real option premiums were checked
# against the 25%-of-capital rule: a near-ATM Nifty lot costs ₹6,500-16,250, so at
# ₹25,000 the cap of ₹6,250 excluded almost every tradeable strike. A tier you cannot
# actually trade in is not a tier, it is a false promise.
TIER_1_CAPITAL = 50_000        # long options unlock (≤25% of capital in premium)
TIER_2_CAPITAL = 150_000       # defined-risk spreads unlock
TIER_3_CAPITAL = 400_000       # futures / writing become discussable (human approval)

EQUITY_SEGMENTS = {"NSE", "BSE"}

# Transaction costs are computed per-position in engine/costs.py, not estimated with a
# flat percentage. See the comment in size_position for why that mattered.


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_state() -> dict:
    """Read the machine-readable state. This is the source of truth for all limits.

    memory/portfolio_state.md is the human-readable mirror; this JSON is what the code
    trusts. If they disagree, that is a circuit-breaker condition (guardrails.md §6).
    """
    if not STATE_FILE.exists():
        raise RuntimeError(f"State file missing: {STATE_FILE}. Cannot trade blind.")
    with open(STATE_FILE) as f:
        return json.load(f)


def save_state(state: dict) -> None:
    state["last_updated"] = dt.datetime.now().isoformat(timespec="seconds")
    tmp = STATE_FILE.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    tmp.replace(STATE_FILE)  # atomic — never leave a half-written state file


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

@dataclass
class GateResult:
    """Outcome of a guardrail check. `allowed` is the only thing callers may act on."""
    allowed: bool
    reasons: list[str] = field(default_factory=list)
    detail: dict = field(default_factory=dict)

    def __str__(self) -> str:
        head = "ALLOWED" if self.allowed else "BLOCKED"
        if not self.reasons:
            return head
        return head + ": " + "; ".join(self.reasons)


@dataclass
class SizingResult:
    approved: bool
    quantity: int = 0
    risk_amount: float = 0.0
    position_cost: float = 0.0
    reward_risk: float = 0.0
    reasons: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# The equity risk sizing and the drawdown ladder run against.
#
# A MIGRATED state carries a `managed` block (state["managed"]) whose
# `portfolio_value` is recomputed from live broker data every sync
# (engine.execute.sync_from_broker):
#     managed equity = managed cash + Σ(managed position qty × current LTP)
# and whose `peak_growth` ratchets on `growth = portfolio_value − Σ(recorded
# cash-flow events)` — so a deposit or withdrawal never moves the drawdown.
#
# A state that PRE-DATES the model — and every fixture in the frozen
# tests/test_guardrails.py — has no `managed` block and falls back to the
# legacy `capital` / `peak_capital` fields, byte-for-byte unchanged.
# See docs/CAPITAL_MODEL.md.
# ---------------------------------------------------------------------------

def _managed(state: dict) -> dict:
    return state.get("managed") or {}


def managed_equity(state: dict) -> float:
    """Current managed portfolio equity — the figure risk is sized against."""
    m = _managed(state)
    v = m.get("portfolio_value")
    return float(v) if v is not None else float(state.get("capital", 0.0))


def _cashflow_total(state: dict) -> float:
    return sum(float(e.get("amount", 0.0)) for e in _managed(state).get("cashflow_events") or [])


def _ladder_peak(state: dict) -> float:
    """Peak equity the AMBER/ORANGE/RED thresholds are drawn from. On the
    dynamic model this is (Σ cash-flows + peak strategy growth), i.e. the
    equity high after removing external deposits/withdrawals."""
    m = _managed(state)
    if m.get("peak_growth") is not None:
        return _cashflow_total(state) + float(m["peak_growth"])
    return float(state.get("peak_capital") or 0.0)


# ---------------------------------------------------------------------------
# Drawdown ladder (guardrails.md §1a)
# ---------------------------------------------------------------------------

def drawdown_pct(state: dict) -> float:
    m = _managed(state)
    if m.get("portfolio_value") is not None and m.get("peak_growth") is not None:
        # cash-flow-adjusted: drawdown is measured on strategy P&L only, so a
        # user deposit/withdrawal (which shifts portfolio_value and the
        # cash-flow total by the same amount) leaves it unchanged.
        flow = _cashflow_total(state)
        growth = float(m["portfolio_value"]) - flow
        peak_growth = float(m["peak_growth"])
        base = flow + peak_growth
        return 0.0 if base <= 0 else max(0.0, (peak_growth - growth) / base)
    peak = state.get("peak_capital") or 0.0                       # legacy path
    if peak <= 0:
        return 0.0
    return max(0.0, (peak - float(state.get("capital", 0.0))) / peak)


def drawdown_level(state: dict) -> str:
    dd = drawdown_pct(state)
    if dd >= DD_RED:
        return "RED"
    if dd >= DD_ORANGE:
        return "ORANGE"
    if dd >= DD_AMBER:
        return "AMBER"
    return "NORMAL"


def current_risk_per_trade(state: dict) -> float:
    """Risk drops to 1% at Orange or after a Red restart, until recovery to within 5%
    of peak. Sizing tracks conviction, and after a serious drawdown conviction is lower."""
    level = drawdown_level(state)
    if level in ("ORANGE", "RED"):
        return RISK_PER_TRADE_DERISKED
    # Explicit de-risk flag persists through recovery until back near peak.
    if state.get("risk_per_trade_pct") == RISK_PER_TRADE_DERISKED:
        if drawdown_pct(state) > DD_RECOVERY:
            return RISK_PER_TRADE_DERISKED
    return RISK_PER_TRADE


# ---------------------------------------------------------------------------
# The main gate — called at the start of EVERY run, before any market analysis
# ---------------------------------------------------------------------------

def check_can_open_positions(state: Optional[dict] = None) -> GateResult:
    """May this run open NEW positions at all?

    Managing/closing existing positions is always permitted (a stop must always be
    honourable). This gate governs new entries only.
    """
    state = state or load_state()
    reasons: list[str] = []

    # --- Explicit pause / human acknowledgement ---
    if state.get("trading_paused"):
        reasons.append(f"trading paused: {state.get('pause_reason') or 'no reason recorded'}")
    if state.get("awaiting_human_ack"):
        reasons.append("awaiting human acknowledgement before resuming")

    # --- Drawdown ladder ---
    level = drawdown_level(state)
    dd = drawdown_pct(state)
    if level == "RED":
        reasons.append(f"RED drawdown {dd:.1%} (>= {DD_RED:.0%}) — full stop, human decision required")
    elif level == "ORANGE":
        reasons.append(f"ORANGE drawdown {dd:.1%} (>= {DD_ORANGE:.0%}) — human ack required to resume")
    elif level == "AMBER":
        reasons.append(f"AMBER drawdown {dd:.1%} (>= {DD_AMBER:.0%}) — diagnostic required before new entries")

    equity = managed_equity(state)

    # --- Daily loss cap ---
    day = state.get("day", {})
    day_start = day.get("starting_managed_equity") or day.get("starting_capital") or equity
    day_pnl = day.get("realized_pnl", 0.0)
    if day_start > 0 and day_pnl < 0 and abs(day_pnl) / day_start >= DAILY_LOSS_CAP:
        reasons.append(f"daily loss cap hit ({day_pnl:.0f} on {day_start:.0f}, cap {DAILY_LOSS_CAP:.0%})")

    # --- Weekly loss cap ---
    week = state.get("week", {})
    week_start = week.get("starting_managed_equity") or week.get("starting_capital") or equity
    week_pnl = equity - week_start
    if week_start > 0 and week_pnl < 0 and abs(week_pnl) / week_start >= WEEKLY_LOSS_CAP:
        reasons.append(f"weekly loss cap hit ({week_pnl:.0f} on {week_start:.0f}, cap {WEEKLY_LOSS_CAP:.0%})")

    # --- Consecutive losing days ---
    if state.get("consecutive_losing_days", 0) >= 2:
        reasons.append(f"{state['consecutive_losing_days']} consecutive losing days — review required")

    # --- Position count ---
    open_positions = state.get("open_positions", [])
    if len(open_positions) >= MAX_OPEN_POSITIONS:
        reasons.append(f"already at max {MAX_OPEN_POSITIONS} open positions")

    # --- Aggregate open risk ---
    total_open_risk = sum(p.get("open_risk", 0.0) for p in open_positions)
    if equity > 0 and total_open_risk / equity >= MAX_TOTAL_OPEN_RISK:
        reasons.append(
            f"total open risk {total_open_risk:.0f} is at/over "
            f"{MAX_TOTAL_OPEN_RISK:.0%} of managed equity"
        )

    return GateResult(
        allowed=not reasons,
        reasons=reasons,
        detail={
            "drawdown_pct": round(dd, 4),
            "drawdown_level": level,
            "risk_per_trade": current_risk_per_trade(state),
            "open_positions": len(open_positions),
            "total_open_risk": round(total_open_risk, 2),
            "capital": equity,
            "peak_capital": _ladder_peak(state),
        },
    )


# ---------------------------------------------------------------------------
# Instrument permissions (capital tiers)
# ---------------------------------------------------------------------------

def allowed_instruments(state: Optional[dict] = None) -> dict:
    """What this account size may trade. See strategy.md capital tiers.
    'Size' here is current managed equity (dynamic, broker-derived), not a
    fixed capital base — TIER_*_CAPITAL stay as policy thresholds."""
    state = state or load_state()
    capital = managed_equity(state)

    perms = {
        "equity_cash": True,
        "long_options": capital >= TIER_1_CAPITAL,
        "option_spreads": capital >= TIER_2_CAPITAL,
        "futures": False,          # Tier 3 + explicit human approval, never automatic
        "option_writing": False,   # forbidden at any capital without written approval
        "tier": 0,
    }
    if capital >= TIER_3_CAPITAL:
        perms["tier"] = 3
    elif capital >= TIER_2_CAPITAL:
        perms["tier"] = 2
    elif capital >= TIER_1_CAPITAL:
        perms["tier"] = 1
    return perms


def check_instrument_permitted(instrument_type: str, state: Optional[dict] = None) -> GateResult:
    """instrument_type: equity_cash | long_options | option_spreads | futures | option_writing"""
    state = state or load_state()
    perms = allowed_instruments(state)

    if instrument_type not in perms:
        return GateResult(False, [f"unknown instrument type '{instrument_type}'"])

    if not perms.get(instrument_type):
        return GateResult(
            False,
            [
                f"'{instrument_type}' not permitted at managed equity "
                f"₹{managed_equity(state):,.0f} (current tier {perms['tier']})"
            ],
            detail=perms,
        )
    return GateResult(True, detail=perms)


# ---------------------------------------------------------------------------
# Position sizing (guardrails.md §3) — formulaic, never discretionary
# ---------------------------------------------------------------------------

def size_position(
    entry_price: float,
    stop_price: float,
    target_price: float,
    state: Optional[dict] = None,
    intraday: bool = False,
) -> SizingResult:
    """Compute quantity from the risk budget and stop distance, then apply every
    structural check. Returns approved=False (and quantity 0) if anything fails.

    There is no 'round up to at least 1' — if the arithmetic says zero, there is no trade.
    """
    state = state or load_state()
    capital = managed_equity(state)   # current managed equity — risk % is applied to THIS
    reasons: list[str] = []

    # --- Basic sanity ---
    if entry_price <= 0 or stop_price <= 0 or target_price <= 0:
        return SizingResult(False, reasons=["prices must be positive"])

    is_long = target_price > entry_price
    if is_long and stop_price >= entry_price:
        return SizingResult(False, reasons=["long trade needs stop below entry"])
    if not is_long and stop_price <= entry_price:
        return SizingResult(False, reasons=["short trade needs stop above entry"])

    stop_distance = abs(entry_price - stop_price)
    reward_distance = abs(target_price - entry_price)
    if stop_distance <= 0:
        return SizingResult(False, reasons=["stop distance is zero"])

    # --- Minimum reward:risk ---
    reward_risk = reward_distance / stop_distance
    if reward_risk < MIN_REWARD_RISK:
        reasons.append(
            f"reward:risk {reward_risk:.2f} below minimum {MIN_REWARD_RISK}"
        )

    # --- Size from risk budget ---
    risk_pct = current_risk_per_trade(state)
    risk_amount = capital * risk_pct
    quantity = math.floor(risk_amount / stop_distance)

    if quantity < 1:
        reasons.append(
            f"computed quantity 0 (risk budget ₹{risk_amount:.0f} / stop distance "
            f"₹{stop_distance:.2f}) — no trade"
        )
        return SizingResult(False, 0, risk_amount, 0.0, reward_risk, reasons)

    position_cost = quantity * entry_price

    # --- Concentration cap (applied BEFORE costs are priced) ---
    if position_cost > capital * MAX_POSITION_PCT:
        # Shrink to fit rather than reject outright — but re-check it still clears 1 unit.
        quantity = math.floor((capital * MAX_POSITION_PCT) / entry_price)
        position_cost = quantity * entry_price
        if quantity < 1:
            reasons.append("position cannot fit inside 40% concentration cap")
            return SizingResult(False, 0, risk_amount, 0.0, reward_risk, reasons)

    # --- Cost clearance, in rupees, against ACTUAL computed charges ---
    # Ordering matters: this must run on the FINAL quantity, after any concentration
    # shrink. A smaller position pays the same flat DP fee and ₹20 brokerage cap, so
    # shrinking makes the cost hurdle proportionally harder — checking before the shrink
    # would wave through trades that no longer pay for themselves.
    #
    # The percentage estimate this replaced (0.22%) omitted the DP charge entirely. Flat
    # fees are invisible in a percentage model and dominate at small size; the true figure
    # at ₹10k is ~0.79%.
    from .costs import equity_round_trip
    charges = equity_round_trip(entry_price, quantity, intraday).total
    gross_profit_at_target = reward_distance * quantity
    if gross_profit_at_target < COST_CLEARANCE_MULTIPLE * charges:
        reasons.append(
            f"target profit ₹{gross_profit_at_target:.0f} does not clear "
            f"{COST_CLEARANCE_MULTIPLE:.0f}x round-trip charges of ₹{charges:.0f} "
            f"(brokerage, STT, flat DP fee, GST) — not worth making at this size"
        )

    # --- Cash buffer ---
    min_buffer = max(MIN_CASH_BUFFER_ABS, capital * MIN_CASH_BUFFER_PCT)
    cash_after = state.get("cash_available", capital) - position_cost
    if cash_after < min_buffer:
        reasons.append(
            f"would leave ₹{cash_after:.0f} cash, below required buffer ₹{min_buffer:.0f}"
        )

    # --- Aggregate open risk ceiling ---
    existing_risk = sum(p.get("open_risk", 0.0) for p in state.get("open_positions", []))
    new_total_risk = existing_risk + (quantity * stop_distance)
    if new_total_risk > capital * MAX_TOTAL_OPEN_RISK:
        reasons.append(
            f"total open risk would be ₹{new_total_risk:.0f}, over "
            f"{MAX_TOTAL_OPEN_RISK:.0%} of capital (₹{capital * MAX_TOTAL_OPEN_RISK:.0f})"
        )

    approved = not reasons
    return SizingResult(
        approved=approved,
        quantity=quantity if approved else 0,
        risk_amount=round(quantity * stop_distance, 2),
        position_cost=round(position_cost, 2),
        reward_risk=round(reward_risk, 2),
        reasons=reasons,
    )


# ---------------------------------------------------------------------------
# Full order validation — execute.py MUST call this and honour the result
# ---------------------------------------------------------------------------

def validate_order(
    symbol: str,
    exchange: str,
    side: str,
    entry_price: float,
    stop_price: float,
    target_price: float,
    instrument_type: str = "equity_cash",
    intraday: bool = False,
    state: Optional[dict] = None,
) -> tuple[GateResult, SizingResult]:
    """The single gate every new position passes through.

    Returns (gate, sizing). The order may proceed ONLY if gate.allowed and
    sizing.approved are both True.
    """
    state = state or load_state()
    reasons: list[str] = []

    # 1. May we open anything at all right now?
    gate = check_can_open_positions(state)
    if not gate.allowed:
        return gate, SizingResult(False, reasons=["blocked by guardrail gate"])

    # 2. Is this instrument permitted at this capital?
    instr = check_instrument_permitted(instrument_type, state)
    if not instr.allowed:
        return GateResult(False, instr.reasons, instr.detail), SizingResult(
            False, reasons=instr.reasons
        )

    # 3. Exchange sanity for equity
    if instrument_type == "equity_cash" and exchange not in EQUITY_SEGMENTS:
        reasons.append(f"exchange '{exchange}' not valid for equity cash")

    # 4. Side sanity
    if side.upper() not in ("BUY", "SELL"):
        reasons.append(f"invalid side '{side}'")

    # 5. Don't double up on a symbol already held
    for pos in state.get("open_positions", []):
        if pos.get("symbol") == symbol:
            reasons.append(
                f"already hold {symbol} — averaging into an existing position is forbidden"
            )

    # 6. Symbols Vaibhav already holds personally are off-limits by default.
    #
    #    Quantities merge at the broker, so overlapping creates two real problems that
    #    approval does not make go away — it only means Vaibhav accepted them:
    #      (a) FIFO tax lots: the agent exiting "its" shares realises Vaibhav's oldest
    #          lots on his tax return, at his holding period.
    #      (b) Rug risk: if he sells his own shares, the agent's stop may fail or cause
    #          short delivery on stock it believes it holds.
    #
    #    So: blocked unless he has explicitly approved that symbol via Telegram
    #    (`approve INFY`), which records informed consent to both.
    unmanaged = {s.upper() for s in
                 (state.get("broker_snapshot", {}) or {}).get("unmanaged_symbols", [])}
    approved_overlap = {s.upper() for s in state.get("overlap_approved_symbols", [])}
    if symbol.upper() in unmanaged and symbol.upper() not in approved_overlap:
        reasons.append(
            f"{symbol} is an existing personal holding outside the agent's mandate — "
            f"trading it would commingle the two books (FIFO tax lots, stop-loss risk). "
            f"Vaibhav can allow it by sending `approve {symbol.upper()}` on Telegram."
        )

    if reasons:
        return GateResult(False, reasons), SizingResult(False, reasons=reasons)

    # 6. Size it
    sizing = size_position(entry_price, stop_price, target_price, state, intraday)
    if not sizing.approved:
        return GateResult(False, sizing.reasons), sizing

    return GateResult(True, detail=gate.detail), sizing


# ---------------------------------------------------------------------------
# Summary for the agent's briefing
# ---------------------------------------------------------------------------

def status_summary(state: Optional[dict] = None) -> dict:
    """Everything the agent needs to know about its constraints, in one dict."""
    state = state or load_state()
    gate = check_can_open_positions(state)
    perms = allowed_instruments(state)
    day = state.get("day", {})
    week = state.get("week", {})
    capital = managed_equity(state)                       # current managed equity
    ladder_peak = _ladder_peak(state)
    day_start = day.get("starting_managed_equity") or day.get("starting_capital") or capital
    week_start = week.get("starting_managed_equity") or week.get("starting_capital") or capital

    return {
        "can_open_new_positions": gate.allowed,
        "blocking_reasons": gate.reasons,
        "capital": capital,                               # = managed equity (dynamic)
        "managed_equity": capital,
        "peak_capital": ladder_peak,                      # cash-flow-adjusted peak
        "cash_available": state.get("cash_available", capital),
        "drawdown_pct": round(drawdown_pct(state) * 100, 2),
        "drawdown_level": drawdown_level(state),
        "risk_per_trade_pct": round(current_risk_per_trade(state) * 100, 2),
        "risk_budget_per_trade": round(capital * current_risk_per_trade(state), 2),
        "daily_loss_headroom": round(day_start * DAILY_LOSS_CAP + day.get("realized_pnl", 0.0), 2),
        "weekly_loss_headroom": round(week_start * WEEKLY_LOSS_CAP + (capital - week_start), 2),
        "open_positions": len(state.get("open_positions", [])),
        "max_open_positions": MAX_OPEN_POSITIONS,
        "total_open_risk": round(sum(p.get("open_risk", 0.0) for p in state.get("open_positions", [])), 2),
        "max_total_open_risk": round(capital * MAX_TOTAL_OPEN_RISK, 2),
        "tier": perms["tier"],
        "permitted_instruments": [k for k, v in perms.items() if v is True],
        "consecutive_losing_days": state.get("consecutive_losing_days", 0),
        "trading_paused": state.get("trading_paused", False),
        "awaiting_human_ack": state.get("awaiting_human_ack", False),
        "ladder_thresholds": {
            "amber": round(ladder_peak * (1 - DD_AMBER), 2),
            "orange": round(ladder_peak * (1 - DD_ORANGE), 2),
            "red": round(ladder_peak * (1 - DD_RED), 2),
        },
    }


if __name__ == "__main__":
    import pprint
    pprint.pprint(status_summary())
