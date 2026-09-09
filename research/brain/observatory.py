"""
The Observatory — Phase 1 Slice B.

Three detectors, one job each: say what is statistically unusual about a
symbol as of a given moment. Nothing here decides whether an anomaly is
tradeable, nothing here proposes a rule, and nothing here calls Claude or any
other LLM. That is a deliberate boundary, not an oversight:

    Observatory  = "here is what's unusual"     (this file — deterministic)
    Investigator = "here is what we make of it"  (a later slice — where a
                                                    bounded Claude call reads
                                                    the Observatory's output
                                                    and proposes a Hypothesis
                                                    as schema-validated DATA)

Collapsing those two into one step is how a system quietly starts trading on
vibes. Keeping them separate means the expensive, fallible, hard-to-audit
part (an LLM noticing a pattern) only ever runs on a short list the cheap,
deterministic part has already flagged — and the flagging itself stays fully
reproducible and testable without touching a model at all.

Every detector reads exclusively through an AsOfView (research/store.py), so
the no-lookahead guarantee is structural here, not something this module has
to re-implement: a value with knowledge_time after `as_of` simply is not in
the rows the view hands back.

Statistical discipline, deliberately conservative:

  WINDOW_DAYS      = 20   trailing baseline window
  MIN_OBSERVATIONS = 10   minimum baseline points before a z-score is trusted
  Z_THRESHOLD      = 3.0  |z| >= 3.0 to call something an anomaly at all

The same ~3-sigma bar is used across all three detectors on purpose — one
consistent statistical standard is easier to reason about and to defend than
three different thresholds tuned per-metric. A detector returns None rather
than a low-confidence guess whenever the baseline is too small or has ~zero
variance; fabricating a z-score against a degenerate baseline is worse than
saying nothing.

Data limitation, stated plainly rather than worked around: `event_frequency_
anomaly` is built against the `bse_announcement` dataset (research/sources/
announcements.py), whose entity IS the stock symbol. It is deliberately NOT
built against `news_arrival` (research/sources/news.py), whose entity is the
RSS feed name, not a symbol — there is no per-symbol news-frequency signal in
this store yet. Building one would mean guessing at symbol attribution from
free text, which this slice does not do.

This module imports nothing from engine/, and the only place it ever writes
(as opposed to reads) is inside `run()`, which is also the only function that
imports research.memory — keeping "detect" and "persist" visibly separate.
"""

from __future__ import annotations

import datetime as dt
import statistics
from collections import namedtuple
from dataclasses import dataclass, field
from typing import Iterable, Optional

from ..store import AsOfView, Store, TimeLike, to_dt

WINDOW_DAYS = 20
MIN_OBSERVATIONS = 10
Z_THRESHOLD = 3.0

_ZResult = namedtuple("_ZResult", "z baseline std")


def _zscore(baseline: list[float], current: float) -> Optional[_ZResult]:
    """The one statistical core every detector shares.

    Returns None — never a fabricated 0.0 — when the baseline is too small
    (fewer than MIN_OBSERVATIONS points) or has ~zero variance (a baseline
    that never moves makes any z-score either meaningless or infinite).
    """
    if len(baseline) < MIN_OBSERVATIONS:
        return None
    mean = statistics.fmean(baseline)
    std = statistics.pstdev(baseline, mu=mean)
    if std <= 1e-12:
        return None
    return _ZResult(z=(current - mean) / std, baseline=mean, std=std)


@dataclass(frozen=True)
class AnomalyRecord:
    """One flagged anomaly. `dataset` names the underlying source dataset the
    detector examined (e.g. 'prices_eod', 'bse_announcement') — not to be
    confused with research.memory.DATASET_ANOMALY, which is where `run()`
    files this record once persisted."""

    dataset: str
    entity: str
    metric: str
    value: float
    baseline: float
    z_score: float
    as_of: dt.datetime
    n_observations: int
    std: float
    detail: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------

def volume_anomaly(view: AsOfView, symbol: str, as_of: TimeLike) -> Optional[AnomalyRecord]:
    """Is today's traded volume unusual against its trailing WINDOW_DAYS?

    The current session is excluded from its own baseline — a spike cannot
    inflate the average it's being measured against.
    """
    sessions = view.prices(symbol, days=WINDOW_DAYS + 1)
    if len(sessions) < MIN_OBSERVATIONS + 1:
        return None

    current, baseline_sessions = sessions[-1], sessions[:-1]
    current_vol = current.get("volume")
    baseline_vols = [s.get("volume") for s in baseline_sessions]
    if current_vol is None or any(v is None for v in baseline_vols):
        return None  # refuse rather than guess on missing volume data

    z = _zscore([float(v) for v in baseline_vols], float(current_vol))
    if z is None or abs(z.z) < Z_THRESHOLD:
        return None

    return AnomalyRecord(
        dataset="prices_eod", entity=symbol.upper(), metric="volume_zscore",
        value=float(current_vol), baseline=z.baseline, z_score=z.z,
        as_of=to_dt(as_of), n_observations=len(baseline_vols), std=z.std,
        detail={"session_date": current["session_date"]},
    )


