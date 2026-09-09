"""
Tests for Phase 1 Slice B: the Observatory (research/brain/observatory.py).

Six things this file has to demonstrate, per the design review:
    1. a genuine anomaly gets detected
    2. a normal observation does NOT get flagged
    3. insufficient history refuses (returns None) rather than guessing
    4. zero/invalid variance refuses rather than fabricating a z-score
    5. the as_of / knowledge-time boundary is respected (no leakage of
       late-arriving data into a detector run at an earlier as_of)
    6. output is deterministic / reproducible

Nothing here touches engine/, contracts.py, replay.py, or Claude permissions
— this slice is research/brain/{__init__,observatory}.py only.

Run with:  python -m tests.test_research_brain
"""

import sys
import tempfile
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from research.store import Store, IST  # noqa: E402
from research.brain import observatory as obs  # noqa: E402
from research import memory as rm  # noqa: E402

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


TMP = Path(tempfile.mkdtemp(prefix="lq-test-brain-"))
START = dt.date(2024, 1, 1)


def fresh(name) -> Store:
    p = TMP / name
    if p.exists():
        p.unlink()
    return Store.open(p)


def seed_prices(store, symbol, n, volumes, closes, *, source="test", knowledge_offset_days=None):
    """Append n consecutive daily sessions starting at START. `knowledge_offset_days`,
    if given, is a list of per-session offsets (days after session_date) for when the
    row became knowable — used only by the as_of boundary test."""
    for i in range(n):
        d = START + dt.timedelta(days=i)
        kd = d if not knowledge_offset_days else d + dt.timedelta(days=knowledge_offset_days[i])
        store.append_price(
            symbol=symbol, session_date=d, knowledge_time=kd, source=source,
            open_=closes[i], high=closes[i], low=closes[i], close=closes[i],
            volume=volumes[i],
        )


def seed_announcements(store, symbol, counts):
    """counts[i] = number of bse_announcement rows on day START+i."""
    for i, n in enumerate(counts):
        d = START + dt.timedelta(days=i)
        for j in range(n):
            store.append(
                dataset="bse_announcement", entity=symbol,
                event_time=dt.datetime.combine(d, dt.time(10, j % 50), tzinfo=IST),
                knowledge_time=dt.datetime.combine(d, dt.time(10, j % 50), tzinfo=IST),
                source="test", payload={"headline": f"note {i}.{j}"},
            )


# ---------------------------------------------------------------------------
print("\n--- volume_anomaly: genuine anomaly detected ---")
# ---------------------------------------------------------------------------

store = fresh("volume.db")
n = obs.WINDOW_DAYS + 1
baseline_vols = [100_000 + (i % 5) * 500 for i in range(n - 1)]
vols = baseline_vols + [900_000]  # unmistakable spike on the last session
closes = [100.0 + 0.01 * i for i in range(n)]
seed_prices(store, "SPIKE", n, vols, closes)

as_of = START + dt.timedelta(days=n - 1)
view = store.view(as_of)
rec = obs.volume_anomaly(view, "SPIKE", as_of)
check("volume spike is flagged", rec is not None)
if rec is not None:
    check("z_score clears the 3.0 threshold", abs(rec.z_score) >= obs.Z_THRESHOLD, str(rec.z_score))
    check("metric name is volume_zscore", rec.metric == "volume_zscore")
    check("current session excluded from its own baseline",
          rec.n_observations == n - 1, f"got {rec.n_observations}")
    check("value carries the actual current volume", rec.value == 900_000.0)

# ---------------------------------------------------------------------------
print("\n--- volume_anomaly: normal observation is NOT flagged ---")
# ---------------------------------------------------------------------------

store2 = fresh("volume_normal.db")
vols_normal = [100_000 + (i % 5) * 500 for i in range(n)]
seed_prices(store2, "CALM", n, vols_normal, closes)
as_of2 = START + dt.timedelta(days=n - 1)
rec_normal = obs.volume_anomaly(store2.view(as_of2), "CALM", as_of2)
check("ordinary in-range volume produces no anomaly record", rec_normal is None)

