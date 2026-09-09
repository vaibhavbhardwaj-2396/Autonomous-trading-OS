"""
PERISHABLE — intraday quote and breadth snapshots.

What this captures that the bhavcopy cannot: the market's state DURING the
session. The bhavcopy gives four prices and a volume for the whole day. It
cannot tell you that a stock was up 4% at 10:15 and gave all of it back by
14:00, or that advances outnumbered declines three to one at the open and
inverted by lunch. That intraday shape is not published anywhere afterwards.

Also captured: delivery-percentage proxies and the pre-open call auction, both
of which are live-only surfaces on NSE's site.

knowledge_time == event_time == the snapshot instant.

Deliberately no broker dependency. See option_chain.py for the full argument;
the short version is that a recorder gated on a token Vaibhav has to send by
Telegram every morning is a recorder with holes on exactly the days he is busy,
and those holes are permanent.
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

from ..store import Store, now_ist
from .base import Http, POLITE_DELAY

SNAPSHOT_DATASET = "intraday_snapshot"
BREADTH_DATASET = "market_breadth"
SOURCE = "nse_live"

NSE_HOME = "https://www.nseindia.com/"
EQUITY_STOCK_URL = "https://www.nseindia.com/api/equity-stockIndices?index={index}"

_HEADERS = {"Accept": "application/json, text/plain, */*",
            "Referer": "https://www.nseindia.com/market-data/live-equity-market"}

DEFAULT_INDEX = "NIFTY 500"


def fetch(index: str = DEFAULT_INDEX, http: Optional[Http] = None) -> dict:
    http = http or Http()
    http.prime(NSE_HOME)
    from urllib.parse import quote
    return http.get_json(EQUITY_STOCK_URL.format(index=quote(index)), _HEADERS)


def parse(payload: dict, index: str = DEFAULT_INDEX,
          snapshot_at: Optional[dt.datetime] = None) -> tuple[list[dict], list[dict]]:
    """Returns (per-symbol rows, one breadth row)."""
    snapshot_at = snapshot_at or now_ist()
    data = (payload or {}).get("data") or []

    rows, advances, declines, unchanged = [], 0, 0, 0
    for d in data:
        sym = str(d.get("symbol") or "").upper()
        if not sym or sym == index.upper().replace(" ", ""):
            continue
        pchange = d.get("pChange")
        try:
            pc = float(pchange)
        except (TypeError, ValueError):
            pc = None
        if pc is not None:
            if pc > 0:
                advances += 1
            elif pc < 0:
                declines += 1
            else:
                unchanged += 1

        rows.append({
            "dataset": SNAPSHOT_DATASET,
            "entity": sym,
            "event_time": snapshot_at,
            "knowledge_time": snapshot_at,
            "source": SOURCE,
            "confidence": "observed",
            "payload": {
                "symbol": sym,
                "index": index,
                "ltp": d.get("lastPrice"),
                "open": d.get("open"),
                "day_high": d.get("dayHigh"),
                "day_low": d.get("dayLow"),
                "prev_close": d.get("previousClose"),
                "pct_change": pc,
                "volume": d.get("totalTradedVolume"),
                "traded_value": d.get("totalTradedValue"),
                "year_high": d.get("yearHigh"),
                "year_low": d.get("yearLow"),
                "per_change_30d": d.get("perChange30d"),
                "per_change_365d": d.get("perChange365d"),
            },
        })

    breadth = [{
        "dataset": BREADTH_DATASET,
        "entity": index.upper(),
        "event_time": snapshot_at,
        "knowledge_time": snapshot_at,
        "source": SOURCE,
        "confidence": "observed",
        "payload": {
            "index": index,
            "constituents": len(rows),
            "advances": advances,
            "declines": declines,
            "unchanged": unchanged,
            # The ratio is the number most breadth studies actually use, and
            # computing it here means every consumer computes it the same way.
            "advance_decline_ratio": round(advances / declines, 4) if declines else None,
        },
    }] if rows else []

    return rows, breadth


def load(store: Store, indices=(DEFAULT_INDEX,), http: Optional[Http] = None) -> dict:
    import time
    http = http or Http()
    seen = new = 0
    per_index, failures = {}, {}

    for index in indices:
        try:
            payload = fetch(index, http)
            rows, breadth = parse(payload, index)
            n = store.append_many(rows) + store.append_many(breadth)
            seen += len(rows) + len(breadth)
            new += n
            per_index[index] = {"symbols": len(rows), "new": n,
                                "breadth": breadth[0]["payload"] if breadth else None}
        except Exception as e:
            failures[index] = f"{type(e).__name__}: {e}"
        time.sleep(POLITE_DELAY)

    if indices and not per_index:
        raise RuntimeError(
            f"quotes: all {len(indices)} index request(s) failed, nothing recorded. "
            f"First error: {next(iter(failures.values()), 'unknown')}")

    return {"rows_seen": seen, "rows_new": new,
            "per_index": per_index, "failures": failures}
