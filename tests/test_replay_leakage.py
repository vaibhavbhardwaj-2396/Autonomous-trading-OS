"""
The time-travel torture suite.

Not "does replay work" — replay working is easy. These are the five ways a
point-in-time system convinces itself it is honest while quietly cheating, plus
the deletion test that catches the ones nobody anticipated.

The distinction every case below is probing:

    We are modelling KNOWLEDGE, not filtering future event dates.

Those sound the same and are not. A corporate action effective on 1 April was
public on 5 March; a system that filters on event date hides it on 10 March and
is wrong. A results filing disseminated at 19:00 describes a quarter that ended
months ago; a system that filters on event date shows it all day and is wrong in
the direction that flatters. Only one of those two errors costs money, and it is
the second.

Run with:  python -m tests.test_replay_leakage
"""

import sys
import shutil
import tempfile
import datetime as dt
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from research.store import Store, IST                                # noqa: E402
from research.replay import (Replay, ReplayStep, ReplayError,        # noqa: E402
                             leak_check, clock_ban_violations, make_provider)
from engine import market_data as md                                 # noqa: E402

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


TMP = Path(tempfile.mkdtemp(prefix="lq-replay-"))
store = Store.open(TMP / "torture.db")


def at(y, m, d, hh=0, mm=0):
    return dt.datetime(y, m, d, hh, mm, tzinfo=IST)


# ---------------------------------------------------------------------------
print("\n--- CASE 1: the after-hours filing ---")
# ---------------------------------------------------------------------------
# event 10 March, disseminated 10 March 19:00. Asked at 10 March 15:30.
# Must not exist: the market had closed before it was published.

store.append(dataset="filing", entity="ACME", event_time=at(2024, 3, 10),
             knowledge_time=at(2024, 3, 10, 19, 0), source="bse",
             payload={"headline": "Q4 results", "case": 1})

check("invisible at 15:30 on its own day",
      len(store.view(at(2024, 3, 10, 15, 30)).observations("filing", "ACME")) == 0)
check("visible at 19:30 the same evening",
      len(store.view(at(2024, 3, 10, 19, 30)).observations("filing", "ACME")) == 1)
check("visible the next morning",
      len(store.view(at(2024, 3, 11, 9, 15)).observations("filing", "ACME")) == 1)


# ---------------------------------------------------------------------------
print("\n--- CASE 2: the late filing ---")
# ---------------------------------------------------------------------------
# Happened 10 March; not disseminated until 11 March 09:15. Asked at the 10
# March close. Must not exist — an event study anchored on event date would
# trade a full session on information nobody had.

store.append(dataset="filing", entity="LATECO", event_time=at(2024, 3, 10),
             knowledge_time=at(2024, 3, 11, 9, 15), source="bse",
             payload={"headline": "Filed late", "case": 2})

check("invisible at the close of the day it happened",
      len(store.view(at(2024, 3, 10, 18, 30)).observations("filing", "LATECO")) == 0,
      "filtering on event_time instead of knowledge_time would show it here")
check("invisible at 09:00 the next day, minutes before dissemination",
      len(store.view(at(2024, 3, 11, 9, 0)).observations("filing", "LATECO")) == 0)
check("visible at 09:30 the next day",
      len(store.view(at(2024, 3, 11, 9, 30)).observations("filing", "LATECO")) == 1)


# ---------------------------------------------------------------------------
print("\n--- CASE 3: known before it happens ---")
# ---------------------------------------------------------------------------
# THE CASE THAT SEPARATES KNOWLEDGE-MODELLING FROM DATE-FILTERING.
# A split announced 5 March, effective 1 April. Asked on 10 March it MUST be
# visible, even though the event is three weeks in the future. A system that
# hides future event dates fails this and is silently useless for anything
# involving scheduled events — which is most of what matters.

store.append(dataset="corp_action", entity="SPLITCO", event_time=at(2024, 4, 1),
             knowledge_time=at(2024, 3, 5, 10, 0), source="bse",
             payload={"type": "SPLIT", "ratio": "1:2", "ex_date": "2024-04-01",
                      "case": 3})

on_10th = store.view(at(2024, 3, 10)).observations("corp_action", "SPLITCO")
check("a future-dated but already-announced action IS visible",
      len(on_10th) == 1,
      "we are modelling knowledge, not filtering future event dates")
check("...and it still reports its future effective date",
      on_10th and on_10th[0]["payload"]["ex_date"] == "2024-04-01")
check("invisible before it was announced",
      len(store.view(at(2024, 3, 4)).observations("corp_action", "SPLITCO")) == 0)

# The deliberate exception, so the difference is explicit rather than accidental.
store.append(dataset="index_membership", entity="NEWCO", event_time=at(2024, 4, 1),
             knowledge_time=at(2024, 3, 5), source="nse_pr",
             payload={"symbol": "NEWCO", "index_name": "Nifty 500",
                      "valid_from": "2024-04-01", "valid_to": None})
