"""
STEP 0 — Data probe.

Answers one gating question before 3,000 lines get written:

    For 2019-2026, can we actually retrieve enough timestamped results filings,
    point-in-time index membership, corporate actions and EOD prices to run
    EXP-B1 as specified?

This file deliberately depends on NOTHING — not on engine/, not on research/.
It is a standalone diagnostic that can be run on the VPS, on a laptop, or here,
and it must be runnable before the research package exists.

    python research/probe/probe.py            # full probe
    python research/probe/probe.py --quick    # skip the slow announcement sweep

Every finding is reported as CONFIRMED (we fetched it and counted it) or
UNAVAILABLE (we tried and it did not work). Nothing is reported as "should
work" — the entire point of this file is to replace assumption with counts.
"""

from __future__ import annotations

import io
import csv
import ssl
import json
import time
import zipfile
import argparse
import datetime as dt
import urllib.error
import urllib.request
from collections import defaultdict

TIMEOUT = 25
SLEEP_BETWEEN_CALLS = 0.6  # be a polite client; these are free public endpoints

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

BSE_HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.bseindia.com/",
    "Origin": "https://www.bseindia.com",
}

NSE_HEADERS = {
    "User-Agent": UA,
    "Accept": "*/*",
    "Referer": "https://www.nseindia.com/",
}

# A cross-section deliberately spanning large / mid / small and several sectors,
# including two symbols that changed name or were delisted, so the probe measures
# real-world messiness rather than the happy path.
SAMPLE_SYMBOLS = [
    ("RELIANCE", "500325"), ("TCS", "532540"), ("HDFCBANK", "500180"),
    ("INFY", "500209"), ("ITC", "500875"), ("SBIN", "500112"),
    ("SUNPHARMA", "524715"), ("TATASTEEL", "500470"), ("MARUTI", "532500"),
    ("ASIANPAINT", "500820"), ("TITAN", "500114"), ("NESTLEIND", "500790"),
    ("DIXON", "540699"), ("POLYCAB", "542652"), ("DEEPAKNTR", "506401"),
    ("CDSL", "543066"), ("KPITTECH", "542651"), ("LAURUSLABS", "540222"),
    ("JUBLFOOD", "533155"), ("BALKRISIND", "502355"),
]

MEMBERSHIP_URLS = [
    # Verified path, 2026-09-02: 6,525 intervals / 42 indices / 2014-01-01..2026-05-15
    "https://raw.githubusercontent.com/aditya-jha/nse-historical-membership/main/"
    "index_history/data/index_membership_history.csv",
]

_CTX = ssl.create_default_context()


# ---------------------------------------------------------------------------
# transport
# ---------------------------------------------------------------------------

def _fetch(url: str, headers: dict, as_bytes: bool = False):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=_CTX) as r:
        raw = r.read()
    return raw if as_bytes else raw.decode("utf-8", errors="replace")


def _json(url: str, headers: dict):
    return json.loads(_fetch(url, headers))


def _try(fn, *a, **kw):
    """Return (ok, value_or_error_string). Probes must never crash mid-sweep —
    a failure is a finding, not an exception."""
    try:
        return True, fn(*a, **kw)
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return False, f"URLError {e.reason}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# 1. Point-in-time index membership
# ---------------------------------------------------------------------------

def probe_membership() -> dict:
    out = {"name": "Point-in-time index membership", "status": "UNAVAILABLE", "detail": {}}
    for url in MEMBERSHIP_URLS:
        ok, res = _try(_fetch, url, {"User-Agent": UA})
        if not ok:
            out["detail"].setdefault("attempts", []).append({"url": url, "error": res})
            continue

        rows = list(csv.DictReader(io.StringIO(res)))
        if not rows:
            continue

        cols = list(rows[0].keys())
        idx_col = next((c for c in cols if c.lower() == "index_name"), None) or next((c for c in cols if "index" in c.lower()), None)
        sym_col = next((c for c in cols if "symbol" in c.lower() or "ticker" in c.lower()), None)
        start_col = next((c for c in cols if "start" in c.lower() or "entry" in c.lower()
                          or "from" in c.lower()), None)
        end_col = next((c for c in cols if "end" in c.lower() or "exit" in c.lower()
                        or "to" in c.lower()), None)

        indices = defaultdict(int)
        if idx_col:
            for r in rows:
                indices[r[idx_col]] += 1

        dates = []
        if start_col:
            dates = sorted(r[start_col] for r in rows if r.get(start_col))

        nifty500 = [k for k in indices if "500" in k]
        out.update({
            "status": "CONFIRMED",
            "detail": {
                "url": url,
                "rows": len(rows),
                "columns": cols,
                "distinct_indices": len(indices),
                "earliest_interval_start": dates[0] if dates else None,
                "latest_interval_start": dates[-1] if dates else None,
                "nifty500_like_index_names": nifty500[:5],
                "nifty500_intervals": sum(indices[k] for k in nifty500),
                "mapped_columns": {"index": idx_col, "symbol": sym_col,
                                   "start": start_col, "end": end_col},
            },
        })
        return out
    return out


