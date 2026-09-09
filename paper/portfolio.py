"""
paper/portfolio.py — signal -> paper order -> paper fill -> paper portfolio
accounting. The one module in this package that actually decides what a
BUY/SELL Signal does to the paper book.

Position model (v1, deliberately the smallest one that supports every
acceptance scenario the AA spec asks for — see the AA spec's sections 5, 7,
9 and 26):

    Each BUY signal that passes sizing/risk checks opens one LOT at a fixed
    paper notional (paper.config.position_notional() / fill price). A
    second BUY for the same (strategy_version_id, symbol) opens a SECOND
    lot ("increasing" the position) rather than being ignored — up to
    paper.config.max_lots_per_position(). A SELL signal closes the OLDEST
    open lot for that (strategy_version_id, symbol) — FIFO — which is a
    full close if only one lot was open ("closing"), or a partial
    reduction if more than one lot was open ("reducing"). No shorting: a
    SELL with no open lot is rejected, never opens a short.

    avg_entry_price (reported at the position level, see
    PaperStore.list_positions) is the quantity-weighted average entry
    price across that position's currently-open lots — recomputed
    automatically as lots open and close, since it is a live aggregate
    query over paper_lots, never a separately-stored, driftable field.

No shorts, no order types beyond BUY/SELL, no partial-lot fills — see the
AA spec section 5's own "do not invent unnecessary order types."
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from strategies.core import Signal

from . import config as paper_config
from .fills import compute_fill
from .store import PaperStore


REJECT_NO_PRICE_DATA = "no_price_data"
REJECT_NO_OPEN_POSITION = "no_open_position"
REJECT_MAX_LOTS = "max_lots_per_position_reached"
REJECT_MAX_CONCURRENT = "max_concurrent_positions_reached"
REJECT_INSUFFICIENT_CASH = "insufficient_paper_cash"
REJECT_OUTSIDE_UNIVERSE = "symbol_outside_universe"
REJECT_DUPLICATE_SYMBOL = "malformed_signal_duplicate_symbol"


@dataclass(frozen=True)
class OrderOutcome:
    order: Optional[dict]      # the paper_orders row, or None if this exact
                                # (cycle, version, symbol, side, signal time)
                                # was already recorded — a duplicate, and a
                                # deliberate no-op (see the module docstring
                                # on idempotency in paper/store.py)
    trade: Optional[dict]      # set only when a SELL closed a lot
    is_new: bool                # False for a duplicate no-op


def _iso(value: Any) -> str:
    """A Signal's generated_at (and a context's as_of) are already
    datetimes handed down from paper.clock — this just renders them
    consistently for storage/dedupe, the same isoformat(timespec='seconds')
    convention research/store.py and engine/journal.py both already use."""
    if hasattr(value, "isoformat"):
        return value.isoformat(timespec="seconds")
    return str(value)


class PaperPortfolio:
    def __init__(self, store: PaperStore) -> None:
        self.store = store

    # -- marking (unrealized P&L support; never opens/closes anything) ----

    def mark_symbol(self, symbol: str, history_df: Any, now: str) -> None:
        """Best-effort: record the latest available close for `symbol` so
        /paper/positions and /paper/performance can report unrealized P&L
        without the API itself ever touching a data feed (see
        paper/schema.sql's paper_marks table docstring). Silently does
        nothing if no price is available this cycle — a stale-but-present
        mark is preferred over an absent one."""
        fill = compute_fill(history_df)
        if fill is None:
            return
        self.store.set_mark(symbol, fill.price, _iso(fill.bar_time), now)

    # -- signal handling ----------------------------------------------------

    def reject_malformed(self, signal: Signal, *, cycle_id: str, reason: str,
                         now: str) -> OrderOutcome:
        """For a signal the runner itself refuses before this module ever
        tries to fill it (e.g. symbol outside the universe it was given —
        the same check research/experiments/strategy_backtest.py's
        run_backtest() already makes for the historical path)."""
        order = self.store.insert_order(
            cycle_id=cycle_id, strategy_id=signal.strategy_id,
            strategy_version_id=signal.strategy_version_id, symbol=signal.symbol,
            side=signal.action, requested_quantity=None,
            signal_generated_at=_iso(signal.generated_at), requested_at=now,
            filled_at=None, fill_price=None, status="REJECTED", reason=reason, now=now,
        )
        return OrderOutcome(order=order, trade=None, is_new=order is not None)

    def process_signal(self, signal: Signal, history_df: Any, *, cycle_id: str,
                       now: str) -> OrderOutcome:
        """The one entry point the runner calls per Signal. Fully
        idempotent per (cycle_id, strategy_version_id, symbol, side,
        signal.generated_at) — see the module docstring and
        paper/store.py's order_dedupe_key()."""
        signal_generated_at = _iso(signal.generated_at)

        fill = compute_fill(history_df)
        if fill is None:
            order = self.store.insert_order(
                cycle_id=cycle_id, strategy_id=signal.strategy_id,
                strategy_version_id=signal.strategy_version_id, symbol=signal.symbol,
                side=signal.action, requested_quantity=None,
                signal_generated_at=signal_generated_at, requested_at=now,
                filled_at=None, fill_price=None, status="REJECTED",
                reason=REJECT_NO_PRICE_DATA, now=now,
            )
            return OrderOutcome(order=order, trade=None, is_new=order is not None)

        if signal.action == "BUY":
            return self._process_buy(signal, fill.price, cycle_id=cycle_id,
                                     signal_generated_at=signal_generated_at, now=now)
        else:  # SELL — Signal.__post_init__ already restricts action to BUY|SELL
            return self._process_sell(signal, fill.price, cycle_id=cycle_id,
                                      signal_generated_at=signal_generated_at, now=now)

    # -- BUY -----------------------------------------------------------------

    def _process_buy(self, signal: Signal, price: float, *, cycle_id: str,
                     signal_generated_at: str, now: str) -> OrderOutcome:
        open_lots = self.store.list_open_lots(
            strategy_version_id=signal.strategy_version_id, symbol=signal.symbol)

        reject_reason = self._buy_risk_check(signal, open_lots)
        if reject_reason is not None:
            order = self.store.insert_order(
                cycle_id=cycle_id, strategy_id=signal.strategy_id,
                strategy_version_id=signal.strategy_version_id, symbol=signal.symbol,
                side="BUY", requested_quantity=None,
                signal_generated_at=signal_generated_at, requested_at=now,
                filled_at=None, fill_price=None, status="REJECTED",
                reason=reject_reason, now=now,
            )
            return OrderOutcome(order=order, trade=None, is_new=order is not None)

        quantity = self._sized_quantity(price)
        if quantity < 1:
            order = self.store.insert_order(
                cycle_id=cycle_id, strategy_id=signal.strategy_id,
                strategy_version_id=signal.strategy_version_id, symbol=signal.symbol,
                side="BUY", requested_quantity=None,
                signal_generated_at=signal_generated_at, requested_at=now,
                filled_at=None, fill_price=None, status="REJECTED",
                reason=REJECT_INSUFFICIENT_CASH, now=now,
            )
            return OrderOutcome(order=order, trade=None, is_new=order is not None)

        order = self.store.insert_order(
            cycle_id=cycle_id, strategy_id=signal.strategy_id,
            strategy_version_id=signal.strategy_version_id, symbol=signal.symbol,
            side="BUY", requested_quantity=quantity,
            signal_generated_at=signal_generated_at, requested_at=now,
            filled_at=now, fill_price=price, status="FILLED", reason=None, now=now,
        )
        if order is None:
            # Duplicate — this exact signal was already processed in this
            # cycle. Never open a second lot for it.
            return OrderOutcome(order=None, trade=None, is_new=False)

        self.store.open_lot(
            strategy_id=signal.strategy_id,
            strategy_version_id=signal.strategy_version_id, symbol=signal.symbol,
            entry_order_id=order["paper_order_id"], entry_price=price,
            quantity=quantity, opened_at=now, now=now,
        )
        return OrderOutcome(order=order, trade=None, is_new=True)

    def _buy_risk_check(self, signal: Signal, open_lots: list[dict]) -> Optional[str]:
        """Simulation-only constraints (paper.config), never
        engine/guardrails.py's live limits — see paper/config.py's module
        docstring."""
        if len(open_lots) >= paper_config.max_lots_per_position():
            return REJECT_MAX_LOTS

        if not open_lots:
            # A genuinely NEW position (not pyramiding an existing one) —
            # only this case counts against the concurrent-positions cap.
            distinct_positions = len({
                (p["strategy_version_id"], p["symbol"])
                for p in self.store.all_open_lots()
            })
            if distinct_positions >= paper_config.max_concurrent_positions():
                return REJECT_MAX_CONCURRENT

        return None

    def _sized_quantity(self, price: float) -> int:
        notional = paper_config.position_notional()
        desired = max(int(notional / price), 1) if price > 0 else 0
        if desired < 1:
            return 0

        account = self.store.get_account()
        buffer = paper_config.min_cash_buffer()
        available = account["cash"] - buffer
        if available <= 0:
            return 0

        affordable = int(available / price)
        return min(desired, affordable)

    # -- SELL ------------------------------------------------------------------

    def _process_sell(self, signal: Signal, price: float, *, cycle_id: str,
                      signal_generated_at: str, now: str) -> OrderOutcome:
        open_lots = self.store.list_open_lots(
            strategy_version_id=signal.strategy_version_id, symbol=signal.symbol)
        if not open_lots:
            order = self.store.insert_order(
                cycle_id=cycle_id, strategy_id=signal.strategy_id,
                strategy_version_id=signal.strategy_version_id, symbol=signal.symbol,
                side="SELL", requested_quantity=None,
                signal_generated_at=signal_generated_at, requested_at=now,
                filled_at=None, fill_price=None, status="REJECTED",
                reason=REJECT_NO_OPEN_POSITION, now=now,
            )
            return OrderOutcome(order=order, trade=None, is_new=order is not None)

        oldest = open_lots[0]  # list_open_lots is already ordered oldest-first (FIFO)

        order = self.store.insert_order(
            cycle_id=cycle_id, strategy_id=signal.strategy_id,
            strategy_version_id=signal.strategy_version_id, symbol=signal.symbol,
            side="SELL", requested_quantity=oldest["quantity"],
            signal_generated_at=signal_generated_at, requested_at=now,
            filled_at=now, fill_price=price, status="FILLED", reason=None, now=now,
        )
        if order is None:
            return OrderOutcome(order=None, trade=None, is_new=False)

        from engine.costs import equity_round_trip  # pure function — see paper/__init__.py

        entry_price = oldest["entry_price"]
        quantity = oldest["quantity"]
        gross_pnl = (price - entry_price) * quantity
        costs = equity_round_trip(entry_price, quantity, intraday=False).total
        net_pnl = gross_pnl - costs

        trade = self.store.close_lot(
            oldest["id"], exit_order_id=order["paper_order_id"], exit_price=price,
            closed_at=now, gross_pnl=gross_pnl, costs=costs, net_pnl=net_pnl,
            exit_reason="signal_exit", now=now,
        )
        return OrderOutcome(order=order, trade=trade, is_new=True)

    # -- performance ----------------------------------------------------------

    def performance_summary(self) -> dict:
        """Deterministic, dependency-free summary over exactly the fields
        the AA spec section 25 asks for — nothing richer (no CAGR/Sharpe;
        see that section's own "avoid adding advanced portfolio analytics
        in AA" instruction)."""
        account = self.store.get_account()
        positions = self.store.list_positions()
        marks = self.store.get_marks()

        unrealized = 0.0
        for p in positions:
            mark = marks.get(p["symbol"])
            if mark is not None and p["avg_entry_price"] is not None:
                unrealized += (mark["price"] - p["avg_entry_price"]) * p["quantity"]

        trades = self.store.list_trades(limit=None)
        n_trades = len(trades)
        wins = sum(1 for t in trades if t["net_pnl"] > 0)
        losses = sum(1 for t in trades if t["net_pnl"] < 0)

        current_equity = account["cash"] + unrealized + sum(
            (p["avg_entry_price"] or 0) * p["quantity"] for p in positions
        )

        return {
            "starting_capital": account["initial_capital"],
            "cash": account["cash"],
            "current_equity": round(current_equity, 2),
            "realized_pnl": account["realized_pnl_alltime"],
            "unrealized_pnl": round(unrealized, 2),
            "total_net_pnl": round(account["realized_pnl_alltime"] + unrealized, 2),
            "total_costs_alltime": account["total_costs_alltime"],
            "n_trades": n_trades,
            "win_count": wins,
            "loss_count": losses,
            "win_rate": (wins / n_trades) if n_trades else None,
            "open_position_count": len(positions),
        }

    def positions_with_marks(self) -> list[dict]:
        marks = self.store.get_marks()
        out = []
        for p in self.store.list_positions():
            mark = marks.get(p["symbol"])
            current_price = mark["price"] if mark else None
            unrealized = None
            if current_price is not None and p["avg_entry_price"] is not None:
                unrealized = round((current_price - p["avg_entry_price"]) * p["quantity"], 2)
            out.append({
                **p,
                "current_price": current_price,
                "marked_as_of": mark["as_of"] if mark else None,
                "unrealized_pnl": unrealized,
            })
        return out
