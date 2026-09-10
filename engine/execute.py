"""
Order execution — THE ONLY PATH TO PLACING A TRADE.

The agent does not call the Kite API directly. It proposes a trade to this module, which
independently re-checks every guardrail and either places the order or refuses with
reasons. That separation is the whole safety design: the agent supplies judgment about
markets, this file supplies the veto.

CLI (this is how the agent invokes it):

    python -m engine.propose  --symbol INFY --side BUY --entry 1500 --stop 1450 \\
                              --target 1600 --thesis "..." --playbook momentum \\
                              --regime TRENDING_UP [--dry-run]

    python -m engine.execute sync      # reconcile local state with the broker
    python -m engine.execute status    # guardrail + portfolio snapshot as JSON
    python -m engine.execute close --symbol INFY --price 1550

Every path prints JSON so the agent can parse the outcome unambiguously.
"""

from __future__ import annotations

import sys
import json
import argparse
from pathlib import Path

from . import guardrails as gr
from . import journal as jr
from .broker import get_broker

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))


# ---------------------------------------------------------------------------
# Broker reconciliation
# ---------------------------------------------------------------------------

def sync_from_broker() -> dict:
    """Reconcile the agent's book against the broker, on the DYNAMIC
    broker-derived capital model (docs/CAPITAL_MODEL.md).

    Two figures, both recomputed from live broker data every sync — nothing
    is a stored/legacy/fixed rupee amount:

      CURRENT ACCOUNT VALUE  = broker free cash
                             + Σ(broker holding/position qty × current LTP)
      MANAGED EQUITY         = managed cash + Σ(managed position qty × current LTP)
        managed cash         = the broker free cash (all of it — no ring-fenced grant)
        managed positions    = the agent's own open positions + any holdings a
                               governed process PROMOTED into state["managed"]["symbols"]
                               (a sync never promotes)

    unmanaged symbols = everything at the broker the agent did not open — recorded
                        so guardrails refuse to trade them; still counted in ACCOUNT
                        VALUE and visible to research, never in MANAGED EQUITY.

    Risk sizing and the drawdown ladder run on MANAGED EQUITY. Deposits and
    withdrawals are recorded as external cash-flow events
    (engine.journal.record_cashflow_event), never inferred as strategy P&L, so
    the drawdown is immune to the user moving money in or out.

    A pre-migration state (no state["managed"] block) still takes the LEGACY
    path (allocated_capital + realised P&L), unchanged — see the `else` branch.

    A genuine mismatch is only one direction: a position the agent THINKS it holds
    that is absent at the broker.
    """
    broker = get_broker()
    try:
        broker_free_cash = broker.funds()
        holdings = broker.holdings()
        positions = broker.positions()
    except Exception as e:
        return {"ok": False, "broker": broker.name, "error": f"{type(e).__name__}: {e}",
                "action": "CIRCUIT BREAKER — cannot reach broker, do not trade this run"}

    # --- CURRENT ACCOUNT VALUE — always broker-derived, never a stored figure ------
    #   = current broker cash + Σ(current broker holding/position qty × current LTP)
    # holdings()/positions() are LTP-priced via the broker's confirmed quote
    # endpoint (engine.broker_indstocks._attach_live_prices); no stored/legacy
    # price is used. An unpriced line contributes 0 and is never guessed.
    holdings_mkt = sum(h.last_price * h.quantity for h in holdings if h.last_price > 0)
    positions_mkt = sum(p.last_price * abs(p.quantity) for p in positions if p.last_price > 0)
    account_total = broker_free_cash + holdings_mkt + positions_mkt

    by_sym = {h.symbol: h for h in holdings}
    by_sym.update({p.symbol: p for p in positions})
    broker_symbols = set(by_sym)

    state = jr.roll_day_if_needed()
    agent_symbols = {p["symbol"] for p in state.get("open_positions", [])}
    unmanaged = sorted(broker_symbols - agent_symbols)
    now_iso = jr.now_ist().isoformat(timespec="seconds")

    state["broker_snapshot"] = {
        "total_account_value": round(account_total, 2),
        "free_cash": round(broker_free_cash, 2),
        "unmanaged_symbols": unmanaged,
        "synced_at": now_iso,
    }

    mgr = state.get("managed")
    warnings: list[str] = []

    if mgr is not None:
        # === DYNAMIC BROKER-DERIVED MODEL (migrated state) =======================
        #   MANAGED EQUITY = current managed cash + Σ(managed position qty × LTP)
        # Managed cash is the whole of the broker free cash — there is NO
        # ring-fenced rupee grant. Managed positions = the agent's own open
        # positions plus any holdings a governed process has PROMOTED into
        # state["managed"]["symbols"] (empty by default; a sync never promotes).
        managed_syms = agent_symbols | {s.upper() for s in mgr.get("symbols", [])}
        managed_pos_mkt = 0.0
        managed_unpriced: list[str] = []
        for s in sorted(managed_syms):
            b = by_sym.get(s)
            if b is None:
                continue  # tracked/promoted but absent at broker -> a mismatch (below)
            if b.last_price > 0:
                managed_pos_mkt += b.last_price * abs(b.quantity)
            else:
                managed_unpriced.append(s)

        managed_cash = broker_free_cash
        managed_equity = managed_cash + managed_pos_mkt
        spendable = broker_free_cash              # every free rupee is the agent's to deploy

        prev_free_cash = mgr.get("free_cash")
        prev_valued_at = mgr.get("valued_at")

        state = jr.update_managed_equity(
            state, portfolio_value=managed_equity, free_cash=managed_cash,
            positions_market_value=managed_pos_mkt, now=now_iso)

        if managed_unpriced:
            warnings.append(
                f"managed position(s) had no live price this sync and are excluded from "
                f"managed equity until priced: {', '.join(managed_unpriced)}"
            )
        # An unexplained managed-cash move is almost always an unrecorded deposit
        # or withdrawal. WARN, never auto-adjust: a real deposit/withdrawal must be
        # recorded with engine.journal.record_cashflow_event so it counts as an
        # external cash flow, not strategy P&L.
        if prev_free_cash is not None:
            delta = round(managed_cash - float(prev_free_cash), 2)
            recorded_since = [e for e in (mgr.get("cashflow_events") or [])
                              if prev_valued_at and str(e.get("ts", "")) > str(prev_valued_at)]
            if abs(delta) > 1.0 and not recorded_since:
                warnings.append(
                    f"managed cash changed by ₹{delta:,.2f} since the last sync with no "
                    f"recorded cash-flow event — if you deposited or withdrew money, record "
                    f"it so it is not treated as strategy P&L."
                )

        # legacy response keys (engine.briefing / scripts read these; not modified)
        allocated = round(managed_cash, 2)
        agent_capital = round(managed_equity, 2)
        agent_notional_cash = managed_cash
    else:
        # === LEGACY MODEL (unmigrated state) — behaviour unchanged ===============
        allocated = float(state.get("allocated_capital", 10000.0))
        agent_capital = allocated + float(state.get("realized_pnl_alltime", 0.0))
        deployed = sum(p["entry"] * p["quantity"] for p in state.get("open_positions", []))
        agent_notional_cash = agent_capital - deployed
        spendable = min(agent_notional_cash, broker_free_cash)
        managed_equity = agent_capital
        managed_cash = max(spendable, 0.0)
        managed_pos_mkt = 0.0
        state = jr.update_capital(agent_capital, max(spendable, 0.0), state)

    # --- Real mismatch: agent thinks it holds something the broker doesn't show -----
    mismatches = []
    missing = sorted(agent_symbols - broker_symbols)
    if missing:
        mismatches.append(
            f"agent tracks positions absent at broker: {missing} — reconcile before trading"
        )

    if spendable <= 0:
        warnings.append(
            f"No spendable cash: broker free cash is ₹{broker_free_cash:,.2f}. The agent "
            f"cannot open any position until cash is available in the account."
        )
    elif mgr is None and spendable < agent_notional_cash:
        warnings.append(
            f"Agent's book says ₹{agent_notional_cash:,.2f} free but the account only has "
            f"₹{broker_free_cash:,.2f} — sizing is capped by the real cash."
        )

    jr.render_portfolio_md(state)

    return {
        "ok": True,
        "broker": broker.name,
        "agent_capital": round(agent_capital, 2),
        "allocated_capital": round(allocated, 2),
        "agent_spendable_cash": round(max(spendable, 0.0), 2),
        "agent_positions": sorted(agent_symbols),
        "broker_free_cash": round(broker_free_cash, 2),
        "account_total_value": round(account_total, 2),
        "managed_portfolio_value": round(managed_equity, 2),
        "managed_free_cash": round(managed_cash, 2),
        "managed_positions_market_value": round(managed_pos_mkt, 2),
        "drawdown_pct": round(gr.drawdown_pct(state), 4),
        "unmanaged_symbols": unmanaged,
        "unmanaged_count": len(unmanaged),
        "mismatches": mismatches,
        "warnings": warnings,
        "note": ("Account holds positions outside the agent's mandate; these are NOT "
                 "capital and must never be traded." if unmanaged else None),
        "blocking": ("STATE MISMATCH — do not open new positions; reconcile first"
                     if mismatches else None),
    }