# ---------------------------------------------------------------------------
# 2. BSE corporate announcements — the B1 gate
# ---------------------------------------------------------------------------

def _bse_announcements(scrip: str, frm: dt.date, to: dt.date, category: str = "Result"):
    url = (
        "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
        f"?pageno=1&strCat={category}&strPrevDate={frm:%Y%m%d}&strScrip={scrip}"
        f"&strSearch=P&strToDate={to:%Y%m%d}&strType=C&subcategory=-1"
    )
    return _json(url, BSE_HEADERS)


def _extract_rows(payload) -> list:
    if isinstance(payload, dict):
        for key in ("Table", "Table1", "data", "Data"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return payload if isinstance(payload, list) else []


TIMESTAMP_FIELDS = ("NEWS_DT", "News_submission_dt", "DissemDT", "TimeStamp",
                    "Exchange_Received_Time", "Exchange_Disseminated_Time")


def _has_timestamp(row: dict) -> tuple[bool, str | None, str | None]:
    for f in TIMESTAMP_FIELDS:
        v = row.get(f)
        if v and str(v).strip() and str(v).strip().lower() not in ("null", "none"):
            return True, f, str(v)
    return False, None, None


def probe_announcements(quick: bool = False) -> dict:
    """The count that decides whether EXP-B1 is buildable as written."""
    out = {"name": "BSE results filings with dissemination timestamp",
           "status": "UNAVAILABLE", "detail": {}}

    years = [2019, 2022, 2026] if quick else [2019, 2020, 2021, 2022, 2023, 2024, 2025, 2026]
    symbols = SAMPLE_SYMBOLS[:6] if quick else SAMPLE_SYMBOLS

    per_year = defaultdict(lambda: {"filings": 0, "with_ts": 0, "symbols_ok": 0,
                                    "symbols_tried": 0, "errors": 0})
    ts_field_counts = defaultdict(int)
    sample_row = None
    first_error = None
    any_success = False

    for year in years:
        frm = dt.date(year, 1, 1)
        to = min(dt.date(year, 12, 31), dt.date.today())
        for sym, scrip in symbols:
            per_year[year]["symbols_tried"] += 1
            ok, payload = _try(_bse_announcements, scrip, frm, to)
            time.sleep(SLEEP_BETWEEN_CALLS)
            if not ok:
                per_year[year]["errors"] += 1
                first_error = first_error or f"{sym} {year}: {payload}"
                continue

            rows = _extract_rows(payload)
            if rows:
                any_success = True
                per_year[year]["symbols_ok"] += 1
            per_year[year]["filings"] += len(rows)
            for r in rows:
                if not isinstance(r, dict):
                    continue
                has, field, val = _has_timestamp(r)
                if has:
                    per_year[year]["with_ts"] += 1
                    ts_field_counts[field] += 1
                    if sample_row is None:
                        sample_row = {"symbol": sym, "field": field, "value": val,
                                      "keys": sorted(r.keys())[:25]}

    total_filings = sum(v["filings"] for v in per_year.values())
    total_ts = sum(v["with_ts"] for v in per_year.values())

    out["detail"] = {
        "symbols_sampled": len(symbols),
        "years_sampled": years,
        "total_filings_found": total_filings,
        "total_with_timestamp": total_ts,
        "timestamp_field_usage": dict(ts_field_counts),
        "per_year": {str(k): dict(v) for k, v in sorted(per_year.items())},
        "sample_row": sample_row,
        "first_error": first_error,
    }
    if any_success and total_ts > 0:
        out["status"] = "CONFIRMED"
    elif any_success:
        out["status"] = "PARTIAL"  # filings returned but no usable timestamps
    return out


# ---------------------------------------------------------------------------
# 3. Results calendar
# ---------------------------------------------------------------------------

def probe_results_calendar() -> dict:
    out = {"name": "BSE forthcoming-results calendar", "status": "UNAVAILABLE", "detail": {}}
    today = dt.date.today()
    windows = [(today, today + dt.timedelta(days=45)),
               (dt.date(2024, 1, 1), dt.date(2024, 3, 31)),
               (dt.date(2020, 1, 1), dt.date(2020, 3, 31))]
    results = []
    for frm, to in windows:
        url = ("https://api.bseindia.com/BseIndiaAPI/api/Corpforthresults/w"
               f"?fromdate={frm:%Y%m%d}&todate={to:%Y%m%d}&scripcode=")
        ok, payload = _try(_json, url, BSE_HEADERS)
        time.sleep(SLEEP_BETWEEN_CALLS)
        rows = _extract_rows(payload) if ok else []
        results.append({"window": f"{frm}..{to}", "ok": ok,
                        "rows": len(rows), "error": None if ok else payload,
                        "sample_keys": sorted(rows[0].keys())[:15]
                        if rows and isinstance(rows[0], dict) else None})
    out["detail"]["windows"] = results
    if any(r["rows"] for r in results):
        out["status"] = "CONFIRMED"
    elif any(r["ok"] for r in results):
        out["status"] = "PARTIAL"
    return out


# ---------------------------------------------------------------------------
# 4. Corporate actions
# ---------------------------------------------------------------------------

def probe_corp_actions() -> dict:
    out = {"name": "BSE corporate actions (splits/bonus/dividend)",
           "status": "UNAVAILABLE", "detail": {}}
    attempts = []
    for scrip in ("500325", "532540", "500180"):
        url = ("https://api.bseindia.com/BseIndiaAPI/api/CorporateAction/w"
               f"?scripcode={scrip}&Fdate=&TDate=&segment=0&Purposecode=&strSearch=S")
        ok, payload = _try(_json, url, BSE_HEADERS)
        time.sleep(SLEEP_BETWEEN_CALLS)
        rows = _extract_rows(payload) if ok else []
        dates = []
        if rows and isinstance(rows[0], dict):
            for f in ("Ex_date", "ExDate", "BCRD_FROM", "ANNOUNCEMENT_DATE"):
                dates = sorted(str(r.get(f)) for r in rows if r.get(f))
                if dates:
                    break
        attempts.append({"scrip": scrip, "ok": ok, "rows": len(rows),
                         "earliest": dates[0] if dates else None,
                         "latest": dates[-1] if dates else None,
                         "keys": sorted(rows[0].keys())[:18]
                         if rows and isinstance(rows[0], dict) else None,
                         "error": None if ok else payload})
    out["detail"]["attempts"] = attempts
    if any(a["rows"] for a in attempts):
        out["status"] = "CONFIRMED"
    elif any(a["ok"] for a in attempts):
        out["status"] = "PARTIAL"
    return out


# ---------------------------------------------------------------------------
# 5. EOD bhavcopy archives — how far back, and in which format era
# ---------------------------------------------------------------------------

def _last_weekday(d: dt.date) -> dt.date:
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def probe_bhavcopy() -> dict:
    out = {"name": "NSE EOD bhavcopy archive", "status": "UNAVAILABLE", "detail": {}}
    probes = []
    # UDiFF era (current) and the legacy era, plus a deep-history check.
    candidates = [
        ("udiff", _last_weekday(dt.date.today() - dt.timedelta(days=5))),
        ("udiff", _last_weekday(dt.date(2025, 6, 10))),
        ("legacy", _last_weekday(dt.date(2023, 1, 10))),
        ("legacy", _last_weekday(dt.date(2019, 1, 10))),
    ]
    for era, d in candidates:
        if era == "udiff":
            url = ("https://nsearchives.nseindia.com/content/cm/"
                   f"BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip")
        else:
            url = ("https://nsearchives.nseindia.com/content/historical/EQUITIES/"
                   f"{d:%Y}/{d:%b}".upper().replace("HTTPS", "https") +
                   f"/cm{d:%d%b%Y}bhav.csv.zip".upper().replace(".CSV.ZIP", ".csv.zip"))
            url = ("https://nsearchives.nseindia.com/content/historical/EQUITIES/"
                   f"{d.year}/{d:%b}".upper().replace(
                       "HTTPS://NSEARCHIVES.NSEINDIA.COM/CONTENT/HISTORICAL/EQUITIES/",
                       "https://nsearchives.nseindia.com/content/historical/EQUITIES/") +
                   f"/cm{d:%d%b%Y}".upper() + "bhav.csv.zip")

        ok, payload = _try(_fetch, url, NSE_HEADERS, True)
        time.sleep(SLEEP_BETWEEN_CALLS)
        rows = None
        cols = None
        if ok:
            try:
                z = zipfile.ZipFile(io.BytesIO(payload))
                name = z.namelist()[0]
                text = z.read(name).decode("utf-8", errors="replace")
                lines = text.splitlines()
                rows = max(len(lines) - 1, 0)
                cols = lines[0].split(",")[:12] if lines else None
            except Exception as e:
                ok, payload = False, f"unzip failed: {type(e).__name__}"
        probes.append({"era": era, "date": str(d), "url": url, "ok": ok,
                       "rows": rows, "columns": cols,
                       "error": None if ok else payload})
    out["detail"]["probes"] = probes
    if any(p["ok"] for p in probes):
        out["status"] = "CONFIRMED" if all(
            p["ok"] for p in probes if p["era"] == "udiff") else "PARTIAL"
    return out


# ---------------------------------------------------------------------------
# 6. Yahoo index series (already used by the live agent)
# ---------------------------------------------------------------------------

def probe_yahoo() -> dict:
    out = {"name": "Yahoo index series (^NSEI)", "status": "UNAVAILABLE", "detail": {}}
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/%5ENSEI"
           "?range=10y&interval=1d")
    ok, payload = _try(_json, url, {"User-Agent": UA})
    if ok:
        try:
            res = payload["chart"]["result"][0]
            ts = res["timestamp"]
            out["status"] = "CONFIRMED"
            out["detail"] = {
                "bars": len(ts),
                "earliest": dt.datetime.utcfromtimestamp(ts[0]).date().isoformat(),
                "latest": dt.datetime.utcfromtimestamp(ts[-1]).date().isoformat(),
            }
        except Exception as e:
            out["detail"]["error"] = f"unexpected shape: {type(e).__name__}"
    else:
        out["detail"]["error"] = payload
    return out


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------