check("index membership applies BOTH gates — announced is not yet a member",
      "NEWCO" not in store.view(at(2024, 3, 10)).universe("Nifty 500"),
      "knowing a stock joins in April does not make it a constituent in March")
check("...and it becomes one once effective",
      "NEWCO" in store.view(at(2024, 4, 5)).universe("Nifty 500"))


# ---------------------------------------------------------------------------
print("\n--- CASE 4: the corrected record ---")
# ---------------------------------------------------------------------------
# Original known 10:00, correction known 14:00. At 11:00 we believed the
# original. History must not be retroactively improved.

store.append(dataset="fundamental", entity="REVCO", event_time=at(2024, 3, 31),
             knowledge_time=at(2024, 5, 1, 10, 0), source="filing",
             payload={"eps": 12.0, "case": 4})
store.append(dataset="fundamental", entity="REVCO", event_time=at(2024, 3, 31),
             knowledge_time=at(2024, 5, 1, 14, 0), source="filing",
             payload={"eps": 9.5, "case": 4}, revision=1)

at11 = store.view(at(2024, 5, 1, 11, 0)).observations("fundamental", "REVCO")
at15 = store.view(at(2024, 5, 1, 15, 0)).observations("fundamental", "REVCO")
check("at 11:00 we see the original number", len(at11) == 1 and at11[0]["payload"]["eps"] == 12.0,
      str(at11))
check("at 15:00 we see the corrected number", len(at15) == 1 and at15[0]["payload"]["eps"] == 9.5,
      str(at15))
check("the original is not deleted — the audit trail survives",
      len(store.view(at(2024, 5, 1, 15, 0)).observations(
          "fundamental", "REVCO", latest_only=False)) == 2)
check("a correction cannot reach backwards to a view before it existed",
      store.view(at(2024, 5, 1, 13, 59)).observations(
          "fundamental", "REVCO")[0]["payload"]["eps"] == 12.0)


# ---------------------------------------------------------------------------
print("\n--- CASE 5: the restatement trap ---")
# ---------------------------------------------------------------------------
# Today's knowledge of a corporate action must not alter yesterday's prices.
# This is the failure that produces backtests which are quietly clairvoyant
# about capital structure, and it is invisible in the numbers.

for i, close in enumerate([1000, 1010, 1020, 1030]):
    d = dt.date(2024, 1, 8) + dt.timedelta(days=i)
    store.append_price(symbol="SPLITCO", session_date=d,
                       knowledge_time=dt.datetime.combine(d, dt.time(18, 0), tzinfo=IST),
                       source="bhavcopy", close=close, open_=close, high=close,
                       low=close, volume=1000, adjusted=False)

# June: a 1:2 split occurs and an adjusted vendor series is loaded, halving
# every January close. It is known only from June onwards.
for i, close in enumerate([500, 505, 510, 515]):
    d = dt.date(2024, 1, 8) + dt.timedelta(days=i)
    store.append_price(symbol="SPLITCO", session_date=d,
                       knowledge_time=at(2024, 6, 1), source="yfinance",
                       close=close, open_=close, high=close, low=close,
                       volume=2000, adjusted=True)

feb = [r["close"] for r in store.view(at(2024, 2, 1)).prices("SPLITCO")]
july_unadj = [r["close"] for r in store.view(at(2024, 7, 1)).prices("SPLITCO")]
july_adj = [r["close"] for r in store.view(at(2024, 7, 1)).prices("SPLITCO", adjusted=True)]

check("February sees the prices as printed", feb == [1000, 1010, 1020, 1030], str(feb))
check("July STILL sees the same unadjusted prices for January",
      july_unadj == [1000, 1010, 1020, 1030], str(july_unadj))
check("the restated series exists but is opt-in only",
      july_adj == [500, 505, 510, 515], str(july_adj))
check("the restated series is invisible before the split was known",
      store.view(at(2024, 2, 1)).prices("SPLITCO", adjusted=True) == [])


# ---------------------------------------------------------------------------
print("\n--- The replay engine ---")
# ---------------------------------------------------------------------------

replay = Replay(store)
days = replay.trading_days("2024-01-01", "2024-01-31")
check("the calendar comes from data, not from a weekday heuristic",
      days == [dt.date(2024, 1, 8), dt.date(2024, 1, 9),
               dt.date(2024, 1, 10), dt.date(2024, 1, 11)], str(days))
check("replay cannot step onto a session we hold no prices for",
      dt.date(2024, 1, 15) not in days)

step = replay.step("2024-01-10")
df = step.history("SPLITCO", days=100)
check("a step reconstructs history as of that session", len(df) == 3, str(len(df)))
check("...and the last bar is that session's", str(df.index[-1].date()) == "2024-01-10")
check("a step cannot see the next session", 1030 not in list(df["close"]))