# ---------------------------------------------------------------------------
# The gate + placement
# ---------------------------------------------------------------------------

def propose_trade(
    symbol: str,
    side: str,
    entry: float,
    stop: float,
    target: float,
    thesis: str,
    regime: str = "",
    playbook: str = "",
    exchange: str = "NSE",
    instrument_type: str = "equity_cash",
    intraday: bool = False,
    dry_run: bool = False,
) -> dict:
    """Validate a proposed trade and, if it passes every guardrail, place it.

    Returns a dict with `placed` (bool) and, when refused, the exact reasons — which the
    agent is expected to log and accept rather than work around.
    """
    symbol = symbol.upper().strip()

    state = jr.roll_day_if_needed()
    gate, sizing = gr.validate_order(
        symbol=symbol, exchange=exchange, side=side, entry_price=entry,
        stop_price=stop, target_price=target, instrument_type=instrument_type,
        intraday=intraday, state=state,
    )

    if not (gate.allowed and sizing.approved):
        reasons = list(dict.fromkeys(gate.reasons + sizing.reasons))
        jr.log_trade(
            "REJECTED", symbol, thesis=thesis, regime=regime, playbook=playbook,
            entry=entry, stop=stop, target=target,
            notes="Blocked by guardrails: " + "; ".join(reasons),
        )
        jr.record_rejection(symbol=symbol, reasons=reasons, regime=regime)
        return {
            "placed": False,
            "symbol": symbol,
            "reasons": reasons,
            "guidance": "This is final. Do not re-propose a variant to get around it; "
                        "log the rejection and move on.",
        }

    # Passed the gate. Log BEFORE placing, so a crash mid-order still leaves a record.
    jr.log_trade(
        side.upper(), symbol, thesis=thesis, regime=regime, playbook=playbook,
        entry=entry, stop=stop, target=target, quantity=sizing.quantity,
        risk_amount=sizing.risk_amount, reward_risk=sizing.reward_risk,
        notes="DRY RUN — no order sent" if dry_run else "",
    )

    if dry_run:
        return {
            "placed": False, "dry_run": True, "symbol": symbol,
            "would_place": {
                "quantity": sizing.quantity, "entry": entry, "stop": stop,
                "target": target, "risk": sizing.risk_amount,
                "position_cost": sizing.position_cost, "reward_risk": sizing.reward_risk,
            },
        }

    broker = get_broker()
    result = broker.place(
        symbol=symbol, side=side, quantity=sizing.quantity, price=entry,
        exchange=exchange, product="INTRADAY" if intraday else "CNC",
        segment="EQUITY", tag=f"agent-{playbook or 'x'}"[:100],
    )
    if not result.ok:
        jr.log_trade(
            "ORDER-FAILED", symbol, thesis=thesis, regime=regime,
            notes=f"Order rejected by broker: {result.error}. STOP — do not retry blindly.",
        )
        return {"placed": False, "symbol": symbol, "broker": broker.name,
                "error": result.error,
                "guidance": "Circuit breaker: log and stop. Do not retry automatically."}
    order_id = result.order_id

    # Protective stop that survives between runs.
    stop_res = broker.place_stop(
        symbol=symbol, side=side, quantity=sizing.quantity, trigger_price=stop,
        last_price=entry, exchange=exchange,
        product="INTRADAY" if intraday else "CNC",
    )
    stop_result = ({"ok": True, "id": stop_res.order_id} if stop_res.ok else
                   {"ok": False, "error": stop_res.error,
                    "ALERT": "STOP-LOSS NOT PLACED — position is unprotected. "
                             "Alert Vaibhav immediately and place the stop manually."})

    state = jr.add_position(
        symbol=symbol, side=side, quantity=sizing.quantity, entry=entry,
        stop=stop, target=target, open_risk=sizing.risk_amount, thesis=thesis,
    )
    jr.record_entry(
        symbol=symbol, side=side, quantity=sizing.quantity, entry=entry, stop=stop,
        target=target, risk_amount=sizing.risk_amount, regime=regime,
        playbook=playbook, thesis=thesis, order_id=str(order_id),
    )
    jr.render_portfolio_md(state)

    return {
        "placed": True,
        "symbol": symbol,
        "order_id": order_id,
        "quantity": sizing.quantity,
        "entry": entry,
        "stop": stop,
        "target": target,
        "risk_amount": sizing.risk_amount,
        "position_cost": sizing.position_cost,
        "reward_risk": sizing.reward_risk,
        "stop_order": stop_result,
    }