def b1_verdict(ann: dict) -> dict:
    """Extrapolate the sampled filing count to the B1 universe and say plainly
    whether the contract is buildable as written."""
    d = ann.get("detail", {})
    n_sym = d.get("symbols_sampled") or 0
    n_years = len(d.get("years_sampled") or [])
    with_ts = d.get("total_with_timestamp") or 0

    if not n_sym or not n_years or not with_ts:
        return {"buildable": False,
                "reason": "No timestamped filings retrieved — B1 cannot be run from "
                          "this source. Revise the contract or find another source "
                          "BEFORE locking it."}

    per_symbol_year = with_ts / (n_sym * n_years)
    # B1's universe after the ADV >= 5cr and price >= 20 filters is ~400-600 names.
    projected = per_symbol_year * 500 * 7

    if projected >= 10_000:
        verdict = ("Projected event count supports the ~12,000-event design. "
                   "B1 is buildable as specified.")
        buildable = True
    elif projected >= 4_000:
        verdict = ("Projected event count is below the design target but still "
                   "usable. Widen the universe or the period, and restate the "
                   "independence assumption BEFORE locking the contract.")
        buildable = True
    else:
        verdict = ("Projected event count is too low for the specified design. "
                   "B1 must be revised before it is locked — revising after "
                   "locking is exactly what the hash exists to prevent.")
        buildable = False

    return {"buildable": buildable,
            "filings_per_symbol_year": round(per_symbol_year, 2),
            "projected_events_500_names_7_years": int(projected),
            "verdict": verdict}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true",
                    help="Fewer symbols and years — faster, less precise")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    started = dt.datetime.now()
    report = {"probe_run_at": started.isoformat(timespec="seconds"), "results": []}

    steps = [
        ("membership", probe_membership),
        ("announcements", lambda: probe_announcements(args.quick)),
        ("results_calendar", probe_results_calendar),
        ("corp_actions", probe_corp_actions),
        ("bhavcopy", probe_bhavcopy),
        ("yahoo", probe_yahoo),
    ]

    for key, fn in steps:
        if not args.json:
            print(f"  … probing {key}", flush=True)
        ok, res = _try(fn)
        report["results"].append(
            {"key": key, **(res if ok else
                            {"name": key, "status": "ERROR", "detail": {"error": res}})})

    ann = next((r for r in report["results"] if r["key"] == "announcements"), {})
    report["b1_verdict"] = b1_verdict(ann)
    report["elapsed_seconds"] = round((dt.datetime.now() - started).total_seconds(), 1)

    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return

    print("\n" + "=" * 72)
    print("  STEP 0 — DATA PROBE")
    print("=" * 72)
    for r in report["results"]:
        print(f"\n[{r['status']:<11}] {r['name']}")
        print("  " + json.dumps(r["detail"], default=str)[:1400])
    print("\n" + "-" * 72)
    print("  EXP-B1 VERDICT")
    print("-" * 72)
    print(json.dumps(report["b1_verdict"], indent=2))
    print(f"\nElapsed: {report['elapsed_seconds']}s")


if __name__ == "__main__":
    main()
