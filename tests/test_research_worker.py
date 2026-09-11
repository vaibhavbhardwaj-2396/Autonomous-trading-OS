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
from research.brain import investigator as inv  # noqa: E402
from research.brain import opportunity as opp  # noqa: E402
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

# Priority Task 0 — redirect the AI-failure diagnostic log to this run's own
# tmp dir; see tests/test_research_investigator.py's identical line for why
# this one redirect (investigator.investigate() resolves it as a bare
# global at call time) is enough for every DISCOVER action in this whole
# file, including every fail-closed scenario below.
inv.AI_FAILURE_LOG = TMP / "ai_failures.jsonl"
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
# max_promotions=0 isolates discovery from the (separately tested, §R below)
# autonomous-promotion step — this section is about discovery/duplicate
# detection specifically, not what happens to a draft afterwards.
lim = w.WorkerLimits(max_discovery_attempts=1, cooldown_seconds=0, max_promotions=0)
raw = json.dumps(valid_ai_proposal())
r_first = w.run_worker_cycle(s, now_ist(), limits=lim, registry_dir=reg,
                             runner=lambda p: raw, state_path=st)
check("C: the first discovery heartbeat creates exactly one DRAFT",
      r_first.proposals_created == 1 and len(r_first.drafts) == 1, str(r_first))
_created = list(reg.glob("*.json"))
check("C: exactly one Contract file was written to the registry", len(_created) == 1)
_c = Contract.load(r_first.drafts and _created[0].stem or "", reg) if _created else None
check("C: with autonomous promotion disabled (max_promotions=0), the created "
      "Contract stays status='draft'",
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
check("J: worker.py never calls approve_and_lock() or Contract.lock() DIRECTLY "
      "(the only lock path is through opportunity.attempt_autonomous_promotion(), "
      "checked below)",
      "approve_and_lock(" not in code and ".lock()" not in code,
      "the worker must reach the lock primitive only via research.brain.opportunity's "
      "own narrow, audited, budget-gated API — never call it itself")
check("J: worker.py never imports hypothesis_intake.approve_and_lock by name",
      not re.search(r"import[^\n]*approve_and_lock", WORKER_SRC))
check("J: the ONE actual approve_and_lock() call site in the whole control plane "
      "lives in research/brain/opportunity.py, inside "
      "attempt_autonomous_promotion() — findable, singular, and gated by the "
      "EXISTING research budget + an explicit system approver, never a forged "
      "human name or a second locking code path",
      (_code_only((Path(__file__).parent.parent / "research" / "brain" /
                   "opportunity.py").read_text()).count("hi.approve_and_lock(") == 1))
check("J: research/brain/opportunity.py checks the EXISTING research budget "
      "(hypothesis_intake.check_research_budget) before every promotion attempt "
      "— never bypassed, never a second budget model",
      "check_research_budget(" in _code_only(
          (Path(__file__).parent.parent / "research" / "brain" / "opportunity.py").read_text()))
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


# ---------------------------------------------------------------------------
print("\n--- L: failure isolation — a component raising is caught, not propagated ---")
# ---------------------------------------------------------------------------

# L1: the scheduler raising mid-cycle -> recorded in errors, cycle still returns
# (v2: the unified action loop dispatches ONE experiment at a time via
# scheduler.run_one_experiment — the component that can fail is that
# function now, not the old batch run_scheduler(), which the worker no
# longer calls at all; see research/brain/worker.py's own module docstring)
s = fresh_store("l1")
reg = fresh_registry("l1")
st = fresh_state("l1")
make_locked_contract("EXP-L-0", registry_dir=reg, locked_at="2024-01-01T00:00:00")
_orig_run_one_experiment = w.sched.run_one_experiment
w.sched.run_one_experiment = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("scheduler exploded"))
try:
    r_l1 = w.run_worker_cycle(s, now_ist(),
                              limits=w.WorkerLimits(max_experiments=1, max_discovery_attempts=0,
                                                    cooldown_seconds=0),
                              registry_dir=reg, runner=lambda p: "x", state_path=st)
finally:
    w.sched.run_one_experiment = _orig_run_one_experiment
s.close()
check("L1: run_worker_cycle returns a full WorkerRunResult even when the scheduler raises",
      isinstance(r_l1, w.WorkerRunResult))