# ---------------------------------------------------------------------------
print("\n--- volume_anomaly: insufficient history refuses ---")
# ---------------------------------------------------------------------------

store3 = fresh("volume_short.db")
short_n = obs.MIN_OBSERVATIONS  # one short of MIN_OBSERVATIONS + 1 baseline+current
seed_prices(store3, "NEWLISTING", short_n, [100_000] * short_n, [50.0] * short_n)
as_of3 = START + dt.timedelta(days=short_n - 1)
rec_short = obs.volume_anomaly(store3.view(as_of3), "NEWLISTING", as_of3)
check("too little history returns None instead of a low-confidence guess", rec_short is None)

# ---------------------------------------------------------------------------
print("\n--- volume_anomaly: zero/invalid variance refuses ---")
# ---------------------------------------------------------------------------

store4 = fresh("volume_flat.db")
flat_vols = [100_000] * (n - 1) + [500_000]  # baseline has ZERO variance
seed_prices(store4, "FLATLINE", n, flat_vols, closes)
as_of4 = START + dt.timedelta(days=n - 1)
rec_flat = obs.volume_anomaly(store4.view(as_of4), "FLATLINE", as_of4)
check("a zero-variance baseline never fabricates a z-score", rec_flat is None)

# direct check on the shared helper too
z_direct = obs._zscore([100_000.0] * obs.MIN_OBSERVATIONS, 500_000.0)
check("_zscore itself refuses on ~zero std", z_direct is None)
z_direct_short = obs._zscore([1.0, 2.0, 3.0], 4.0)
check("_zscore itself refuses on too-short baseline", z_direct_short is None)


# ---------------------------------------------------------------------------
print("\n--- price_move_anomaly: genuine anomaly detected ---")
# ---------------------------------------------------------------------------

store5 = fresh("price.db")
pm_n = obs.WINDOW_DAYS + 2
pm_closes = [100.0 + i for i in range(pm_n - 1)]  # steady ~+1% daily drift
pm_closes.append(pm_closes[-1] * 0.80)  # a sharp -20% drop on the last session
seed_prices(store5, "DROP", pm_n, [100_000] * pm_n, pm_closes)
as_of5 = START + dt.timedelta(days=pm_n - 1)
rec_move = obs.price_move_anomaly(store5.view(as_of5), "DROP", as_of5)
check("a sharp price move is flagged", rec_move is not None)
if rec_move is not None:
    check("price move z_score clears threshold", abs(rec_move.z_score) >= obs.Z_THRESHOLD)
    check("flagged even though the move is negative (both directions matter)",
          rec_move.value < 0)

# ---------------------------------------------------------------------------
print("\n--- price_move_anomaly: normal observation is NOT flagged ---")
# ---------------------------------------------------------------------------

store6 = fresh("price_normal.db")
pm_closes_normal = [100.0 + i for i in range(pm_n)]  # steady drift, no shock
seed_prices(store6, "STEADY", pm_n, [100_000] * pm_n, pm_closes_normal)
as_of6 = START + dt.timedelta(days=pm_n - 1)
rec_move_normal = obs.price_move_anomaly(store6.view(as_of6), "STEADY", as_of6)
check("an ordinary day's return produces no anomaly record", rec_move_normal is None)

# ---------------------------------------------------------------------------
print("\n--- event_frequency_anomaly: genuine anomaly detected ---")
# ---------------------------------------------------------------------------

