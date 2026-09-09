"""
Writing to the memory files.

Each cron run is a fresh process with no memory of the last one, so continuity lives
entirely in these files. This module is how a run leaves a trace the next run can use.

Logs are append-only and newest-first. Nothing here ever deletes history — the trade log
is the dataset the weekly review computes statistics on, and a log that gets quietly
rewritten is worse than no log at all.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Optional

from .guardrails import load_state, save_state, drawdown_level, drawdown_pct

PROJECT_ROOT = Path(__file__).parent.parent
MEMORY = PROJECT_ROOT / "memory"
TRADE_LOG = MEMORY / "trade_log.md"
RESEARCH_LOG = MEMORY / "research_log.md"
PORTFOLIO_MD = MEMORY / "portfolio_state.md"

# Machine-readable, append-only trade record. The markdown log is for humans; this is
# what engine/stats.py computes from. Statistics must never be derived by reading prose.
TRADES_JSONL = MEMORY / "trades.jsonl"
REGIME_JSONL = MEMORY / "regime_log.jsonl"

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def now_ist() -> dt.datetime:
    return dt.datetime.now(IST)


def _timestamp() -> str:
    return now_ist().strftime("%Y-%m-%d %H:%M IST")


def _prepend_entry(path: Path, entry: str) -> None:
    """Insert a new entry directly below the file's header block, keeping newest first."""
    if not path.exists():
        path.write_text(f"# {path.stem.replace('_', ' ').title()}\n\n{entry}\n")
        return

    content = path.read_text()
    lines = content.split("\n")

    # Find the end of the header/comment preamble: first blank line after any HTML comment
    insert_at = 0
    in_comment = False
    for i, line in enumerate(lines):
        if line.strip().startswith("<!--"):
            in_comment = True
        if in_comment and "-->" in line:
            in_comment = False
            insert_at = i + 1
            break
        if line.startswith("# ") and insert_at == 0:
            insert_at = i + 1

    head = "\n".join(lines[:insert_at])
    tail = "\n".join(lines[insert_at:])
    path.write_text(f"{head}\n\n{entry}\n{tail}")


# ---------------------------------------------------------------------------
# Trade log
# ---------------------------------------------------------------------------

def log_trade(
    action: str,               # BUY | SELL | EXIT | NO-TRADE | REJECTED
    symbol: str,
    thesis: str,
    regime: str = "",
    entry: Optional[float] = None,
    stop: Optional[float] = None,
    target: Optional[float] = None,
    quantity: Optional[int] = None,
    risk_amount: Optional[float] = None,
    reward_risk: Optional[float] = None,
    order_id: Optional[str] = None,
    playbook: str = "",
    notes: str = "",
) -> None:
    """Record a trade decision. Called BEFORE fill confirmation is awaited, so that a
    crash mid-order still leaves a record (guardrails.md §7)."""
    lines = [f"## {_timestamp()} — [{action.upper()}] {symbol}"]
    if regime:
        lines.append(f"- Regime at decision: {regime}")
    if playbook:
        lines.append(f"- Playbook: {playbook}")
    lines.append(f"- Thesis: {thesis}")
    if entry is not None:
        lines.append(f"- Entry: ₹{entry:,.2f}")
    if stop is not None:
        lines.append(f"- Stop-loss: ₹{stop:,.2f}")
    if target is not None:
        lines.append(f"- Target: ₹{target:,.2f}")
    if quantity is not None:
        lines.append(f"- Quantity: {quantity}")
    if risk_amount is not None:
        lines.append(f"- Risk (1R): ₹{risk_amount:,.2f}")
    if reward_risk is not None:
        lines.append(f"- Reward:Risk: {reward_risk:.2f}")
    if order_id:
        lines.append(f"- Order ID: {order_id}")
    if notes:
        lines.append(f"- Notes: {notes}")
    lines.append("- Outcome: _(pending)_")
    lines.append("- Lesson: _(pending)_")

    _prepend_entry(TRADE_LOG, "\n".join(lines))


def record(kind: str, path: Path = TRADES_JSONL, **fields) -> None:
    """Append one structured record. Append-only by design — the statistics the weekly
    review depends on are worthless if history can be quietly rewritten."""
    import json
    row = {"ts": now_ist().isoformat(timespec="seconds"), "kind": kind, **fields}
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(row, default=str) + "\n")


def record_entry(symbol: str, side: str, quantity: int, entry: float, stop: float,
                 target: float, risk_amount: float, regime: str, playbook: str,
                 thesis: str, order_id: str = "") -> None:
    record("ENTRY", symbol=symbol, side=side.upper(), quantity=quantity, entry=entry,
           stop=stop, target=target, risk_amount=risk_amount, regime=regime,
           playbook=playbook, thesis=thesis, order_id=order_id)