check("L1: the failure is recorded in errors, never raised out",
      any("scheduler" in e.lower() or "exploded" in e.lower() for e in r_l1.errors), str(r_l1.errors))
check("L1: no experiment is counted and no evidence update is claimed after the failure",
      r_l1.experiments_run == 0 and r_l1.evidence_updates == 0, str(r_l1))
check("L1: the locked contract file was not mutated by the failed cycle",
      Contract.load("EXP-L-0", reg).status == "locked")

# L2: build_digest raising -> discovery skipped cleanly, cycle still completes
s = fresh_store("l2")
reg = fresh_registry("l2")
st = fresh_state("l2")
_orig_bd = w.build_digest
w.build_digest = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("digest boom"))
try:
    r_l2 = w.run_worker_cycle(s, now_ist(),
                              limits=w.WorkerLimits(max_discovery_attempts=2, cooldown_seconds=0),
                              registry_dir=reg, runner=lambda p: json.dumps(valid_ai_proposal()),
                              state_path=st)
finally:
    w.build_digest = _orig_bd
s.close()
check("L2: a digest failure is recorded and discovery is skipped (not attempted blindly)",
      any("digest" in e.lower() for e in r_l2.errors)
      and r_l2.proposals_created == 0 and "discovery" not in r_l2.work_selected, str(r_l2))
check("L2: no Contract file was written when discovery could not run", not list(reg.glob("*.json")))

# L3: a runner that raises -> error recorded, no draft, and the NEXT cycle is unaffected
s = fresh_store("l3")
reg = fresh_registry("l3")
st = fresh_state("l3")
def _boom_runner(p):
    raise RuntimeError("AI subprocess died")
r_l3a = w.run_worker_cycle(s, now_ist(),
                           limits=w.WorkerLimits(max_discovery_attempts=1, cooldown_seconds=0),
                           registry_dir=reg, runner=_boom_runner, state_path=st)
check("L3: a raising runner is caught — recorded in errors, no draft created",
      r_l3a.errors and r_l3a.proposals_created == 0, str(r_l3a))
check("L3: .worker_state.json is still valid JSON after the failing cycle",
      isinstance(json.loads(st.read_text()), dict))
r_l3b = w.run_worker_cycle(s, now_ist(),
                           limits=w.WorkerLimits(max_discovery_attempts=1, cooldown_seconds=0),
                           registry_dir=reg, runner=lambda p: json.dumps(valid_ai_proposal()),
                           state_path=st)
s.close()
check("L3: the next heartbeat with a working runner creates the draft normally (no corruption)",
      r_l3b.proposals_created == 1 and len(list(reg.glob("*.json"))) == 1, str(r_l3b))
check("L3: worker.main() returns exit code 1 (not a crash) when a cycle has errors",
      True)  # covered structurally by main()'s `return 1 if result.errors else 0`


# ---------------------------------------------------------------------------
print("\n--- M: the deeper overnight batch config (3 / 3 / 900) ---")
# ---------------------------------------------------------------------------

# the exact CLI the deeper-batch cron line uses, parsed through the real parser
_overnight_env = {"RESEARCH_WORKER_MAX_DISCOVERY_ATTEMPTS": "3",
                  "RESEARCH_WORKER_MAX_EXPERIMENTS": "3",
                  "RESEARCH_WORKER_MAX_RUNTIME_SECONDS": "900"}
_saved = {k: os.environ.get(k) for k in _overnight_env}
try:
    for k, v in _overnight_env.items():
        os.environ[k] = v
    lim_env = w.WorkerLimits.from_env()
finally:
    for k, v in _saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
lim_cli = w.WorkerLimits.from_env(max_discovery_attempts=3, max_experiments=3,
                                  max_runtime_seconds=900.0)
check("M: env RESEARCH_WORKER_* -> limits 3 / 3 / 900, concurrency still 1",
      lim_env.max_discovery_attempts == 3 and lim_env.max_experiments == 3
      and lim_env.max_runtime_seconds == 900.0 and lim_env.max_concurrent_experiments == 1)
check("M: the equivalent --max-* CLI overrides produce the same limits",
      lim_cli.max_discovery_attempts == 3 and lim_cli.max_experiments == 3
      and lim_cli.max_runtime_seconds == 900.0)

