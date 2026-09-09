"""
paper/clock.py — the one permitted source of "now" for this package.

strategies/core.py's own contract is explicit: a Strategy must never call
datetime.now()/time.time() itself, and must only ever timestamp a Signal
from whatever `context.as_of` hands it. paper/ takes the same discipline
one level up — nothing in this package scatters its own datetime.now()
calls; every module that needs "the current moment" takes a `clock`
argument (a zero-arg callable returning a tz-aware datetime) instead.

Deliberately does NOT import engine.journal.now_ist — that would pull in
engine.journal, which this package's isolation boundary keeps out (see
paper/__init__.py's module docstring: paper/ imports only
engine.market_data and engine.costs from engine/). A timezone constant is
two lines; duplicating those two lines here is a much smaller coupling
than importing a module that itself imports engine.guardrails.
"""

from __future__ import annotations

import datetime as dt
from typing import Callable, Optional

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

Clock = Callable[[], dt.datetime]
"""A Clock is any zero-argument callable returning a tz-aware datetime."""


def system_clock() -> dt.datetime:
    """The real, live clock — IST, tz-aware. The only place in this
    package `datetime.now()` is actually called."""
    return dt.datetime.now(IST)


class FixedClock:
    """A Clock that always returns the same, explicitly-given instant.
    Used by tests (and available to any caller) to make a paper cycle's
    "now" fully deterministic and reproducible — the same role
    research.replay.ReplayStep.as_of plays for a backtest step, but for
    paper/live-style current operation instead of historical replay.
    """

    __slots__ = ("_at",)

    def __init__(self, at: dt.datetime) -> None:
        if at.tzinfo is None:
            at = at.replace(tzinfo=IST)
        self._at = at

    def __call__(self) -> dt.datetime:
        return self._at


def resolve_clock(clock: Optional[Clock]) -> Clock:
    """`clock or system_clock` as a named helper, so every call site reads
    the same way and a future change to the default needs one edit."""
    return clock if clock is not None else system_clock
