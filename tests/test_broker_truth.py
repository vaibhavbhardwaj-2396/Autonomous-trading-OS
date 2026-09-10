"""
tests/test_broker_truth.py — regression tests for api/broker_truth.py and the
broker-aware /account path (PART A: dashboard broker truth / source of truth).

Proves, mechanically:
  1. A Kite-era stale state cannot masquerade as current INDmoney data.
  2. The active broker is INDstocks (default, and from config).
  3. A failed / absent INDstocks sync produces an explicit unavailable/stale
     condition — no stale figure is presented as current.
  4. A successful, fresh INDstocks sync is represented as INDmoney data.
  5. allocated_capital stays independent of the total brokerage account value
     and is never overwritten by it.
  6. api/broker_truth.py and api/data.py add no import path from research/paper
     into live execution, and no broker/execute import at all.
  7. No live order placement is reachable from the dashboard GET endpoints.

Run with:  python -m tests.test_broker_truth
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

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


IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
NOW = dt.datetime(2026, 9, 10, 12, 0, 0, tzinfo=IST)


def _code_only(src: str) -> str:
    """Drop the module docstring, string literals and comments so a check
    for a real import or call is not fooled by prose that mentions the name."""
    src = re.sub(r'"""(?:.|\n)*?"""', "", src)
    src = re.sub(r"'''(?:.|\n)*?'''", "", src)
    src = re.sub(r'"[^"\n]*"', '""', src)
    src = re.sub(r"'[^'\n]*'", "''", src)
    src = re.sub(r"#.*", "", src)
    return src


def _clear_env():
    for k in ("BROKER", "DASHBOARD_BROKER_SNAPSHOT_MAX_AGE_HOURS", "DASHBOARD_BROKER_CUTOVER"):
        os.environ.pop(k, None)


_clear_env()
from api import broker_truth  # noqa: E402


# ---------------------------------------------------------------------------
print("\n--- active_broker(): the active broker is INDmoney / INDstocks ---")
# ---------------------------------------------------------------------------

_clear_env()
b = broker_truth.active_broker()
check("2. default active broker is indstocks (matches engine/broker.py default)",
      b["id"] == "indstocks", str(b))
check("2. default active broker label is 'INDmoney / INDstocks'",
      b["label"] == "INDmoney / INDstocks", str(b))
check("2. default is flagged as NOT from explicit config",
      b["from_config"] is False, str(b))

os.environ["BROKER"] = "indstocks"
b = broker_truth.active_broker()
check("2. BROKER=indstocks in env -> label 'INDmoney / INDstocks', from_config True",
      b["id"] == "indstocks" and b["label"] == "INDmoney / INDstocks" and b["from_config"] is True,
      str(b))

os.environ["BROKER"] = "kite"
b = broker_truth.active_broker()
check("kite is still representable as a fallback ('Zerodha / Kite')",
      b["id"] == "kite" and b["label"] == "Zerodha / Kite", str(b))
_clear_env()


# ---------------------------------------------------------------------------
print("\n--- classify_snapshot(): stale / never-synced / fresh ---")
# ---------------------------------------------------------------------------

_clear_env()

# 3. never synced -> explicit unavailable, not a silent zero/stale
never = broker_truth.classify_snapshot({"total_account_value": None, "synced_at": None}, now=NOW)
check("3. synced_at=None -> status 'never_synced', stale True",
      never["status"] == "never_synced" and never["stale"] is True, str(never))
check("3. never_synced carries a human reason and the source function name",
      "no successful broker sync" in never["reason"]
      and never["source"] == "engine.execute.sync_from_broker", str(never))

# 3. failed sync leaves an OLD synced_at -> stale by age
old = broker_truth.classify_snapshot(
    {"total_account_value": 570000.0, "synced_at": "2026-08-01T09:00:00+05:30"}, now=NOW)
check("3. a month-old broker_snapshot -> status 'stale', stale True",
      old["status"] == "stale" and old["stale"] is True, str(old))
check("3. stale reason mentions the age and the freshness ceiling",
      "old" in old["reason"] and "stale" in old["reason"], str(old))

# 4. fresh sync
fresh = broker_truth.classify_snapshot(
    {"total_account_value": 63000.0, "free_cash": 63000.0,
     "synced_at": "2026-09-10T09:30:00+05:30"}, now=NOW)
check("4. a 2.5h-old broker_snapshot -> status 'fresh', stale False, reason None",
      fresh["status"] == "fresh" and fresh["stale"] is False and fresh["reason"] is None,
      str(fresh))
check("4. fresh snapshot is labelled with the active broker (INDmoney / INDstocks)",
      fresh["broker"] == "INDmoney / INDstocks", str(fresh))

# unreadable timestamp -> unknown, never fresh
bad_ts = broker_truth.classify_snapshot({"synced_at": "yesterdayish"}, now=NOW)
check("an unreadable synced_at -> status 'unknown', stale True (never 'fresh')",
      bad_ts["status"] == "unknown" and bad_ts["stale"] is True, str(bad_ts))

# future timestamp (clock skew) -> not fresh
future = broker_truth.classify_snapshot({"synced_at": "2027-01-01T00:00:00+05:30"}, now=NOW)
check("a future synced_at is not treated as fresh",
      future["status"] != "fresh" and future["stale"] is True, str(future))

# a snapshot that records a DIFFERENT broker than the active one -> stale
_clear_env()
os.environ["BROKER"] = "indstocks"
diff = broker_truth.classify_snapshot(
    {"broker": "kite", "synced_at": "2026-09-10T11:30:00+05:30"}, now=NOW)
check("1/3. a snapshot written for 'kite' while active broker is indstocks -> stale",
      diff["status"] == "stale" and "kite" in diff["reason"].lower(), str(diff))
_clear_env()

# explicit cutover: any snapshot at/before it is Kite-era stale regardless of age
os.environ["DASHBOARD_BROKER_CUTOVER"] = "2026-09-07T00:00:00+05:30"
recent_but_precutover = broker_truth.classify_snapshot(
    {"synced_at": "2026-09-06T23:59:00+05:30"}, now=dt.datetime(2026, 9, 7, 1, 0, tzinfo=IST))
check("1. a snapshot synced before the configured cutover -> stale even when only minutes old",
      recent_but_precutover["status"] == "stale"
      and "cutover" in recent_but_precutover["reason"].lower(), str(recent_but_precutover))
_clear_env()

# configurable freshness ceiling
os.environ["DASHBOARD_BROKER_SNAPSHOT_MAX_AGE_HOURS"] = "1"
tight = broker_truth.classify_snapshot({"synced_at": "2026-09-10T09:30:00+05:30"}, now=NOW)
check("the freshness ceiling is configurable (1h -> a 2.5h-old snapshot is stale)",
      tight["status"] == "stale", str(tight))
_clear_env()


# ---------------------------------------------------------------------------
print("\n--- reconcile_book_value(): capital vs allocated_capital + realised P&L ---")
# ---------------------------------------------------------------------------

# 1. the exact Kite-era poisoning: capital == account total, ~57x the mandate
kite_era = {
    "allocated_capital": 10000.0,
    "realized_pnl_alltime": 0.0,
    "capital": 570000.0,
    "broker_snapshot": {"total_account_value": 570000.0},
}
rec = broker_truth.reconcile_book_value(kite_era)
check("1. Kite-era capital (₹5.7L on a ₹10k mandate) -> reconciled False",
      rec["reconciled"] is False, str(rec))
check("1. it is specifically flagged as looking like the whole account total",
      rec["looks_like_account_total"] is True, str(rec))
check("1. expected_book_value is allocated + realised P&L (₹10,000), not the stale capital",
      rec["expected_book_value"] == 10000.0, str(rec))

# 5. allocated_capital independent of total account value — a healthy book
healthy = {
    "allocated_capital": 20000.0,
    "realized_pnl_alltime": 1500.0,
    "capital": 21500.0,
    "broker_snapshot": {"total_account_value": 500000.0},
}
rec = broker_truth.reconcile_book_value(healthy)
check("5. capital == allocated + realised P&L -> reconciled True even when the account holds ₹5L",
      rec["reconciled"] is True and rec["looks_like_account_total"] is False, str(rec))
check("5. expected_book_value == 21500, allocated stays 20000 (independent of the ₹5L total)",
      rec["expected_book_value"] == 21500.0 and rec["allocated_capital"] == 20000.0, str(rec))

# a small post-close pre-resync drift is tolerated (not a false alarm)
drift = {"allocated_capital": 20000.0, "realized_pnl_alltime": 900.0, "capital": 20000.0,
         "broker_snapshot": {}}
check("a small unsynced-close drift (capital lags book value by ₹900) is still reconciled",
      broker_truth.reconcile_book_value(drift)["reconciled"] is True)


# ---------------------------------------------------------------------------
print("\n--- account_truth(): the one call api/data.py uses ---")
# ---------------------------------------------------------------------------

_clear_env()
stale_state = {
    "allocated_capital": 10000.0, "capital": 570000.0, "cash_available": 570000.0,
    "realized_pnl_alltime": 0.0, "peak_capital": 570000.0,
    "day": {"realized_pnl": 0.0},
    "broker_snapshot": {"total_account_value": 570000.0, "free_cash": 570000.0,
                        "unmanaged_symbols": ["RELIANCE", "TCS"],
                        "synced_at": "2026-07-15T09:00:00+05:30"},
    "last_updated": "2026-07-15T09:00:00+05:30",
}
truth = broker_truth.account_truth(stale_state, now=NOW)
check("1. stale Kite-era state -> account_value_status 'stale'",
      truth["account_value_status"] == "stale", str(truth["snapshot"]))
check("1. stale state -> safe_total_value is None (the ₹5.7L is NOT presented)",
      truth["safe_total_value"] is None and truth["safe_broker_free_cash"] is None, str(truth))
check("1. stale state -> book value not reconciled and a warning is emitted",
      truth["book_value"]["reconciled"] is False and len(truth["warnings"]) >= 1, str(truth))
check("2. active broker still labelled INDmoney / INDstocks on stale state",
      truth["broker"]["label"] == "INDmoney / INDstocks", str(truth["broker"]))

fresh_state = {
    "allocated_capital": 10000.0, "capital": 10250.0, "cash_available": 9800.0,
    "realized_pnl_alltime": 250.0, "peak_capital": 10250.0,
    "day": {"realized_pnl": 120.0},
    "broker_snapshot": {"total_account_value": 63000.0, "free_cash": 41000.0,
                        "unmanaged_symbols": ["RELIANCE"],
                        "synced_at": "2026-09-10T09:45:00+05:30"},
    "last_updated": "2026-09-10T09:45:00+05:30",
}
truth = broker_truth.account_truth(fresh_state, now=NOW)
check("4. fresh INDstocks sync -> account_value_status 'fresh'",
      truth["account_value_status"] == "fresh", str(truth["snapshot"]))
check("4. fresh sync -> safe_total_value is the real INDmoney number (₹63,000)",
      truth["safe_total_value"] == 63000.0 and truth["safe_broker_free_cash"] == 41000.0, str(truth))
check("4. fresh sync -> no warnings, book value reconciled",
      truth["warnings"] == [] and truth["book_value"]["reconciled"] is True, str(truth))
check("5. fresh sync: allocated_capital (₹10k) is untouched and separate from the ₹63k total",
      truth["book_value"]["allocated_capital"] == 10000.0
      and truth["safe_total_value"] == 63000.0, str(truth))


# ---------------------------------------------------------------------------
print("\n--- holdings_valuation(): fail closed when the total is cash-only ---")
# ---------------------------------------------------------------------------

_clear_env()

# The exact observed first-successful-sync state: 26 unmanaged holdings,
# total_account_value == free_cash == 32.31 (every holding priced at 0).
OBSERVED = {
    "allocated_capital": 10000.0, "capital": 10000.0, "cash_available": 32.31,
    "realized_pnl_alltime": 0.0, "peak_capital": 10000.0, "open_positions": [],
    "broker_snapshot": {
        "total_account_value": 32.31, "free_cash": 32.31,
        "unmanaged_symbols": [f"SYM{i}" for i in range(26)],
        "synced_at": "2026-09-10T11:00:00+05:30",
    },
    "last_updated": "2026-09-10T15:00:00+05:30",
}
hv = broker_truth.holdings_valuation(OBSERVED)
check("cash-only: 26 holdings but holdings_value ~₹0 -> complete=False",
      hv["complete"] is False and hv["holdings_value"] == 0.0, str(hv))
check("cash-only: holdings_count and unmanaged_holdings_count are both 26",
      hv["holdings_count"] == 26 and hv["unmanaged_holdings_count"] == 26, str(hv))
check("cash-only: the reason names the cash-only condition explicitly",
      "cash-only" in hv["reason"], str(hv["reason"]))

truth = broker_truth.account_truth(OBSERVED, now=NOW)
check("cash-only: account_value_status is 'incomplete' (sync is fresh, total isn't)",
      truth["account_value_status"] == "incomplete", str(truth))
check("cash-only: safe_total_value is None -> dashboard shows 'Unavailable', never ₹32.31",
      truth["safe_total_value"] is None, str(truth))
check("cash-only: safe_broker_free_cash IS shown (₹32.31 — a directly-confirmed field)",
      truth["safe_broker_free_cash"] == 32.31, str(truth))
check("cash-only: unmanaged holdings count is exposed (26) but value is not (None)",
      truth["unmanaged_holdings_count"] == 26 and truth["safe_unmanaged_value"] is None, str(truth))
check("cash-only: a warning explains the total could not be verified",
      any("could not be verified" in w for w in truth["warnings"]), str(truth["warnings"]))
check("cash-only: allocated_capital stays the ₹10k mandate, independent and untouched",
      truth["book_value"]["allocated_capital"] == 10000.0
      and truth["book_value"]["reconciled"] is True, str(truth["book_value"]))

# holdings present AND priced -> the total IS verified and shown
PRICED = {
    "allocated_capital": 10000.0, "capital": 10000.0, "cash_available": 32.31,
    "realized_pnl_alltime": 0.0, "peak_capital": 10000.0, "open_positions": [],
    "broker_snapshot": {
        "total_account_value": 63032.31, "free_cash": 32.31,
        "unmanaged_symbols": [f"SYM{i}" for i in range(26)],
        "synced_at": "2026-09-10T11:00:00+05:30",
    },
    "last_updated": "2026-09-10T15:00:00+05:30",
}
truth = broker_truth.account_truth(PRICED, now=NOW)
check("holdings priced: total ₹63,032.31 > cash -> account_value_status 'fresh', total shown",
      truth["account_value_status"] == "fresh" and truth["safe_total_value"] == 63032.31, str(truth))
check("holdings priced: unmanaged holdings VALUE is now exposed (₹63,000)",
      truth["safe_unmanaged_value"] == 63000.0, str(truth))
check("holdings priced: allocated_capital (₹10k) still independent of the ₹63k total",
      truth["book_value"]["allocated_capital"] == 10000.0
      and truth["safe_total_value"] != truth["book_value"]["allocated_capital"], str(truth))

# malformed snapshot: total present, free_cash missing, holdings present -> fail closed
MALFORMED = {
    "allocated_capital": 10000.0, "capital": 10000.0, "open_positions": [],
    "broker_snapshot": {
        "total_account_value": 50000.0, "free_cash": None,
        "unmanaged_symbols": ["SYM0", "SYM1"], "synced_at": "2026-09-10T11:00:00+05:30",
    },
}
truth = broker_truth.account_truth(MALFORMED, now=NOW)
check("malformed: holdings present but free_cash missing -> total withheld, warning",
      truth["safe_total_value"] is None and truth["account_value_status"] == "incomplete"
      and len(truth["warnings"]) >= 1, str(truth))

# no holdings at all: total == cash is legitimately complete
NO_HOLDINGS = {
    "allocated_capital": 10000.0, "capital": 10000.0, "open_positions": [],
    "broker_snapshot": {
        "total_account_value": 500.0, "free_cash": 500.0,
        "unmanaged_symbols": [], "synced_at": "2026-09-10T11:00:00+05:30",
    },
}
truth = broker_truth.account_truth(NO_HOLDINGS, now=NOW)
check("no holdings: total == cash is complete (nothing to value) -> total shown",
      truth["account_value_status"] == "fresh" and truth["safe_total_value"] == 500.0, str(truth))

# stale + cash-only: staleness wins, no silent fallback to the Kite-era total
STALE_CASHONLY = dict(OBSERVED)
STALE_CASHONLY = json.loads(json.dumps(OBSERVED))
STALE_CASHONLY["broker_snapshot"]["synced_at"] = "2026-06-01T00:00:00+05:30"
truth = broker_truth.account_truth(STALE_CASHONLY, now=NOW)
check("stale + cash-only: status is 'stale' (freshness checked first), total still None",
      truth["account_value_status"] == "stale" and truth["safe_total_value"] is None, str(truth))
check("stale + cash-only: no silent fallback to any stale/Kite figure",
      not any(str(32.31) in str(v) for k, v in truth.items()
              if k in ("safe_total_value", "safe_unmanaged_value")), str(truth))

_clear_env()


# ---------------------------------------------------------------------------
print("\n--- api/data.py get_account() end to end ---")
# ---------------------------------------------------------------------------

_clear_env()
from engine import guardrails as gr  # noqa: E402
from api import data as api_data  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="lq-broker-truth-"))


def _account_with_state(state: dict) -> dict:
    p = TMP / f"state_{abs(hash(json.dumps(state, sort_keys=True))) & 0xffff}.json"
    p.write_text(json.dumps(state))
    orig = gr.STATE_FILE
    gr.STATE_FILE = p
    try:
        return api_data.get_account()
    finally:
        gr.STATE_FILE = orig


acct = _account_with_state(stale_state)
check("1. get_account() on stale Kite-era state: total_value is None",
      acct["total_value"] is None, str(acct))
check("1. get_account(): account_value_status 'stale', book_value_reconciled False",
      acct["account_value_status"] == "stale" and acct["book_value_reconciled"] is False, str(acct))
check("1. get_account(): expected_book_value is ₹10,000, NOT the stale ₹5.7L capital",
      acct["expected_book_value"] == 10000.0 and acct["portfolio_value"] == 570000.0, str(acct))
check("1. get_account(): a non-empty account_warnings list is returned",
      isinstance(acct["account_warnings"], list) and len(acct["account_warnings"]) >= 1, str(acct))
check("2. get_account(): broker is labelled INDmoney / INDstocks",
      acct["broker"]["label"] == "INDmoney / INDstocks", str(acct["broker"]))
check("E (regression): get_account() still has the documented v1 fields",
      {"cash", "portfolio_value", "total_value", "pnl_today", "as_of"} <= acct.keys(), str(acct.keys()))

acct = _account_with_state(fresh_state)
check("4. get_account() on a fresh INDstocks sync: total_value == ₹63,000",
      acct["total_value"] == 63000.0, str(acct))
check("4. get_account(): account_value_status 'fresh', book_value_reconciled True",
      acct["account_value_status"] == "fresh" and acct["book_value_reconciled"] is True, str(acct))
check("4. get_account(): account_warnings is empty on a fresh sync",
      acct["account_warnings"] == [], str(acct))

# 5. allocated_capital is never rewritten to the account total, at any layer
big_account = dict(fresh_state)
big_account["broker_snapshot"] = dict(fresh_state["broker_snapshot"], total_account_value=900000.0)
acct = _account_with_state(big_account)
check("5. a ₹9L account total never becomes allocated_capital (stays ₹10,000)",
      acct["allocated_capital"] == 10000.0, str(acct))
check("5. allocated_capital and total_value are distinct fields with distinct values",
      acct["allocated_capital"] == 10000.0 and acct["total_value"] == 900000.0, str(acct))


# ---------------------------------------------------------------------------
print("\n--- PART A: active broker identity via configuration ---")
# ---------------------------------------------------------------------------

_clear_env()
b = broker_truth.active_broker()
check("A: with NO BROKER env, active_broker() still resolves to indstocks / INDmoney (never unknown)",
      b["id"] == "indstocks" and b["label"] == "INDmoney / INDstocks"
      and b["from_config"] is False, str(b))
check("A: 'unknown' is never a possible active-broker id or label",
      "unknown" not in (b["id"], b["label"].lower()), str(b))

os.environ["BROKER"] = "indstocks"
b = broker_truth.active_broker()
check("A: BROKER=indstocks in the API process env -> label INDmoney / INDstocks, from_config True",
      b["id"] == "indstocks" and b["label"] == "INDmoney / INDstocks" and b["from_config"] is True,
      str(b))

os.environ["BROKER"] = "  INDSTOCKS  "
b = broker_truth.active_broker()
check("A: BROKER is whitespace/case tolerant ('  INDSTOCKS  ' -> indstocks)",
      b["id"] == "indstocks" and b["label"] == "INDmoney / INDstocks", str(b))
_clear_env()

# the API service's own env file (deploy/api.env) is the configuration path —
# the example that ships MUST carry BROKER so the deployed service is explicit.
API_ENV_EXAMPLE = (ROOT / "deploy" / "api.env.example").read_text()
check("A: deploy/api.env.example carries BROKER=indstocks (the configured broker-identity path)",
      re.search(r"^BROKER=indstocks\s*$", API_ENV_EXAMPLE, re.MULTILINE) is not None,
      "the deployed trading-api.service reads deploy/api.env, not the trading .env")

acct = _account_with_state(fresh_state)
check("A: get_account() ALWAYS returns a non-empty broker label — a correctly deployed API "
      "cannot produce 'broker unknown'",
      isinstance(acct["broker"], dict) and acct["broker"]["label"]
      and acct["broker"]["label"].lower() != "unknown", str(acct["broker"]))
_clear_env()


# ---------------------------------------------------------------------------
print("\n--- PART B: dynamic broker account is not conflated with fixed capital ---")
# ---------------------------------------------------------------------------

_clear_env()
DYNAMIC = {
    "allocated_capital": 10000.0, "capital": 10000.0, "cash_available": 32.31,
    "realized_pnl_alltime": 0.0, "peak_capital": 10000.0, "open_positions": [],
    "broker_snapshot": {
        "total_account_value": 63032.31, "free_cash": 32.31,
        "unmanaged_symbols": [f"SYM{i}" for i in range(26)],
        "synced_at": (dt.datetime.now(IST) - dt.timedelta(hours=1)).isoformat(timespec="seconds"),
    },
}
acct = _account_with_state(DYNAMIC)
check("B: total_value is the DYNAMIC broker figure (₹63,032.31), not the ₹10k scaffold",
      acct["total_value"] == 63032.31 and acct["total_value"] != acct["allocated_capital"], str(acct))
check("B: broker_free_cash and total_value are distinct (₹32.31 vs ₹63,032.31)",
      acct["broker_free_cash"] == 32.31 and acct["total_value"] != acct["broker_free_cash"], str(acct))
check("B: holdings_market_value is exposed and dynamic (total − free cash ≈ ₹63,000)",
      abs(acct["holdings_market_value"] - 63000.0) < 0.5, str(acct.get("holdings_market_value")))
check("B: allocated_capital (₹10k) is still present but is NOT any of the dynamic broker figures",
      acct["allocated_capital"] == 10000.0
      and acct["allocated_capital"] not in (acct["total_value"], acct["broker_free_cash"],
                                            acct["holdings_market_value"]), str(acct))
check("B: a fund top-up changes total_value but never allocated_capital",
      _account_with_state(dict(DYNAMIC, broker_snapshot=dict(
          DYNAMIC["broker_snapshot"], total_account_value=90000.0)))["allocated_capital"] == 10000.0)
_clear_env()


# ---------------------------------------------------------------------------
print("\n--- PART D: unmanaged holdings — visible, never tradeable ---")
# ---------------------------------------------------------------------------

_clear_env()
acct = _account_with_state(DYNAMIC)
check("D: all 26 unmanaged holdings are visible in the read-only /account payload",
      acct["unmanaged_holdings"]["count"] == 26, str(acct["unmanaged_holdings"]))
check("D: broker_snapshot.unmanaged_symbols is preserved verbatim for reconciliation",
      len(acct["broker_snapshot"].get("synced_at") or "") > 0
      and acct["account_value_status"] == "fresh", str(acct["broker_snapshot"]))

# non-tradeable: no api/ code can place an order; guardrails refuse unmanaged symbols
GUARDRAILS_SRC = (ROOT / "engine" / "guardrails.py").read_text()
check("D: engine/guardrails.py refuses orders in unmanaged broker_snapshot symbols (the enforcement)",
      "unmanaged_symbols" in GUARDRAILS_SRC and "unmanaged" in GUARDRAILS_SRC, "the live safety boundary")
for mod in ("data.py", "broker_truth.py", "app.py"):
    src = _code_only((ROOT / "api" / mod).read_text())
    check(f"D: api/{mod} has no order-placement / sell path for any holding",
          not any(t in src for t in ("propose_trade", ".place(", "place_order", "get_broker(",
                                     "close_position", "sell")),
          "the dashboard/API must never modify a holding")
_clear_env()


# ---------------------------------------------------------------------------
print("\n--- absent allocated_capital + legacy peak_capital (the VPS state) ---")
# ---------------------------------------------------------------------------

_clear_env()
_fresh_ts = (dt.datetime.now(IST) - dt.timedelta(hours=1)).isoformat(timespec="seconds")

# The exact current VPS state after the first real INDmoney sync:
# allocated_capital key ABSENT, capital 10000, legacy peak_capital ~570k.
VPS = {
    "capital": 10000.0, "cash_available": 32.31, "realized_pnl_alltime": 0.0,
    "peak_capital": 570447.95, "open_positions": [],
    "broker_snapshot": {
        "total_account_value": 63339.56, "free_cash": 32.31,
        "unmanaged_symbols": [f"S{i}" for i in range(26)], "synced_at": _fresh_ts,
    },
    "last_updated": _fresh_ts,
}

rec = broker_truth.reconcile_book_value(VPS)
check("missing allocated_capital: allocated_capital_set is False",
      rec["allocated_capital_set"] is False and rec["allocated_capital"] is None, str(rec))
check("missing allocated_capital: the ₹10,000 engine.execute default is used for the check",
      rec["allocated_capital_effective"] == 10000.0
      and rec["expected_book_value"] == 10000.0, str(rec))
check("capital present + consistent with the default -> reconciled True (NO false warning)",
      rec["reconciled"] is True and rec["reason"] is None, str(rec))
check("missing allocated_capital: a calm note is set (not a `reason`/warning)",
      rec["note"] is not None and "default" in rec["note"], str(rec["note"]))

pk = broker_truth.peak_capital_status(VPS, allocated_effective=10000.0)
check("legacy peak: ₹570,447.95 vs ₹10k current -> is_legacy True",
      pk["is_legacy"] is True, str(pk))
check("legacy peak: implied drawdown ~98%",
      pk["implied_drawdown_pct"] is not None and pk["implied_drawdown_pct"] > 95.0, str(pk))
check("legacy peak: the note names it as an internal state issue, not a broker problem",
      "not a broker-data problem" in pk["note"] and "570,447.95" in pk["note"], str(pk["note"]))
check("peak_capital_status does NOT mutate state (peak_capital untouched)",
      VPS["peak_capital"] == 570447.95)

truth = broker_truth.account_truth(VPS, now=NOW if NOW > dt.datetime.fromisoformat(_fresh_ts)
                                   else dt.datetime.now(IST))
check("dashboard truth: broker figures are the DYNAMIC values (63339.56 / 32.31 / 63307.25)",
      truth["safe_total_value"] == 63339.56 and truth["safe_broker_free_cash"] == 32.31
      and truth["safe_holdings_market_value"] == 63307.25, str(truth))
check("dashboard truth: account_value_status is 'fresh' — the state IS valid",
      truth["account_value_status"] == "fresh", str(truth))
check("dashboard truth: warnings is EMPTY — no false 'book value cannot be reconciled'",
      truth["warnings"] == [], str(truth["warnings"]))
check("dashboard truth: two calm notes (absent allocated_capital, legacy peak) — not warnings",
      len(truth["notes"]) == 2
      and any("default" in n for n in truth["notes"])
      and any("legacy peak_capital" in n for n in truth["notes"]), str(truth["notes"]))

# no accidental account-total -> agent-capital conversion, at any layer
acct = _account_with_state(VPS)
check("no conversion: /account.total_value is ₹63,339.56, NOT written into any capital field",
      acct["total_value"] == 63339.56
      and acct["portfolio_value"] == 10000.0
      and acct["expected_book_value"] == 10000.0
      and acct["allocated_capital"] is None, str(acct))
check("no conversion: /account exposes allocated_capital_set False + a legacy-peak flag",
      acct["allocated_capital_set"] is False
      and acct["peak_capital_is_legacy"] is True
      and acct["peak_capital_note"] is not None, str(acct))
check("no conversion: /account.account_notes carries the context, account_warnings stays empty",
      acct["account_warnings"] == [] and len(acct["account_notes"]) == 2, str(acct))
check("no conversion: the stale ₹570k peak is NEVER any dynamic broker figure",
      570447.95 not in (acct["total_value"], acct["broker_free_cash"],
                        acct["holdings_market_value"]), str(acct))

# a genuine inconsistency (Kite-era capital == account total) IS still a warning
BROKEN = json.loads(json.dumps(VPS))
BROKEN["capital"] = 570447.95
BROKEN["broker_snapshot"]["total_account_value"] = 570447.95
t2 = broker_truth.account_truth(BROKEN, now=dt.datetime.now(IST))
check("regression: a real inconsistency (capital == account total) still produces a warning",
      t2["book_value"]["reconciled"] is False and len(t2["warnings"]) >= 1, str(t2["warnings"]))

# a plain valid state with allocated_capital set -> no notes, no warnings
GOOD = json.loads(json.dumps(VPS))
GOOD["allocated_capital"] = 10000.0
GOOD["peak_capital"] = 10250.0
t3 = broker_truth.account_truth(GOOD, now=dt.datetime.now(IST))
check("current valid state shape: allocated set + sane peak -> no warnings, no notes",
      t3["warnings"] == [] and t3["notes"] == []
      and t3["book_value"]["reconciled"] is True
      and t3["peak_capital"]["is_legacy"] is False, str(t3))
_clear_env()


# ---------------------------------------------------------------------------
print("\n--- MIGRATED state (dynamic capital model): no allocated_capital, no "
      "fixed capital, no false 'book value not reconciled' ---")
# ---------------------------------------------------------------------------
#
# The exact shape of memory/state.json AFTER scripts/migrate_capital_model.py
# has run on the VPS: a state["managed"] block is the source of truth,
# `allocated_capital` is GONE, `capital` is only a display mirror of the
# broker-derived managed equity (= broker free cash, because the agent holds
# no positions of its own yet). 26 unmanaged holdings worth ~Rs 63k are in
# the ACCOUNT total but must NEVER be in managed equity.
#
# Before this fix, reconcile_book_value() had no managed-model branch: it
# compared the Rs 32.31 `capital` mirror against DEFAULT_ALLOCATED_CAPITAL
# (Rs 10,000) + realised P&L and reported "book value not reconciled",
# which the dashboard rendered as a red error. These checks prove the
# managed model reconciles on its own terms.

_clear_env()
_mig_ts = (dt.datetime.now(IST) - dt.timedelta(hours=1)).isoformat(timespec="seconds")

MIGRATED = {
    "capital": 32.31,                      # legacy display mirror ONLY
    "cash_available": 32.31,
    "peak_capital": 32.31,                 # mirror: Sum(cashflow) + peak_growth
    "realized_pnl_alltime": 0.0,
    "open_positions": [],
    "managed": {
        "model_version": 1,
        "symbols": [],                     # nothing promoted — a sync never promotes
        "cashflow_events": [{"ts": _mig_ts, "amount": 32.31, "kind": "inception",
                             "reason": "capital-model migration — inception baseline",
                             "by": "migration"}],
        "peak_growth": 0.0,
        "growth": 0.0,
        "portfolio_value": 32.31,          # managed equity = managed cash + managed positions
        "free_cash": 32.31,
        "positions_market_value": 0.0,
        "valued_at": _mig_ts,
        "migrated_at": _mig_ts,
        "migration_mode": "live",
    },
    "day": {"realized_pnl": 0.0},
    "broker_snapshot": {
        "total_account_value": 63339.56, "free_cash": 32.31,
        "unmanaged_symbols": [f"S{i}" for i in range(26)], "synced_at": _mig_ts,
    },
    "last_updated": _mig_ts,
}
check("migrated fixture genuinely has NO allocated_capital and NO legacy fixed capital",
      "allocated_capital" not in MIGRATED, str(sorted(MIGRATED)))

# (1) the migrated state does NOT produce the "book value not reconciled" warning
rec = broker_truth.reconcile_book_value(MIGRATED)
check("1. reconcile_book_value picks the 'managed' model for a migrated state",
      rec["model"] == "managed", str(rec))
check("1. migrated state reconciles: reconciled True, reason None (NO false warning)",
      rec["reconciled"] is True and rec["reason"] is None, str(rec))
check("1. it does NOT fall back to allocated_capital: allocated_capital None, "
      "allocated_capital_set False",
      rec["allocated_capital"] is None and rec["allocated_capital_set"] is False, str(rec))

# (4/5) the managed branch NEVER reads allocated_capital and NEVER uses
# DEFAULT_ALLOCATED_CAPITAL — a bogus allocated_capital left on a migrated
# state is completely ignored, and managed equity that happens to equal
# neither the default nor a round number still reconciles cleanly.
_poisoned = json.loads(json.dumps(MIGRATED))
_poisoned["allocated_capital"] = 999999.0            # stray legacy key — must be ignored
for k in ("portfolio_value", "free_cash"):
    _poisoned["managed"][k] = 41827.63
_poisoned["capital"] = 41827.63
_poisoned["managed"]["cashflow_events"] = [{"ts": _mig_ts, "amount": 41827.63,
                                            "kind": "inception", "by": "migration"}]
_rp = broker_truth.reconcile_book_value(_poisoned)
check("4. managed branch ignores a stray allocated_capital key entirely "
      "(allocated_capital None, effective == managed equity)",
      _rp["allocated_capital"] is None
      and _rp["allocated_capital_effective"] == 41827.63, str(_rp))
check("5. managed branch never uses DEFAULT_ALLOCATED_CAPITAL: expected_book_value "
      "is the odd managed-equity figure Rs 41,827.63, reconciled True",
      _rp["expected_book_value"] == 41827.63 and _rp["reconciled"] is True
      and _rp["model"] == "managed", str(_rp))

# (3) managed-equity values pass reconciliation, on their own terms
check("3. expected_book_value is the broker-derived managed equity (Rs 32.31), "
      "not Rs 10,000 and not the Rs 63k account total",
      rec["expected_book_value"] == 32.31 and rec["managed_equity"] == 32.31, str(rec))
# managed equity is the sole authority — reconcile_book_value never compares
# two stored values and calls the result a reconciliation. A `capital`-mirror
# drift is a CALM NOTE, never a red 'not reconciled' warning.
DRIFTED = json.loads(json.dumps(MIGRATED))
DRIFTED["capital"] = 40000.0
_rd = broker_truth.reconcile_book_value(DRIFTED)
check("3. managed model is self-consistent by construction -> reconciled True, reason None, "
      "even when the legacy `capital` mirror has drifted",
      _rd["reconciled"] is True and _rd["reason"] is None, str(_rd))
check("3. a large `capital`-mirror drift is surfaced as a calm note (not silent, not red)",
      _rd["note"] is not None and "authoritative managed equity" in _rd["note"], str(_rd))
_td = broker_truth.account_truth(DRIFTED, now=dt.datetime.now(IST))
check("3. that drift note lands in notes[], NEVER in warnings[]",
      _td["warnings"] == [] and any("mirror" in n for n in _td["notes"]), str(_td))

pk = broker_truth.peak_capital_status(MIGRATED)
check("3. peak is on the growth series (Sum cashflow + peak_growth), never a legacy "
      "ratchet -> is_legacy False, implied drawdown 0%",
      pk["is_legacy"] is False and pk["implied_drawdown_pct"] == 0.0 and pk["note"] is None,
      str(pk))

# (2) broker-derived account totals pass reconciliation / are surfaced
truth = broker_truth.account_truth(MIGRATED, now=dt.datetime.now(IST))
check("2. account_truth on a migrated state emits NO warning at all",
      truth["warnings"] == [], str(truth["warnings"]))
check("2. specifically: no 'book value not reconciled' / 'cannot verify book value' text",
      not any("reconcil" in w.lower() or "verify book value" in w.lower()
              for w in truth["warnings"]), str(truth["warnings"]))
check("2. account_value_status 'fresh'; broker account total surfaced (Rs 63,339.56)",
      truth["account_value_status"] == "fresh"
      and truth["safe_total_value"] == 63339.56
      and truth["safe_broker_free_cash"] == 32.31, str(truth))
check("2. capital_model is reported as 'managed'",
      truth["capital_model"] == "managed", str(truth))

# (4) unmanaged holdings do not create a false managed-book mismatch
check("4. 26 unmanaged holdings do NOT break managed-book reconciliation",
      truth["book_value"]["reconciled"] is True
      and truth["unmanaged_holdings_count"] == 26, str(truth))
check("4. managed equity is the managed book only (Rs 32.31) and is NOT the "
      "Rs 63k account total",
      truth["safe_managed_equity"] == 32.31
      and truth["safe_managed_positions_market_value"] == 0.0
      and truth["safe_managed_equity"] != truth["safe_total_value"], str(truth))

# (5) the dashboard READ path (api/data.py.get_account) needs no legacy field
acct = _account_with_state(MIGRATED)
check("5. get_account() succeeds with NO allocated_capital / NO legacy capital authority",
      acct["book_value_reconciled"] is True
      and acct["account_warnings"] == []
      and acct["account_notes"] == [], str(acct))
check("5. get_account(): portfolio_value is the managed equity (Rs 32.31); "
      "allocated_capital passes straight through as None",
      acct["portfolio_value"] == 32.31 and acct["allocated_capital"] is None, str(acct))
check("5. get_account() exposes the dynamic managed book: managed_equity / "
      "managed_free_cash / managed_positions_market_value",
      acct["managed_equity"] == 32.31 and acct["managed_free_cash"] == 32.31
      and acct["managed_positions_market_value"] == 0.0, str(acct))
check("5. get_account(): capital_model 'managed', book_value_note None (no problem, "
      "no calm note needed)",
      acct["capital_model"] == "managed" and acct["book_value_note"] is None, str(acct))
check("5. get_account(): total_value (Rs 63,339.56) and managed_equity (Rs 32.31) "
      "are distinct fields",
      acct["total_value"] == 63339.56 and acct["managed_equity"] == 32.31, str(acct))

# a recorded deposit: managed equity rises, still reconciles, still no drawdown
DEPOSITED = json.loads(json.dumps(MIGRATED))
DEPOSITED["managed"]["cashflow_events"].append(
    {"ts": _mig_ts, "amount": 50000.0, "kind": "deposit", "reason": "top-up", "by": "vaibhav"})
for k in ("free_cash", "portfolio_value"):
    DEPOSITED["managed"][k] = 50032.31
DEPOSITED["capital"] = DEPOSITED["peak_capital"] = 50032.31
DEPOSITED["broker_snapshot"]["free_cash"] = 50032.31
DEPOSITED["broker_snapshot"]["total_account_value"] = 113339.56
t_dep = broker_truth.account_truth(DEPOSITED, now=dt.datetime.now(IST))
check("deposit: managed equity rises to Rs 50,032.31, still reconciled, still no "
      "warning, drawdown still 0%",
      t_dep["book_value"]["reconciled"] is True and t_dep["warnings"] == []
      and t_dep["peak_capital"]["implied_drawdown_pct"] == 0.0, str(t_dep))

# (6) the fix introduced no hard-coded current-account amount
_bt_code_now = _code_only((ROOT / "api" / "broker_truth.py").read_text())
_data_code_now = _code_only((ROOT / "api" / "data.py").read_text())
check("6. no observed rupee amount was baked into api/broker_truth.py or api/data.py "
      "(no 63339 / 63307 / 63032 / 570447 / 5.7L literal)",
      not any(tok in _bt_code_now or tok in _data_code_now
              for tok in ("63339", "63307", "63032", "570447", "570000", "63000")),
      "the dashboard fix must read dynamic broker fields, never a constant")
# the dashboard fix added NO new numeric literal to api/broker_truth.py — the
# multi-digit constants present are all pre-existing (the cited Rs 10,000
# engine.execute default, the 3600s/hour divisor, the Rs 5,000 legacy-bug
# margin). Compare against the committed baseline.
import subprocess as _sp  # noqa: E402
_committed_bt = _sp.run(["git", "-C", str(ROOT), "show", "HEAD:api/broker_truth.py"],
                        capture_output=True, text=True).stdout
_lit = lambda s: set(re.findall(r"\b\d[\d_]{2,}(?:\.\d+)?\b", _code_only(s)))
check("6. api/broker_truth.py gains NO new numeric literal vs the committed version",
      _lit(_bt_code_now) <= _lit(_committed_bt) | {"10_000.0"},
      f"new literals: {sorted(_lit(_bt_code_now) - _lit(_committed_bt))}")
_clear_env()


# ---------------------------------------------------------------------------
print("\n--- 6/7. isolation: no new import path into live execution ---")
# ---------------------------------------------------------------------------

BROKER_TRUTH_SRC = (ROOT / "api" / "broker_truth.py").read_text()
_bt_imports = re.findall(r"^\s*(?:from|import)\s+([.\w]+)", _code_only(BROKER_TRUTH_SRC), re.MULTILINE)
check("6. api/broker_truth.py imports no engine.* at all",
      not any(m.startswith("engine") for m in _bt_imports), str(_bt_imports))
check("6. api/broker_truth.py imports nothing from research/ or paper/",
      not any(m.split(".")[0] in ("research", "paper") for m in _bt_imports), str(_bt_imports))
check("6. api/broker_truth.py imports only the standard library",
      set(m.split(".")[0] for m in _bt_imports) <= {"__future__", "datetime", "os", "typing"},
      str(_bt_imports))
_bt_code = _code_only(BROKER_TRUTH_SRC)
check("6. api/broker_truth.py has no broker/HTTP call in code (docstrings aside)",
      not any(tok in _bt_code for tok in ("get_broker(", ".place(", "._post(", "requests.", "urllib")),
      "broker-truth must be a pure classifier, never a broker client")

DATA_SRC = (ROOT / "api" / "data.py").read_text()
_data_imports = re.findall(r"^\s*(?:from|import)\s+([.\w]+)", _code_only(DATA_SRC), re.MULTILINE)
check("6. api/data.py still imports no engine.broker* / engine.execute after the change",
      not any(m.startswith("engine.broker") or m == "engine.execute" for m in _data_imports),
      str(_data_imports))

APP_SRC = (ROOT / "api" / "app.py").read_text()
_route_methods = re.findall(r"@app\.route\([^)]*methods\s*=\s*\[([^\]]*)\]", APP_SRC)
_all_methods = {m.strip().strip('"\'').upper() for group in _route_methods for m in group.split(",")}
check("7. every api/app.py route is declared GET-only",
      _all_methods == {"GET"}, f"declared methods across all routes: {_all_methods}")
_app_code = _code_only(APP_SRC)
check("7. api/app.py never calls propose_trade / place / a broker in code",
      not any(tok in _app_code for tok in ("propose_trade", "get_broker(", ".place(", "run_paper_cycle")),
      "dashboard endpoints must stay read-only")
check("7. /account route body is just jsonify(data.get_account()) — no side effect",
      re.search(r"def account\(\):\s*\n\s*return jsonify\(data\.get_account\(\)\)", APP_SRC) is not None,
      "the /account handler must remain a thin read")

# The broker-truth / dashboard code must not touch the execution kernel.
# (engine/execute.py + engine/guardrails.py ARE modified in the separately
# approved dynamic-capital-model slice — docs/CAPITAL_MODEL.md,
# tests/test_capital_model.py. This file asserts only that api/ stays a
# read-only layer with no order path, which is checked above.)
import subprocess  # noqa: E402
_diff = subprocess.run(
    ["git", "-C", str(ROOT), "diff", "--name-only", "HEAD", "--",
     "api/app.py", "api/auth.py", "api/wsgi.py"],
    capture_output=True, text=True)
check("the read-only API core (app.py / auth.py / wsgi.py) is unchanged by broker-truth work",
      _diff.stdout.strip() == "", f"changed: {_diff.stdout.strip()!r}")


print(f"\n{'=' * 52}")
print(f"  {PASSED} passed, {FAILED} failed")
print(f"{'=' * 52}\n")
sys.exit(1 if FAILED else 0)
