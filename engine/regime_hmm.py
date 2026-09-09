"""
Hidden Markov Model regime detection — SHADOW MODE.

The threshold classifier in regime.py drives all trading decisions. This module runs
alongside it, logging what it *would* have said, so that after enough live decisions we
can answer with data: was the probabilistic model actually better?

Why shadow mode rather than switching to it:
  - An HMM fitted on history is easy to over-fit, and it produces confident-looking
    output regardless. Confidence is not accuracy.
  - We can fit it today (years of Nifty data are free), but we cannot know whether it
    improves OUR decisions until it has called regimes on days we actually traded.
  - So it earns promotion by out-predicting the simple rules on live data — not by being
    more sophisticated.

What it adds over thresholds when it works:
  - Probabilities rather than a hard label, so position size could scale with certainty
  - A transition matrix, so rising probability of a regime change can be seen BEFORE the
    threshold rules flip — which is exactly where threshold systems whipsaw

Promotion criteria (decide in a weekly review, needs Vaibhav's approval):
  ≥60 logged regime calls AND the HMM's classification is better correlated with forward
  5-day Nifty returns than the threshold classifier's.

Requires `hmmlearn`. Degrades to "unavailable" if absent — never blocks a run.
"""

from __future__ import annotations

import json
import pickle
import datetime as dt
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from . import market_data as md

PROJECT_ROOT = Path(__file__).parent.parent
MODEL_CACHE = PROJECT_ROOT / "memory" / ".hmm_model.pkl"

N_STATES = 3
FIT_DAYS = 1800          # ~7 years of sessions
REFIT_AFTER_DAYS = 7     # refit weekly; daily refits add noise, not information
RANDOM_SEED = 42         # fixed so classifications are reproducible run to run

# Bump whenever the feature set or labelling scheme changes. A cache written by an older
# scheme is silently wrong rather than obviously broken, which is the worse failure —
# so it is discarded on sight instead of being served until its 7-day expiry.
SCHEMA_VERSION = 2


def _features(df: pd.DataFrame) -> pd.DataFrame:
    """Return, realized volatility and range — the minimum that separates market states
    without inviting over-fitting."""
    out = pd.DataFrame(index=df.index)
    out["ret"] = df["close"].pct_change()
    out["vol5"] = out["ret"].rolling(5).std()
    out["range"] = (df["high"] - df["low"]) / df["close"]
    return out.dropna()


def _label_states(model, feats: np.ndarray, states: np.ndarray) -> tuple[dict, dict]:
    """Name each cluster after what it actually IS, and return its statistics alongside.

    Important framing: these labels describe the *historical character of a cluster*, not
    a forecast. Calling the highest-mean-return state "BULL" was actively misleading in
    live use — on a mostly-rising index the calm low-volatility state wins on mean return,
    so the model reported "BULL, 100% confident" on a day the index was below both its
    50- and 200-day averages and down 3% on the month. The model was right about which
    cluster we were in; the label lied about what that meant.

    So labels now name volatility first (which is what the clusters genuinely separate on)
    and drift second, and the raw statistics travel with the output so the label can never
    be the only thing a reader sees.
    """
    stats = {}
    for s in range(model.n_components):
        mask = states == s
        if not mask.any():
            stats[s] = {"mean_daily_return_pct": 0.0, "mean_vol_pct": 0.0, "share_pct": 0.0}
            continue
        stats[s] = {
            "mean_daily_return_pct": round(float(feats[mask, 0].mean()) * 100, 4),
            "mean_vol_pct": round(float(feats[mask, 1].mean()) * 100, 4),
            "share_pct": round(float(mask.sum()) / len(states) * 100, 1),
        }

    by_vol = sorted(stats, key=lambda s: stats[s]["mean_vol_pct"])
    vol_rank = {s: i for i, s in enumerate(by_vol)}

    labels = {}
    for s in stats:
        vol_band = ["CALM", "NORMAL", "TURBULENT"][min(vol_rank[s], 2)] \
            if len(stats) >= 3 else ("CALM" if vol_rank[s] == 0 else "TURBULENT")
        drift = stats[s]["mean_daily_return_pct"]
        if drift > 0.02:
            drift_word = "UPDRIFT"
        elif drift < -0.02:
            drift_word = "DOWNDRIFT"
        else:
            drift_word = "FLAT"
        labels[s] = f"{vol_band}_{drift_word}"

    return labels, stats


def _fit(df: pd.DataFrame):
    from hmmlearn.hmm import GaussianHMM

    feats_df = _features(df)
    X = feats_df.values
    model = GaussianHMM(
        n_components=N_STATES,
        covariance_type="full",
        n_iter=200,
        random_state=RANDOM_SEED,
    )
    model.fit(X)
    states = model.predict(X)
    labels, stats = _label_states(model, X, states)
    return model, labels, stats, feats_df


