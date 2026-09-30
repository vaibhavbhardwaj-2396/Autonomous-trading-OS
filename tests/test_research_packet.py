"""ResearchPacket significance, compactness, persistence and lineage tests."""
from __future__ import annotations
import sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from research import memory as rm
from research.brain.packet import build_packet, persist_packet
from research.store import Store

PASSED = FAILED = 0
def check(name, condition, detail=""):
    global PASSED, FAILED
    if condition: PASSED += 1; print(f"  ✓ {name}")
    else: FAILED += 1; print(f"  ✗ {name}\n      {detail}")

base = {"as_of":"2026-09-29T10:00:00+05:30",
        "provenance":{"data_firewall":"knowledge_time <= as_of"},
        "anomalies":{"shown":[],"truncated":False},
        "evidence":{"hypotheses":[],"truncated":False},
        "exact_duplicates":{"truncated":False}}
quiet = build_packet(base)
check("quiet arrival is filtered before AI", quiet["significance"]["admitted"] is False)
anomalous = dict(base)
anomalous["anomalies"] = {"truncated":False,"shown":[{"anomaly_id":17,"entity":"INFY",
    "metric":"volume_zscore","value":3.4,"baseline":1.0,"z_score":3.1,"as_of":base["as_of"]}]}
packet = build_packet(anomalous)
check("significant deterministic anomaly is admitted", packet["significance"]["admitted"] is True)
check("packet carries stable ID, source lineage and knowledge time",
      packet["packet_id"].startswith("RP-") and packet["source_artifacts"] == ["detection:17"] and packet["knowledge_timestamp"] == base["as_of"])
check("packet excludes giant registry/raw logs", "contract_registry" not in packet and "raw" not in packet)
store = Store.open(Path(tempfile.mkdtemp()) / "memory.db")
try:
    first, second = persist_packet(store, packet), persist_packet(store, packet)
    rows = rm.query_research_log(store, rm.DATASET_RESEARCH_PACKET, limit=None)
    check("packet persistence is append-only and idempotent", first is not None and second is None and len(rows) == 1)
finally: store.close()
print(f"\n{PASSED} passed, {FAILED} failed")
if FAILED: raise SystemExit(1)
