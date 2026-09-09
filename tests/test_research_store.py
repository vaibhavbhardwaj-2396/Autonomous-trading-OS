"""
Tests for the Market Memory and the two firewalls.

The most important test in this file is `no-lookahead deletion test`. Everything
else checks that the store behaves; that one checks that a study cannot cheat,
which is the only property the whole research programme rests on.

Run with:  python -m tests.test_research_store
"""

import sys
import json
import shutil
import sqlite3
import tempfile
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from research.store import Store, AppendOnlyViolation, to_dt, ts  # noqa: E402
from research import contracts as ct  # noqa: E402

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


TMP = Path(tempfile.mkdtemp(prefix="lq-test-"))


def fresh(name="t.db") -> Store:
    p = TMP / name
    if p.exists():
        p.unlink()
    return Store.open(p)


# ---------------------------------------------------------------------------
print("\n--- Time handling ---")
# ---------------------------------------------------------------------------

check("bare date as as_of means end of day",
      to_dt("2024-03-14", end_of_day=True).hour == 23)
check("bare date as event means start of day",
      to_dt("2024-03-14").hour == 0)
check("naive datetimes are assumed IST",
      to_dt("2024-03-14T10:00:00").utcoffset() == dt.timedelta(hours=5, minutes=30))
check("explicit offsets are respected, not overwritten",
      to_dt("2024-03-14T10:00:00+00:00").utcoffset() == dt.timedelta(0))
# The reason knowledge_ts is an integer: these two strings sort the wrong way.
check("UTC vs IST ordering is correct as integers (string sort would fail)",
      ts("2024-03-14T18:00:00+05:30") < ts("2024-03-14T18:00:00+00:00"),
      "IST 18:00 is earlier in absolute time than UTC 18:00")


# ---------------------------------------------------------------------------
print("\n--- Append and read ---")
# ---------------------------------------------------------------------------

s = fresh()
rid = s.append(dataset="announcement", entity="INFY",
               event_time="2024-01-11T09:00:00", knowledge_time="2024-01-11T17:42:00",
               source="bse", payload={"headline": "Q3 results"})
check("append returns a row id", isinstance(rid, int) and rid > 0)

dup = s.append(dataset="announcement", entity="INFY",
               event_time="2024-01-11T09:00:00", knowledge_time="2024-01-11T17:42:00",
               source="bse", payload={"headline": "Q3 results"})
check("identical re-ingest is a no-op (backfills are re-runnable)", dup is None)

rows = s.view("2024-01-31").observations("announcement", "INFY")
check("observation reads back", len(rows) == 1, str(rows))
check("payload deserialises", rows and rows[0]["payload"]["headline"] == "Q3 results")


# ---------------------------------------------------------------------------
print("\n--- The as-of gate ---")
# ---------------------------------------------------------------------------

s.append(dataset="announcement", entity="INFY",
         event_time="2024-02-08T09:00:00", knowledge_time="2024-02-08T18:10:00",
         source="bse", payload={"headline": "Buyback"})

before = s.view("2024-02-01").observations("announcement", "INFY")
after = s.view("2024-02-28").observations("announcement", "INFY")
check("facts published after as_of are invisible", len(before) == 1, str(before))
check("...and visible once as_of passes them", len(after) == 2, str(after))

# Same calendar day, different hour: knowledge at 18:10 is NOT available at 09:00.
intraday = s.view("2024-02-08T09:00:00").observations("announcement", "INFY")
check("the gate has hour resolution, not just day resolution", len(intraday) == 1,
      f"got {len(intraday)} — an 18:10 filing must not be visible at 09:00")


# ---------------------------------------------------------------------------
print("\n--- Revisions ---")
# ---------------------------------------------------------------------------

s2 = fresh("rev.db")
s2.append(dataset="fundamental", entity="ACME", event_time="2024-03-31",
          knowledge_time="2024-04-20", source="filing", payload={"eps": 10.0})
s2.append(dataset="fundamental", entity="ACME", event_time="2024-03-31",
          knowledge_time="2024-06-01", source="filing", payload={"eps": 8.5},
          revision=1)

early = s2.view("2024-05-01").observations("fundamental", "ACME")
late = s2.view("2024-07-01").observations("fundamental", "ACME")
check("before the restatement we see the original number",
      len(early) == 1 and early[0]["payload"]["eps"] == 10.0, str(early))
check("after the restatement we see the corrected number",
      len(late) == 1 and late[0]["payload"]["eps"] == 8.5, str(late))
check("latest_only=False exposes the full audit trail",
      len(s2.view("2024-07-01").observations("fundamental", "ACME",
                                             latest_only=False)) == 2)


# ---------------------------------------------------------------------------
print("\n--- Append-only enforcement ---")
# ---------------------------------------------------------------------------