def record_exit(symbol: str, exit_price: float, pnl: float, r_multiple: float,
                regime: str = "", reason: str = "", holding_days: Optional[int] = None) -> None:
    record("EXIT", symbol=symbol, exit_price=exit_price, pnl=pnl,
           r_multiple=r_multiple, regime=regime, reason=reason,
           holding_days=holding_days)


def record_rejection(symbol: str, reasons: list[str], regime: str = "") -> None:
    record("REJECTED", symbol=symbol, reasons=reasons, regime=regime)


def record_regime(regime: str, playbook: str, confidence: str, evidence: dict,
                  shadow: Optional[dict] = None) -> None:
    """Log every regime call, including the shadow HMM classification when available.
    This is the dataset that eventually answers 'is the fancier model actually better?'"""
    record("REGIME", path=REGIME_JSONL, regime=regime, playbook=playbook,
           confidence=confidence, evidence=evidence, shadow=shadow)


def log_no_trade(reason: str, regime: str = "") -> None:
    """A decision not to trade is a decision, and belongs in the record. Days with no
    trades are expected and unpunished — but they must be explained."""
    log_trade("NO-TRADE", "—", thesis=reason, regime=regime)


# ---------------------------------------------------------------------------
# Research log
# ---------------------------------------------------------------------------

def log_research(cycle: str, regime: str, content: str) -> None:
    entry = f"## {_timestamp()} — [{cycle}]\n- Regime: {regime}\n\n{content.strip()}"
    _prepend_entry(RESEARCH_LOG, entry)


# ---------------------------------------------------------------------------
# Portfolio state (machine state + human-readable mirror)
# ---------------------------------------------------------------------------

def roll_day_if_needed(state: Optional[dict] = None) -> dict:
    """At the start of a new trading day, snapshot the day's opening capital and reset
    daily counters. Also rolls the week on Mondays."""
    state = state or load_state()
    today = now_ist().date().isoformat()
    day = state.setdefault("day", {})

    if day.get("date") != today:
        # New day — carry yesterday's result into the losing-day streak first.
        prev_pnl = day.get("realized_pnl", 0.0)
        if day.get("date") is not None:
            if prev_pnl < 0:
                state["consecutive_losing_days"] = state.get("consecutive_losing_days", 0) + 1
            elif prev_pnl > 0:
                state["consecutive_losing_days"] = 0

        state["day"] = {
            "date": today,
            "starting_capital": state["capital"],
            "realized_pnl": 0.0,
            "trades_taken": 0,
            "process_grade": None,
        }

        week = state.setdefault("week", {})
        if now_ist().weekday() == 0 or not week.get("start_date"):
            state["week"] = {"start_date": today, "starting_capital": state["capital"]}

        save_state(state)
    return state


def update_capital(capital: float, cash_available: float, state: Optional[dict] = None) -> dict:
    """Update capital from the broker's actual numbers and ratchet peak capital.
    Peak only ever goes up — that's what makes the drawdown ladder meaningful."""
    state = state or load_state()
    state["capital"] = round(float(capital), 2)
    state["cash_available"] = round(float(cash_available), 2)
    if state["capital"] > state.get("peak_capital", 0):
        state["peak_capital"] = state["capital"]
    state["drawdown_level"] = drawdown_level(state)
    save_state(state)
    return state


def add_position(
    symbol: str, side: str, quantity: int, entry: float, stop: float,
    target: float, open_risk: float, thesis: str = "", state: Optional[dict] = None,
) -> dict:
    state = state or load_state()
    state.setdefault("open_positions", []).append({
        "symbol": symbol,
        "side": side.upper(),
        "quantity": quantity,
        "entry": round(entry, 2),
        "stop": round(stop, 2),
        "target": round(target, 2),
        "open_risk": round(open_risk, 2),
        "thesis": thesis,
        "opened": now_ist().isoformat(timespec="seconds"),
    })
    state.setdefault("day", {}).setdefault("trades_taken", 0)
    state["day"]["trades_taken"] += 1
    save_state(state)
    return state


def close_position(symbol: str, exit_price: float, costs: float = 0.0,
                   state: Optional[dict] = None) -> dict:
    state = state or load_state()
    remaining, realized = [], 0.0
    for pos in state.get("open_positions", []):
        if pos["symbol"] != symbol:
            remaining.append(pos)
            continue
        direction = 1 if pos["side"] == "BUY" else -1
        realized += (exit_price - pos["entry"]) * pos["quantity"] * direction - costs

    state["open_positions"] = remaining
    state["realized_pnl_alltime"] = round(state.get("realized_pnl_alltime", 0.0) + realized, 2)
    state["total_costs_alltime"] = round(state.get("total_costs_alltime", 0.0) + costs, 2)
    state.setdefault("day", {})
    state["day"]["realized_pnl"] = round(state["day"].get("realized_pnl", 0.0) + realized, 2)
    save_state(state)
    return state


