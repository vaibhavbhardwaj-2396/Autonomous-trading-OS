"""
STEP 4 — the Market Replay Engine.

    replay = Replay(store)
    for step in replay.walk("2022-08-01", "2022-08-31"):
        reg = step.regime()                       # what the agent would have said
        cands = step.candidates(reg.playbook)     # what it would have screened
        ...

The point is not backtesting. It is being able to say "pretend today is 17
August 2022" and have the entire existing signal stack — indicators, regime
classifier, screener — answer from what was KNOWN then, without a single line of
that stack being aware it is time-travelling.

How the engine stays unaware
----------------------------
`engine.market_data.get_history` takes an optional `provider` callable. Replay
supplies one bound to an AsOfView. Engine never imports research; research
injects into engine. The dependency runs one way and the isolation test fails
the build if that ever inverts.

Three defences, in increasing order of how much they are worth
--------------------------------------------------------------
1. Structural. The provider only ever returns rows from an AsOfView, and the
   view has no method that can reach past its own horizon. Prevents accidents.

2. The clock guard. A provider whose `as_of` does not match the view it was
   built from raises. Catches a step reusing a stale provider — which is the
   most likely way a careful person leaks in a loop.

3. The deletion test (`leak_check` below). Run the computation twice at one
   `as_of`: once on the full store, once on a copy with every later row
   physically deleted. If the outputs differ, the computation used data it could
   not have had.

Only the third can actually fail. The first two rely on nobody making a mistake;
the third detects the mistake. That is the difference between a convention and a
control.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Callable, Iterator, Optional, Any

from .store import Store, AsOfView, to_dt, ts, IST

# The bhavcopy publishes in the early evening. An as_of at 15:30 sees no close
# for that session; an as_of at 18:30 does. Replay defaults to the evening so a
# daily-timeframe study sees the session it is standing on — but the default is
# stated here, once, rather than assumed in each experiment.
DEFAULT_AS_OF_TIME = dt.time(18, 30)


class ReplayError(RuntimeError):
    pass


def make_provider(view: AsOfView) -> Callable:
    """Build the callable engine.market_data.get_history will use.

    Signature is fixed by engine: provider(symbol, days, exchange, as_of).
    """
    bound_gate = int(view.as_of.timestamp())

    def provider(symbol: str, days: int, exchange: str, as_of) -> Any:
        # Defence 2. A provider carried over from a previous loop iteration is
        # the realistic way a careful person leaks: the code looks right, the
        # view is just one step stale. Compare the actual instants.
        requested = int(to_dt(as_of, end_of_day=True).timestamp())
        if requested != bound_gate:
            raise ReplayError(
                f"provider bound to {view.as_of.isoformat()} was asked for "
                f"{to_dt(as_of, end_of_day=True).isoformat()}. A stale provider "
                f"in a replay loop leaks silently — refusing.")
        return view.history(symbol, days=days)

    return provider


class ReplayStep:
    """One instant. Everything reachable from here was knowable at `as_of`."""

    __slots__ = ("as_of", "view", "provider", "_store")

    def __init__(self, store: Store, as_of: dt.datetime) -> None:
        self.as_of = as_of
        self.view = store.view(as_of)
        self.provider = make_provider(self.view)
        self._store = store

    def __repr__(self) -> str:
        return f"<ReplayStep {self.as_of.isoformat()}>"

    # -- the existing engine stack, run at this instant ----------------------

    def history(self, symbol: str, days: int = 260):
        return self.view.history(symbol, days=days)

    def regime(self):
        """engine.regime, unmodified, answering from what was known."""
        from engine import regime as rg
        return rg.classify(log=False, as_of=self.as_of, provider=self.provider)

    def candidates(self, playbook: str, symbols: Optional[list[str]] = None,
                   limit: int = 8):
        """engine.screener, unmodified, over a point-in-time universe."""
        from engine import screener as sc
        return sc.scan_for_playbook(playbook, limit=limit, symbols=symbols,
                                    as_of=self.as_of, provider=self.provider)

    def universe(self, index_name: str = "Nifty 500") -> list[str]:
        return self.view.universe(index_name)


class Replay:
    def __init__(self, store: Store, as_of_time: dt.time = DEFAULT_AS_OF_TIME) -> None:
        self.store = store
        self.as_of_time = as_of_time

    # -- calendar ------------------------------------------------------------

    def trading_days(self, start, end) -> list[dt.date]:
        """Sessions the exchange actually traded, taken from the price data we
        hold rather than from a weekday heuristic.

        Deriving the calendar from the data means holidays are right for free,
        and — more usefully — a replay can never step onto a day for which we
        have no prices and quietly treat it as a real session.
        """
        conn = self.store._unsafe_connection()
        rows = conn.execute(
            "SELECT DISTINCT session_date FROM prices_eod "
            "WHERE session_date >= ? AND session_date <= ? ORDER BY session_date",
            (to_dt(start).date().isoformat(), to_dt(end).date().isoformat()),
        ).fetchall()
        return [dt.date.fromisoformat(r[0]) for r in rows]

    def step(self, day) -> ReplayStep:
        d = to_dt(day).date()
        return ReplayStep(self.store,
                          dt.datetime.combine(d, self.as_of_time, tzinfo=IST))

    def walk(self, start, end) -> Iterator[ReplayStep]:
        for d in self.trading_days(start, end):
            yield self.step(d)

    def run(self, start, end, fn: Callable[[ReplayStep], Any],
            on_error: str = "raise") -> list:
        """Apply `fn` at every session. `on_error='collect'` keeps going and
        records failures, which matters on multi-hour sweeps."""
        out = []
        for step in self.walk(start, end):
            try:
                out.append({"as_of": step.as_of.isoformat(), "result": fn(step)})
            except Exception as e:
                if on_error == "raise":
                    raise
                out.append({"as_of": step.as_of.isoformat(),
                            "error": f"{type(e).__name__}: {e}"})
        return out


# ---------------------------------------------------------------------------
# Defence 3 — the deletion test, as reusable infrastructure
# ---------------------------------------------------------------------------

def leak_check(store: Store, as_of, fn: Callable[[ReplayStep], Any],
               scratch: Optional[Path] = None) -> dict:
    """Run `fn` at `as_of` against the full store and against a physically
    truncated copy. Identical output means `fn` used nothing it could not have
    known; different output means it did.

    This is deliberately general so it can be pointed at a real experiment
    rather than only at toy functions. Before EXP-B1's discovery run, B1's own
    signal function goes through here — a leak test that only ever sees test
    fixtures is decoration.

    Note it compares `repr`, not identity: two DataFrames that are equal are not
    the same object, and a comparison that demanded identity would pass
    everything and prove nothing.
    """
    scratch = Path(scratch or (store.path.parent / "_leakcheck.db"))
    full_result = fn(ReplayStep(store, to_dt(as_of, end_of_day=True)))

    truncated = store.truncated_snapshot(as_of, scratch)
    try:
        trunc_result = fn(ReplayStep(truncated, to_dt(as_of, end_of_day=True)))
    finally:
        truncated.close()

    same = repr(full_result) == repr(trunc_result)
    return {
        "as_of": str(as_of),
        "leak_free": same,
        "full": repr(full_result)[:400],
        "truncated": repr(trunc_result)[:400],
        "verdict": (
            "No leak detected: the computation is blind to data published after "
            "as_of." if same else
            "LEAK: output changed when future rows were physically removed. The "
            "computation reached past its horizon — do not trust any result it "
            "has produced."),
    }


# ---------------------------------------------------------------------------
# Defence 2b — the clock ban, as a callable check
# ---------------------------------------------------------------------------

CLOCK_CALLS = ("datetime.now(", "date.today(", "time.time(", "dt.datetime.now(",
               "dt.date.today(", "now_ist(", "utcnow(")


def clock_ban_violations(directory: Path) -> list[str]:
    """Any wall-clock read inside an experiment is a leak waiting to happen: it
    makes the result depend on when it was run rather than on what was known.

    Applied to research/experiments/ only. The recorder and the sources read the
    clock constantly and must — they are recording the present.
    """
    out = []
    if not directory.exists():
        return out
    for f in sorted(directory.rglob("*.py")):
        for lineno, line in enumerate(f.read_text().splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or not stripped:
                continue
            # One violation per line. `dt.date.today(` matches two patterns and
            # reporting it twice makes the count meaningless.
            hit = next((c for c in CLOCK_CALLS if c in line), None)
            if hit:
                out.append(f"{f.name}:{lineno}: {hit} — "
                           f"experiments take time from as_of, never a clock")
    return out
