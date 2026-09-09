"""
Indicator math verification against synthetic series with known properties.

Can't reach live market data from every environment, but the arithmetic must be provably
right regardless — a subtly wrong RSI would poison every downstream decision.

Run with:  python -m tests.test_indicators
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from engine import market_data as md  # noqa: E402

PASSED, FAILED = 0, 0


def check(name, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  ✓ {name}")
    else:
        FAILED += 1
        print(f"  ✗ {name}  {detail}")


def make_df(closes):
    """Build an OHLCV frame from a close series with plausible highs/lows."""
    closes = pd.Series(closes, dtype="float64")
    idx = pd.date_range("2025-01-01", periods=len(closes), freq="B")
    return pd.DataFrame({
        "open": closes.shift(1).fillna(closes.iloc[0]).values,
        "high": (closes * 1.01).values,
        "low": (closes * 0.99).values,
        "close": closes.values,
        "volume": np.full(len(closes), 1_000_000.0),
    }, index=idx)


n = 300
print("\n--- EMA / SMA ---")
flat = pd.Series([100.0] * n)
check("EMA of a constant equals the constant", abs(md.ema(flat, 20).iloc[-1] - 100.0) < 1e-9)
check("SMA of a constant equals the constant", abs(md.sma(flat, 20).iloc[-1] - 100.0) < 1e-9)

rising = pd.Series(np.linspace(100, 200, n))
check("EMA lags a rising series", md.ema(rising, 50).iloc[-1] < rising.iloc[-1])
check("fast EMA is above slow EMA in an uptrend",
      md.ema(rising, 20).iloc[-1] > md.ema(rising, 50).iloc[-1])

falling = pd.Series(np.linspace(200, 100, n))
check("fast EMA is below slow EMA in a downtrend",
      md.ema(falling, 20).iloc[-1] < md.ema(falling, 50).iloc[-1])

print("\n--- RSI ---")
check("RSI is ~100 on a monotonic rise", md.rsi(rising).iloc[-1] > 95,
      f"got {md.rsi(rising).iloc[-1]:.2f}")
check("RSI is ~0 on a monotonic fall", md.rsi(falling).iloc[-1] < 5,
      f"got {md.rsi(falling).iloc[-1]:.2f}")
check("RSI is mid-range on a flat series",
      40 < md.rsi(flat).iloc[-1] < 60, f"got {md.rsi(flat).iloc[-1]:.2f}")
osc = pd.Series([100 + 5 * np.sin(i / 3) for i in range(n)])
check("RSI stays within 0-100 bounds", 0 <= md.rsi(osc).iloc[-1] <= 100)

print("\n--- ATR ---")
df_flat = make_df([100.0] * n)
atr_flat = md.atr(df_flat).iloc[-1]
check("ATR is small for a flat series", atr_flat < 3.0, f"got {atr_flat:.4f}")

volatile = [100 + (10 if i % 2 else -10) for i in range(n)]
atr_vol = md.atr(make_df(volatile)).iloc[-1]
check("ATR is larger for a volatile series", atr_vol > atr_flat * 3,
      f"flat={atr_flat:.2f} vol={atr_vol:.2f}")

print("\n--- ADX ---")
adx_trend = md.adx(make_df(np.linspace(100, 200, n))).iloc[-1]
adx_chop = md.adx(make_df([100 + 2 * np.sin(i / 2) for i in range(n)])).iloc[-1]
check("ADX is high in a clean trend", adx_trend > 25, f"got {adx_trend:.2f}")
check("ADX is lower in chop than in trend", adx_chop < adx_trend,
      f"chop={adx_chop:.2f} trend={adx_trend:.2f}")

print("\n--- MACD ---")
m_up = md.macd(rising)
check("MACD line positive in an uptrend", m_up["macd"].iloc[-1] > 0)
m_down = md.macd(falling)
check("MACD line negative in a downtrend", m_down["macd"].iloc[-1] < 0)

print("\n--- Bollinger ---")
bb = md.bollinger(osc)
check("upper band above mid", bb["upper"].iloc[-1] > bb["mid"].iloc[-1])
check("lower band below mid", bb["lower"].iloc[-1] < bb["mid"].iloc[-1])
bb_flat = md.bollinger(flat)
check("bands collapse on a constant series",
      abs(bb_flat["upper"].iloc[-1] - bb_flat["lower"].iloc[-1]) < 1e-6)

print("\n--- Percentile rank ---")
ramp = pd.Series(np.linspace(0, 100, 200))
check("latest value of a rising series ranks near 100",
      md.percentile_rank(ramp) > 95, f"got {md.percentile_rank(ramp):.1f}")
check("latest value of a falling series ranks near 0",
      md.percentile_rank(pd.Series(np.linspace(100, 0, 200))) < 5)

print("\n--- Snapshot ---")
snap = md.indicator_snapshot(make_df(np.linspace(100, 200, n)))
required = ["close", "ema20", "ema50", "ema200", "rsi14", "adx14", "atr14",
            "atr_pct", "macd_hist", "bb_upper", "high_20d", "volume_ratio",
            "avg_traded_value_20d_cr"]
missing = [k for k in required if k not in snap]
check("snapshot contains all required keys", not missing, f"missing {missing}")
check("snapshot has no NaN leakage",
      all(v is None or v == v for v in snap.values()))
check("uptrend snapshot shows close above EMA50",
      snap["close"] > snap["ema50"], f"{snap['close']} vs {snap['ema50']}")

print("\n--- Degradation ---")
check("empty frame yields empty snapshot", md.indicator_snapshot(pd.DataFrame()) == {})
check("too-short frame yields empty snapshot",
      md.indicator_snapshot(make_df([100.0] * 10)) == {})

# --------------------------------------------------------------------------------------
# Regression: pd.NA promoting a float series to object dtype broke .ewm() in production.
# The original synthetic data was too well-behaved to trigger it — real stocks have flat
# sessions where directional movement is zero on both sides. Every indicator must stay
# numeric no matter what the input looks like.
# --------------------------------------------------------------------------------------
print("\n--- Regression: degenerate data must stay numeric ---")


def truly_flat_df(n=300):
    """No movement at all: high == low == close. Forces zero DI on both sides."""
    idx = pd.date_range("2025-01-01", periods=n, freq="B")
    return pd.DataFrame({
        "open": [100.0] * n, "high": [100.0] * n, "low": [100.0] * n,
        "close": [100.0] * n, "volume": [1_000_000.0] * n,
    }, index=idx)


def stepped_df(n=300):
    """Long flat stretches punctuated by jumps — the shape that actually occurs in
    illiquid or halted names."""
    closes = []
    for i in range(n):
        closes.append(100.0 if (i // 20) % 2 == 0 else 105.0)
    return make_df(closes)


for label, frame in [("all-flat", truly_flat_df()), ("stepped", stepped_df())]:
    try:
        a = md.adx(frame)
        check(f"ADX runs on {label} data", True)
        check(f"ADX stays numeric dtype on {label}",
              pd.api.types.is_numeric_dtype(a), f"got {a.dtype}")
        check(f"ADX has no NaN on {label}", not a.isna().any())
    except Exception as e:
        check(f"ADX runs on {label} data", False, f"{type(e).__name__}: {e}")

    for fn_name, fn in [("rsi", lambda d: md.rsi(d["close"])),
                        ("atr", md.atr),
                        ("macd", lambda d: md.macd(d["close"])["histogram"]),
                        ("bollinger", lambda d: md.bollinger(d["close"])["mid"])]:
        try:
            out = fn(frame)
            check(f"{fn_name} stays numeric on {label}",
                  pd.api.types.is_numeric_dtype(out), f"got {out.dtype}")
        except Exception as e:
            check(f"{fn_name} runs on {label}", False, f"{type(e).__name__}: {e}")

    try:
        snap = md.indicator_snapshot(frame)
        check(f"snapshot builds on {label} data", isinstance(snap, dict) and bool(snap))
    except Exception as e:
        check(f"snapshot builds on {label} data", False, f"{type(e).__name__}: {e}")

print(f"\n{'=' * 46}")
print(f"  {PASSED} passed, {FAILED} failed")
print(f"{'=' * 46}\n")
sys.exit(1 if FAILED else 0)