def set_pause(paused: bool, reason: str = "", awaiting_ack: bool = False,
              state: Optional[dict] = None) -> dict:
    state = state or load_state()
    state["trading_paused"] = bool(paused)
    state["pause_reason"] = reason or None
    state["awaiting_human_ack"] = bool(awaiting_ack)
    save_state(state)
    return state


def render_portfolio_md(state: Optional[dict] = None) -> None:
    """Regenerate the human-readable mirror of state.json."""
    state = state or load_state()
    cap, peak = state["capital"], state["peak_capital"]
    dd = drawdown_pct(state) * 100
    level = drawdown_level(state)
    day = state.get("day", {})
    day_start = day.get("starting_capital") or cap

    emoji = {"NORMAL": "🟢", "AMBER": "🟡", "ORANGE": "🟠", "RED": "🔴"}[level]

    positions_md = "None."
    if state.get("open_positions"):
        rows = ["| Symbol | Side | Qty | Entry | Stop | Target | Open risk |",
                "|---|---|---|---|---|---|---|"]
        for p in state["open_positions"]:
            rows.append(
                f"| {p['symbol']} | {p['side']} | {p['quantity']} | ₹{p['entry']:,.2f} | "
                f"₹{p['stop']:,.2f} | ₹{p['target']:,.2f} | ₹{p['open_risk']:,.2f} |"
            )
        positions_md = "\n".join(rows)

    snap = state.get("broker_snapshot", {}) or {}
    unmanaged = snap.get("unmanaged_symbols") or []
    unmanaged_md = ""
    if unmanaged:
        unmanaged_md = (
            f"\n## Outside the agent's mandate (DO NOT TRADE)\n"
            f"The brokerage account also holds ₹{snap.get('total_account_value') or 0:,.2f} "
            f"across {len(unmanaged)} personal positions. These are **not** the agent's "
            f"capital and **not** its positions:\n\n"
            f"`{', '.join(unmanaged)}`\n\n"
            f"Broker free cash: ₹{snap.get('free_cash') or 0:,.2f}\n"
        )

    content = f"""---
purpose: Human-readable mirror of memory/state.json (which is the machine source of truth).
Regenerated automatically at the end of every run — do not hand-edit.
last_updated: {now_ist().strftime('%Y-%m-%d %H:%M IST')}
---

# Portfolio State

## Capital (the agent's mandate only)
- **Allocated capital: ₹{state.get('allocated_capital', 0):,.2f}** ← set by Vaibhav
- Current book value: ₹{cap:,.2f}
- Spendable cash: ₹{state.get('cash_available', 0):,.2f}
- **Peak capital: ₹{peak:,.2f}**
- Realized P&L (all-time): ₹{state.get('realized_pnl_alltime', 0):,.2f}
- Total costs paid (all-time): ₹{state.get('total_costs_alltime', 0):,.2f}
{unmanaged_md}

## Open positions
{positions_md}

## Today ({day.get('date') or '—'})
- Day's starting capital: ₹{day_start:,.2f}
- Realized P&L today: ₹{day.get('realized_pnl', 0):,.2f}
- Trades taken today: {day.get('trades_taken', 0)}
- Process grade: {day.get('process_grade') or '—'}

## Drawdown ladder
**Current drawdown from peak: {dd:.2f}%** — level: {emoji} **{level}**

| Level | Trigger | Capital at trigger | Status |
|---|---|---|---|
| 🟡 Amber | −10% | ₹{peak * 0.90:,.2f} | {'REACHED' if dd >= 10 else 'Not reached'} |
| 🟠 Orange | −15% | ₹{peak * 0.85:,.2f} | {'REACHED' if dd >= 15 else 'Not reached'} |
| 🔴 Red | −20% | ₹{peak * 0.80:,.2f} | {'REACHED' if dd >= 20 else 'Not reached'} |

- Consecutive losing days: {state.get('consecutive_losing_days', 0)}
- Trading paused?: **{'yes — ' + (state.get('pause_reason') or '') if state.get('trading_paused') else 'no'}**
- Awaiting human acknowledgement?: {'yes' if state.get('awaiting_human_ack') else 'no'}
"""
    PORTFOLIO_MD.write_text(content)