def _load_cached():
    if not MODEL_CACHE.exists():
        return None
    try:
        with open(MODEL_CACHE, "rb") as f:
            blob = pickle.load(f)
        if blob.get("schema_version") != SCHEMA_VERSION:
            return None  # stale labelling scheme — refit rather than mislead
        fitted = dt.date.fromisoformat(blob["fitted_on"])
        if (dt.date.today() - fitted).days > REFIT_AFTER_DAYS:
            return None
        return blob
    except Exception:
        return None


def classify(force_refit: bool = False) -> dict:
    """Current regime probabilities. Never raises — returns {'available': False} with a
    reason if anything is missing, because a shadow model must never block a run."""
    try:
        import hmmlearn  # noqa: F401
    except ImportError:
        return {"available": False,
                "reason": "hmmlearn not installed (pip install hmmlearn) — shadow mode inactive"}

    df = md.get_history(md.NIFTY_YF, days=FIT_DAYS)
    if df.empty or len(df) < 300:
        return {"available": False, "reason": "insufficient Nifty history to fit"}

    blob = None if force_refit else _load_cached()
    if blob is None:
        try:
            model, labels, state_stats, feats_df = _fit(df)
        except Exception as e:
            return {"available": False, "reason": f"fit failed: {type(e).__name__}: {e}"}
        blob = {
            "model": model,
            "labels": {int(k): v for k, v in labels.items()},
            "state_stats": {int(k): v for k, v in state_stats.items()},
            "schema_version": SCHEMA_VERSION,
            "fitted_on": dt.date.today().isoformat(),
            "n_obs": len(feats_df),
        }
        try:
            MODEL_CACHE.parent.mkdir(parents=True, exist_ok=True)
            with open(MODEL_CACHE, "wb") as f:
                pickle.dump(blob, f)
        except Exception:
            pass  # caching is an optimisation, not a requirement

    model, labels = blob["model"], blob["labels"]
    state_stats = blob.get("state_stats", {})
    feats_df = _features(df)
    X = feats_df.values

    try:
        posteriors = model.predict_proba(X)
        current = posteriors[-1]
        state = int(np.argmax(current))
        trans = model.transmat_[state]
    except Exception as e:
        return {"available": False, "reason": f"inference failed: {type(e).__name__}: {e}"}

    probs = {labels.get(i, f"state_{i}"): round(float(p), 4)
             for i, p in enumerate(current)}
    transitions = {labels.get(i, f"state_{i}"): round(float(p), 4)
                   for i, p in enumerate(trans)}

    confidence = float(current[state])
    stay_prob = float(trans[state])

    return {
        "available": True,
        "regime": labels.get(state, f"state_{state}"),
        "confidence": round(confidence, 4),
        "state_probabilities": probs,
        "transition_from_here": transitions,
        "probability_of_staying": round(stay_prob, 4),
        "current_state_stats": state_stats.get(state, {}),
        "all_state_stats": {labels.get(i, f"state_{i}"): v for i, v in state_stats.items()},
        "fitted_on": blob["fitted_on"],
        "observations": blob["n_obs"],
        "as_of": str(df.index[-1].date()),
        "note": ("SHADOW MODE — logged for comparison only. This does not influence any "
                 "trading decision. regime.py's threshold classifier is authoritative."),
        "interpretation": _interpret(
            labels.get(state, "?"), confidence, stay_prob, state_stats.get(state, {})
        ),
    }


def _interpret(regime: str, confidence: float, stay_prob: float,
               stats: dict | None = None) -> str:
    parts = [f"Most likely state: {regime} ({confidence:.0%} posterior probability)."]
    if stats:
        parts.append(
            f"That cluster historically averaged "
            f"{stats.get('mean_daily_return_pct', 0):+.3f}%/day at "
            f"{stats.get('mean_vol_pct', 0):.3f}% daily volatility, covering "
            f"{stats.get('share_pct', 0):.0f}% of sessions."
        )
    parts.append(
        "This describes which historical cluster today resembles — it is NOT a direction "
        "forecast, and it says nothing about where price sits relative to its moving "
        "averages."
    )
    if confidence < 0.6:
        parts.append("Low confidence — the model sees a genuinely ambiguous market.")
    if stay_prob < 0.85:
        parts.append(
            f"Only {stay_prob:.0%} chance of remaining in this state tomorrow — "
            f"elevated transition risk."
        )
    else:
        parts.append(f"{stay_prob:.0%} chance of persisting — regime looks stable.")
    return " ".join(parts)


if __name__ == "__main__":
    print(json.dumps(classify(), indent=2, default=str))
