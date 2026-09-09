"""
Market data and technical indicators.

Data sourcing strategy:
  - Daily OHLCV / indicators  → yfinance (free, adequate for a daily-timeframe strategy)
  - Live quotes at execution  → the configured broker (authoritative — it is what we
    actually trade against)
  - Positions / funds / orders → the configured broker, via engine/broker.py

This split keeps historical-data subscriptions optional, and means nothing here changes
when the broker changes.
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Symbols
# ---------------------------------------------------------------------------

NIFTY_YF = "^NSEI"
VIX_YF = "^INDIAVIX"


def to_yf(symbol: str, exchange: str = "NSE") -> str:
    """Convert an NSE/BSE trading symbol to its Yahoo Finance ticker."""
    if symbol.startswith("^"):
        return symbol
    suffix = ".NS" if exchange.upper() == "NSE" else ".BO"
    return f"{symbol.upper()}{suffix}"


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

def get_history(symbol: str, days: int = 400, exchange: str = "NSE",
                as_of=None, provider=None) -> pd.DataFrame:
    """Daily OHLCV. Returns a DataFrame indexed by date with columns
    open/high/low/close/volume. Empty DataFrame on failure — callers must check.

    THE AS-OF SEAM
    --------------
    Every historical read in this project funnels through this function, and it
    computes its own end date from the system clock. That single line is what
    makes the live stack unable to answer "what would you have thought on 14
    March 2023" — and therefore what any historical research has to get past.

    Passing `as_of` asks for a point-in-time view. `provider` supplies it:

        provider(symbol, days, exchange, as_of) -> DataFrame

    The provider is injected rather than imported on purpose. This module must
    never know that a research package exists — the dependency runs one way,
    research -> engine, and adding an import here would quietly invert it.

    An `as_of` with no provider RAISES. It must never fall through to the live
    fetch below, because that would silently return today's data for a question
    about 2023 — the exact look-ahead this parameter exists to prevent, in the
    one form nobody would notice.

    Default `as_of=None` is the live path, unchanged byte for byte.
    """
    if as_of is not None:
        if provider is None:
            raise ValueError(
                f"get_history({symbol!r}, as_of={as_of!r}) needs a provider. "
                f"Engine cannot honour an as-of read from a live feed without "
                f"look-ahead, and returning today's data here would be a silent "
                f"leak rather than an error."
            )
        return provider(symbol, days, exchange, as_of)

    import yfinance as yf

    ticker = to_yf(symbol, exchange)
    end = dt.date.today() + dt.timedelta(days=1)
    start = end - dt.timedelta(days=int(days * 1.6) + 10)  # pad for weekends/holidays

    try:
        df = yf.download(
            ticker, start=start, end=end, progress=False, auto_adjust=False, threads=False
        )
    except Exception:
        return pd.DataFrame()

    if df is None or df.empty:
        return pd.DataFrame()

    # yfinance returns MultiIndex columns for single tickers in newer versions
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df = df.rename(columns=str.lower)
    keep = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
    df = df[keep].dropna()
    return df.tail(days)


# ---------------------------------------------------------------------------
# Indicators — all take/return plain pandas, no TA library dependency
# ---------------------------------------------------------------------------

def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI.

    The zero-average-loss case is handled explicitly rather than left to NaN: an
    uninterrupted rise is RSI 100 (maximally overbought), not "unknown". Masking that as
    a neutral 50 would hide exactly the signal the reading exists to give.
    """
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()

    rs = avg_gain.divide(avg_loss.replace(0.0, float("nan")))
    out = 100 - (100 / (1 + rs))

    # avg_loss == 0 with gains present → 100. Both zero (dead flat) → 50.
    no_loss = (avg_loss == 0) & (avg_gain > 0)
    both_zero = (avg_loss == 0) & (avg_gain == 0)
    out = out.mask(no_loss, 100.0)
    out = out.mask(both_zero, 50.0)
    return out.fillna(50.0)


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    return pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    return true_range(df).ewm(alpha=1 / period, adjust=False).mean()


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average Directional Index — trend strength (not direction). >20 = trending.

    Note on the divide-by-zero handling: `pd.NA` must NOT be used here. Inserting it into
    a float series promotes the whole series to object dtype, and pandas then refuses to
    run .ewm() on it ("No numeric types to aggregate"). Use np.nan, which keeps float64.
    Flat sessions where both directional movements are zero make this a real case, not a
    theoretical one.
    """
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = ((up > down) & (up > 0)) * up.clip(lower=0)
    minus_dm = ((down > up) & (down > 0)) * down.clip(lower=0)

    tr_smooth = true_range(df).ewm(alpha=1 / period, adjust=False).mean()
    tr_smooth = tr_smooth.replace(0.0, np.nan)
    plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False).mean() / tr_smooth
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False).mean() / tr_smooth

    denom = (plus_di + minus_di).replace(0.0, np.nan)
    dx = (100 * (plus_di - minus_di).abs() / denom).astype("float64")
    return dx.ewm(alpha=1 / period, adjust=False).mean().fillna(0.0)


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = ema(series, fast) - ema(series, slow)
    sig = ema(line, signal)
    return pd.DataFrame({"macd": line, "signal": sig, "histogram": line - sig})


def bollinger(series: pd.Series, period: int = 20, num_std: float = 2.0) -> pd.DataFrame:
    mid = sma(series, period)
    std = series.rolling(period).std()
    return pd.DataFrame({"mid": mid, "upper": mid + num_std * std, "lower": mid - num_std * std})


def percentile_rank(series: pd.Series, lookback: int = 126) -> float:
    """Where the latest value sits within its own recent history, 0-100.
    126 sessions ≈ 6 months. Used for volatility regime."""
    window = series.dropna().tail(lookback)
    if len(window) < 20:
        return 50.0
    latest = window.iloc[-1]
    return float((window < latest).sum() / len(window) * 100)


def indicator_snapshot(df: pd.DataFrame) -> dict:
    """All the indicators the strategy uses, for the most recent bar."""
    if df.empty or len(df) < 60:
        return {}

    close = df["close"]
    macd_df = macd(close)
    bb = bollinger(close)
    atr_series = atr(df)
    last = -1

    def val(x):
        try:
            v = float(x)
            return round(v, 4) if v == v else None  # filter NaN
        except (TypeError, ValueError):
            return None

    return {
        "close": val(close.iloc[last]),
        "ema20": val(ema(close, 20).iloc[last]),
        "ema50": val(ema(close, 50).iloc[last]),
        "ema200": val(ema(close, 200).iloc[last]) if len(df) >= 200 else None,
        "rsi14": val(rsi(close).iloc[last]),
        "adx14": val(adx(df).iloc[last]),
        "atr14": val(atr_series.iloc[last]),
        "atr_pct": val(atr_series.iloc[last] / close.iloc[last] * 100),
        "atr_percentile_6m": val(percentile_rank(atr_series / close * 100)),
        "macd_hist": val(macd_df["histogram"].iloc[last]),
        "macd_hist_prev": val(macd_df["histogram"].iloc[last - 1]),
        "bb_upper": val(bb["upper"].iloc[last]),
        "bb_mid": val(bb["mid"].iloc[last]),
        "bb_lower": val(bb["lower"].iloc[last]),
        "high_20d": val(df["high"].tail(20).max()),
        "low_20d": val(df["low"].tail(20).min()),
        "volume": val(df["volume"].iloc[last]),
        "volume_avg20": val(df["volume"].tail(20).mean()),
        "volume_ratio": val(df["volume"].iloc[last] / df["volume"].tail(20).mean()),
        "return_5d": val((close.iloc[last] / close.iloc[-6] - 1) * 100) if len(df) > 6 else None,
        "return_20d": val((close.iloc[last] / close.iloc[-21] - 1) * 100) if len(df) > 21 else None,
        "avg_traded_value_20d_cr": val(
            (df["close"] * df["volume"]).tail(20).mean() / 1e7
        ),
    }


# ---------------------------------------------------------------------------
# Live quotes (Kite)
# ---------------------------------------------------------------------------

def get_live_quotes(symbols: list[str], exchange: str = "NSE") -> dict:
    """Live quotes from whichever broker is configured. Returns {SYMBOL: last_price}.

    Missing symbols are simply absent. Callers must treat an absent price as unknown and
    stop, never as zero — a zero price would sail through arithmetic and produce a
    confidently wrong position size.
    """
    from .broker import get_broker
    try:
        return get_broker().quote(symbols, exchange)
    except Exception:
        return {}


def get_ltp(symbol: str, exchange: str = "NSE") -> Optional[float]:
    """Last traded price for one symbol, or None if unavailable."""
    return get_live_quotes([symbol], exchange).get(symbol.upper())