def price_move_anomaly(view: AsOfView, symbol: str, as_of: TimeLike) -> Optional[AnomalyRecord]:
    """Is today's close-to-close return unusual against its trailing
    WINDOW_DAYS of returns? Flags either direction — an unusually large drop
    is exactly as much an anomaly as an unusually large rally.

    Refuses outright (returns None) on any missing close or a zero previous
    close, rather than skipping the bad point and quietly computing a
    baseline of a different length than it claims.
    """
    sessions = view.prices(symbol, days=WINDOW_DAYS + 2)
    if len(sessions) < MIN_OBSERVATIONS + 2:
        return None

    closes = [s.get("close") for s in sessions]
    if any(c is None for c in closes):
        return None

    returns: list[float] = []
    for prev, cur in zip(closes, closes[1:]):
        if prev == 0:
            return None
        returns.append((cur - prev) / prev)

    current_return, baseline_returns = returns[-1], returns[:-1]
    z = _zscore(baseline_returns, current_return)
    if z is None or abs(z.z) < Z_THRESHOLD:
        return None

    return AnomalyRecord(
        dataset="prices_eod", entity=symbol.upper(), metric="price_move_zscore",
        value=current_return, baseline=z.baseline, z_score=z.z,
        as_of=to_dt(as_of), n_observations=len(baseline_returns), std=z.std,
        detail={"session_date": sessions[-1]["session_date"]},
    )


def event_frequency_anomaly(view: AsOfView, symbol: str, as_of: TimeLike) -> Optional[AnomalyRecord]:
    """Is today's count of BSE announcements for this symbol unusual against
    its trailing daily rate?

    The baseline window is anchored to trading days with known price history
    (via view.prices) rather than raw calendar days — that is the honest way
    to answer "do we have enough history to trust a zero count", since a
    calendar day with zero announcements is legitimate data, not a gap, but a
    day before this symbol's price history even starts is a gap, not a zero.
    """
    sessions = view.prices(symbol, days=WINDOW_DAYS + 1)
    if len(sessions) < MIN_OBSERVATIONS + 1:
        return None

    current_day = sessions[-1]["session_date"]
    baseline_days = [s["session_date"] for s in sessions[:-1]]

    rows = view.observations(
        "bse_announcement", entity=symbol.upper(),
        event_from=baseline_days[0], event_to=current_day, latest_only=False,
    )
    counts: dict[str, int] = {}
    for r in rows:
        day = str(r["event_time"])[:10]
        counts[day] = counts.get(day, 0) + 1

    current_count = counts.get(current_day, 0)
    baseline_counts = [float(counts.get(d, 0)) for d in baseline_days]

    z = _zscore(baseline_counts, float(current_count))
    if z is None or abs(z.z) < Z_THRESHOLD:
        return None

    return AnomalyRecord(
        dataset="bse_announcement", entity=symbol.upper(), metric="event_frequency_zscore",
        value=float(current_count), baseline=z.baseline, z_score=z.z,
        as_of=to_dt(as_of), n_observations=len(baseline_counts), std=z.std,
        detail={"session_date": current_day},
    )


DETECTORS = (volume_anomaly, price_move_anomaly, event_frequency_anomaly)


# ---------------------------------------------------------------------------
# Orchestration — the only function that persists anything
# ---------------------------------------------------------------------------

def run(
    view: AsOfView,
    store: Store,
    as_of: TimeLike,
    symbols: Iterable[str],
    persist: bool = True,
) -> list[AnomalyRecord]:
    """Run all three detectors over `symbols` as of `as_of`.

    This is the only function in observatory.py that imports research.memory
    or writes anything — every detector above is a pure read. `persist=False`
    lets a caller (or a test) see what the Observatory would flag without
    filing it, which matters for the reproducibility test: calling this twice
    with persist=False against the same store/as_of must return identical
    AnomalyRecords, since nothing about the detection itself is stochastic.
    """
    found: list[AnomalyRecord] = []
    for symbol in symbols:
        for detector in DETECTORS:
            record = detector(view, symbol, as_of)
            if record is not None:
                found.append(record)

    if persist and found:
        from .. import memory as rm  # local: this is the sole write path

        for record in found:
            rm.record_anomaly(
                store,
                entity=record.entity,
                metric=record.metric,
                value=record.value,
                baseline=record.baseline,
                z_score=record.z_score,
                as_of=record.as_of,
                source=f"observatory.{record.metric}",
                extra={
                    "n_observations": record.n_observations,
                    "std": record.std,
                    "source_dataset": record.dataset,
                    **record.detail,
                },
            )

    return found