# ---------------------------------------------------------------------------
print("\n--- The engine seam: as_of without a provider must RAISE ---")
# ---------------------------------------------------------------------------

try:
    md.get_history("SPLITCO", days=10, as_of="2024-01-10")
    check("as_of with no provider raises rather than silently fetching live", False,
          "it returned data — this would be a silent leak")
except ValueError as e:
    check("as_of with no provider raises rather than silently fetching live",
          "provider" in str(e))

got = md.get_history("SPLITCO", days=100, as_of=step.as_of, provider=step.provider)
check("engine.get_history routes through the injected provider", len(got) == 3)

# The word "research" appears in market_data.py's docstring explaining why the
# provider is injected. Naming the rule is not breaking it — check for an actual
# import statement, the way test_kernel_isolation does.
import re as _re  # noqa: E402
_imports = _re.findall(r"^\s*(?:from|import)\s+([.\w]+)",
                       (ROOT / "engine" / "market_data.py").read_text(), _re.MULTILINE)
check("engine never imports research — the provider is injected",
      not any(m.split(".")[0] == "research" for m in _imports), str(_imports))


# ---------------------------------------------------------------------------
print("\n--- The stale-provider trap ---")
# ---------------------------------------------------------------------------
# The realistic way a careful person leaks in a loop: the code looks right, the
# provider is one iteration old.

old_step = replay.step("2024-01-09")
try:
    md.get_history("SPLITCO", days=10, as_of=step.as_of, provider=old_step.provider)
    check("a provider from a previous step is refused", False, "it returned data")
except ReplayError as e:
    check("a provider from a previous step is refused", "stale" in str(e).lower()
          or "bound to" in str(e))


# ---------------------------------------------------------------------------
print("\n--- The deletion test, on the replay path ---")
# ---------------------------------------------------------------------------

def honest(s: ReplayStep):
    return [r["close"] for r in s.view.prices("SPLITCO")]


def cheating(s: ReplayStep):
    return [r[0] for r in s._store._unsafe_connection().execute(
        "SELECT close FROM prices_eod WHERE symbol='SPLITCO' AND adjusted=0 "
        "ORDER BY session_date")]


ok = leak_check(store, "2024-01-09", honest, TMP / "lc1.db")
bad = leak_check(store, "2024-01-09", cheating, TMP / "lc2.db")
check("an honest computation is declared leak-free", ok["leak_free"], ok["verdict"])
check("a peeking computation is CAUGHT", not bad["leak_free"], bad["verdict"][:120])
check("the verdict says not to trust the peeking result",
      "do not trust" in bad["verdict"].lower())

reg_check = leak_check(store, "2024-01-10", honest, TMP / "lc3.db")
check("leak_check is reusable against any callable, not just fixtures",
      reg_check["leak_free"])


# ---------------------------------------------------------------------------
print("\n--- The clock ban ---")
# ---------------------------------------------------------------------------

exp_dir = ROOT / "research" / "experiments"
check("no experiment reads a wall clock",
      not clock_ban_violations(exp_dir),
      "; ".join(clock_ban_violations(exp_dir)))

scratch = TMP / "fake_experiments"
scratch.mkdir()
(scratch / "bad_exp.py").write_text(
    "import datetime as dt\n\ndef signal(step):\n"
    "    if dt.date.today().weekday() == 0:\n        return 1\n    return 0\n")
v = clock_ban_violations(scratch)
check("...and the ban actually detects one when present", len(v) == 1, str(v))
check("the violation names the file and line", v and "bad_exp.py:4" in v[0], str(v))


# ---------------------------------------------------------------------------
print("\n--- Live behaviour is unchanged ---")
# ---------------------------------------------------------------------------

import inspect  # noqa: E402
sig = inspect.signature(md.get_history)
check("as_of and provider default to None (live path untouched)",
      sig.parameters["as_of"].default is None
      and sig.parameters["provider"].default is None)

from engine import regime as rg, screener as sc  # noqa: E402
check("regime.classify_thresholds gained an optional as_of",
      inspect.signature(rg.classify_thresholds).parameters["as_of"].default is None)
check("screener.scan_for_playbook gained an optional as_of and universe",
      inspect.signature(sc.scan_for_playbook).parameters["as_of"].default is None
      and inspect.signature(sc.scan_for_playbook).parameters["symbols"].default is None)
check("replay never writes to the live regime log",
      "if log and as_of is None:" in (ROOT / "engine" / "regime.py").read_text())
check("the not-as-of-aware shadow HMM is skipped during replay, not silently run",
      "shadow HMM skipped" in (ROOT / "engine" / "regime.py").read_text())


store.close()
print(f"\n{'=' * 52}")
print(f"  {PASSED} passed, {FAILED} failed")
print(f"{'=' * 52}\n")
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAILED else 0)