conn = s2._unsafe_connection()
try:
    conn.execute("UPDATE observations SET payload='{}' WHERE id=1")
    check("UPDATE is blocked by trigger", False, "the UPDATE succeeded")
except sqlite3.IntegrityError:
    check("UPDATE is blocked by trigger", True)
except sqlite3.OperationalError as e:
    check("UPDATE is blocked by trigger", "append-only" in str(e), str(e))
except sqlite3.DatabaseError as e:
    check("UPDATE is blocked by trigger", "append-only" in str(e), str(e))

try:
    conn.execute("DELETE FROM observations WHERE id=1")
    check("DELETE is blocked by trigger", False, "the DELETE succeeded")
except sqlite3.DatabaseError as e:
    check("DELETE is blocked by trigger", "append-only" in str(e), str(e))


# ---------------------------------------------------------------------------
print("\n--- Prices ---")
# ---------------------------------------------------------------------------

s3 = fresh("px.db")
# Bhavcopy publishes ~18:00 IST. A close is NOT knowable at 15:30 on the day.
for i, close in enumerate([100, 102, 101, 105, 108]):
    d = dt.date(2024, 4, 1) + dt.timedelta(days=i)
    s3.append_price(symbol="ACME", session_date=d,
                    knowledge_time=dt.datetime.combine(d, dt.time(18, 0)),
                    source="bhavcopy", open_=close - 1, high=close + 1,
                    low=close - 2, close=close, volume=1000 * (i + 1))

check("prices read back in date order",
      [r["close"] for r in s3.view("2024-04-30").prices("ACME")] == [100, 102, 101, 105, 108])
check("a close is not knowable at 15:30 on its own session",
      len(s3.view("2024-04-05T15:30:00").prices("ACME")) == 4,
      "the 5th close is published at 18:00 — a 'buy at today's close' strategy "
      "using today's close is not implementable")
check("...and is knowable that evening",
      len(s3.view("2024-04-05T18:30:00").prices("ACME")) == 5)

df = s3.view("2024-04-30").history("ACME", days=260)
check("history() returns an engine-shaped DataFrame",
      list(df.columns) == ["open", "high", "low", "close", "volume"], str(list(df.columns)))
check("history() index is datetime", str(df.index.dtype).startswith("datetime"))

# The adjusted/unadjusted split
s3.append_price(symbol="ACME", session_date="2024-04-01", knowledge_time="2026-01-01",
                source="yfinance", close=50.0, adjusted=True)
check("unadjusted series is unaffected by a later restated series",
      s3.view("2026-06-01").prices("ACME")[0]["close"] == 100)
check("restated series is opt-in only",
      s3.view("2026-06-01").prices("ACME", adjusted=True)[0]["close"] == 50.0)


# ---------------------------------------------------------------------------
print("\n--- THE NO-LOOKAHEAD DELETION TEST ---")
# ---------------------------------------------------------------------------
# Run the same computation twice at the same as_of: once against the full
# database, once against a copy with every future row physically removed. If the
# answers differ, the computation saw something it should not have.
#
# This is the only defence here that can actually fail. Structural gating and
# code review both rely on nobody making a mistake; this one detects the mistake.

def honest_signal(view):
    """Uses only the view. Should be identical on both databases."""
    px = view.prices("ACME")
    return round(sum(r["close"] for r in px) / len(px), 4) if px else None


def cheating_signal(view, store):
    """Reaches past the view to the raw connection — the exact mistake the test
    exists to catch."""
    rows = store._unsafe_connection().execute(
        "SELECT close FROM prices_eod WHERE symbol='ACME' AND adjusted=0").fetchall()
    return round(sum(r[0] for r in rows) / len(rows), 4) if rows else None


AS_OF = "2024-04-03T18:30:00"
full = s3
trunc = s3.truncated_snapshot(AS_OF, TMP / "truncated.db")

h_full = honest_signal(full.view(AS_OF))
h_trunc = honest_signal(trunc.view(AS_OF))
check("honest computation is identical on full and truncated data",
      h_full == h_trunc, f"full={h_full} truncated={h_trunc}")

c_full = cheating_signal(full.view(AS_OF), full)
c_trunc = cheating_signal(trunc.view(AS_OF), trunc)
check("the test DETECTS a computation that peeks (it must differ)",
      c_full != c_trunc, f"full={c_full} truncated={c_trunc} — if these matched, "
                         f"the test would be worthless")
trunc.close()


# ---------------------------------------------------------------------------
print("\n--- Point-in-time universe ---")
# ---------------------------------------------------------------------------

s4 = fresh("univ.db")
# Announced 2023-02-24, effective 2023-03-31 — the ~4 week gap that makes
# bitemporal storage necessary rather than merely tidy.
s4.append(dataset="index_membership", entity="NEWCO", event_time="2023-03-31",
          knowledge_time="2023-02-24", source="nse_pr",
          payload={"symbol": "NEWCO", "index_name": "Nifty 500",
                   "valid_from": "2023-03-31", "valid_to": None})