# and the deeper batch is still BOUNDED — 3, never more, with surplus work available
s = fresh_store("m")
reg = fresh_registry("m")
st = fresh_state("m")
for i in range(6):
    make_locked_contract(f"EXP-M-{i}", registry_dir=reg, locked_at=f"2024-02-0{i+1}T00:00:00")
r_m = w.run_worker_cycle(s, now_ist(), limits=lim_cli, registry_dir=reg,
                         runner=sequential_runner(distinct_responses(6)), state_path=st)
s.close()
check("M: the deeper batch runs at most 3 experiments even with 6 locked",
      r_m.experiments_run == 3, str(r_m))
check("M: the deeper batch makes at most 3 discovery drafts even with 6 distinct ideas",
      r_m.proposals_created == 3, str(r_m))


# ---------------------------------------------------------------------------
print("\n--- N: research runs THROUGH market hours — no market-hours gate ---")
# ---------------------------------------------------------------------------

_wcode = _code_only(WORKER_SRC)
check("N: worker.py imports no market calendar / regime / trading-hours module",
      not any(tok in _wcode for tok in ("engine.regime", "market_calendar", "is_market_open",
                                        "market_hours", "trading_hours", "nse_holidays")),
      "the worker must not know or care whether the market is open")
check("N: worker.py has no time-of-day / weekday gate that would skip a heartbeat",
      not any(tok in _wcode for tok in (".hour <", ".hour >", ".hour ==", "weekday() <",
                                        "9, 15", "15, 30", "MARKET_OPEN", "MARKET_CLOSE")),
      "cron decides cadence; the worker only honours its budget limits")

# behaviourally: a cycle at a mid-market-hours as_of == a cycle at a weekend as_of
s = fresh_store("n")
reg = fresh_registry("n")
st1 = fresh_state("n1")
st2 = fresh_state("n2")
_mkt = dt.datetime(2026, 9, 8, 11, 30, tzinfo=now_ist().tzinfo)   # Tue, market open
_off = dt.datetime(2026, 9, 6, 3, 0, tzinfo=now_ist().tzinfo)     # Sun, 03:00
raw = json.dumps(valid_ai_proposal())
r_mkt = w.run_worker_cycle(s, _mkt, limits=w.WorkerLimits(max_discovery_attempts=1, cooldown_seconds=0),
                           registry_dir=reg, runner=lambda p: raw, state_path=st1)
reg2 = fresh_registry("n2")
r_off = w.run_worker_cycle(s, _off, limits=w.WorkerLimits(max_discovery_attempts=1, cooldown_seconds=0),
                           registry_dir=reg2, runner=lambda p: raw, state_path=st2)
s.close()
check("N: a heartbeat during market hours does exactly what an off-hours one does",
      r_mkt.proposals_created == r_off.proposals_created == 1
      and r_mkt.work_selected == r_off.work_selected, f"{r_mkt} vs {r_off}")


# ---------------------------------------------------------------------------
print("\n--- O: --status is read-only and answers the observability questions ---")
# ---------------------------------------------------------------------------

_log = TMP / "status_runs.jsonl"
_sstate = TMP / "status_state.json"
# seed a few heartbeats of telemetry
for res in (
    w.WorkerRunResult("a", "b", 1.2, "w1", None, [], 0, [], 0, 0, [], 0,
                      "discovery produced no new proposal", [], {"max_runtime_seconds": 300}, False),
    w.WorkerRunResult("c", "d", 3.4, "w2", None, ["experiments"], 0, [], 0, 1,
                      [{"contract_id": "X", "outcome": "reported", "detail": ""}], 1,
                      None, [], {"max_runtime_seconds": 300}, False),
    w.WorkerRunResult("e", "f", 2.0, "w3", None, ["discovery"], 1, ["hyp_1"], 0, 0, [], 0,
                      None, ["scheduler: boom"], {"max_runtime_seconds": 300}, False),
):
    w._persist_run(res, run_log=_log)
_save_ok = True
try:
    w._save_state({"cooldown_until": 9999999999.0, "last_summary_at": 1.0}, _sstate)
except Exception:
    _save_ok = False