store7 = fresh("events.db")
ef_n = obs.WINDOW_DAYS + 1
seed_prices(store7, "NEWSY", ef_n, [100_000] * ef_n, [50.0] * ef_n)
ef_counts = [0 if i % 4 else 1 for i in range(ef_n - 1)] + [6]  # calm baseline, sudden burst
seed_announcements(store7, "NEWSY", ef_counts)
as_of7 = START + dt.timedelta(days=ef_n - 1)
rec_events = obs.event_frequency_anomaly(store7.view(as_of7), "NEWSY", as_of7)
check("a burst of announcements is flagged", rec_events is not None)
if rec_events is not None:
    check("event frequency z_score clears threshold", abs(rec_events.z_score) >= obs.Z_THRESHOLD)
    check("value carries the current day's announcement count", rec_events.value == 6.0)

# ---------------------------------------------------------------------------
print("\n--- event_frequency_anomaly: normal + insufficient history ---")
# ---------------------------------------------------------------------------

store8 = fresh("events_normal.db")
seed_prices(store8, "QUIET", ef_n, [100_000] * ef_n, [50.0] * ef_n)
ef_counts_normal = [0 if i % 4 else 1 for i in range(ef_n)]
seed_announcements(store8, "QUIET", ef_counts_normal)
as_of8 = START + dt.timedelta(days=ef_n - 1)
rec_events_normal = obs.event_frequency_anomaly(store8.view(as_of8), "QUIET", as_of8)
check("an ordinary announcement day produces no anomaly record", rec_events_normal is None)

store9 = fresh("events_short.db")
ef_short_n = obs.MIN_OBSERVATIONS
seed_prices(store9, "TOOYOUNG", ef_short_n, [100_000] * ef_short_n, [50.0] * ef_short_n)
seed_announcements(store9, "TOOYOUNG", [1] * ef_short_n)
as_of9 = START + dt.timedelta(days=ef_short_n - 1)
rec_events_short = obs.event_frequency_anomaly(store9.view(as_of9), "TOOYOUNG", as_of9)
check("too little trading-day history to anchor the window returns None",
      rec_events_short is None)


# ---------------------------------------------------------------------------
print("\n--- as_of / knowledge-time boundary: late-arriving data cannot leak ---")
# ---------------------------------------------------------------------------

store10 = fresh("boundary.db")
b_n = obs.WINDOW_DAYS + 1
b_vols = [100_000 + (i % 5) * 500 for i in range(b_n - 1)] + [900_000]
b_closes = [100.0 + 0.01 * i for i in range(b_n)]
# every row published same-day EXCEPT the final (spike) session, which the
# broker/vendor only reports 3 days late — a knowledge_time in the future
# relative to the spike's own session_date.
offsets = [0] * (b_n - 1) + [3]
seed_prices(store10, "DELAYED", b_n, b_vols, b_closes, knowledge_offset_days=offsets)

spike_session_date = START + dt.timedelta(days=b_n - 1)

# Querying as_of the spike's own session date: the row is not yet knowable.
early_view = store10.view(spike_session_date)
rec_early = obs.volume_anomaly(early_view, "DELAYED", spike_session_date)
check("as_of the spike day itself, the late-arriving spike is invisible",
      rec_early is None,
      "detector saw the spike before it was actually knowable")

# The most recent VISIBLE session at that as_of is one of the calm baseline
# days, not the spike — confirm the view itself agrees, independent of the
# detector's verdict above.
visible_sessions = early_view.prices("DELAYED", days=b_n)
check("the view's own last visible session is not the spike day",
      visible_sessions[-1]["session_date"] != spike_session_date.isoformat(),
      str(visible_sessions[-1]["session_date"]))

# Querying as_of 3 days later (after the vendor actually published it): now
# it is knowable, and — since it genuinely is a spike — gets flagged.
late_as_of = spike_session_date + dt.timedelta(days=3)
late_view = store10.view(late_as_of)
rec_late = obs.volume_anomaly(late_view, "DELAYED", late_as_of)
check("once knowledge_time has actually passed, the same spike IS flagged",
      rec_late is not None)


# ---------------------------------------------------------------------------
print("\n--- determinism / reproducibility ---")
# ---------------------------------------------------------------------------

