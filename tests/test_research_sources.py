"""
Source parsing and — the part that actually matters — knowledge_time assignment.

fetch() talks to the network and is not tested here; it fails for boring,
loud reasons. parse() is pure, and it is where a study silently learns to cheat.
Every test below that mentions knowledge_time is testing whether a future
experiment can see something it should not have.

Run with:  python -m tests.test_research_sources
"""

import sys
import json
import shutil
import tempfile
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from research.store import Store, to_dt, IST                       # noqa: E402
from research.sources import (prices_eod, announcements, option_chain,  # noqa: E402
                              news, quotes, decisions)
from research.sources.base import (parse_bse_datetime,             # noqa: E402
                                   bhavcopy_knowledge_time, run_source)

PASSED, FAILED = 0, 0


def check(name, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  ✓ {name}")
    else:
        FAILED += 1
        print(f"  ✗ {name}")
        if detail:
            print(f"      {detail}")


TMP = Path(tempfile.mkdtemp(prefix="lq-src-"))


# ---------------------------------------------------------------------------
print("\n--- Bhavcopy: legacy format ---")
# ---------------------------------------------------------------------------

LEGACY = (
    "SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,"
    "TIMESTAMP,TOTALTRADES,ISIN,\n"
    "INFY,EQ,1500.00,1525.50,1495.10,1519.80,1520.00,1498.00,4500000,"
    "68000000000.00,10-JAN-2023,120000,INE009A01021,\n"
    "RELIANCE,EQ,2400.00,2430.00,2390.00,2425.00,2424.00,2398.00,3000000,"
    "72500000000.00,10-JAN-2023,98000,INE002A01018,\n"
    "SOMESME,BE,50.00,52.00,49.00,51.00,51.00,50.00,1000,51000.00,"
    "10-JAN-2023,20,INE999A01011,\n"
)

rows = prices_eod.parse(LEGACY)
check("legacy bhavcopy parses", len(rows) == 2, f"{len(rows)} rows")
check("BE series is excluded (different microstructure)",
      all(r["symbol"] != "SOMESME" for r in rows))
check("OHLCV mapped correctly",
      rows[0]["symbol"] == "INFY" and rows[0]["close"] == 1519.80
      and rows[0]["volume"] == 4500000, str(rows[0]))
check("prices are stored unadjusted", all(r["adjusted"] is False for r in rows))
check("session date parsed from DD-MMM-YYYY",
      rows[0]["session_date"] == dt.date(2023, 1, 10), str(rows[0]["session_date"]))

kt = rows[0]["knowledge_time"]
check("knowledge_time is 18:00 IST on the session date, not 15:30",
      kt.hour == 18 and kt.date() == dt.date(2023, 1, 10), str(kt))


# ---------------------------------------------------------------------------
print("\n--- Bhavcopy: UDiFF format ---")
# ---------------------------------------------------------------------------

UDIFF = (
    "TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,"
    "OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,TtlTradgVol,TtlTrfVal\n"
    "2025-06-10,2025-06-10,CM,NSE,STK,1594,INE009A01021,INFY,EQ,"
    "1600.00,1620.00,1590.00,1612.40,1612.00,1598.00,5000000,80000000000\n"
    "2025-06-10,2025-06-10,FO,NSE,STO,9999,INE009A01021,INFY,EQ,"
    "1.0,1.0,1.0,1.0,1.0,1.0,1,1\n"
)

u = prices_eod.parse(UDIFF)
check("UDiFF format is detected by header, not by date", len(u) == 1, f"{len(u)}")
check("non-CM segment rows are dropped", u[0]["symbol"] == "INFY")
check("UDiFF fields map to the same shape as legacy",
      u[0]["close"] == 1612.40 and u[0]["session_date"] == dt.date(2025, 6, 10),
      str(u[0]))
check("both eras produce identical row shapes",
      set(u[0].keys()) == set(rows[0].keys()))


# ---------------------------------------------------------------------------
print("\n--- BSE announcements: the knowledge_time rules ---")
# ---------------------------------------------------------------------------

ANN = {"Table": [
    {"SCRIP_CD": 500209, "SLONGNAME": "INFOSYS LTD", "NEWSSUB": "Q3 FY24 Results",
     "CATEGORYNAME": "Result", "NEWS_DT": "2024-01-11 19:40:12.000",
     "ATTACHMENTNAME": "abc.pdf"},
    {"SCRIP_CD": 500209, "SLONGNAME": "INFOSYS LTD", "NEWSSUB": "Board Meeting Intimation",
     "CATEGORYNAME": "Board Meeting", "NEWS_DT": "2024-01-02"},
    {"SCRIP_CD": 500209, "NEWSSUB": "No timestamp at all"},
]}

a = announcements.parse(ANN, "INFY")
check("announcements parse", len(a) == 2, f"{len(a)} — the untimestamped row "
                                          f"must be dropped, not guessed at")

precise = next(r for r in a if "Q3" in r["payload"]["headline"])
check("a precise dissemination stamp is used verbatim",
      precise["knowledge_time"].hour == 19 and precise["knowledge_time"].minute == 40,
      str(precise["knowledge_time"]))
check("...and is marked observed", precise["confidence"] == "observed")
check("a 19:40 filing is NOT actionable that session",
      precise["knowledge_time"].hour > 15,
      "an event study treating filing date as trade date would be a session early")

floor = next(r for r in a if "Board" in r["payload"]["headline"])
check("a date-only filing falls back to END of day, not a made-up morning",
      floor["knowledge_time"].hour == 23, str(floor["knowledge_time"]))
check("...and is marked inferred_floor so it can be excluded from a primary study",
      floor["confidence"] == "inferred_floor")
check("precision is recorded in the payload for later filtering",
      floor["payload"]["timestamp_precision"] == "date_only")

check("BSE datetime parser handles the shapes BSE actually returns",
      all(parse_bse_datetime(v) is not None for v in
          ["2024-01-11 19:40:12.000", "2024-01-11 19:40:12", "11-01-2024 19:40:12",
           "2024-01-11"]))
check("...and returns None rather than a wrong guess on junk",
      parse_bse_datetime("not a date") is None)


# ---------------------------------------------------------------------------
print("\n--- Option chain ---")
# ---------------------------------------------------------------------------

CHAIN = {"records": {"underlyingValue": 24000, "timestamp": "10-Jun-2025 10:15:00",
                     "data": [
    {"strikePrice": 24000, "expiryDate": "26-Jun-2025",
     "CE": {"openInterest": 100, "changeinOpenInterest": 10, "impliedVolatility": 12.5,
            "lastPrice": 150.0, "totalTradedVolume": 5000},
     "PE": {"openInterest": 200, "changeinOpenInterest": -5, "impliedVolatility": 13.1,
            "lastPrice": 120.0, "totalTradedVolume": 6000}},
    {"strikePrice": 24500, "expiryDate": "26-Jun-2025",
     "CE": {"openInterest": 50, "impliedVolatility": 11.0, "lastPrice": 40.0}},
    {"strikePrice": 40000, "expiryDate": "26-Jun-2025",
     "CE": {"openInterest": 1, "lastPrice": 0.05}},
]}}

snap = dt.datetime(2025, 6, 10, 10, 15, tzinfo=IST)
oc = option_chain.parse(CHAIN, "NIFTY", snap)
check("option chain parses in-band strikes", len(oc) == 2, f"{len(oc)}")
check("far wings are excluded (their OI is mostly noise)",
      all(r["payload"]["strike"] != 40000 for r in oc))
check("CE and PE legs both captured",
      "CE" in oc[0]["payload"] and "PE" in oc[0]["payload"])
check("IV is preserved — it is the reason this feed exists",
      oc[0]["payload"]["CE"]["iv"] == 12.5)
check("a live observation is known exactly when it is true",
      oc[0]["knowledge_time"] == oc[0]["event_time"] == snap)
check("a one-sided strike still records",
      any(r["payload"]["strike"] == 24500 for r in oc))


# ---------------------------------------------------------------------------
print("\n--- News arrival: pubDate is a claim, first_seen is a fact ---")
# ---------------------------------------------------------------------------

RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Nifty ends higher</title><link>https://x.test/a</link>
<pubDate>Tue, 10 Jun 2025 09:00:00 +0530</pubDate>
<description>&lt;p&gt;Markets rose&lt;/p&gt;</description></item>
<item><title>Rupee weakens</title><link>https://x.test/b</link>
<pubDate>Tue, 10 Jun 2025 11:30:00 +0530</pubDate></item>
</channel></rss>"""

seen_at = dt.datetime(2025, 6, 10, 12, 0, tzinfo=IST)
n = news.parse(RSS, "testfeed", seen_at, poll_interval_minutes=30)
check("RSS parses", len(n) == 2, f"{len(n)}")
check("knowledge_time is when WE saw it, never the publisher's claim",
      all(r["knowledge_time"] == seen_at for r in n))
check("the publisher's claim is kept for reference, not used as the gate",
      n[0]["payload"]["published_claimed"].startswith("2025-06-10T09:00"))
check("event_time uses the claimed publication",
      n[0]["event_time"].hour == 9)
check("HTML is stripped from descriptions",
      "<p>" not in (n[0]["payload"]["description"] or ""))
check("poll interval is recorded so a study knows the blur it inherited",
      n[0]["payload"]["poll_interval_minutes"] == 30)
check("items get a stable id so re-polling does not reset arrival time",
      n[0]["payload"]["id"] == news.parse(RSS, "testfeed",
                                          seen_at + dt.timedelta(hours=1))[0]["payload"]["id"])


# ---------------------------------------------------------------------------
print("\n--- Quotes and breadth ---")
# ---------------------------------------------------------------------------

STOCKS = {"data": [
    {"symbol": "INFY", "lastPrice": 1600, "pChange": 1.5, "open": 1580,
     "dayHigh": 1610, "dayLow": 1575, "previousClose": 1576,
     "totalTradedVolume": 100000, "totalTradedValue": 1.6e9},
    {"symbol": "TCS", "lastPrice": 3900, "pChange": -0.8, "totalTradedVolume": 50000},
    {"symbol": "WIPRO", "lastPrice": 250, "pChange": 0.0},
]}

q, b = quotes.parse(STOCKS, "NIFTY 500", snap)
check("per-symbol snapshot rows produced", len(q) == 3)
check("breadth row produced", len(b) == 1)
check("advances/declines/unchanged counted",
      (b[0]["payload"]["advances"], b[0]["payload"]["declines"],
       b[0]["payload"]["unchanged"]) == (1, 1, 1), str(b[0]["payload"]))
check("A/D ratio computed once, centrally",
      b[0]["payload"]["advance_decline_ratio"] == 1.0)
check("intraday shape is captured — the bhavcopy cannot reconstruct this",
      q[0]["payload"]["day_high"] == 1610 and q[0]["payload"]["ltp"] == 1600)


# ---------------------------------------------------------------------------
print("\n--- Agent decision context ---")
# ---------------------------------------------------------------------------

REGIME_RECS = [
    {"ts": "2026-09-01T08:31:00+05:30", "kind": "REGIME", "regime": "RANGE_BOUND",
     "playbook": "mean_reversion", "confidence": "medium",
     "evidence": {"adx14": 14.2, "nifty_close": 25100},
     "shadow": {"available": True, "regime": "CALM_UPDRIFT", "confidence": 0.71}},
]
d = decisions.parse_regime_rows(REGIME_RECS)
check("regime calls ingest", len(d) == 1)
check("the agent knew its own call when it made it",
      d[0]["knowledge_time"] == d[0]["event_time"] == to_dt("2026-09-01T08:31:00+05:30"))
check("the shadow HMM opinion is preserved — it is the promotion evidence",
      d[0]["payload"]["shadow"]["regime"] == "CALM_UPDRIFT")

CANDS = [{"symbol": "INFY", "score": 7.5, "playbook": "momentum", "direction": "LONG",
          "suggested_entry": 1600, "suggested_stop": 1550, "suggested_target": 1700,
          "reward_risk": 2.0, "reasons": ["breakout"],
          "indicators": {"rsi14": 62.1, "adx14": 27.0}}]
c = decisions.parse_candidate_rows(CANDS, snap, "premarket", "TRENDING_UP", "momentum_long")
check("candidate sets ingest with their full indicator context", len(c) == 1)
check("indicators survive — this is what makes the record reusable",
      c[0]["payload"]["indicators"]["adx14"] == 27.0)
check("record_candidates never raises, whatever it is handed",
      decisions.record_candidates([{"bad": "shape"}], store=None) >= 0)

TRADE_RECS = [
    {"ts": "2026-09-01T09:45:00+05:30", "kind": "REJECTED", "symbol": "TATASTEEL",
     "reasons": ["cost clearance"], "regime": "RANGE_BOUND"},
]
t = decisions.parse_trade_rows(TRADE_RECS)
check("guardrail rejections are kept — the nearest thing to a control group",
      len(t) == 1 and t[0]["payload"]["kind"] == "REJECTED")


# ---------------------------------------------------------------------------
print("\n--- Failure isolation ---")
# ---------------------------------------------------------------------------

ok = run_source("good", lambda: {"rows_seen": 5, "rows_new": 3})
bad = run_source("bad", lambda: (_ for _ in ()).throw(ValueError("endpoint moved")))
check("a healthy source reports counts", ok.ok and ok.rows_new == 3)
check("a failing source returns a result instead of raising",
      not bad.ok and "endpoint moved" in bad.error)
check("...so one dead feed cannot cost the others their snapshot",
      isinstance(bad.line(), str) and "FAILED" in bad.line())


# ---------------------------------------------------------------------------
print("\n--- End to end through the store ---")
# ---------------------------------------------------------------------------

s = Store.open(TMP / "src.db")
s.append_prices(prices_eod.parse(LEGACY))
s.append_many(announcements.parse(ANN, "INFY"))
s.append_many(option_chain.parse(CHAIN, "NIFTY", snap))

check("prices land and read back",
      len(s.view("2023-01-31").prices("INFY")) == 1)
check("a 19:40 filing is invisible to a 15:30 view",
      len(s.view("2024-01-11T15:30:00").observations("bse_announcement", "INFY")) == 1,
      "only the 02-Jan board meeting should be visible")
check("...and visible that night",
      len(s.view("2024-01-11T23:59:59").observations("bse_announcement", "INFY")) == 2)
check("re-parsing and re-appending the same source is a no-op",
      s.append_prices(prices_eod.parse(LEGACY)) == 0)
s.close()

print(f"\n{'=' * 52}")
print(f"  {PASSED} passed, {FAILED} failed")
print(f"{'=' * 52}\n")
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAILED else 0)
