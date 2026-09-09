"""
paper/notify.py — best-effort, unmistakably-labeled paper/shadow Telegram
alerts.

Reuses scripts/telegram_notify.send_message() verbatim (the AA spec's own
instruction: "reuse the existing Telegram sending capability... where the
existing Telegram mechanism can be safely reused" — this module IS that
reuse, not a second notifier). Every message this module sends is prefixed
so it can never be mistaken for a live trade alert (scripts/telegram_notify
is also used, unmodified, by run_cycle.sh's live cycles) — see
_format_* below.

Best-effort, on purpose: a paper cycle's core state (the SQLite writes in
paper/store.py) is always committed BEFORE this module is ever called (see
paper/runner.py) — a Telegram failure here can never lose or roll back
paper state, and every call in this module swallows its own exceptions.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).parent.parent


def _send(text: str) -> bool:
    """Returns True on success, False on any failure — never raises. The
    one place this module actually calls the network."""
    try:
        sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
        from telegram_notify import send_message  # existing, unmodified
        send_message(text)
        return True
    except Exception:
        return False


def notify_paper_trade(*, side: str, strategy_id: str, version_id: str, symbol: str,
                       quantity: int, fill_price: float, reason: Optional[str] = None) -> bool:
    lines = [
        "🟡 PAPER TRADE — simulation only, no real order was placed",
        "",
        f"Strategy: {strategy_id}",
        f"Version: {version_id}",
        f"Symbol: {symbol}",
        f"Action: {side}",
        f"Qty: {quantity}",
        f"Paper Fill: ₹{fill_price:,.2f}",
    ]
    if reason:
        lines.append(f"Note: {reason}")
    return _send("\n".join(lines))


def notify_paper_rejection(*, side: str, strategy_id: str, version_id: str, symbol: str,
                           reason: str) -> bool:
    lines = [
        "🟡 PAPER ORDER REJECTED — simulation only",
        "",
        f"Strategy: {strategy_id}",
        f"Version: {version_id}",
        f"Symbol: {symbol}",
        f"Action: {side}",
        f"Reason: {reason}",
    ]
    return _send("\n".join(lines))


def notify_paper_cycle_summary(*, cycle_id: str, summary: dict) -> bool:
    lines = [
        f"🟡 PAPER CYCLE SUMMARY ({cycle_id}) — simulation only",
        "",
        f"Signals evaluated: {summary.get('signals_evaluated', 0)}",
        f"Orders filled: {summary.get('orders_filled', 0)}",
        f"Orders rejected: {summary.get('orders_rejected', 0)}",
        f"Open positions: {summary.get('open_position_count', '—')}",
        f"Total net P&L (paper): ₹{summary.get('total_net_pnl', 0):,.2f}",
    ]
    return _send("\n".join(lines))


def notify_paper_cycle_failure(*, cycle_id: str, error: str) -> bool:
    lines = [
        f"🔴 PAPER CYCLE FAILED ({cycle_id}) — simulation only, no live impact",
        "",
        f"Error: {error}",
    ]
    return _send("\n".join(lines))