s4.append(dataset="index_membership", entity="OLDCO", event_time="2020-01-01",
          knowledge_time="2020-01-01", source="nse_pr",
          payload={"symbol": "OLDCO", "index_name": "Nifty 500",
                   "valid_from": "2020-01-01", "valid_to": "2023-03-31"})

check("known-but-not-yet-effective is NOT a member",
      s4.view("2023-03-01").universe("Nifty 500") == ["OLDCO"],
      str(s4.view("2023-03-01").universe("Nifty 500")))
check("effective and published -> member",
      s4.view("2023-04-15").universe("Nifty 500") == ["NEWCO"],
      str(s4.view("2023-04-15").universe("Nifty 500")))


# ---------------------------------------------------------------------------
print("\n--- Contracts: hashing and the data firewall ---")
# ---------------------------------------------------------------------------

def make(**over):
    base = dict(
        id="EXP-TEST", title="t", hypothesis="h", null_hypothesis="n",
        universe="u", signal="s", entry_rule="e", exit_rule="x",
        splits={"discovery": ["2019", "2022"]}, independence="clustered by event date",
        falsification="clustered t > 2.5 on holdout",
        abandon_condition="gross Q5-Q1 < 1.5% in discovery",
        evaluation_start="2019-01-01", evaluation_end="2022-12-31",
    )
    base.update(over)
    return ct.Contract(**base)


def verifies(contract) -> bool:
    try:
        contract.verify()
        return True
    except ct.ContractViolation:
        return False


c = make().lock()
check("locking records a hash", bool(c.locked_hash))
check("a locked, unchanged contract verifies", verifies(c))

c.hypothesis = "something more convenient"
try:
    c.verify()
    check("editing a locked hypothesis is refused", False, "verify() passed")
except ct.ContractViolation as e:
    check("editing a locked hypothesis is refused", "edited" in str(e).lower())

c2 = make(notes="added later")
c2.lock()
check("non-hashed bookkeeping does not change the hash",
      c2.content_hash() == make().lock().content_hash())

try:
    make(abandon_condition="  ").lock()
    check("a contract with no abandon condition cannot be locked", False)
except ct.ContractViolation:
    check("a contract with no abandon condition cannot be locked", True)


# ---------------------------------------------------------------------------
print("\n--- Contracts: THE MODEL FIREWALL ---")
# ---------------------------------------------------------------------------

leaky = make(id="EXP-A", llm_features=True, llm_model_id="claude-opus-5",
             llm_knowledge_cutoff="2026-05-05",
             evaluation_start="2019-01-01", evaluation_end="2022-12-31")
v = leaky.model_firewall_violations()
check("LLM features + pre-cutoff window = firewall breach", len(v) == 1, str(v))
check("the breach explains that the leak is in the weights, not the query",
      v and "weights" in v[0])

try:
    leaky.lock()
    check("a breaching contract cannot be locked", False, "it locked")
except ct.ContractViolation:
    check("a breaching contract cannot be locked", True)

forward = make(id="EXP-A", llm_features=True, llm_model_id="claude-opus-5",
               llm_knowledge_cutoff="2026-05-05",
               evaluation_start="2026-06-01", evaluation_end="2027-06-01")
check("the same experiment locks cleanly when forward-only",
      not forward.model_firewall_violations())
forward.lock()
check("forward-only contract verifies", verifies(forward))

unnamed = make(llm_features=True, evaluation_start="2026-06-01",
               evaluation_end="2027-06-01")
check("an LLM feature with no declared model/cutoff is refused",
      len(unnamed.model_firewall_violations()) >= 1)

deterministic = make(id="EXP-B1", llm_features=False, evaluation_start="2019-01-01")
check("a deterministic contract is unaffected by the model firewall",
      not deterministic.model_firewall_violations())


# ---------------------------------------------------------------------------
print("\n--- Registry ---")
# ---------------------------------------------------------------------------

reg = TMP / "registry"
make(id="EXP-B1").lock().save(reg)
forward.save(reg)
make(id="EXP-DRAFT").save(reg)          # left as a draft on purpose
check("registry loads what it saved", len(ct.registry(reg)) == 3)
check("the comparison denominator counts locked contracts only",
      ct.comparison_count(reg) == 2, str(ct.comparison_count(reg)))
check("a saved contract round-trips with its hash intact",
      ct.Contract.load("EXP-B1", reg).content_hash() ==
      make(id="EXP-B1").content_hash())


print(f"\n{'=' * 52}")
print(f"  {PASSED} passed, {FAILED} failed")
print(f"{'=' * 52}\n")
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAILED else 0)