def close_position(symbol: str, price: float, exchange: str = "NSE",
                   intraday: bool = False, reason: str = "") -> dict:
    """Exit an open position. Always permitted — guardrails restrict opening, never
    closing; a stop must always be honourable."""
    symbol = symbol.upper().strip()
    state = gr.load_state()
    pos = next((p for p in state.get("open_positions", []) if p["symbol"] == symbol), None)
    if not pos:
        return {"closed": False, "error": f"No tracked open position in {symbol}"}

    exit_side = "SELL" if pos["side"] == "BUY" else "BUY"
    broker = get_broker()
    result = broker.place(
        symbol=symbol, side=exit_side, quantity=pos["quantity"], price=price,
        exchange=exchange, product="INTRADAY" if intraday else "CNC", tag="agent-exit",
    )
    if not result.ok:
        return {"closed": False, "error": result.error, "broker": broker.name}
    order_id = result.order_id

    direction = 1 if pos["side"] == "BUY" else -1
    pnl = (price - pos["entry"]) * pos["quantity"] * direction
    r_multiple = pnl / pos["open_risk"] if pos.get("open_risk") else 0.0

    holding_days = None
    if pos.get("opened"):
        try:
            opened = __import__("datetime").datetime.fromisoformat(pos["opened"])
            holding_days = (jr.now_ist() - opened).days
        except Exception:
            pass

    jr.log_trade(
        "EXIT", symbol, thesis=reason or "Position closed",
        entry=pos["entry"], quantity=pos["quantity"], order_id=order_id,
        notes=f"Exit ₹{price:,.2f} | P&L ₹{pnl:,.2f} | {r_multiple:+.2f}R",
    )
    jr.record_exit(
        symbol=symbol, exit_price=price, pnl=round(pnl, 2),
        r_multiple=round(r_multiple, 3), reason=reason, holding_days=holding_days,
    )
    state = jr.close_position(symbol, price)
    jr.render_portfolio_md(state)

    return {"closed": True, "symbol": symbol, "order_id": order_id,
            "exit_price": price, "pnl": round(pnl, 2), "r_multiple": round(r_multiple, 2)}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Trading agent execution gateway")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("propose", help="Propose a trade (validated before placement)")
    p.add_argument("--symbol", required=True)
    p.add_argument("--side", required=True, choices=["BUY", "SELL", "buy", "sell"])
    p.add_argument("--entry", type=float, required=True)
    p.add_argument("--stop", type=float, required=True)
    p.add_argument("--target", type=float, required=True)
    p.add_argument("--thesis", required=True)
    p.add_argument("--regime", default="")
    p.add_argument("--playbook", default="")
    p.add_argument("--exchange", default="NSE")
    p.add_argument("--instrument-type", default="equity_cash")
    p.add_argument("--intraday", action="store_true")
    p.add_argument("--dry-run", action="store_true")

    c = sub.add_parser("close", help="Close an open position")
    c.add_argument("--symbol", required=True)
    c.add_argument("--price", type=float, required=True)
    c.add_argument("--exchange", default="NSE")
    c.add_argument("--intraday", action="store_true")
    c.add_argument("--reason", default="")

    sub.add_parser("sync", help="Reconcile local state with the broker")
    sub.add_parser("status", help="Guardrail and portfolio snapshot")

    args = parser.parse_args()

    if args.command == "propose":
        out = propose_trade(
            symbol=args.symbol, side=args.side, entry=args.entry, stop=args.stop,
            target=args.target, thesis=args.thesis, regime=args.regime,
            playbook=args.playbook, exchange=args.exchange,
            instrument_type=args.instrument_type, intraday=args.intraday,
            dry_run=args.dry_run,
        )
    elif args.command == "close":
        out = close_position(args.symbol, args.price, args.exchange, args.intraday, args.reason)
    elif args.command == "sync":
        out = sync_from_broker()
    else:
        out = gr.status_summary()

    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
