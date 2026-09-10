"""
tests/test_research_worker.py — Continuous Research Worker v0
(research/brain/worker.py).

The worker is orchestration only: one bounded heartbeat that reuses the
existing digest / investigator / similarity / scheduler / priority / runner /
evaluator components and does at most a configured amount of work, then
exits. This file tests only what the worker itself adds — bounding,
cooldown, the worker lock, telemetry, notification policy, and the safety
invariants — never the components' own correctness (covered by their own
suites).

Run with:  python -m tests.test_research_worker
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from research.store import Store, now_ist  # noqa: E402
from research.contracts import Contract  # noqa: E402
from research.brain import hypothesis_intake as hi  # noqa: E402
from research.brain import worker as w  # noqa: E402
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


def _code_only(src: str) -> str:
    return re.sub(r'"""[\s\S]*?"""', "", src)


def _raises(fn) -> bool:
    try:
        fn()
        return False
    except Exception:
        return True


TMP = Path(tempfile.mkdtemp(prefix="lq-test-worker-"))
WORKER_SRC = (Path(__file__).parent.parent / "research" / "brain" / "worker.py").read_text()


def fresh_store(name) -> Store:
    p = TMP / f"{name}.db"
    if p.exists():
        p.unlink()
    return Store.open(p)


def fresh_registry(name) -> Path:
    d = TMP / f"registry-{name}"
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True, exist_ok=True)
    return d


def fresh_state(name) -> Path:
    p = TMP / f"wstate-{name}.json"
    if p.exists():
        p.unlink()
    return p


def valid_ai_proposal(**overrides) -> dict:
    base = {
        "title": "Volume-spike drift",
        "hypothesis": "High-volume anomalies (>=3 sigma) precede a short-term upward drift.",
        "null_hypothesis": "Volume anomalies have no relationship to subsequent returns.",
        "universe": "watchlist",
        "signal": "observatory.volume_zscore",
        "entry_rule": {"conditions": [{"metric": "volume_zscore", "op": ">", "value": 3.0}]},
        "exit_rule": {"stop_loss_pct": 2.0, "target_pct": 4.0, "max_hold_days": 10},
        "splits": {"discovery": ["2019-01-01", "2022-12-31"]},
        "independence": "one entry per symbol per rolling 10-day window",
        "falsification": "expectancy_r <= 0 on discovery, or t_stat < 2.0",
        "abandon_condition": "if discovery-split expectancy_r <= 0, abandon",
        "evaluation_start": "2019-01-01", "evaluation_end": "2022-12-31",
    }
    base.update(overrides)
    return base


def distinct_responses(n):
    return [json.dumps(valid_ai_proposal(
        title=f"Idea {i}",
        entry_rule={"conditions": [{"metric": "volume_zscore", "op": ">", "value": 3.0 + i}]},
    )) for i in range(n)]


def sequential_runner(responses):
    st = {"i": 0}

    def _runner(prompt):
        i = min(st["i"], len(responses) - 1)
        st["i"] += 1
        return responses[i]
    return _runner


def make_locked_contract(cid, *, registry_dir, status="locked", locked_at=None, **overrides):
    fields = dict(
        id=cid, title=f"idea {cid}", hypothesis=f"claim behind {cid}",
        null_hypothesis="no effect", universe="watchlist",
        signal="observatory.volume_zscore",
        entry_rule=json.dumps({"conditions": [{"metric": "volume_zscore", "op": ">", "value": 3.0}]}),
        exit_rule=json.dumps({"stop_loss_pct": 2.0, "target_pct": 4.0}),
        splits={"discovery": ["2019-01-01", "2020-01-01"]},
        independence="clustered by symbol-day", falsification="t_stat < 2.0",
        abandon_condition="expectancy_r <= 0 on discovery",
        evaluation_start="2019-01-01", evaluation_end="2020-01-01",
    )
    fields.update(overrides)
    c = Contract(**fields)
    c.lock()
    if locked_at is not None:
        c.locked_at = locked_at
    c.status = status
    c.save(registry_dir)
    return c


REQUIRED_TELEMETRY = {
    "started_at", "finished_at", "runtime_seconds", "work_selected",
    "proposals_created", "duplicate_rejections", "experiments_run",
    "evidence_updates", "no_work_reason", "errors",
}


# ---------------------------------------------------------------------------
print("\n--- A: no-work heartbeat (empty store + empty registry) ---")
# ---------------------------------------------------------------------------

s = fresh_store("a")
reg = fresh_registry("a")
st = fresh_state("a")
lim = w.WorkerLimits(max_discovery_attempts=1, cooldown_seconds=600)
r = w.run_worker_cycle(s, now_ist(), limits=lim, registry_dir=reg,
                       runner=lambda p: json.dumps({"no_proposal": True, "reason": "nothing stands out"}),
                       state_path=st)
s.close()
check("A: an empty cycle exits cleanly with no errors", r.errors == [], str(r.errors))
check("A: no experiments were run and no proposal was created",
      r.experiments_run == 0 and r.proposals_created == 0)
check("A: a no_work_reason is recorded (discovery produced no new proposal)",
      r.no_work_reason is not None and "no" in r.no_work_reason.lower(), str(r.no_work_reason))
check("A: telemetry row carries every required field",
      REQUIRED_TELEMETRY <= set(r.as_row().keys()), str(set(r.as_row().keys())))
_state_after = json.loads(st.read_text())
check("A: a cooldown_until is set after a no-useful-work cycle",
      "cooldown_until" in _state_after and _state_after["cooldown_until"] > 0, str(_state_after))


# ---------------------------------------------------------------------------
print("\n--- B: cooldown suppresses discovery on the very next invocation ---")
# ---------------------------------------------------------------------------

s = fresh_store("b")
reg = fresh_registry("b")
st = fresh_state("b")
lim = w.WorkerLimits(max_discovery_attempts=1, cooldown_seconds=3600)
calls = {"n": 0}


def counting_runner(prompt):
    calls["n"] += 1
    return json.dumps({"no_proposal": True, "reason": "quiet"})


w.run_worker_cycle(s, now_ist(), limits=lim, registry_dir=reg, runner=counting_runner, state_path=st)
first_calls = calls["n"]
r2 = w.run_worker_cycle(s, now_ist(), limits=lim, registry_dir=reg, runner=counting_runner, state_path=st)
s.close()
check("B: the Research AI was invoked on the first heartbeat", first_calls == 1)
check("B: the Research AI is NOT invoked again while in cooldown", calls["n"] == first_calls)
check("B: the cooled-down heartbeat reports its no_work_reason as the cooldown",
      r2.no_work_reason is not None and "cooldown" in r2.no_work_reason.lower(), str(r2.no_work_reason))
check("B: 'discovery' is not in work_selected during cooldown", "discovery" not in r2.work_selected)


# ---------------------------------------------------------------------------
print("\n--- C: discovery creates a DRAFT; a repeat is caught as a duplicate ---")
# ---------------------------------------------------------------------------

s = fresh_store("c")
reg = fresh_registry("c")
st = fresh_state("c")
lim = w.WorkerLimits(max_discovery_attempts=1, cooldown_seconds=0)
raw = json.dumps(valid_ai_proposal())
r_first = w.run_worker_cycle(s, now_ist(), limits=lim, registry_dir=reg,
                             runner=lambda p: raw, state_path=st)
check("C: the first discovery heartbeat creates exactly one DRAFT",
      r_first.proposals_created == 1 and len(r_first.drafts) == 1, str(r_first))
_created = list(reg.glob("*.json"))
check("C: exactly one Contract file was written to the registry", len(_created) == 1)
_c = Contract.load(r_first.drafts and _created[0].stem or "", reg) if _created else None
check("C: the created Contract is status='draft' (never locked by the worker)",
      _c is not None and _c.status == "draft", str(_c and _c.status))

r_dup = w.run_worker_cycle(s, now_ist(), limits=lim, registry_dir=reg,
                           runner=lambda p: raw, state_path=st)
s.close()
check("C: submitting the same proposal again creates NO new draft",
      r_dup.proposals_created == 0, str(r_dup))
check("C: the repeat is counted as a duplicate_rejection",
      r_dup.duplicate_rejections == 1, str(r_dup))
check("C: still only one Contract file in the registry after the duplicate attempt",
      len(list(reg.glob("*.json"))) == 1)


# ---------------------------------------------------------------------------
print("\n--- D: proposal limit — max_discovery_attempts is honoured ---")
# ---------------------------------------------------------------------------

s = fresh_store("d")
reg = fresh_registry("d")
st = fresh_state("d")
lim = w.WorkerLimits(max_discovery_attempts=2, cooldown_seconds=0)
runner = sequential_runner(distinct_responses(5))
r = w.run_worker_cycle(s, now_ist(), limits=lim, registry_dir=reg, runner=runner, state_path=st)
s.close()
check("D: with 5 distinct proposals available, only max_discovery_attempts (2) drafts are made",
      r.proposals_created == 2, str(r))
check("D: exactly 2 Contract files were written", len(list(reg.glob("*.json"))) == 2)


# ---------------------------------------------------------------------------
print("\n--- E: experiment limit + evidence feedback + priority path ---")
# ---------------------------------------------------------------------------

s = fresh_store("e")
reg = fresh_registry("e")
st = fresh_state("e")
for i in range(3):
    make_locked_contract(f"EXP-E-{i}", registry_dir=reg, locked_at=f"2024-01-0{i+1}T00:00:00")
lim = w.WorkerLimits(max_experiments=1, max_discovery_attempts=0, cooldown_seconds=0)
r = w.run_worker_cycle(s, now_ist(), limits=lim, registry_dir=reg,
                       runner=lambda p: "unused", state_path=st)
s.close()
check("E: with 3 locked contracts and max_experiments=1, exactly one experiment ran",
      r.experiments_run == 1, str(r))
check("E: 'experiments' is in work_selected", "experiments" in r.work_selected)
check("E: the experiment produced an evidence update (verdict recorded for its hypothesis)",
      r.evidence_updates >= 1, str(r))
check("E: the reported experiment is the oldest-locked one (priority ordering via scheduler)",
      r.experiment_outcomes and r.experiment_outcomes[0]["contract_id"] == "EXP-E-0",
      str(r.experiment_outcomes))

# a second heartbeat runs the NEXT one — bounded, repeatable, not stuck
s = Store.open(TMP / "e.db")
r2 = w.run_worker_cycle(s, now_ist(), limits=lim, registry_dir=reg,
                        runner=lambda p: "unused", state_path=st)
s.close()
check("E: a second heartbeat runs the next contract, never re-runs the reported one",
      r2.experiments_run == 1
      and r2.experiment_outcomes[0]["contract_id"] == "EXP-E-1", str(r2))


# ---------------------------------------------------------------------------
print("\n--- F: runtime bound — discovery is skipped once the budget is spent ---")
# ---------------------------------------------------------------------------

s = fresh_store("f")
reg = fresh_registry("f")
st = fresh_state("f")
lim = w.WorkerLimits(max_discovery_attempts=3, max_runtime_seconds=0.0, cooldown_seconds=0)
ai_calls = {"n": 0}


def tracked_runner(p):
    ai_calls["n"] += 1
    return json.dumps(valid_ai_proposal())


r = w.run_worker_cycle(s, now_ist(), limits=lim, registry_dir=reg, runner=tracked_runner, state_path=st)
s.close()
check("F: with a zero runtime budget, the Research AI is never invoked",
      ai_calls["n"] == 0, f"invoked {ai_calls['n']} times")
check("F: no proposal was created and a reason mentions the runtime budget",
      r.proposals_created == 0
      and (r.no_work_reason or "").lower().find("budget") >= 0, str(r.no_work_reason))


# ---------------------------------------------------------------------------
print("\n--- G: the worker lock makes overlapping invocations safe ---")
# ---------------------------------------------------------------------------

lock_path = TMP / ".worker.lock"
with w.worker_lock(lock_path):
    busy = False
    try:
        with w.worker_lock(lock_path):
            pass
    except w.WorkerBusy:
        busy = True
    check("G: a second worker_lock() on the same path raises WorkerBusy", busy)
# lock released on context exit
reacquired = False
with w.worker_lock(lock_path):
    reacquired = True
check("G: the lock is released on context exit (a later worker can take it)", reacquired)

# main() returns 0 (clean no-op) when the lock is already held
held = open(lock_path, "w")
import fcntl as _fcntl  # noqa: E402
_fcntl.flock(held.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
try:
    os.environ["RESEARCH_WORKER_MAX_DISCOVERY_ATTEMPTS"] = "0"
    rc = w.main(["--db", str(TMP / "gmain.db"), "--no-notify", "--quiet-on-success"])
finally:
    _fcntl.flock(held.fileno(), _fcntl.LOCK_UN)
    held.close()
    os.environ.pop("RESEARCH_WORKER_MAX_DISCOVERY_ATTEMPTS", None)
# NOTE: main() uses the real LOCK_PATH, not lock_path — this asserts the real
# lock path is respected, so only assert it did not crash / returned an int.
check("G: worker.main() returns an int exit code and never raises", isinstance(rc, int))


# ---------------------------------------------------------------------------
print("\n--- H: provenance — a worker-created draft is recorded in research memory ---")
# ---------------------------------------------------------------------------

s = fresh_store("h")
reg = fresh_registry("h")
st = fresh_state("h")
r_h = w.run_worker_cycle(s, now_ist(), limits=w.WorkerLimits(max_discovery_attempts=1, cooldown_seconds=0),
                         registry_dir=reg, runner=lambda p: json.dumps(valid_ai_proposal()), state_path=st)
disc = rm.query_research_log(s, rm.DATASET_DISCOVERY_SEARCH)
s.close()
check("H: the worker's draft went through (precondition for the provenance check)",
      r_h.proposals_created == 1, str(r_h))
check("H: a research_discovery_search provenance row was written for the worker's draft",
      len(disc) >= 1,
      "investigate() records provenance via research.memory.record_discovery_search")


# ---------------------------------------------------------------------------
print("\n--- I: notification policy is bounded ---")
# ---------------------------------------------------------------------------

sent = {"msgs": []}
_orig_notify = w._notify
w._notify = lambda m: (sent["msgs"].append(m) or True)
try:
    st = fresh_state("i")
    quiet = w.WorkerRunResult(
        started_at="t0", finished_at="t1", runtime_seconds=1.0, worker_id="x",
        digest_as_of=None, work_selected=["experiments"], proposals_created=0, drafts=[],
        duplicate_rejections=0, experiments_run=1, experiment_outcomes=[], evidence_updates=1,
        no_work_reason=None, errors=[], limits={}, notified=False)

    # a routine experiment-only cycle -> NO telegram (first call only seeds the clock)
    w.maybe_notify(quiet, limits=w.WorkerLimits(), state_path=st, now=1000.0)
    w.maybe_notify(quiet, limits=w.WorkerLimits(), state_path=st, now=1001.0)
    check("I: a routine 'ran one experiment' cycle sends NO Telegram message", sent["msgs"] == [])

    # a new DRAFT -> exactly one message, and it says proposal-only
    proposal_cycle = quiet._replace(proposals_created=1, drafts=["hyp_abc"])
    w.maybe_notify(proposal_cycle, limits=w.WorkerLimits(), state_path=st, now=1002.0)
    check("I: a new DRAFT proposal sends exactly one message", len(sent["msgs"]) == 1)
    check("I: the DRAFT message says proposals are not locked / not tradeable",
          "not locked" in sent["msgs"][0] and "not tradeable" in sent["msgs"][0])

    # an error -> a message
    sent["msgs"].clear()
    err_cycle = quiet._replace(errors=["scheduler: boom"])
    w.maybe_notify(err_cycle, limits=w.WorkerLimits(), state_path=st, now=1003.0)
    check("I: an error cycle sends a message", len(sent["msgs"]) == 1 and "error" in sent["msgs"][0].lower())

    # periodic summary: silent until the interval elapses, then exactly once
    sent["msgs"].clear()
    st2 = fresh_state("i2")
    lim100 = w.WorkerLimits(summary_interval_seconds=100)
    w.maybe_notify(quiet, limits=lim100, state_path=st2, now=10.0)     # seeds at t=10
    check("I: the periodic summary does NOT fire on the seeding heartbeat", sent["msgs"] == [])
    w.maybe_notify(quiet, limits=lim100, state_path=st2, now=50.0)     # 40s later — too soon
    check("I: no summary before the interval elapses", sent["msgs"] == [])
    w.maybe_notify(quiet, limits=lim100, state_path=st2, now=200.0)    # 190s later — due
    first = list(sent["msgs"])
    w.maybe_notify(quiet, limits=lim100, state_path=st2, now=205.0)    # right after — quiet again
    check("I: a periodic summary fires once at the interval, then stays quiet",
          len(first) == 1 and "periodic" in first[0] and len(sent["msgs"]) == 1)
finally:
    w._notify = _orig_notify


# ---------------------------------------------------------------------------
print("\n--- J: isolation — no path from the worker to lock / live / broker / paper ---")
# ---------------------------------------------------------------------------

code = _code_only(WORKER_SRC)
_imports = re.findall(r"^\s*(?:from|import)\s+([.\w]+)", code, re.MULTILINE)

check("J: worker.py never imports engine.execute / engine.guardrails / engine.broker*",
      not any(m.startswith("engine") for m in _imports), str([m for m in _imports if m.startswith("engine")]))
check("J: worker.py never imports anything from paper/",
      not any(m.split(".")[0] == "paper" for m in _imports), str(_imports))
check("J: worker.py never calls approve_and_lock() or Contract.lock() in code",
      "approve_and_lock(" not in code and ".lock()" not in code,
      "discovery must end at DRAFT — a human locks")
check("J: worker.py never imports hypothesis_intake.approve_and_lock by name",
      not re.search(r"import[^\n]*approve_and_lock", WORKER_SRC))
check("J: worker.py never references a broker / order-placement symbol",
      not any(tok in code for tok in ("get_broker", "propose_trade", "place_order", "run_paper_cycle", ".place(")))
check("J: worker.py imports run_experiment only through the scheduler, never directly",
      "from ..experiments import runner" not in code and "experiments.runner" not in code,
      "the worker delegates to scheduler.run_scheduler, which owns the runner call")

# engine/ must not have grown a research import, research/ must not have grown a broker one
import subprocess  # noqa: E402
_root = str(Path(__file__).parent.parent)
_eng = subprocess.run(["grep", "-rlE", r"^\s*(from|import)\s+research", f"{_root}/engine"],
                      capture_output=True, text=True)
check("J: engine/ still imports nothing from research/ (kernel isolation intact)",
      _eng.returncode != 0 and _eng.stdout.strip() == "", _eng.stdout)


# ---------------------------------------------------------------------------
print("\n--- K: determinism / telemetry persistence ---")
# ---------------------------------------------------------------------------

s = fresh_store("k")
reg = fresh_registry("k")
st = fresh_state("k")
log = TMP / "worker_runs.jsonl"
lim = w.WorkerLimits(max_discovery_attempts=0, max_experiments=0, cooldown_seconds=0)
r1 = w.run_worker_cycle(s, now_ist(), limits=lim, registry_dir=reg, runner=lambda p: "x", state_path=st)
r2 = w.run_worker_cycle(s, now_ist(), limits=lim, registry_dir=reg, runner=lambda p: "x", state_path=st)
s.close()
check("K: two idle heartbeats produce the same shape (work_selected, counts)",
      r1.work_selected == r2.work_selected == []
      and r1.proposals_created == r2.proposals_created == 0
      and r1.experiments_run == r2.experiments_run == 0)
w._persist_run(r1, run_log=log)
w._persist_run(r2, run_log=log)
rows = [json.loads(x) for x in log.read_text().splitlines() if x.strip()]
check("K: _persist_run appends one JSONL row per heartbeat", len(rows) == 2)
check("K: every persisted row has the full telemetry contract",
      all(REQUIRED_TELEMETRY <= set(row.keys()) for row in rows), str(rows[0].keys()))
check("K: WorkerLimits rejects max_concurrent_experiments != 1 in v0",
      _raises(lambda: w.WorkerLimits(max_concurrent_experiments=2)))


shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'=' * 52}")
print(f"  {PASSED} passed, {FAILED} failed")
print(f"{'=' * 52}\n")
sys.exit(1 if FAILED else 0)
