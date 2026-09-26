"""Scientific equivalence and safety coverage for bulk experiment replay."""

import datetime as dt
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from research.contracts import Contract
from research.experiments import bulk_replay, runner
from research.store import IST, Store

PASSED = FAILED = 0


def check(name, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1; print(f"  ✓ {name}")
    else:
        FAILED += 1; print(f"  ✗ {name}: {detail}")


def contract(cid, universe, start, end, conditions):
    return Contract(id=cid, title=cid, hypothesis="deterministic fixture",
        null_hypothesis="no edge", universe=universe, signal="fixture",
        entry_rule=json.dumps({"conditions": conditions}),
        exit_rule=json.dumps({"stop_loss_pct": 4, "target_pct": 6, "max_hold_days": 4}),
        splits={"discovery": [start, end]}, independence="daily",
        falsification="non-positive", abandon_condition="invalid data",
        evaluation_start=start, evaluation_end=end)


tmp = Path(tempfile.mkdtemp(prefix="lq-bulk-replay-"))
store = Store.open(tmp / "research.db")
try:
    start = dt.date(2024, 1, 1)
    for symbol, offset in (("RELIANCE", 0.0), ("TCS", 7.0)):
        for i in range(90):
            day = start + dt.timedelta(days=i)
            close = 100 + offset + (i % 13) * 0.7 + (8 if i in (35, 61) else 0)
            store.append_price(symbol, day,
                dt.datetime.combine(day, dt.time(18), tzinfo=IST), "fixture",
                open_=close - 0.3, high=close * 1.07, low=close * 0.95,
                close=close, volume=1000 + i * 17 + (9000 if i in (35, 61) else 0))

    c = contract("BULK-WATCH", "watchlist", "2024-01-25", "2024-03-25", [
        {"metric": "volume_zscore", "op": ">", "value": 1.5, "window_days": 20},
        {"metric": "close", "op": ">", "value": 50},
        {"metric": "return_1d", "op": ">", "value": 0.01},
    ])
    legacy = runner._simulate_legacy(c, store)
    bulk = bulk_replay.simulate(c, store)
    check("bulk replay produces byte-equivalent trade records", bulk.trades == legacy,
          f"legacy={legacy} bulk={bulk.trades}")
    check("bulk replay collapses the database hot path to bounded queries",
          bulk.profile["db_query_count"] <= 5, str(bulk.profile))
    check("bulk profile records rows, symbols, memory and stages",
          bulk.profile["rows_processed"] > 0 and bulk.profile["symbols_processed"] >= 2
          and bulk.profile["peak_rss_bytes"] > 0
          and bulk.profile["stages"]["bulk_price_retrieval"] >= 0, str(bulk.profile))

    # Point-in-time membership changes: BBB is known but only becomes effective
    # halfway through the evaluation window. Both engines must admit it then.
    for symbol in ("RELIANCE", "TCS"):
        effective = "2024-01-01" if symbol == "RELIANCE" else "2024-02-15"
        store.append("index_membership", symbol, effective, "2023-12-20", "fixture",
                     {"index_name": "Nifty 50", "symbol": symbol,
                      "valid_from": effective, "valid_to": None})
    c2 = contract("BULK-PIT", "Nifty 50", "2024-01-25", "2024-03-25", [
        {"metric": "price_move_zscore", "op": ">", "value": 1.0, "window_days": 20},
    ])
    legacy2 = runner._simulate_legacy(c2, store)
    bulk2 = bulk_replay.simulate(c2, store).trades
    check("bulk replay preserves point-in-time membership and price signals",
          bulk2 == legacy2, f"legacy={legacy2} bulk={bulk2}")

    delayed = Store.open(tmp / "delayed.db")
    try:
        for i in range(15):
            day = start + dt.timedelta(days=i)
            known = day + dt.timedelta(days=1 if i == 7 else 0)
            delayed.append_price("RELIANCE", day,
                dt.datetime.combine(known, dt.time(18), tzinfo=IST), "fixture",
                close=100 + i, high=101 + i, low=99 + i, volume=1000 + i)
        c3 = contract("BULK-DELAY", "watchlist", "2024-01-01", "2024-01-15", [
            {"metric": "close", "op": ">", "value": 100}])
        trades3, profile3 = runner.simulate_profiled(c3, delayed)
        check("late-arriving data fails closed to exact legacy replay",
              profile3["engine"] == "legacy_point_in_time" and
              "late-arriving" in profile3["fallback_reason"], str(profile3))
        check("fallback result remains the exact legacy result",
              trades3 == runner._simulate_legacy(c3, delayed))
    finally:
        delayed.close()
finally:
    store.close(); shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{PASSED} passed, {FAILED} failed")
sys.exit(1 if FAILED else 0)
