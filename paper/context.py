"""
paper/context.py — the "current operation" StrategyContext adapter.

research/experiments/strategy_backtest.py already built the HISTORICAL half
of this bridge (ReplayStrategyContext, backed by research.replay.ReplayStep
— ultimately Store.view(as_of).history()). Per strategies/core.py's own
module docstring, a Strategy implementation is meant to work unmodified
against either a backtest adapter or "a future engine-side live adapter" —
this module is that second adapter, for paper/live-STYLE *current*
operation instead of historical replay (see the AA spec, section 11:
"do not accidentally route current execution through historical replay
machinery").

PaperStrategyContext satisfies strategies.core.StrategyContext exactly
(as_of + history(symbol, days)) and is backed by:

    as_of      -> an injected Clock (paper.clock.Clock) — never
                  datetime.now() called directly here
    history()  -> an injected HistoryProvider callable, defaulting to
                  engine.market_data.get_history(symbol, days=days) — the
                  SAME function engine/screener.py and engine/regime.py
                  already use for indicators. No `as_of`/`provider` kwarg is
                  passed to it (that pair is reserved for the RESEARCH
                  as-of-seam — see get_history()'s own docstring); calling
                  it with neither means "give me data up to today", exactly
                  engine/'s own existing live path, unchanged.

This is the ONLY place in paper/ that imports engine.market_data, and it is
a read of daily OHLCV — never a broker call (engine.market_data.
get_live_quotes/get_ltp, which DO call the broker for a live quote, are
never imported here or anywhere else in this package).
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Callable, Optional

from .clock import Clock, resolve_clock

HistoryProvider = Callable[[str, int], Any]
"""(symbol, days) -> a pandas DataFrame indexed by date with at least a
'close' column — the exact shape engine.market_data.get_history() already
returns. A test may inject any callable with this signature, e.g. one
backed by a small in-memory fixture DataFrame, to make a paper cycle's
input data fully deterministic and network-free."""


def _default_history_provider(symbol: str, days: int) -> Any:
    from engine.market_data import get_history  # read-only OHLCV, no broker call
    return get_history(symbol, days=days)


class PaperStrategyContext:
    """One instance is built per paper cycle (see paper/runner.py) and
    handed to every eligible Strategy's generate_signal() call for that
    cycle — `as_of` is therefore identical across every Strategy and every
    symbol evaluated in the same cycle, the same "one context per step"
    discipline ReplayStrategyContext already follows.
    """

    __slots__ = ("_clock", "_history_provider", "_as_of")

    def __init__(self, *, clock: Optional[Clock] = None,
                history_provider: Optional[HistoryProvider] = None) -> None:
        self._clock = resolve_clock(clock)
        self._history_provider = history_provider or _default_history_provider
        # Frozen once, at construction — every history()/fill call within
        # this context's lifetime sees the exact same "now", even if the
        # cycle takes a few seconds to walk several symbols.
        self._as_of = self._clock()

    @property
    def as_of(self) -> dt.datetime:
        return self._as_of

    def history(self, symbol: str, days: int) -> Any:
        return self._history_provider(symbol, days)