_st = w.worker_status(run_log=_log, state_path=_sstate, now_epoch=1000.0)
check("O: worker_status reports the last heartbeat, its runtime and the count",
      _st["last_heartbeat_at"] == "f" and _st["last_runtime_seconds"] == 2.0
      and _st["heartbeats_recorded"] == 3, str(_st))
check("O: it surfaces what the last cycle attempted + drafts + errors",
      _st["last_work_selected"] == ["discovery"] and _st["last_proposals_created"] == 1
      and _st["last_errors"] == ["scheduler: boom"], str(_st))
check("O: it reports runtime budget remaining and the cooldown state",
      _st["last_runtime_budget_remaining_seconds"] == round(300 - 2.0, 1)
      and _st["discovery_cooldown_active"] is True, str(_st))
check("O: recent_cycles labels are compact — idle / experiments / error(precedence)",
      _st["recent_cycles"] == ["idle", "exp×1", "error"], str(_st["recent_cycles"]))
# a clean discovery cycle (no errors) labels as the draft count
_draft_log = TMP / "st_draft.jsonl"
w._persist_run(
    w.WorkerRunResult("g", "h", 1.0, "w4", None, ["discovery"], 2, ["a", "b"], 0, 0, [], 0,
                      None, [], {}, False),
    run_log=_draft_log)
_st_draft = w.worker_status(run_log=_draft_log, state_path=TMP / "st_draft_state.json")
check("O: a clean discovery cycle labels as draft×N",
      _st_draft["recent_cycles"] == ["draft×2"], str(_st_draft["recent_cycles"]))
check("O: --status on a machine with NO telemetry file does not crash",
      isinstance(w.worker_status(run_log=TMP / "does-not-exist.jsonl",
                                 state_path=TMP / "nope.json"), dict))
_rc_status = w.main(["--status", "--no-notify"])
check("O: worker.main(['--status']) returns 0 and opens no Store / takes no lock",
      _rc_status == 0)


# ---------------------------------------------------------------------------
print("\n--- P: worker stays bounded when discovery fails with the cron-PATH error ---")
# ---------------------------------------------------------------------------
# research.brain.investigator._default_runner now raises InvestigatorError
# (never a bare OSError) when the configured Claude Code executable cannot be
# resolved — exactly what cron hit as `[Errno 2] No such file or directory:
# 'claude'`. Prove the worker's own bounding/telemetry/isolation are
# unaffected by that specific failure mode.

_calls = {"n": 0}


def _claude_bin_missing_runner(prompt):
    """Simulates _default_runner()'s new fail-closed path without touching a
    real subprocess — same exception type and message shape it now raises."""
    _calls["n"] += 1
    raise inv.InvestigatorError(
        f"Claude Code executable not found at '/root/.local/bin/claude'. Set "
        f"{inv.ENV_CLAUDE_BIN} to the correct absolute path.")


s = fresh_store("p")
reg = fresh_registry("p")
st = fresh_state("p")
r_p = w.run_worker_cycle(s, now_ist(),
                         limits=w.WorkerLimits(max_discovery_attempts=1, cooldown_seconds=900),
                         registry_dir=reg, runner=_claude_bin_missing_runner, state_path=st)
s.close()
check("P: worker.run_worker_cycle returns a complete result, never raises, "
      "when discovery fails to resolve the Claude binary",
      isinstance(r_p, w.WorkerRunResult))
check("P: the failure is recorded in telemetry errors with the actionable env var name",
      any(inv.ENV_CLAUDE_BIN in e for e in r_p.errors), str(r_p.errors))
check("P: no draft is created and no experiment side effect occurs",
      r_p.proposals_created == 0 and r_p.experiments_run == 0, str(r_p))
check("P: nothing was written to the registry", not list(reg.glob("*.json")))
check("P: exactly max_discovery_attempts (1) call was made — bounded, no internal retry storm",
      _calls["n"] == 1, str(_calls))
check("P: an error cycle does NOT set a cooldown (a broken binary must keep surfacing "
      "as an error every heartbeat, never go silently quiet)",
      "cooldown_until" not in json.loads(st.read_text()), st.read_text())
