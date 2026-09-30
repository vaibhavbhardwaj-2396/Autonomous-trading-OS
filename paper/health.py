"""Deterministic paper strategy health; no research or live dependencies."""

from __future__ import annotations

def strategy_health(store) -> list[dict]:
    rows = []
    version_ids = sorted({t["strategy_version_id"] for t in store.list_trades(limit=None)} |
                         {p["strategy_version_id"] for p in store.list_positions()})
    for version_id in version_ids:
        trades = store.list_trades(strategy_version_id=version_id, limit=None)
        pnl = round(sum(float(t["net_pnl"]) for t in trades), 2)
        wins = sum(1 for t in trades if float(t["net_pnl"]) > 0)
        win_rate = wins / len(trades) if trades else None
        if len(trades) >= 20 and win_rate is not None and win_rate < 0.30:
            state, reason = "INVALIDATED", "20+ paper trades with win rate below 30%"
        elif len(trades) >= 10 and pnl < 0:
            state, reason = "DEGRADING", "10+ paper trades with negative aggregate net P&L"
        elif len(trades) < 10:
            state, reason = "WATCH", "insufficient paper sample for health inference"
        else:
            state, reason = "HEALTHY", "paper sample is not breaching deterministic decay checks"
        rows.append({"strategy_version_id": version_id, "state": state, "reason": reason,
                     "trades": len(trades), "net_pnl": pnl, "win_rate": win_rate})
    return rows
