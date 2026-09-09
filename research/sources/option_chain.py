"""
PERISHABLE — option chain snapshots.

This is the single most irreplaceable feed in the project. NSE publishes the
live chain and does not archive it. Open interest, implied volatility and the
shape of the volatility smile at 10:15 on a Tuesday exist for as long as you are
looking at them and then they are gone permanently. No vendor sells the history
back cheaply, and no amount of later effort reconstructs it.

Every trading day this does not run is a day that can never be recovered. That
is the entire reason the recorder ships before the replay engine, before the
backfill, and before EXP-B1 — none of which lose anything by waiting.

knowledge_time == event_time == the snapshot instant. A live observation is
known exactly when it is true; there is no publication lag to model.

No broker dependency, deliberately. The INDstocks token expires every 24 hours
and arrives by Telegram when Vaibhav remembers to send it. A recorder that
authenticated through the broker would go silent on precisely the days he is
busy — and those gaps would be permanent.
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

from ..store import Store, now_ist
from .base import Http, POLITE_DELAY

DATASET = "option_chain"
SOURCE = "nse_option_chain"

NSE_HOME = "https://www.nseindia.com/option-chain"
INDEX_URL = "https://www.nseindia.com/api/option-chain-indices?symbol={sym}"
EQUITY_URL = "https://www.nseindia.com/api/option-chain-equities?symbol={sym}"

DEFAULT_INDICES = ("NIFTY", "BANKNIFTY", "FINNIFTY")

# Storing every strike is wasteful and storing only ATM is myopic. The band
# below keeps the part of the surface where the information lives: the wings
# beyond ~15% are illiquid enough that their OI is mostly noise.
STRIKE_BAND_PCT = 0.15

_HEADERS = {"Accept": "application/json, text/plain, */*",
            "Referer": "https://www.nseindia.com/option-chain"}


def fetch(symbol: str, http: Optional[Http] = None, equity: bool = False) -> dict:
    http = http or Http()
    http.prime("https://www.nseindia.com/")
    url = (EQUITY_URL if equity else INDEX_URL).format(sym=symbol.upper())
    return http.get_json(url, _HEADERS)


def parse(payload: dict, symbol: str, snapshot_at: Optional[dt.datetime] = None,
          band_pct: float = STRIKE_BAND_PCT) -> list[dict]:
    """Chain JSON -> store rows. Pure; tested against a fixture."""
    snapshot_at = snapshot_at or now_ist()
    records = (payload or {}).get("records") or {}
    data = records.get("data") or []
    underlying = records.get("underlyingValue")

    rows: list[dict] = []
    for item in data:
        strike = item.get("strikePrice")
        expiry = item.get("expiryDate")
        if strike is None or not expiry:
            continue
        if underlying and band_pct:
            if abs(strike - underlying) > underlying * band_pct:
                continue

        leg = {}
        for side in ("CE", "PE"):
            d = item.get(side) or {}
            if not d:
                continue
            leg[side] = {
                "oi": d.get("openInterest"),
                "oi_change": d.get("changeinOpenInterest"),
                "volume": d.get("totalTradedVolume"),
                "iv": d.get("impliedVolatility"),
                "ltp": d.get("lastPrice"),
                "bid": d.get("bidprice"),
                "ask": d.get("askPrice"),
            }
        if not leg:
            continue

        rows.append({
            "dataset": DATASET,
            "entity": symbol.upper(),
            "event_time": snapshot_at,
            "knowledge_time": snapshot_at,
            "source": SOURCE,
            "confidence": "observed",
            "payload": {
                "underlying": symbol.upper(),
                "underlying_value": underlying,
                "expiry": expiry,
                "strike": strike,
                "timestamp_reported": records.get("timestamp"),
                **leg,
            },
        })
    return rows


def load(store: Store, symbols=DEFAULT_INDICES, http: Optional[Http] = None) -> dict:
    import time
    http = http or Http()
    seen = new = 0
    per_symbol, failures = {}, {}

    for sym in symbols:
        try:
            payload = fetch(sym, http)
            rows = parse(payload, sym)
            inserted = store.append_many(rows)
            seen += len(rows)
            new += inserted
            per_symbol[sym] = {"strikes": len(rows), "new": inserted}
        except Exception as e:
            # One dead symbol must not cost the others their snapshot.
            failures[sym] = f"{type(e).__name__}: {e}"
        time.sleep(POLITE_DELAY)

    # A source that reports success while recording nothing is the worst
    # possible outcome for a perishable feed: the run log says green, the data
    # is gone, and nobody looks again for months. Partial failure is tolerated;
    # total failure must be loud.
    if symbols and not per_symbol:
        raise RuntimeError(
            f"option chain: all {len(symbols)} symbol(s) failed, nothing recorded. "
            f"First error: {next(iter(failures.values()), 'unknown')}")

    return {"rows_seen": seen, "rows_new": new,
            "per_symbol": per_symbol, "failures": failures}