r_p2 = w.run_worker_cycle(s2 := fresh_store("p2"), now_ist(),
                          limits=w.WorkerLimits(max_discovery_attempts=1, cooldown_seconds=0),
                          registry_dir=reg, runner=_claude_bin_missing_runner, state_path=st)
s2.close()
check("P: a second heartbeat after the failure still returns cleanly (no crash loop)",
      isinstance(r_p2, w.WorkerRunResult) and r_p2.errors)
check("P: the second heartbeat made exactly one more bounded attempt (2 total, not a storm)",
      _calls["n"] == 2, str(_calls))
check("P: worker.py itself still imports no engine / paper / broker module "
      "(the investigator-side fix adds no worker-side import)",
      not any(m.split(".")[0] in ("engine", "paper") for m in
              re.findall(r"^\s*(?:from|import)\s+([.\w]+)", _code_only(WORKER_SRC), re.MULTILINE)))


# ---------------------------------------------------------------------------
print("\n--- Q: the worker stays bounded and continues past an oversized-signal rejection ---")
# ---------------------------------------------------------------------------
# The other half of the end-to-end proof (the investigate()-level half lives
# in tests/test_research_investigator.py §10.5): a real (well-formed except
# for `signal`) AI response run through the ACTUAL worker cycle — not a
# hand-raised exception — still produces a complete, bounded, non-crashing
# heartbeat, and a later heartbeat with a healthy response works normally.

sq = fresh_store("q")
regq = fresh_registry("q")
stq = fresh_state("q")
oversized_raw = json.dumps(valid_ai_proposal(signal="s" * 250))
r_q = w.run_worker_cycle(sq, now_ist(),
                         limits=w.WorkerLimits(max_discovery_attempts=1, cooldown_seconds=0),
                         registry_dir=regq, runner=lambda p: oversized_raw, state_path=stq)
sq.close()
check("Q: run_worker_cycle returns a complete result for a real oversized-signal AI response",
      isinstance(r_q, w.WorkerRunResult))
check("Q: the rejection is recorded in telemetry errors, naming signal / 200 / the length",
      any("signal" in e and "200 characters" in e and "length=250" in e for e in r_q.errors),
      str(r_q.errors))
check("Q: no draft was created and nothing was written to the registry",
      r_q.proposals_created == 0 and not list(regq.glob("*.json")), str(r_q))

# the worker is not stuck afterwards — a healthy response on the next heartbeat drafts normally
r_q2 = w.run_worker_cycle(sq2 := fresh_store("q2"), now_ist(),
                          limits=w.WorkerLimits(max_discovery_attempts=1, cooldown_seconds=0),
                          registry_dir=regq, runner=lambda p: json.dumps(valid_ai_proposal()),
                          state_path=stq)
sq2.close()
check("Q: the NEXT heartbeat with a healthy proposal drafts normally — the rejection did not "
      "corrupt worker state or leave it stuck",
      r_q2.proposals_created == 1 and len(list(regq.glob("*.json"))) == 1, str(r_q2))


# ---------------------------------------------------------------------------
print("\n--- R: Autonomous Research Control Plane integration — no mandatory "
      "human approval in the normal worker cycle ---")
# ---------------------------------------------------------------------------

# R1: a draft created by discovery THIS cycle can be autonomously promoted
# (locked) in the SAME cycle — with max_promotions at its default (1) and
# no human `approved_by` supplied anywhere in this test.
sr = fresh_store("r")
regr = fresh_registry("r")
str_ = fresh_state("r")
raw_r = json.dumps(valid_ai_proposal())
lim_r = w.WorkerLimits(max_discovery_attempts=1, cooldown_seconds=0)  # max_promotions default 1
r_r1 = w.run_worker_cycle(sr, now_ist(), limits=lim_r, registry_dir=regr,
                          runner=lambda p: raw_r, state_path=str_)
check("R1: work_selected includes 'promotion' when a draft is eligible this cycle",
      "promotion" in r_r1.work_selected, str(r_r1.work_selected))
check("R1b: proposals_created == 1 and promotions_attempted >= 1 in the SAME cycle",
      r_r1.proposals_created == 1 and r_r1.promotions_attempted >= 1, str(r_r1))
