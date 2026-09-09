"""
Tests for Phase 1 Slice A: the experiment_results table and research/memory.py.

Nothing here touches engine/, contracts.py, replay.py, or Claude permissions —
this slice is schema + memory.py only, exactly as scoped.

Run with:  python -m tests.test_research_memory
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from research.store import Store  # noqa: E402
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


TMP = Path(tempfile.mkdtemp(prefix="lq-test-mem-"))


def fresh(name="t.db") -> Store:
    p = TMP / name
    if p.exists():
        p.unlink()
    return Store.open(p)


# ---------------------------------------------------------------------------
print("\n--- experiment_results: schema + append ---")
# ---------------------------------------------------------------------------

store = fresh()

rid1 = store.append_experiment_result(
    contract_id="EXP-TEST-001", trade_seq=1, entity="INFY",
    entry_time="2024-01-05", exit_time="2024-01-12",
    entry_price=1500.0, exit_price=1560.0, quantity=10,
    gross_pnl=600.0, costs=25.4, net_pnl=574.6, r_multiple=1.2,
    exit_reason="target",
)
check("first trade result inserts", rid1 is not None)

rid2 = store.append_experiment_result(
    contract_id="EXP-TEST-001", trade_seq=2, entity="TCS",
    entry_time="2024-02-01", exit_time="2024-02-03",
    entry_price=3800.0, exit_price=3720.0, quantity=5,
    gross_pnl=-400.0, costs=18.2, net_pnl=-418.2, r_multiple=-1.0,
    exit_reason="stop",
)
check("second trade result (different trade_seq) inserts", rid2 is not None)
check("row ids differ", rid1 != rid2)

# ---------------------------------------------------------------------------
print("\n--- experiment_results: idempotency ---")
# ---------------------------------------------------------------------------

rid_dup = store.append_experiment_result(
    contract_id="EXP-TEST-001", trade_seq=1, entity="INFY",
    entry_time="2024-01-05", exit_time="2024-01-12",
    entry_price=1500.0, exit_price=1560.0, quantity=10,
    gross_pnl=600.0, costs=25.4, net_pnl=574.6, r_multiple=1.2,
    exit_reason="target",
)
check("re-running the same (contract_id, trade_seq) is a no-op",
      rid_dup is None, f"got {rid_dup}, expected None")

results = store.experiment_results("EXP-TEST-001")
check("exactly 2 rows for the contract despite the duplicate attempt",
      len(results) == 2, f"got {len(results)}")
check("results come back ordered by trade_seq",
      [r["trade_seq"] for r in results] == [1, 2])
check("other-contract query returns nothing",
      store.experiment_results("EXP-DOES-NOT-EXIST") == [])

# A different contract can reuse trade_seq=1 — the unique key is the PAIR.
rid_other_contract = store.append_experiment_result(
    contract_id="EXP-TEST-002", trade_seq=1, entity="INFY",
    entry_time="2024-03-01", exit_time="2024-03-04",
    entry_price=1600.0, exit_price=1580.0, quantity=8,
    gross_pnl=-160.0, costs=15.0, net_pnl=-175.0, r_multiple=-0.5,
    exit_reason="stop",
)
check("trade_seq=1 for a DIFFERENT contract is not treated as a duplicate",
      rid_other_contract is not None)

# ---------------------------------------------------------------------------
print("\n--- experiment_results: append-only enforcement ---")
# ---------------------------------------------------------------------------

conn = store._unsafe_connection()
try:
    conn.execute("UPDATE experiment_results SET net_pnl = 0 WHERE contract_id = 'EXP-TEST-001'")
    check("UPDATE on experiment_results is blocked", False, "no exception raised")
except Exception as e:
    check("UPDATE on experiment_results is blocked", "append-only" in str(e).lower(), str(e))

try:
    conn.execute("DELETE FROM experiment_results WHERE contract_id = 'EXP-TEST-001'")
    check("DELETE on experiment_results is blocked", False, "no exception raised")
except Exception as e:
    check("DELETE on experiment_results is blocked", "append-only" in str(e).lower(), str(e))

check("data survived the blocked mutation attempts unchanged",
      len(store.experiment_results("EXP-TEST-001")) == 2)


# ---------------------------------------------------------------------------
print("\n--- research/memory.py: record_anomaly ---")
# ---------------------------------------------------------------------------

store2 = fresh("t2.db")

row = rm.record_anomaly(
    store2, entity="RELIANCE", metric="volume_zscore", value=3.4, baseline=1.0,
    z_score=3.4, as_of="2024-05-10", source="observatory.volume_spike",
)
check("record_anomaly appends", row is not None)

anomalies = rm.query_research_log(store2, rm.DATASET_ANOMALY, as_of="2024-05-11")
check("anomaly readable back via query_research_log", len(anomalies) == 1)
check("anomaly payload round-trips correctly",
      anomalies[0]["payload"]["metric"] == "volume_zscore"
      and anomalies[0]["payload"]["z_score"] == 3.4,
      str(anomalies[0]["payload"]))
check("anomaly entity stored correctly", anomalies[0]["entity"] == "RELIANCE")

anomalies_before = rm.query_research_log(store2, rm.DATASET_ANOMALY, as_of="2024-05-09")
check("as_of gating applies to research memory too (invisible before it happened)",
      len(anomalies_before) == 0)


# ---------------------------------------------------------------------------
print("\n--- research/memory.py: record_hypothesis_proposal ---")
# ---------------------------------------------------------------------------

hid1, hrow1 = rm.record_hypothesis_proposal(
    store2, claim="High-volume breakouts may have higher subsequent expectancy.",
    source="research_cycle.claude",
)
check("hypothesis id has the expected shape", hid1.startswith("HYP-") and len(hid1) == 4 + 8 + 1 + 8)
check("hypothesis proposal appends", hrow1 is not None)

hid2, hrow2 = rm.record_hypothesis_proposal(
    store2, claim="Unrelated second claim.", source="research_cycle.claude",
)
check("two proposals get different hypothesis ids", hid1 != hid2)

# Recording a second note against the SAME hypothesis id, per the
# EXP-001-A/B/C design (several experiments can trace back to one claim).
_, hrow3 = rm.record_hypothesis_proposal(
    store2, claim="High-volume breakouts may have higher subsequent expectancy — v2 refinement.",
    source="research_cycle.claude", hypothesis_id=hid1,
)
proposals = rm.query_research_log(store2, rm.DATASET_HYPOTHESIS)
same_hid = [p for p in proposals if p["payload"]["hypothesis_id"] == hid1]
check("a hypothesis id can be reused across multiple proposal rows",
      len(same_hid) == 2, f"got {len(same_hid)}")


# ---------------------------------------------------------------------------
print("\n--- research/memory.py: record_experiment_verdict ---")
# ---------------------------------------------------------------------------

vrow = rm.record_experiment_verdict(
    store2, contract_id="EXP-TEST-001", hypothesis_id=hid1,
    verdict={"expectancy_r": 0.18, "win_rate": 0.55, "n_trades": 42, "t_stat": 2.1},
)
check("verdict appends", vrow is not None)

verdicts = rm.query_research_log(store2, rm.DATASET_VERDICT)
check("verdict readable back", len(verdicts) == 1)
check("verdict links contract_id and hypothesis_id together",
      verdicts[0]["payload"]["contract_id"] == "EXP-TEST-001"
      and verdicts[0]["payload"]["hypothesis_id"] == hid1)
check("verdict statistics pass through untouched",
      verdicts[0]["payload"]["t_stat"] == 2.1)


# ---------------------------------------------------------------------------
print("\n--- research/memory.py: record_research_note ---")
# ---------------------------------------------------------------------------

nrow = rm.record_research_note(
    store2, note="Volume anomaly in RELIANCE looked promising but news-driven, not structural.",
    source="research_cycle.claude",
)
check("note appends", nrow is not None)
notes = rm.query_research_log(store2, rm.DATASET_NOTE)
check("note readable back", len(notes) == 1 and "RELIANCE" in notes[0]["payload"]["note"])


# ---------------------------------------------------------------------------
print("\n--- Isolation: memory.py imports nothing from engine/ ---")
# ---------------------------------------------------------------------------

import re  # noqa: E402
src = (Path(__file__).parent.parent / "research" / "memory.py").read_text()
imports = re.findall(r"^\s*(?:from|import)\s+([.\w]+)", src, re.MULTILINE)
check("research/memory.py imports no engine module",
      not any(m.startswith("engine") for m in imports), str(imports))


store.close()
store2.close()

print(f"\n{'=' * 52}")
print(f"  {PASSED} passed, {FAILED} failed")
print(f"{'=' * 52}\n")
sys.exit(1 if FAILED else 0)
