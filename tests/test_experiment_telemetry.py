"""Read-only experiment telemetry aggregation."""

import datetime as dt
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from research import memory as rm
from research.contracts import Contract
from research.experiments import telemetry
from research.store import IST, Store

PASSED = FAILED = 0
def check(name, condition, detail=""):
    global PASSED, FAILED
    if condition: PASSED += 1; print(f"  ✓ {name}")
    else: FAILED += 1; print(f"  ✗ {name}: {detail}")

tmp = Path(tempfile.mkdtemp(prefix="lq-experiment-telemetry-"))
store = Store.open(tmp / "research.db"); reg = tmp / "registry"; log = tmp / "runs.jsonl"
now = dt.datetime.now(IST).replace(microsecond=0)
contract = Contract(id="EXP-TEL", title="t", hypothesis="h", null_hypothesis="n",
    universe="watchlist", signal="s",
    entry_rule=json.dumps({"conditions":[{"metric":"close","op":">","value":1}]}),
    exit_rule=json.dumps({"max_hold_days":1}), splits={"discovery":["2024-01-01","2024-01-02"]},
    independence="i", falsification="f", abandon_condition="a",
    evaluation_start="2024-01-01", evaluation_end="2024-01-02")
contract.save(reg)
rm.record_research_note(store, note="done", source="research.experiments.runner",
    extra={"contract_id":"EXP-TEL", "execution_profile":{"engine":"bulk_price_features_v1",
           "wall_seconds":2.5, "rows_processed":100, "stages":{"bulk_price_retrieval":1.0}}})
log.write_text("\n".join(json.dumps(x) for x in [
    {"timestamp":(now-dt.timedelta(hours=2)).isoformat(),"contract_id":"OLD",
     "state":"ABANDONED","failure_reason":"TIMEOUT","pid":None},
    {"timestamp":(now-dt.timedelta(minutes=1)).isoformat(),"contract_id":"EXP-TEL",
     "state":"COMPLETED","failure_reason":None,"pid":123},
]))
original = telemetry.components.get
telemetry.components.get = lambda name: {"desired_state":"RUNNING","reason":"verified"}
try:
    status = telemetry.get_status(store, execution_log=log, registry_dir=reg)
finally:
    telemetry.components.get = original
check("telemetry reports desired state and queue", status["state"] == "RUNNING" and status["queue_depth"] == 1, status)
check("telemetry exposes last completion and timeout count", status["last_completion"]["contract_id"] == "EXP-TEL" and status["timeouts_24h"] == 1, status)
check("telemetry exposes persisted runtime profile", status["latest_profile"]["engine"] == "bulk_price_features_v1" and status["median_runtime_seconds"] == 2.5, status)
check("terminal completion is not reported as a live PID", status["process_state"] == "IDLE" and status["current_pid"] is None, status)
store.close(); shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{PASSED} passed, {FAILED} failed")
raise SystemExit(1 if FAILED else 0)
