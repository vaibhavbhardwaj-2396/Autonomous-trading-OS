"""
Source: point-in-time NSE index membership.

Origin: github.com/aditya-jha/nse-historical-membership (MIT code / CC-BY data),
reconstructed from NSE Indices press releases and circulars.

Why this dataset decides whether any of this works
--------------------------------------------------
Without as-of membership, every backtest silently studies the companies that
survived to be in the index today. That bias is large, it always flatters, and
it cannot be corrected after the fact. This CSV is the difference between EXP-B1
being a study and being a flattering illusion.

The knowledge_time rule, which is the only interesting decision here
-------------------------------------------------------------------
The file gives `valid_from` — the date a change took EFFECT. That is event_time.
It does not give the date the change was ANNOUNCED, which is what we could
actually have known. The two differ by roughly four weeks, and that gap is
itself a documented, tradeable effect — so getting it wrong does not produce a
small error, it produces a study of the wrong thing.

The announcement date is recoverable for ~80% of rows from the press-release
filename embedded in `source_url` (`ind_prs20022015.pdf` -> 2015-02-20). Where
it is recoverable we use it. Where it is not, we fall back to the effective date
and mark the row `inferred_floor`.

That fallback errs in the safe direction on purpose: it makes the agent believe
it learned the change LATER than it really did, so the experiment under-uses
information rather than over-using it. An honest study is allowed to be
pessimistic; it is not allowed to be optimistic.
"""

from __future__ import annotations

import io
import csv
import re
import ssl
import urllib.request
import datetime as dt
from typing import Iterator, Optional

from ..store import Store

BASE = "https://raw.githubusercontent.com/aditya-jha/nse-historical-membership/main/"
MEMBERSHIP_CSV = BASE + "index_history/data/index_membership_history.csv"
FNO_CSV = BASE + "fno_history/data/fno_membership_history.csv"

DATASET = "index_membership"
FNO_DATASET = "fno_membership"
SOURCE = "nse-historical-membership"

# ind_prs20022015.pdf / ind_prs18032015_2.pdf  ->  DD MM YYYY
_PR_DATE = re.compile(r"ind_prs?(\d{2})(\d{2})(\d{4})", re.IGNORECASE)

_CTX = ssl.create_default_context()


def _fetch_csv(url: str) -> list[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": "living-quant-research"})
    with urllib.request.urlopen(req, timeout=90, context=_CTX) as r:
        text = r.read().decode("utf-8", errors="replace")
    return list(csv.DictReader(io.StringIO(text)))


def press_release_date(*blobs: Optional[str]) -> Optional[dt.date]:
    """Recover the announcement date from a press-release filename."""
    for blob in blobs:
        if not blob:
            continue
        m = _PR_DATE.search(blob)
        if m:
            d, mth, y = m.group(1), m.group(2), m.group(3)
            try:
                return dt.date(int(y), int(mth), int(d))
            except ValueError:
                continue
    return None


def _rows(raw: list[dict]) -> Iterator[dict]:
    for r in raw:
        symbol = (r.get("symbol") or "").strip().upper()
        valid_from = (r.get("valid_from") or "").strip()
        if not symbol or not valid_from:
            continue

        effective = dt.date.fromisoformat(valid_from)
        announced = press_release_date(r.get("source_url"), r.get("notes"))

        if announced and announced <= effective:
            knowledge, confidence = announced, "observed"
        else:
            # Either no press release found, or the filename date is after the
            # effective date (a correction or a re-issued circular). Fall back
            # to the pessimistic assumption.
            knowledge, confidence = effective, "inferred_floor"

        yield {
            "dataset": DATASET,
            "entity": symbol,
            "event_time": effective,
            "knowledge_time": knowledge,
            "source": SOURCE,
            "source_ref": (r.get("source_url") or "").strip() or None,
            "confidence": confidence,
            "payload": {
                "symbol": symbol,
                "index_name": (r.get("index_name") or "").strip(),
                "index_id": (r.get("index_id") or "").strip(),
                "valid_from": valid_from,
                "valid_to": (r.get("valid_to") or "").strip() or None,
                "weightage": (r.get("weightage") or "").strip() or None,
                "origin": (r.get("source") or "").strip(),
                "announced_on": announced.isoformat() if announced else None,
                "notes": (r.get("notes") or "").strip() or None,
            },
        }


def load(store: Store, url: str = MEMBERSHIP_CSV) -> dict:
    raw = _fetch_csv(url)
    rows = list(_rows(raw))
    inserted = store.append_many(rows)
    observed = sum(1 for r in rows if r["confidence"] == "observed")
    return {
        "source_rows": len(raw),
        "prepared": len(rows),
        "inserted": inserted,
        "already_present": len(rows) - inserted,
        "with_announcement_date": observed,
        "inferred_floor": len(rows) - observed,
        "announcement_date_coverage_pct": round(100 * observed / max(len(rows), 1), 1),
    }


def load_fno(store: Store, url: str = FNO_CSV) -> dict:
    raw = _fetch_csv(url)
    rows = []
    for r in raw:
        symbol = (r.get("symbol") or "").strip().upper()
        vf = (r.get("valid_from") or "").strip()
        if not symbol or not vf:
            continue
        effective = dt.date.fromisoformat(vf)
        rows.append({
            "dataset": FNO_DATASET,
            "entity": symbol,
            "event_time": effective,
            # F&O inclusion arrives by circular; the circular number is recorded
            # but not its date, so this is a floor until the circulars are parsed.
            "knowledge_time": effective,
            "source": SOURCE,
            "source_ref": (r.get("source_url") or "").strip() or None,
            "confidence": "inferred_floor",
            "payload": {
                "symbol": symbol,
                "valid_from": vf,
                "valid_to": (r.get("valid_to") or "").strip() or None,
                "circular_no": (r.get("circular_no") or "").strip() or None,
            },
        })
    inserted = store.append_many(rows)
    return {"source_rows": len(raw), "prepared": len(rows), "inserted": inserted}


if __name__ == "__main__":
    import json
    with Store.open() as s:
        print(json.dumps({"index": load(s), "fno": load_fno(s)}, indent=2))
        print(json.dumps(s.stats(), indent=2, default=str))
