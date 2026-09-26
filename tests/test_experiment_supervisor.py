"""Hard-deadline, cleanup, and restart reconciliation for experiments."""

from __future__ import annotations

import datetime as dt
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from research.store import Store
from research.brain import hypothesis_intake as hi
from research.contracts import Contract
from research.experiments import supervisor

PASSED = FAILED = 0
def check(name, condition, detail=""):
    global PASSED, FAILED
    if condition: PASSED += 1; print(f"  ✓ {name}")
    else: FAILED += 1; print(f"  ✗ {name}\n      {detail}")

tmp = Path(tempfile.mkdtemp(prefix="lq-supervisor-"))
db = tmp / "research.db"; reg = tmp / "registry"; log = tmp / "executions.jsonl"
store = Store.open(db)
start = dt.date(2020, 1, 1)
for i in range(45):
    day = start + dt.timedelta(days=i)
    store.append_price(symbol="RELIANCE", session_date=day, knowledge_time=day,
                       source="test", open_=100, high=101, low=99, close=100,
                       volume=5_000_000 if i == 30 else 100_000 + i)

proposal = {
    "title":"supervisor", "hypothesis":"volume spike continuation", "null_hypothesis":"none",
    "universe":"watchlist", "signal":"observatory.volume_zscore",
    "entry_rule":{"conditions":[{"metric":"volume_zscore","op":">","value":3.0}]},
    "exit_rule":{"stop_loss_pct":5.0,"target_pct":8.0,"max_hold_days":5},
    "splits":{"discovery":["2020-01-01","2020-02-14"]}, "independence":"one entry",
    "falsification":"t<2", "abandon_condition":"expectancy<=0",
    "evaluation_start":"2020-01-01", "evaluation_end":"2020-02-14",
}

draft = hi.create_draft(store, proposal, registry_dir=reg)
locked = hi.approve_and_lock(store, draft.contract.id, approved_by="test", registry_dir=reg)
result = supervisor.run_supervised(locked.id, store, registry_dir=reg,
                                   deadline_seconds=30, execution_log=log)
check("normal supervised experiment completes", result["status"] == "reported", result)
check("completed run records stage timings and evidence",
      "stage_timings_seconds" in result and result.get("evidence"), result)

draft2 = hi.create_draft(store, {**proposal, "title":"timeout", "evaluation_end":"2020-02-13"},
                         registry_dir=reg)
locked2 = hi.approve_and_lock(store, draft2.contract.id, approved_by="test", registry_dir=reg)
timed = supervisor.run_supervised(locked2.id, store, registry_dir=reg,
                                  deadline_seconds=0.001, execution_log=log)
check("deadline returns an explicit TIMEOUT outcome",
      timed["status"] == "abandoned" and timed["failure_reason"] == "TIMEOUT", timed)
check("timed out contract is ABANDONED, never scientific evidence",
      Contract.load(locked2.id, reg).status == "abandoned")
rows = [json.loads(x) for x in log.read_text().splitlines()]
states = [r["state"] for r in rows if r["contract_id"] == locked2.id]
check("timeout lifecycle records QUEUED/RUNNING/CANCEL_REQUESTED/ABANDONED",
      states == ["QUEUED","RUNNING","CANCEL_REQUESTED","ABANDONED"], states)

draft3 = hi.create_draft(store, {**proposal, "title":"stale", "evaluation_end":"2020-02-12"},
                         registry_dir=reg)
locked3 = hi.approve_and_lock(store, draft3.contract.id, approved_by="test", registry_dir=reg)
locked3.status = "running"; locked3.save(reg)
recovered = supervisor.recover_stale_running(store, registry_dir=reg, execution_log=log)
check("restart recovery abandons stale RUNNING work", locked3.id in recovered)
check("restart recovery preserves a WORKER_CRASH reason",
      any(r.get("contract_id") == locked3.id and r.get("failure_reason") == "WORKER_CRASH"
          for r in [json.loads(x) for x in log.read_text().splitlines()]))

store.close(); shutil.rmtree(tmp)
print(f"\n{PASSED} passed, {FAILED} failed")
raise SystemExit(1 if FAILED else 0)
