"""
Layer 2 — signal generation.

Scans the universe under whichever playbook the regime selected and returns ranked
candidates with every number that justified them. It does NOT decide to trade; it
surfaces what qualifies, and the agent applies Layer 3 (news/context) judgment on top.

Two playbooks with deliberately opposite logic:
  momentum       → only in trending regimes
  mean_reversion → only in range-bound regimes

Running either in the wrong regime is the classic way systematic strategies lose money,
so the playbook is passed in rather than chosen here.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict, field

from . import market_data as md
from .watchlist import UNIVERSE, MIN_TRADED_VALUE_CR


@dataclass
class Candidate:
    symbol: str
    playbook: str
    direction: str                  # LONG | SHORT
    close: float
    suggested_entry: float
    suggested_stop: float
    suggested_target: float
    reward_risk: float
    score: float
    reasons: list[str] = field(default_factory=list)
    indicators: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def _liquid(ind: dict) -> bool:
    val = ind.get("avg_traded_value_20d_cr")
    return val is not None and val >= MIN_TRADED_VALUE_CR


def scan_momentum(symbols: list[str] | None = None,
                  as_of=None, provider=None) -> list[Candidate]:
    """Breakout/trend-continuation longs. Low win rate, high reward:risk by design —
    profitability comes from a few large winners, so the losses must stay small."""
    out: list[Candidate] = []
    for sym in (symbols or UNIVERSE):
        df = md.get_history(sym, days=260, as_of=as_of, provider=provider)
        if df.empty or len(df) < 60:
            continue
        ind = md.indicator_snapshot(df)
        if not ind or not _liquid(ind):
            continue

        close = ind["close"]
        ema20, ema50 = ind.get("ema20"), ind.get("ema50")
        atr14 = ind.get("atr14")
        high20 = ind.get("high_20d")
        vol_ratio = ind.get("volume_ratio") or 0
        macd_hist = ind.get("macd_hist") or 0
        macd_prev = ind.get("macd_hist_prev") or 0

        if None in (ema20, ema50, atr14, high20) or atr14 <= 0:
            continue

        reasons, score = [], 0.0

        if close > ema20 > ema50:
            reasons.append(f"stacked above EMA20 ({ema20:.1f}) and EMA50 ({ema50:.1f})")
            score += 2
        else:
            continue  # structural requirement, not a bonus

        # Breakout: at or near the 20-day high
        if close >= high20 * 0.995:
            reasons.append(f"at/near 20-day high ({high20:.1f})")
            score += 2
        else:
            continue

        # Volume confirmation is required — breakouts without it fail disproportionately
        if vol_ratio >= 1.5:
            reasons.append(f"volume {vol_ratio:.2f}x its 20-day average")
            score += 2
        else:
            continue

        if macd_hist > 0 and macd_hist > macd_prev:
            reasons.append("MACD histogram positive and expanding")
            score += 1

        r20 = ind.get("return_20d")
        if r20 is not None and r20 > 0:
            reasons.append(f"20-day return {r20:+.1f}%")
            score += min(r20 / 10, 1.5)

        entry = close
        stop = close - 1.5 * atr14
        target = close + 3.0 * atr14      # 2:1 on the ATR-based stop
        rr = (target - entry) / (entry - stop)

        out.append(Candidate(
            symbol=sym, playbook="momentum", direction="LONG", close=close,
            suggested_entry=round(entry, 2), suggested_stop=round(stop, 2),
            suggested_target=round(target, 2), reward_risk=round(rr, 2),
            score=round(score, 2), reasons=reasons, indicators=ind,
        ))

    return sorted(out, key=lambda c: c.score, reverse=True)


def scan_mean_reversion(symbols: list[str] | None = None,
                        as_of=None, provider=None) -> list[Candidate]:
    """Oversold bounces — ONLY valid in a confirmed range-bound regime. In a downtrend
    this same setup is a falling knife, which is why the regime gate comes first."""
    out: list[Candidate] = []
    for sym in (symbols or UNIVERSE):
        df = md.get_history(sym, days=260, as_of=as_of, provider=provider)
        if df.empty or len(df) < 60:
            continue
        ind = md.indicator_snapshot(df)
        if not ind or not _liquid(ind):
            continue

        close = ind["close"]
        rsi14 = ind.get("rsi14")
        bb_lower, bb_mid = ind.get("bb_lower"), ind.get("bb_mid")
        ema50, atr14 = ind.get("ema50"), ind.get("atr14")

        if None in (rsi14, bb_lower, bb_mid, ema50, atr14) or atr14 <= 0:
            continue

        reasons, score = [], 0.0

        if rsi14 < 30:
            reasons.append(f"RSI {rsi14:.1f} oversold")
            score += 2
        else:
            continue

        if close <= bb_lower:
            reasons.append(f"at/below lower Bollinger band ({bb_lower:.1f})")
            score += 2
        else:
            continue

        # Guard against catching a falling knife: require the stock isn't in freefall
        # below its own longer-term average.
        if close < ema50 * 0.90:
            continue
        reasons.append(f"within 10% of EMA50 ({ema50:.1f}) — not in freefall")
        score += 1

        r5 = ind.get("return_5d")
        if r5 is not None and r5 < 0:
            reasons.append(f"5-day return {r5:+.1f}%")
            score += min(abs(r5) / 5, 1.0)

        entry = close
        stop = close - 1.5 * atr14
        target = bb_mid                    # reversion to the 20-day mean
        if target <= entry:
            continue
        rr = (target - entry) / (entry - stop)

        out.append(Candidate(
            symbol=sym, playbook="mean_reversion", direction="LONG", close=close,
            suggested_entry=round(entry, 2), suggested_stop=round(stop, 2),
            suggested_target=round(target, 2), reward_risk=round(rr, 2),
            score=round(score, 2), reasons=reasons, indicators=ind,
        ))

    return sorted(out, key=lambda c: c.score, reverse=True)


def scan_for_playbook(playbook: str, limit: int = 8,
                      symbols: list[str] | None = None,
                      as_of=None, provider=None) -> list[Candidate]:
    """Dispatch to the playbook the regime selected.

    `symbols` lets research scan a point-in-time universe of hundreds of names
    while live trading stays on the 35 in watchlist.py. Two universes, two jobs:
    widening the traded list is a risk decision for a weekly review, widening
    the studied list is free.
    """
    if playbook == "momentum_long":
        return scan_momentum(symbols, as_of, provider)[:limit]
    if playbook == "mean_reversion":
        return scan_mean_reversion(symbols, as_of, provider)[:limit]
    return []  # defensive / no_new_positions → nothing to scan


if __name__ == "__main__":
    import json
    from .regime import classify
    r = classify()
    print(f"Regime: {r.regime} → playbook {r.playbook}")
    print(json.dumps([c.to_dict() for c in scan_for_playbook(r.playbook)], indent=2))
