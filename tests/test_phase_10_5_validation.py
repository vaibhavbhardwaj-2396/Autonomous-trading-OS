"""Phase 10.5 capacity, funnel, news and adapter acceptance checks."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from control import capacity
from engine.broker import BrokerCapabilities
from engine.broker_indstocks import INDstocksBroker
from engine.broker_kite import KiteBroker
from research import memory as rm
from research import validation
from research.sources.news import quantitative_features
from research.store import Store, now_ist

PASSED = FAILED = 0


def check(name, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  ✓ {name}")
    else:
        FAILED += 1
        print(f"  ✗ {name}\n      {detail}")


def resource(legacy="HEALTHY", load=0.05, mem_available=.8):
    return {"state": legacy, "measured_at": "2026-09-19T12:00:00+05:30",
            "reasons": [], "unmeasured": [], "snapshot": {
                "cpu_count": 4, "load_ratio": load,
                "mem_available_ratio": mem_available, "disk_free_ratio": .8}}


print("\n--- capacity planner ---")
check("healthy idle measurements normalize to IDLE",
      capacity.normalize(resource()) == "IDLE")
check("healthy moderate measurements normalize to NORMAL",
      capacity.normalize(resource(load=.7, mem_available=.5)) == "NORMAL")
check("legacy pressure normalizes to HIGH_LOAD",
      capacity.normalize(resource("PRESSURED")) == "HIGH_LOAD")
critical = capacity.plan(resource("CRITICAL"))
alloc = {a["work_class"]: a for a in critical["allocations"]}
check("CRITICAL reserves only the safety/telemetry lane",
      alloc["critical"]["enabled"] and
      not any(alloc[k]["enabled"] for k in ("continuous", "heavy", "background")))
try:
    capacity.permits("made-up", resource())
    unknown_failed = False
except ValueError:
    unknown_failed = True
check("unknown work classes fail explicitly", unknown_failed)

print("\n--- durable validation funnel ---")
tmp = Path(tempfile.mkdtemp(prefix="lq-phase-10-5-"))
old_elig = os.environ.get("PAPER_ELIGIBILITY_DIR")
old_paper = os.environ.get("PAPER_DB_PATH")
os.environ["PAPER_ELIGIBILITY_DIR"] = str(tmp / "eligibility")
os.environ["PAPER_DB_PATH"] = str(tmp / "paper.db")
try:
    with Store.open(tmp / "memory.db") as store:
        now = now_ist()
        store.append(dataset=rm.DATASET_ANOMALY, entity="_market", event_time=now,
                     knowledge_time=now, source="test", payload={"metric": "x"})
        rm.record_hypothesis_proposal(
            store, claim="test claim", source="test", extra={"research_area": "test"})
        snapshot = validation.build_snapshot(
            store, resource_state=resource(), registry_dir=tmp / "strategies",
            contract_dir=tmp / "contracts")
    check("snapshot counts append-only detections and hypotheses",
          snapshot["funnel"]["detections"] == 1 and snapshot["funnel"]["hypotheses"] == 1,
          snapshot["funnel"])
    check("paper stays blocked without explicit eligibility",
          not snapshot["paper_readiness"]["ready"] and
          any("paper-eligibility" in b for b in snapshot["paper_readiness"]["blockers"]),
          snapshot["paper_readiness"])
    check("Phase 11 is mechanically reported locked",
          snapshot["live_promotion"]["state"] == "LOCKED")
    ledger = tmp / "ledger.jsonl"
    validation.append_snapshot(snapshot, path=ledger, lock_path=tmp / "ledger.lock")
    validation.append_snapshot(snapshot, path=ledger, lock_path=tmp / "ledger.lock")
    check("campaign ledger is append-only and readable",
          len(validation.read_history(path=ledger)) == 2)
    check("every declared funnel stage is present",
          set(validation.FUNNEL_STAGES) <= set(snapshot["funnel"]))
finally:
    if old_elig is None:
        os.environ.pop("PAPER_ELIGIBILITY_DIR", None)
    else:
        os.environ["PAPER_ELIGIBILITY_DIR"] = old_elig
    if old_paper is None:
        os.environ.pop("PAPER_DB_PATH", None)
    else:
        os.environ["PAPER_DB_PATH"] = old_paper

print("\n--- news and provider surfaces ---")
features = quantitative_features("Company wins approval after profit growth")
check("news features are deterministic and versioned",
      features["schema_version"] == 1 and features["method"] == "deterministic_lexicon_v1")
check("news feature polarity is quantitative", features["polarity_lexical"] > 0)
ind = INDstocksBroker().capabilities()
kite = KiteBroker().capabilities()
check("broker capabilities are typed and expose daily auth",
      isinstance(ind, BrokerCapabilities) and ind.requires_daily_auth and kite.requires_daily_auth)
check("unsupported INDstocks history reads are explicit",
      not ind.orders_read and not ind.trades_read)
check("Kite advertises its supported history reads", kite.orders_read and kite.trades_read)

print(f"\n{PASSED} passed, {FAILED} failed")
raise SystemExit(1 if FAILED else 0)