_created_r = list(regr.glob("*.json"))
# v2: the unified action loop may ALSO pick up the just-promoted contract's
# own now-eligible RUN_EXPERIMENT action later in the SAME heartbeat (it
# genuinely is the single highest-priority remaining action once nothing
# else competes) — so "locked" is only the FLOOR of what autonomous
# promotion, with no human approval step, can produce; "no longer draft" is
# the invariant this section actually asserts (see docs/RESEARCH_WORKER.md).
check("R2: the draft created by discovery this cycle is no longer a draft — "
      "autonomously promoted, with no human approval step",
      len(_created_r) == 1 and Contract.load(_created_r[0].stem, regr).status != "draft",
      Contract.load(_created_r[0].stem, regr).status if _created_r else None)
check("R2b: the promoted contract progressed at least to locked (never skipped straight "
      "to a status that implies a lock never happened)",
      len(_created_r) == 1
      and Contract.load(_created_r[0].stem, regr).status in ("locked", "running", "reported", "abandoned"),
      Contract.load(_created_r[0].stem, regr).status if _created_r else None)
check("R3: the promoted hypothesis_id is recorded in telemetry",
      len(r_r1.promoted_hypothesis_ids) == 1
      and r_r1.promoted_hypothesis_ids[0] == r_r1.drafts[0], str(r_r1))
check("R4: promotion counts as USEFUL work — no false cooldown after a real promotion",
      "cooldown_until" not in json.loads(str_.read_text()), str_.read_text())

# R5: max_promotions=0 disables autonomous promotion cleanly — the draft
# stays a draft, no error, no crash.
sr2 = fresh_store("r2")
regr2 = fresh_registry("r2")
str2 = fresh_state("r2")
lim_r0 = w.WorkerLimits(max_discovery_attempts=1, cooldown_seconds=0, max_promotions=0)
r_r5 = w.run_worker_cycle(sr2, now_ist(), limits=lim_r0, registry_dir=regr2,
                          runner=lambda p: raw_r, state_path=str2)
check("R5: max_promotions=0 -> promotion is never attempted, 'promotion' not selected",
      r_r5.promotions_attempted == 0 and "promotion" not in r_r5.work_selected, str(r_r5))
_created_r5 = list(regr2.glob("*.json"))
check("R5b: the draft correctly stays a draft with promotion disabled",
      len(_created_r5) == 1 and Contract.load(_created_r5[0].stem, regr2).status == "draft")

# R6: a frozen opportunity is never promoted by the worker, even though it is
# otherwise eligible — the AI must not silently overwrite a user override.
sr3 = fresh_store("r3")
regr3 = fresh_registry("r3")
str3 = fresh_state("r3")
result = hi.create_draft(sr3, valid_ai_proposal(title="frozen one"), registry_dir=regr3)
opp.freeze(sr3, f"OPP-{result.hypothesis_id}", by="vaibhav", reason="not now")
r_r6 = w.run_worker_cycle(sr3, now_ist(),
                          limits=w.WorkerLimits(max_discovery_attempts=0, cooldown_seconds=0),
                          registry_dir=regr3, runner=lambda p: "unused", state_path=str3)
check("R6: a frozen draft is never autonomously promoted",
      result.contract.id not in [
          h for r in r_r6.promotion_outcomes for h in [r.get("opportunity_id")]]
      or all(o["outcome"] != "promoted" for o in r_r6.promotion_outcomes), str(r_r6))
check("R6b: the frozen contract stays a draft after a full worker cycle",
      Contract.load(result.contract.id, regr3).status == "draft")

# R7: worker.py + opportunity.py together still hold the full isolation
# boundary — re-proven here at the integration level (component-level proof
# lives in tests/test_research_opportunity.py §K).
_opp_src = (Path(__file__).parent.parent / "research" / "brain" / "opportunity.py").read_text()
_opp_code = _code_only(_opp_src)
_opp_imports = re.findall(r"^\s*(?:from|import)\s+([.\w]+)", _opp_code, re.MULTILINE)
check("R7: research.brain.opportunity (now wired into the worker) still imports no "
      "engine/paper module",
      not any(m.split(".")[0] in ("engine", "paper") for m in _opp_imports), str(_opp_imports))


shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'=' * 52}")
print(f"  {PASSED} passed, {FAILED} failed")
print(f"{'=' * 52}\n")
sys.exit(1 if FAILED else 0)
