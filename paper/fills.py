"""
paper/fills.py — the deterministic paper fill model (v1).

THE CONVENTION (read this before changing anything here)
------------------------------------------------------------
A paper order fills at the CLOSE of the most recent bar available in the
exact same history data the Signal was generated from — i.e. if a Strategy
called context.history(symbol, days) and that returned data through bar T,
the fill price is that bar T's own close.

This is deliberately the same convention
research/experiments/strategy_backtest.py's _close_trade() already uses at
each Replay step (`bar_rows[-1]["close"]`) — same-bar-close — chosen here
for three reasons:

  1. It is leak-free by construction: the fill can never see a price the
     Signal itself did not already have access to, because both are read
     from the identical history() call's result. There is no separate
     "now fetch a fresh quote" step that could race ahead of the data the
     Strategy actually reasoned from.
  2. It needs no broker/live-quote dependency at all (see paper/context.py
     and paper/__init__.py's isolation boundary) — engine.market_data.
     get_live_quotes()/get_ltp() are never called anywhere in this package.
  3. It keeps the paper engine's results comparable to the existing
     Strategy backtest path's own trade model, which matters for later
     judging "does this Strategy's paper performance look like its
     backtest performance."

Documented limitation: this is a conservative v1 approximation, not a claim
of live-fill realism — a genuine live order would fill against a real-time
quote, intraday, not the prior daily close. See docs/PAPER_TRADING.md.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class FillResult:
    price: float
    bar_time: Any  # whatever the history data's own index type is (pandas Timestamp, etc.)


def compute_fill(history_df: Any) -> Optional[FillResult]:
    """`history_df` is exactly what a HistoryProvider (see
    paper/context.py) returned for this symbol this cycle — normally a
    pandas DataFrame indexed by date with a 'close' column, the same shape
    engine.market_data.get_history() and its DataFrame convention already
    use everywhere else in this project.

    Returns None (never a fabricated price) when:
      - history_df is None or has no rows at all
      - the most recent row's close is NaN/missing

    A None return means "no fill" — the caller (paper/portfolio.py) records
    the order as REJECTED with reason="no_price_data", exactly the same
    "an absent price is unknown, never zero" discipline
    engine.market_data.get_ltp()'s own docstring already states for the
    live path.
    """
    if history_df is None:
        return None
    try:
        if len(history_df) == 0:
            return None
    except TypeError:
        return None

    try:
        last_row = history_df.iloc[-1]
        close = last_row["close"]
        bar_time = history_df.index[-1]
    except (KeyError, IndexError, AttributeError):
        return None

    try:
        price = float(close)
    except (TypeError, ValueError):
        return None
    if math.isnan(price) or price <= 0:
        return None

    return FillResult(price=price, bar_time=bar_time)
