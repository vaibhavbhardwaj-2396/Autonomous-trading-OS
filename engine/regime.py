"""
Layer 1 — market regime classification.

Runs on Nifty 50 daily data before any individual stock is looked at. The regime decides
WHICH playbook is allowed to run, because momentum and mean-reversion are opposite bets
and running the wrong one in the wrong market is the standard way systematic strategies
bleed out.

Deliberately simple, threshold-based and fully auditable — every classification can be
explained after the fact from the numbers it recorded, which is what makes it possible to
tell later whether it was any good.

An HMM/regime-switching model runs alongside in SHADOW MODE (engine/regime_hmm.py): it
classifies every run and both calls are logged to memory/regime_log.jsonl, but only the
thresholds here influence trading. The HMM earns promotion by out-predicting these rules
on live data, not by being more sophisticated. See docs/METHODOLOGY.md §2b.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict

from . import market_data as md

TRENDING_UP = "TRENDING_UP"
TRENDING_DOWN = "TRENDING_DOWN"
RANGE_BOUND = "RANGE_BOUND"
HIGH_VOLATILITY = "HIGH_VOLATILITY"
UNKNOWN = "UNKNOWN"

# Thresholds — starting hypotheses, to be validated against logged outcomes.
ADX_TRENDING = 20.0
ATR_PERCENTILE_STRESS = 80.0
VIX_STRESS = 20.0
GAP_STRESS_PCT = 2.0

PLAYBOOK = {
    TRENDING_UP: "momentum_long",
    TRENDING_DOWN: "defensive",      # shorts need puts → Tier 1+. Below that: mostly cash.
    RANGE_BOUND: "mean_reversion",
    HIGH_VOLATILITY: "no_new_positions",
    UNKNOWN: "no_new_positions",
}


@dataclass
class Regime:
    regime: str
    playbook: str
    confidence: str          # high | medium | low — honest about ambiguous readings
    evidence: dict
    notes: list[str]
    shadow: dict = None      # HMM classification, logged for comparison, never acted on

    def to_dict(self) -> dict:
        return asdict(self)


def shadow_classification() -> dict:
    """The HMM's opinion. Never influences the decision — see engine/regime_hmm.py for
    why it has to earn promotion on live data rather than on sophistication."""
    try:
        from .regime_hmm import classify as hmm_classify
        return hmm_classify()
    except Exception as e:
        return {"available": False, "reason": f"{type(e).__name__}: {e}"}


def classify_thresholds(as_of=None, provider=None) -> Regime:
    """Threshold classifier — the authoritative one. Drives all trading decisions.

    `as_of` / `provider` are the replay seam (see market_data.get_history). Both
    default to None, which is the live path, unchanged.
    """
    notes: list[str] = []

    nifty = md.get_history(md.NIFTY_YF, days=400, as_of=as_of, provider=provider)
    if nifty.empty or len(nifty) < 200:
        return Regime(
            UNKNOWN, PLAYBOOK[UNKNOWN], "low",
            {"error": "insufficient Nifty history"},
            ["Could not fetch enough Nifty history — no new positions this run."],
        )

    ind = md.indicator_snapshot(nifty)
    close = ind.get("close")
    ema50 = ind.get("ema50")
    ema200 = ind.get("ema200")
    adx_val = ind.get("adx14") or 0.0
    atr_pct = ind.get("atr_pct")
    atr_pctile = ind.get("atr_percentile_6m") or 50.0

    # India VIX — informative but optional; absence must not block classification.
    vix_level = None
    vix_change = None
    vix_df = md.get_history(md.VIX_YF, days=60, as_of=as_of, provider=provider)
    if not vix_df.empty and len(vix_df) > 5:
        vix_level = round(float(vix_df["close"].iloc[-1]), 2)
        vix_change = round(
            float(vix_df["close"].iloc[-1] / vix_df["close"].iloc[-6] - 1) * 100, 2
        )
    else:
        notes.append("India VIX unavailable this run — volatility read is ATR-only.")

    # Overnight gap (today's open vs yesterday's close)
    gap_pct = None
    if len(nifty) >= 2:
        prev_close = float(nifty["close"].iloc[-2])
        today_open = float(nifty["open"].iloc[-1])
        if prev_close:
            gap_pct = round((today_open / prev_close - 1) * 100, 2)

    evidence = {
        "nifty_close": close,
        "ema50": ema50,
        "ema200": ema200,
        "adx14": adx_val,
        "atr_pct": atr_pct,
        "atr_percentile_6m": atr_pctile,
        "india_vix": vix_level,
        "vix_change_5d_pct": vix_change,
        "gap_pct": gap_pct,
        "return_5d_pct": ind.get("return_5d"),
        "return_20d_pct": ind.get("return_20d"),
        "as_of": str(nifty.index[-1].date()),
    }

    # --- Stress check first: it overrides everything ---
    stress_signals = []
    if atr_pctile >= ATR_PERCENTILE_STRESS:
        stress_signals.append(f"ATR at {atr_pctile:.0f}th percentile of last 6 months")
    if vix_level is not None and vix_level >= VIX_STRESS:
        stress_signals.append(f"India VIX elevated at {vix_level}")
    if gap_pct is not None and abs(gap_pct) >= GAP_STRESS_PCT:
        stress_signals.append(f"index gapped {gap_pct:+.2f}%")

    if stress_signals:
        notes.append(
            "High-volatility regime: " + "; ".join(stress_signals)
            + ". No new positions — manage existing only."
        )
        return Regime(HIGH_VOLATILITY, PLAYBOOK[HIGH_VOLATILITY], "high", evidence, notes)

    # --- Trend classification ---
    if None in (close, ema50, ema200):
        notes.append("Missing moving averages — cannot classify with confidence.")
        return Regime(UNKNOWN, PLAYBOOK[UNKNOWN], "low", evidence, notes)

    trending = adx_val >= ADX_TRENDING
    stacked_up = close > ema50 > ema200
    stacked_down = close < ema50 < ema200

    if trending and stacked_up:
        confidence = "high" if adx_val >= 25 else "medium"
        notes.append(
            f"Uptrend: price {close:.0f} > EMA50 {ema50:.0f} > EMA200 {ema200:.0f}, "
            f"ADX {adx_val:.1f}. Momentum long playbook active."
        )
        return Regime(TRENDING_UP, PLAYBOOK[TRENDING_UP], confidence, evidence, notes)

    if trending and stacked_down:
        confidence = "high" if adx_val >= 25 else "medium"
        notes.append(
            f"Downtrend: price {close:.0f} < EMA50 {ema50:.0f} < EMA200 {ema200:.0f}, "
            f"ADX {adx_val:.1f}. Expressing shorts needs long puts (Tier 1+); at Tier 0 "
            f"the correct action is usually to stay in cash."
        )
        return Regime(TRENDING_DOWN, PLAYBOOK[TRENDING_DOWN], confidence, evidence, notes)

    notes.append(
        f"No clear trend: ADX {adx_val:.1f} (<{ADX_TRENDING} = choppy), moving averages "
        f"tangled. Mean-reversion playbook active — and note that mean reversion is ONLY "
        f"valid in this regime."
    )
    confidence = "medium" if adx_val < 15 else "low"
    return Regime(RANGE_BOUND, PLAYBOOK[RANGE_BOUND], confidence, evidence, notes)


def classify(log: bool = True, with_shadow: bool = True,
             as_of=None, provider=None) -> Regime:
    """Authoritative regime call, with the HMM's shadow opinion attached for logging.

    The threshold classifier decides; the shadow is recorded so a future weekly review can
    test whether the probabilistic model would have been better. If the shadow errors for
    any reason it is simply absent — it must never affect the run.
    """
    reg = classify_thresholds(as_of=as_of, provider=provider)

    # The HMM caches a fit over a fixed trailing window and is NOT as-of aware,
    # so running it during replay would leak future data into a "shadow" that
    # looks harmless precisely because it is never acted on. Skipping it is the
    # honest option; making it as-of aware is a separate piece of work.
    if as_of is not None and with_shadow:
        with_shadow = False
        reg.notes.append("[replay] shadow HMM skipped — it is not as-of aware.")

    if with_shadow:
        reg.shadow = shadow_classification()
        if reg.shadow.get("available"):
            reg.notes.append(
                f"[shadow HMM, not acted on] {reg.shadow['regime']} at "
                f"{reg.shadow['confidence']:.0%} confidence; "
                f"{reg.shadow['probability_of_staying']:.0%} chance of persisting."
            )

    if log and as_of is None:
        try:
            from .journal import record_regime
            record_regime(reg.regime, reg.playbook, reg.confidence, reg.evidence, reg.shadow)
        except Exception:
            pass  # logging must never break a run

    return reg


if __name__ == "__main__":
    import json
    print(json.dumps(classify().to_dict(), indent=2, default=str))