store11 = fresh("determinism.db")
seed_prices(store11, "SPIKE", n, vols, closes)
seed_prices(store11, "CALM", n, vols_normal, closes)
view11 = store11.view(as_of)

run_a = obs.run(view11, store11, as_of, ["SPIKE", "CALM"], persist=False)
run_b = obs.run(view11, store11, as_of, ["SPIKE", "CALM"], persist=False)
check("two runs with persist=False produce an identical anomaly list",
      run_a == run_b, f"{run_a!r} != {run_b!r}")
check("the deterministic run actually found the SPIKE anomaly",
      len(run_a) == 1 and run_a[0].entity == "SPIKE", str(run_a))

# direct detector calls twice in a row must also agree
d1 = obs.volume_anomaly(view11, "SPIKE", as_of)
d2 = obs.volume_anomaly(view11, "SPIKE", as_of)
check("calling a detector twice with identical inputs gives an identical record",
      d1 == d2)

# ---------------------------------------------------------------------------
print("\n--- run(): persistence goes through research.memory, and is idempotent ---")
# ---------------------------------------------------------------------------

store12 = fresh("persist.db")
seed_prices(store12, "SPIKE", n, vols, closes)
view12 = store12.view(as_of)

found1 = obs.run(view12, store12, as_of, ["SPIKE"], persist=True)
check("run(persist=True) returns the same anomalies it files", len(found1) == 1)

logged = rm.query_research_log(store12, rm.DATASET_ANOMALY, as_of=as_of)
check("the anomaly is readable back via research.memory", len(logged) == 1)
check("the logged payload's metric matches the detector's metric",
      logged[0]["payload"]["metric"] == "volume_zscore")
check("the logged source names the detector", logged[0]["source"] == "observatory.volume_zscore")

found2 = obs.run(view12, store12, as_of, ["SPIKE"], persist=True)
logged_again = rm.query_research_log(store12, rm.DATASET_ANOMALY, as_of=as_of)
check("re-running run() against the same store/as_of does not duplicate the log entry",
      len(logged_again) == 1, f"got {len(logged_again)} rows")
check("but the detector's own return value is unaffected by prior persistence",
      len(found2) == 1)


# ---------------------------------------------------------------------------
print("\n--- Isolation: research/brain imports no LIVE-CAPITAL engine module ---")
# ---------------------------------------------------------------------------
# The authoritative rule (tests/test_kernel_isolation.py) is narrower than
# "no engine import at all": research/ may read plain, non-live-capital engine
# modules the same way research/replay.py already does (engine.market_data,
# engine.regime, engine.screener). What must never happen is an import of
# guardrails/execute/journal/broker* — the modules that hold or move real
# money. As of Slice C, research/brain/hypothesis_intake.py does exactly one
# such sanctioned read: engine.watchlist.UNIVERSE, a plain list of ticker
# strings, used only to validate a proposal's "universe" field.

import re  # noqa: E402

FORBIDDEN_ENGINE_MODULES = {"guardrails", "execute", "journal", "broker",
                             "broker_kite", "broker_indstocks"}

brain_dir = Path(__file__).parent.parent / "research" / "brain"
for py_file in sorted(brain_dir.glob("*.py")):
    src = py_file.read_text()
    imports = re.findall(r"^\s*(?:from|import)\s+([.\w]+)", src, re.MULTILINE)
    bad = [m for m in imports
           if m.split(".")[0] == "engine" and m.split(".")[-1] in FORBIDDEN_ENGINE_MODULES]
    check(f"{py_file.name} imports no live-capital engine module",
          not bad, str(imports))


for s in (store, store2, store3, store4, store5, store6, store7, store8, store9,
          store10, store11, store12):
    s.close()

print(f"\n{'=' * 52}")
print(f"  {PASSED} passed, {FAILED} failed")
print(f"{'=' * 52}\n")
sys.exit(1 if FAILED else 0)
