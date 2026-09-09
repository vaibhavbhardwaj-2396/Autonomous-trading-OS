"""
EOD prices from the NSE bhavcopy — the one dataset that is both backfillable
and urgent, for reasons that are not obvious.

The restatement trap
--------------------
Every adjusted price archive — yfinance included — silently rewrites history
whenever a split, bonus or large dividend occurs. A series downloaded today
encodes corporate actions that had not happened on the dates it describes. Any
backtest built on it is quietly clairvoyant about capital structure, and the
resulting bias is invisible: the numbers look completely normal.

The bhavcopy is the close as PRINTED on the day. It is never restated. Storing
it with `adjusted=0` means the store holds one series that is true as of its own
date, and any adjustment an experiment wants gets computed at replay time from
corporate actions known before `as_of`.

So the recorder captures today's bhavcopy every evening even though the archive
is backfillable — because what we store today is the unrestated version, and
after the next bonus issue the archived version will no longer match it.

knowledge_time = 18:00 IST on the session date. NSE publishes in the early
evening, so a session's close is not knowable at 15:30 that day. That single
constant rules out an entire family of "enter at today's close on today's
signal" strategies, and it is better to have it enforced by the data than
remembered by a person.

Two file formats
----------------
UDiFF (from mid-2024) and the legacy format before it. Detected by header, not
by date, because the changeover was not clean and a date rule would silently
mis-parse the boundary weeks.
"""

from __future__ import annotations

import io
import csv
import zipfile
import datetime as dt
from typing import Iterable, Optional

from ..store import Store
from .base import Http, bhavcopy_knowledge_time, POLITE_DELAY

SOURCE = "nse_bhavcopy"

UDIFF_URL = ("https://nsearchives.nseindia.com/content/cm/"
             "BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip")
LEGACY_URL = ("https://nsearchives.nseindia.com/content/historical/EQUITIES/"
              "{d:%Y}/{mon}/cm{d:%d}{mon}{d:%Y}bhav.csv.zip")

_HEADERS = {"Accept": "*/*", "Referer": "https://www.nseindia.com/"}

# Equity series only. BE/BZ are trade-to-trade and surveillance segments where
# the microstructure is different enough that mixing them in silently corrupts
# any liquidity-based screen.
KEEP_SERIES = {"EQ"}


def _num(v) -> Optional[float]:
    try:
        f = float(str(v).strip())
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def parse(csv_text: str, session_date: Optional[dt.date] = None,
          keep_series: set = KEEP_SERIES) -> list[dict]:
    """Bhavcopy CSV (either era) -> price rows. Pure; tested against fixtures."""
    reader = csv.DictReader(io.StringIO(csv_text))
    fields = {(f or "").strip().upper() for f in (reader.fieldnames or [])}
    udiff = "TCKRSYMB" in fields

    rows: list[dict] = []
    for raw in reader:
        r = {(k or "").strip().upper(): (v.strip() if isinstance(v, str) else v)
             for k, v in raw.items()}

        if udiff:
            if (r.get("SGMT") or "CM") != "CM":
                continue
            series = (r.get("SCTYSRS") or "").upper()
            symbol = (r.get("TCKRSYMB") or "").upper()
            sd = r.get("TRADDT") or ""
            o, h, l, c = (r.get("OPNPRIC"), r.get("HGHPRIC"),
                          r.get("LWPRIC"), r.get("CLSPRIC"))
            vol, val = r.get("TTLTRADGVOL"), r.get("TTLTRFVAL")
        else:
            series = (r.get("SERIES") or "").upper()
            symbol = (r.get("SYMBOL") or "").upper()
            sd = r.get("TIMESTAMP") or ""
            o, h, l, c = r.get("OPEN"), r.get("HIGH"), r.get("LOW"), r.get("CLOSE")
            vol, val = r.get("TOTTRDQTY"), r.get("TOTTRDVAL")

        if not symbol or (keep_series and series not in keep_series):
            continue

        d = session_date or _parse_session_date(sd)
        if d is None:
            continue

        rows.append({
            "symbol": symbol,
            "session_date": d,
            "knowledge_time": bhavcopy_knowledge_time(d),
            "source": SOURCE,
            "open_": _num(o), "high": _num(h), "low": _num(l), "close": _num(c),
            "volume": int(_num(vol) or 0),
            "traded_value": _num(val),
            "adjusted": False,          # as printed. Never restated.
        })
    return rows


def _parse_session_date(value: str) -> Optional[dt.date]:
    v = (value or "").strip()
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    return None


def fetch(session_date: dt.date, http: Optional[Http] = None) -> str:
    """Try UDiFF then legacy. Format is detected by what actually downloads,
    not by a date cutoff — the changeover was messy and a date rule mis-parses
    the boundary weeks in a way nobody notices for months."""
    http = http or Http()
    http.prime("https://www.nseindia.com/")

    urls = [UDIFF_URL.format(d=session_date),
            LEGACY_URL.format(d=session_date,
                              mon=session_date.strftime("%b").upper())]
    last = None
    for url in urls:
        try:
            blob = http.get(url, _HEADERS)
            z = zipfile.ZipFile(io.BytesIO(blob))
            return z.read(z.namelist()[0]).decode("utf-8", "replace")
        except Exception as e:
            last = f"{url.rsplit('/', 1)[-1]}: {type(e).__name__}"
    raise RuntimeError(f"no bhavcopy for {session_date} ({last})")


def load_day(store: Store, session_date: dt.date, http: Optional[Http] = None) -> dict:
    text = fetch(session_date, http)
    rows = parse(text, session_date)
    new = store.append_prices(rows)
    return {"rows_seen": len(rows), "rows_new": new, "session_date": str(session_date)}


def load_range(store: Store, start: dt.date, end: dt.date,
               http: Optional[Http] = None, on_progress=None) -> dict:
    """Backfill. Holidays and weekends simply have no file, which is not an
    error — reporting them as failures would bury the real ones."""
    import time
    http = http or Http()
    seen = new = 0
    days = missing = 0
    failures: dict[str, str] = {}

    d = start
    while d <= end:
        if d.weekday() < 5:
            days += 1
            try:
                r = load_day(store, d, http)
                seen += r["rows_seen"]
                new += r["rows_new"]
                if on_progress:
                    on_progress(d, r)
            except Exception as e:
                missing += 1
                if len(failures) < 25:
                    failures[str(d)] = f"{type(e).__name__}"
            time.sleep(POLITE_DELAY)
        d += dt.timedelta(days=1)

    return {"rows_seen": seen, "rows_new": new, "weekdays_attempted": days,
            "weekdays_without_file": missing,
            "note": "weekdays without a file are usually NSE holidays, not errors",
            "sample_failures": failures}
