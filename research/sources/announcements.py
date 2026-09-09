"""
BSE corporate announcements — the feed EXP-B1 stands on.

This is the one source whose historical depth was still unverified when the
roadmap was written, and it is the reason STEP 0 exists. If BSE does not serve
timestamped results filings back to 2019 in useful volume, B1's ~12,000-event
design is not buildable and the contract must be revised BEFORE it is locked.

knowledge_time
--------------
BSE stamps each filing with its dissemination time (NEWS_DT and friends). That
is genuinely the moment the market could first have seen it, so unusually for
this project the source hands us the right answer directly.

Two consequences that matter more than they look:

  * A filing disseminated at 19:40 was NOT actionable that day. An event study
    that treats the filing date as the trading date is off by one session on a
    large fraction of its sample, in the direction that flatters.

  * Where a timestamp is missing entirely, the row is stored with
    knowledge_time set to the END of the filing date and confidence
    `inferred_floor`. That is pessimistic on purpose. Guessing 09:00 would
    manufacture a tradeable morning that may never have existed.

Rows with `inferred_floor` confidence should be excluded from B1's primary
analysis and reported separately. If the conclusion depends on them, it is not a
conclusion.
"""

from __future__ import annotations

import hashlib
import datetime as dt
from typing import Iterable, Optional

from ..store import Store, to_dt
from .base import Http, parse_bse_datetime, POLITE_DELAY, IST

DATASET = "bse_announcement"
SOURCE = "bse"

BSE_HOME = "https://www.bseindia.com/"
ANN_URL = ("https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
           "?pageno={page}&strCat={cat}&strPrevDate={frm:%Y%m%d}&strScrip={scrip}"
           "&strSearch=P&strToDate={to:%Y%m%d}&strType=C&subcategory=-1")

_HEADERS = {"Accept": "application/json, text/plain, */*",
            "Referer": "https://www.bseindia.com/",
            "Origin": "https://www.bseindia.com"}

# Order matters: the first field present wins, so the most precise
# dissemination stamp must come first.
TIMESTAMP_FIELDS = ("Exchange_Disseminated_Time", "DissemDT", "NEWS_DT",
                    "News_submission_dt", "Exchange_Received_Time", "TimeStamp")

DATE_ONLY_FIELDS = ("NEWS_DT", "DT_TM", "News_Date")


def _rows_from(payload) -> list:
    if isinstance(payload, dict):
        for key in ("Table", "Table1", "data", "Data"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return payload if isinstance(payload, list) else []


def _knowledge(rec: dict) -> tuple[Optional[dt.datetime], str, Optional[str]]:
    """Returns (knowledge_time, confidence, field_used)."""
    for f in TIMESTAMP_FIELDS:
        parsed = parse_bse_datetime(rec.get(f) or "")
        if parsed and (parsed.hour or parsed.minute):
            return parsed, "observed", f
    # A date with no time. Assume the last moment of that day rather than
    # inventing a morning that may not have existed.
    for f in DATE_ONLY_FIELDS:
        parsed = parse_bse_datetime(rec.get(f) or "")
        if parsed:
            return (parsed.replace(hour=23, minute=59, second=59),
                    "inferred_floor", f)
    return None, "unusable", None


def parse(payload, symbol: str = "", scrip_code: str = "") -> list[dict]:
    """BSE JSON -> store rows. Pure; tested against a fixture."""
    rows = []
    for rec in _rows_from(payload):
        if not isinstance(rec, dict):
            continue
        knowledge, confidence, field_used = _knowledge(rec)
        if knowledge is None:
            continue

        entity = (symbol or rec.get("SLONGNAME") or rec.get("SCRIP_CD") or "").upper()
        headline = (rec.get("NEWSSUB") or rec.get("HEADLINE") or
                    rec.get("SUBJECT") or "").strip()
        ident = hashlib.sha1(
            f"{entity}|{headline}|{knowledge.isoformat()}".encode()).hexdigest()[:16]

        rows.append({
            "dataset": DATASET,
            "entity": entity,
            # The filing is both the event and, once disseminated, the knowledge.
            "event_time": knowledge,
            "knowledge_time": knowledge,
            "source": SOURCE,
            "source_ref": rec.get("ATTACHMENTNAME") or rec.get("NSURL") or None,
            "confidence": confidence,
            "payload": {
                "id": ident,
                "symbol": entity,
                "scrip_code": str(rec.get("SCRIP_CD") or scrip_code or "") or None,
                "headline": headline or None,
                "category": rec.get("CATEGORYNAME") or rec.get("News_Type") or None,
                "subcategory": rec.get("SUBCATNAME") or None,
                "timestamp_field_used": field_used,
                "timestamp_precision": ("minute" if confidence == "observed"
                                        else "date_only"),
                "detail": (rec.get("MORE") or rec.get("NEWS_BODY") or "")[:2000] or None,
            },
        })
    return rows


def fetch(scrip_code: str, frm: dt.date, to: dt.date, category: str = "Result",
          page: int = 1, http: Optional[Http] = None):
    http = http or Http()
    http.prime(BSE_HOME)
    url = ANN_URL.format(page=page, cat=category, frm=frm, to=to, scrip=scrip_code)
    return http.get_json(url, _HEADERS)


def load_symbol(store: Store, symbol: str, scrip_code: str, frm: dt.date, to: dt.date,
                category: str = "Result", http: Optional[Http] = None,
                max_pages: int = 12) -> dict:
    import time
    http = http or Http()
    seen = new = 0
    precision = {"observed": 0, "inferred_floor": 0}

    for page in range(1, max_pages + 1):
        payload = fetch(scrip_code, frm, to, category, page, http)
        rows = parse(payload, symbol, scrip_code)
        if not rows:
            break
        for r in rows:
            precision[r["confidence"]] = precision.get(r["confidence"], 0) + 1
        seen += len(rows)
        new += store.append_many(rows)
        time.sleep(POLITE_DELAY)
        if len(rows) < 10:      # last page
            break

    return {"rows_seen": seen, "rows_new": new, "precision": precision}


def load_universe(store: Store, symbol_to_scrip: dict, frm: dt.date, to: dt.date,
                  category: str = "Result", http: Optional[Http] = None) -> dict:
    http = http or Http()
    seen = new = 0
    precision = {"observed": 0, "inferred_floor": 0}
    failures: dict[str, str] = {}

    for symbol, scrip in symbol_to_scrip.items():
        try:
            r = load_symbol(store, symbol, str(scrip), frm, to, category, http)
            seen += r["rows_seen"]
            new += r["rows_new"]
            for k, v in r["precision"].items():
                precision[k] = precision.get(k, 0) + v
        except Exception as e:
            if len(failures) < 40:
                failures[symbol] = f"{type(e).__name__}"

    total = precision.get("observed", 0) + precision.get("inferred_floor", 0)
    return {
        "rows_seen": seen, "rows_new": new,
        "symbols": len(symbol_to_scrip), "failures": failures,
        "precision": precision,
        "usable_timestamp_pct": round(
            100 * precision.get("observed", 0) / total, 1) if total else 0.0,
    }
