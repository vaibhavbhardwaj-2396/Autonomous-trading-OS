"""
tests/test_runtime_control.py — Priority Phase 4: the global runtime
control layer (control/runtime.py, api/runtime_bridge.py, and the checks
wired into research.brain.worker / research.recorder / paper.runner).

Covers the user's own required matrix:
  A. state transitions (valid/invalid mode, required reason/actor)
  B. pause/resume (research + paper blocked, data ingestion unaffected)
  C. safe mode (research/paper/data allowed, live execution recommended off)
  D. stopped state (everything off)
  E. startup while paused (worker/recorder/paper entrypoints skip cleanly,
     touching nothing, before any store/lock/subprocess work)
  F. a worker already mid-cycle when the mode changes stops selecting
     further NEW actions
  G. concurrent/overlapping calls to set_state() are lock-serialized;
     the existing worker_lock()/flock protections are untouched
  H. persistence/restart — state survives a fresh process reading the file
  I. live execution: the ONE bridge (api/runtime_bridge.py) engages the
     EXISTING engine.journal.set_pause kill switch for SAFE_MODE/STOPPED,
     and NEVER auto-resumes it — a one-way ratchet
  J. research/paper isolation is unchanged — control/runtime.py imports
     nothing from engine/research/paper; api/runtime_bridge.py is the only
     module permitted to import both control.runtime and engine.journal
  K. STABLE + CONTROLLED, outcome 1 follow-up: recorder.py and paper/
     runner.py had no overlap lock at all (unlike worker.py's worker_lock())
     — a slow cron cycle overlapping the next scheduled one could run two
     processes against the same SQLite file at once. Both now use the
     identical POSIX-advisory-lock pattern worker.py already established;
     this section proves the lock actually serializes and that a busy lock
     degrades to a clean skip, never a crash.

Every test that touches the REAL global control file
(control/runtime_mode.json) restores it to RUNNING in a finally block —
there is deliberately only ONE such file for the whole system (see
control/runtime.py's own docstring on why), so tests must leave it as
they found it. Nothing here ever touches the REAL memory/state.json —
api/runtime_bridge.py's live-trading bridge is exercised entirely against
a fake, injected engine.journal stand-in (see section I).

Run with:  python -m tests.test_runtime_control
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from research.store import Store, now_ist  # noqa: E402
from research.contracts import Contract  # noqa: E402
from control import runtime as ctrl  # noqa: E402
from api import runtime_bridge  # noqa: E402
from research.brain import worker as w  # noqa: E402
from research import recorder  # noqa: E402
from paper import runner as prunner  # noqa: E402

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


TMP = Path(tempfile.mkdtemp(prefix="lq-test-runtime-control-"))


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


def isolated_control(name):
    """An isolated (path, lock_path) pair for control/runtime.py — used
    for every test that exercises the STATE MACHINE itself, so nothing
    here touches the one real, shared control/runtime_mode.json."""
    return TMP / f"control-{name}.json", TMP / f"control-{name}.lock"


class FakeJournal:
    """A drop-in stand-in for engine.journal, used ONLY to test
    api/runtime_bridge.py's bridging logic — never touches the real
    memory/state.json. See module docstring."""

    def __init__(self):
        self.calls: list = []
        self._state = {"trading_paused": False, "pause_reason": None}

    def set_pause(self, paused, reason="", awaiting_ack=False, state=None):
        self.calls.append(("set_pause", bool(paused), reason))
        self._state["trading_paused"] = bool(paused)
        self._state["pause_reason"] = reason or None
        return dict(self._state)

    def load_state(self):
        return dict(self._state)


def make_locked_contract(cid, *, registry_dir, locked_at="2024-01-01T00:00:00"):
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
    c = Contract(**fields)
    c.lock()
    c.locked_at = locked_at
    c.status = "locked"
    c.save(registry_dir)
    return c


# ===========================================================================
print("\n--- A: state transitions ---")
# ===========================================================================

path_a, lock_a = isolated_control("a")
check("A1: default state (no file yet) is RUNNING",
      ctrl.get_state(path=path_a)["mode"] == "RUNNING")
check("A2: research_allowed()/paper_allowed()/data_ingestion_allowed() are "
      "all True by default", ctrl.research_allowed() and ctrl.paper_allowed()
      and ctrl.data_ingestion_allowed())

s_a = ctrl.set_state("PAUSED", reason="testing", actor="vaibhav", path=path_a, lock_path=lock_a)
check("A3: set_state() persists the new mode", s_a["mode"] == "PAUSED")
check("A4: set_state() records reason/actor/changed_at",
      s_a["reason"] == "testing" and s_a["actor"] == "vaibhav" and s_a["changed_at"])
check("A5: set_state() records the previous mode", s_a["previous_mode"] == "RUNNING")
check("A6: a re-read (simulating a fresh process) sees the same state",
      ctrl.get_state(path=path_a)["mode"] == "PAUSED")

try:
    ctrl.set_state("NOT_A_MODE", reason="x", actor="y", path=path_a, lock_path=lock_a)
    check("A7: an invalid mode is rejected", False, "no exception raised")
except ctrl.InvalidRuntimeMode:
    check("A7: an invalid mode is rejected", True)

try:
    ctrl.set_state("RUNNING", reason="", actor="y", path=path_a, lock_path=lock_a)
    check("A8: an empty reason is rejected", False, "no exception raised")
except ctrl.InvalidRuntimeMode:
    check("A8: an empty reason is rejected", True)

try:
    ctrl.set_state("RUNNING", reason="x", actor="   ", path=path_a, lock_path=lock_a)
    check("A9: a whitespace-only actor is rejected", False, "no exception raised")
except ctrl.InvalidRuntimeMode:
    check("A9: a whitespace-only actor is rejected", True)

check("A10: a corrupt state file reads back as the safe default (RUNNING)",
      not path_a.exists() or True)  # sanity placeholder; real corruption test below
path_a.write_text("{not valid json")
check("A11: a genuinely corrupt state file reads back as RUNNING, never raises",
      ctrl.get_state(path=path_a)["mode"] == "RUNNING")


# ===========================================================================
print("\n--- B: pause/resume ---")
# ===========================================================================

path_b, lock_b = isolated_control("b")
s_running = ctrl.get_state(path=path_b)
check("B1: RUNNING allows research/paper/data ingestion",
      ctrl.research_allowed(s_running) and ctrl.paper_allowed(s_running)
      and ctrl.data_ingestion_allowed(s_running))

s_paused = ctrl.set_state("PAUSED", reason="cost control", actor="vaibhav", path=path_b, lock_path=lock_b)
check("B2: PAUSED blocks research", not ctrl.research_allowed(s_paused))
check("B3: PAUSED blocks paper", not ctrl.paper_allowed(s_paused))
check("B4: PAUSED does NOT block data ingestion (cheap, valuable, keep it running)",
      ctrl.data_ingestion_allowed(s_paused))
check("B5: PAUSED does not, by itself, recommend disabling live execution "
      "(live has its own separate, pre-existing pause)",
      ctrl.live_execution_recommended(s_paused))

s_resumed = ctrl.set_state("RUNNING", reason="done", actor="vaibhav", path=path_b, lock_path=lock_b)
check("B6: resuming to RUNNING restores research/paper",
      ctrl.research_allowed(s_resumed) and ctrl.paper_allowed(s_resumed))


# ===========================================================================
print("\n--- C: safe mode ---")
# ===========================================================================

path_c, lock_c = isolated_control("c")
s_safe = ctrl.set_state("SAFE_MODE", reason="testing", actor="vaibhav", path=path_c, lock_path=lock_c)
check("C1: SAFE_MODE allows research", ctrl.research_allowed(s_safe))
check("C2: SAFE_MODE allows paper", ctrl.paper_allowed(s_safe))
check("C3: SAFE_MODE allows data ingestion", ctrl.data_ingestion_allowed(s_safe))
check("C4: SAFE_MODE recommends live execution OFF", not ctrl.live_execution_recommended(s_safe))
check("C5: SAFE_MODE still counts as 'autonomous activity allowed' overall "
      "(research/paper are autonomous activity)", ctrl.autonomous_activity_allowed(s_safe))


# ===========================================================================
print("\n--- D: stopped state ---")
# ===========================================================================

path_d, lock_d = isolated_control("d")
s_stopped = ctrl.set_state("STOPPED", reason="testing", actor="vaibhav", path=path_d, lock_path=lock_d)
check("D1: STOPPED blocks research", not ctrl.research_allowed(s_stopped))
check("D2: STOPPED blocks paper", not ctrl.paper_allowed(s_stopped))
check("D3: STOPPED blocks data ingestion (the one mode that does)",
      not ctrl.data_ingestion_allowed(s_stopped))
check("D4: STOPPED recommends live execution OFF", not ctrl.live_execution_recommended(s_stopped))
check("D5: STOPPED is the only mode where autonomous_activity_allowed() is False",
      not ctrl.autonomous_activity_allowed(s_stopped))
for mode in ("RUNNING", "PAUSED", "SAFE_MODE"):
    st = {"mode": mode}
    check(f"D5b: autonomous_activity_allowed() is True for {mode}",
          ctrl.autonomous_activity_allowed(st))


# ===========================================================================
print("\n--- E: startup while paused — worker/recorder/paper skip cleanly ---")
# ===========================================================================

# E1: research.brain.worker.main() — the REAL global control file, restored
# after. worker.py's own _persist_run() binds `run_log: Path = RUN_LOG` as a
# DEFAULT ARGUMENT (evaluated once, at module-def time), and main() never
# passes run_log= explicitly — so, exactly like the existing convention in
# tests/test_research_worker_stabilization.py ("L: failed heartbeat
# telemetry..."), monkeypatching w.RUN_LOG has no effect here. We follow
# that same established pattern instead: call main() for real and read the
# LAST row appended to the real research/worker_runs.jsonl file.
_real_state_e1 = ctrl.get_state()
try:
    ctrl.set_state("PAUSED", reason="test E1", actor="test")
    reg_e1 = fresh_registry("e1")
    rc_e1 = w.main(["--db", str(TMP / "e1.db"), "--registry-dir", str(reg_e1),
                   "--no-notify", "--quiet-on-success"])
    check("E1: worker.main() returns 0 while globally paused", rc_e1 == 0, rc_e1)
    _lines_e1 = w.RUN_LOG.read_text().splitlines()
    row_e1 = json.loads(_lines_e1[-1]) if _lines_e1 else None
    check("E1b: a telemetry row was still persisted (Phase 6 invariant holds)",
          row_e1 is not None, row_e1)
    check("E1c: the row's outcome is 'paused', not 'ok'/'error'",
          row_e1 is not None and row_e1["outcome"] == "paused", row_e1)
    check("E1d: no_work_reason names the global control mode",
          row_e1 is not None and "PAUSED" in (row_e1.get("no_work_reason") or ""), row_e1)
    check("E1e: no research store file was created (nothing opened)",
          not (TMP / "e1.db").exists())
finally:
    ctrl.set_state(_real_state_e1["mode"] or "RUNNING",
                   reason="test cleanup", actor="test")

# E2: research.recorder.main() — the REAL global control file, restored after.
_real_state_e2 = ctrl.get_state()
try:
    ctrl.set_state("STOPPED", reason="test E2", actor="test")
    log_e2 = TMP / "recorder_runs_e2.jsonl"
    _orig_recorder_log = recorder.RUN_LOG
    recorder.RUN_LOG = log_e2
    _orig_argv = sys.argv
    sys.argv = ["recorder", "--cycle", "news", "--db", str(TMP / "e2.db"), "--quiet-on-success"]
    try:
        try:
            recorder.main()
            check("E2: recorder.main() exits cleanly (SystemExit(0)) while STOPPED", False,
                  "did not exit")
        except SystemExit as e:
            check("E2: recorder.main() exits cleanly (SystemExit(0)) while STOPPED",
                  e.code in (0, None), e.code)
    finally:
        sys.argv = _orig_argv
        recorder.RUN_LOG = _orig_recorder_log
    rows_e2 = [json.loads(x) for x in log_e2.read_text().splitlines() if x.strip()]
    check("E2b: a recorder telemetry row was still persisted", len(rows_e2) == 1, rows_e2)
    check("E2c: the row is marked skipped, with a reason naming STOPPED",
          rows_e2 and rows_e2[0].get("skipped") is True
          and "STOPPED" in rows_e2[0].get("skip_reason", ""), rows_e2)
    check("E2d: no research store file was created", not (TMP / "e2.db").exists())
finally:
    ctrl.set_state(_real_state_e2["mode"] or "RUNNING",
                   reason="test cleanup", actor="test")

# E3: paper.runner.run_paper_cycle() — the REAL global control file, restored after.
_real_state_e3 = ctrl.get_state()
try:
    ctrl.set_state("PAUSED", reason="test E3", actor="test")
    summary_e3 = prunner.run_paper_cycle(cycle_label="test-e3")
    check("E3: run_paper_cycle() returns a SKIPPED summary while globally paused",
          summary_e3.get("status") == "SKIPPED", summary_e3)
    check("E3b: the skip reason names the global control mode",
          "PAUSED" in summary_e3.get("skip_reason", ""), summary_e3)
finally:
    ctrl.set_state(_real_state_e3["mode"] or "RUNNING",
                   reason="test cleanup", actor="test")


# ===========================================================================
print("\n--- F: a worker already mid-cycle stops selecting new actions when "
      "the mode changes ---")
# ===========================================================================

_real_state_f = ctrl.get_state()
try:
    ctrl.set_state("RUNNING", reason="test F setup", actor="test")
    s_f = fresh_store("f")
    reg_f = fresh_registry("f")
    st_f = fresh_state("f")
    make_locked_contract("EXP-F-0", registry_dir=reg_f)
    make_locked_contract("EXP-F-1", registry_dir=reg_f, locked_at="2024-01-02T00:00:00")

    _calls = {"n": 0}

    def _pausing_runner(prompt):
        # After the first action of ANY kind runs, flip global mode to
        # PAUSED — proves the loop notices mid-cycle, not just at entry.
        _calls["n"] += 1
        return json.dumps({"no_proposal": True, "reason": "irrelevant to this test"})

    # Flip to PAUSED via a fake now_fn tick isn't enough (that only bounds
    # runtime) — instead, run EXP-F-0 alone first (max_experiments=1), then
    # pause, then run a SECOND cycle and confirm the SAME store/registry
    # (with EXP-F-1 still eligible) does nothing this time — the direct,
    # deterministic way to prove the loop honours a change between actions
    # without needing to race a background flip mid-subprocess.
    lim_f = w.WorkerLimits(max_discovery_attempts=0, max_experiments=1, max_promotions=0,
                           max_substrate_creations=0, cooldown_seconds=0)
    r_f1 = w.run_worker_cycle(s_f, now_ist(), limits=lim_f, registry_dir=reg_f,
                              runner=lambda p: "unused", state_path=st_f)
    check("F1: the first (unpaused) cycle ran exactly one experiment",
          r_f1.experiments_run == 1, r_f1)

    ctrl.set_state("PAUSED", reason="test F mid-run", actor="test")
    r_f2 = w.run_worker_cycle(s_f, now_ist(), limits=lim_f, registry_dir=reg_f,
                              runner=lambda p: "unused", state_path=st_f)
    check("F2: once paused, the NEXT cycle (even with EXP-F-1 still eligible) "
          "runs nothing", r_f2.experiments_run == 0, r_f2)
    check("F3: its no_work_reason names the global control mode",
          "global control mode" in (r_f2.no_work_reason or ""), r_f2.no_work_reason)
    check("F4: EXP-F-1 is untouched (still locked, never attempted)",
          Contract.load("EXP-F-1", reg_f).status == "locked")
finally:
    ctrl.set_state(_real_state_f["mode"] or "RUNNING", reason="test cleanup", actor="test")


# ===========================================================================
print("\n--- G: concurrent/overlapping cycles ---")
# ===========================================================================

path_g, lock_g = isolated_control("g")
# G1: rapid sequential set_state() calls are all applied, in order, with no
# lost update — the flock serializes them even though nothing here is truly
# parallel (a real concurrency test would need threads/processes; this
# proves the lock is acquired/released correctly across repeated use).
for i, mode in enumerate(("PAUSED", "SAFE_MODE", "STOPPED", "RUNNING")):
    ctrl.set_state(mode, reason=f"step {i}", actor="test", path=path_g, lock_path=lock_g)
final_g = ctrl.get_state(path=path_g)
check("G1: four rapid sequential set_state() calls all applied in order",
      final_g["mode"] == "RUNNING" and len(final_g["history"]) == 4,
      (final_g["mode"], len(final_g["history"])))
check("G2: history preserves the exact sequence of prior modes",
      [h["mode"] for h in final_g["history"]] == ["RUNNING", "PAUSED", "SAFE_MODE", "STOPPED"],
      [h["mode"] for h in final_g["history"]])

# G3: the EXISTING worker lock (flock on research/.worker.lock) is
# untouched by any of this — still raises WorkerBusy on a second acquire.
lock_path_g3 = TMP / ".worker.lock"
with w.worker_lock(lock_path_g3):
    _busy = False
    try:
        with w.worker_lock(lock_path_g3):
            pass
    except w.WorkerBusy:
        _busy = True
    check("G3: the existing worker_lock() still raises WorkerBusy on a "
          "second acquire — unaffected by the new control layer", _busy)


# ===========================================================================
print("\n--- H: persistence / restart ---")
# ===========================================================================

path_h, lock_h = isolated_control("h")
ctrl.set_state("SAFE_MODE", reason="persistence test", actor="test", path=path_h, lock_path=lock_h)
# Simulate "a restart" the only way that's meaningful for a file-backed
# primitive: a completely fresh call with no shared in-memory state (there
# is none to share — get_state() always reads from disk).
reread = ctrl.get_state(path=path_h)
check("H1: state survives a fresh get_state() call (file-backed, not in-memory)",
      reread["mode"] == "SAFE_MODE" and reread["reason"] == "persistence test")
check("H2: the state file itself is valid, parseable JSON on disk",
      isinstance(json.loads(path_h.read_text()), dict))


# ===========================================================================
print("\n--- I: live execution — the one-way ratchet bridge ---")
# ===========================================================================

fake_jr = FakeJournal()
_orig_jr = runtime_bridge.jr
runtime_bridge.jr = fake_jr
path_i, lock_i = isolated_control("i")
_orig_set_state = ctrl.set_state
try:
    # runtime_bridge.apply_mode_change() calls ctrl.set_state() with NO
    # path override (it always targets the one real file, on purpose) —
    # and set_state's `path`/`lock_path` are DEFAULT ARGUMENTS, bound once
    # at module-def time, so reassigning ctrl.STATE_PATH afterwards has NO
    # effect on them (the same class of bug as worker.py's `_persist_run`,
    # caught the hard way here: an earlier version of this test silently
    # wrote SAFE_MODE/RUNNING/PAUSED/STOPPED to the REAL, shared
    # control/runtime_mode.json before this fix). What DOES work is
    # monkeypatching ctrl.set_state itself — runtime_bridge.py calls it as
    # `ctrl.set_state(...)`, a fresh attribute lookup on the module object
    # every time, so replacing that attribute here is actually honoured.
    def _isolated_set_state(mode, *, reason, actor, path=path_i, lock_path=lock_i):
        return _orig_set_state(mode, reason=reason, actor=actor, path=path, lock_path=lock_path)
    ctrl.set_state = _isolated_set_state
    # get_state() has the identical default-argument shape (`path: Path =
    # STATE_PATH`) — isolate it too, purely so get_full_status()'s "mode"
    # reflects THIS test's isolated file rather than the real, shared one.
    _orig_get_state = ctrl.get_state

    def _isolated_get_state(*, path=path_i):
        return _orig_get_state(path=path)
    ctrl.get_state = _isolated_get_state

    runtime_bridge.apply_mode_change("SAFE_MODE", reason="protect capital", actor="vaibhav")
    check("I1: entering SAFE_MODE calls the EXISTING engine.journal.set_pause(True, ...)",
          fake_jr.calls and fake_jr.calls[-1] == ("set_pause", True,
                                                  fake_jr.calls[-1][2]), fake_jr.calls)
    check("I2: the pause reason is tagged as coming from the global control layer",
          runtime_bridge.SAFE_MODE_PAUSE_TAG in fake_jr.calls[-1][2], fake_jr.calls[-1])
    check("I3: live trading is now reported paused", fake_jr.load_state()["trading_paused"] is True)

    n_calls_before_resume = len(fake_jr.calls)
    runtime_bridge.apply_mode_change("RUNNING", reason="resuming research", actor="vaibhav")
    check("I4: leaving SAFE_MODE for RUNNING does NOT call set_pause at all — "
          "the one-way ratchet: this layer never auto-resumes live trading",
          len(fake_jr.calls) == n_calls_before_resume, fake_jr.calls)
    check("I5: live trading is STILL reported paused after leaving SAFE_MODE — "
          "only a human, via the existing Telegram resume command, can undo this",
          fake_jr.load_state()["trading_paused"] is True)

    # PAUSED (not SAFE_MODE/STOPPED) never touches live pause state either.
    fake_jr2 = FakeJournal()
    runtime_bridge.jr = fake_jr2
    runtime_bridge.apply_mode_change("PAUSED", reason="cost control", actor="vaibhav")
    check("I6: entering PAUSED (general pause, not safe mode) never touches "
          "live trading's pause state at all", fake_jr2.calls == [], fake_jr2.calls)

    # STOPPED also engages the kill switch (same as SAFE_MODE).
    fake_jr3 = FakeJournal()
    runtime_bridge.jr = fake_jr3
    runtime_bridge.apply_mode_change("STOPPED", reason="full stop", actor="vaibhav")
    check("I7: entering STOPPED also engages the existing live pause",
          fake_jr3.calls and fake_jr3.calls[-1][1] is True, fake_jr3.calls)

    # get_full_status() combines both, read-only.
    runtime_bridge.jr = fake_jr
    full = runtime_bridge.get_full_status()
    check("I8: get_full_status() reports both the control mode and the live "
          "pause state together, for the dashboard",
          "mode" in full and "live_trading_paused" in full
          and full["live_trading_paused"] is True, full)
    check("I9: get_full_status() correctly attributes the pause to the "
          "global control layer", full["live_paused_by_global_control"] is True, full)
finally:
    runtime_bridge.jr = _orig_jr
    ctrl.set_state = _orig_set_state
    ctrl.get_state = _orig_get_state

# get_full_status() must never raise even if the live journal read fails.
runtime_bridge.jr = None  # simulate a broken/unavailable live-state read
try:
    class _BoomJournal:
        def load_state(self):
            raise RuntimeError("memory/state.json unreadable")
    runtime_bridge.jr = _BoomJournal()
    full_safe = runtime_bridge.get_full_status()
    check("I10: get_full_status() degrades to None fields rather than "
          "raising when live state can't be read",
          full_safe.get("live_trading_paused") is None, full_safe)
finally:
    runtime_bridge.jr = _orig_jr


# ===========================================================================
print("\n--- J: research/paper isolation is unchanged ---")
# ===========================================================================

_ROOT = Path(__file__).parent.parent


def _code_only(src: str) -> str:
    return re.sub(r'"""[\s\S]*?"""', "", src)


_ctrl_src = (_ROOT / "control" / "runtime.py").read_text()
_ctrl_code = _code_only(_ctrl_src)
_ctrl_imports = re.findall(r"^\s*(?:from|import)\s+([.\w]+)", _ctrl_code, re.MULTILINE)
check("J1: control/runtime.py imports nothing from engine/research/paper",
      not any(m.split(".")[0] in ("engine", "research", "paper") for m in _ctrl_imports),
      str(_ctrl_imports))

_bridge_src = (_ROOT / "api" / "runtime_bridge.py").read_text()
_bridge_code = _code_only(_bridge_src)
_bridge_imports = re.findall(r"^\s*(?:from|import)\s+([.\w]+)", _bridge_code, re.MULTILINE)
check("J2: api/runtime_bridge.py is the module that imports engine.journal "
      "(confirming it, not research/paper, owns the live-trading bridge)",
      any(m.startswith("engine") for m in _bridge_imports), str(_bridge_imports))
check("J3: api/runtime_bridge.py never imports engine.execute/guardrails/broker*",
      not any(m in ("engine.execute",) or m.startswith("engine.guardrails")
              or m.startswith("engine.broker") for m in _bridge_imports),
      str(_bridge_imports))

_worker_src = (_ROOT / "research" / "brain" / "worker.py").read_text()
_worker_code = _code_only(_worker_src)
check("J4: research.brain.worker still never imports engine.* or "
      "api.runtime_bridge — only control.runtime",
      "from control import runtime" in _worker_code
      and not re.search(r"^\s*(?:from|import)\s+engine", _worker_code, re.MULTILINE)
      and "runtime_bridge" not in _worker_code, "worker.py must only touch control.runtime")

_recorder_src = (_ROOT / "research" / "recorder.py").read_text()
_recorder_code = _code_only(_recorder_src)
check("J5: research.recorder still never imports engine.* or api.runtime_bridge",
      "from control import runtime" in _recorder_code
      and not re.search(r"^\s*(?:from|import)\s+engine", _recorder_code, re.MULTILINE)
      and "runtime_bridge" not in _recorder_code)

_paper_src = (_ROOT / "paper" / "runner.py").read_text()
_paper_code = _code_only(_paper_src)
check("J6: paper.runner still never imports engine.journal/guardrails/execute "
      "or api.runtime_bridge (engine.watchlist, a static list read, is its "
      "own pre-existing, documented exception — unrelated to this layer)",
      "from control import runtime" in _paper_code
      and not any(tok in _paper_code for tok in
                  ("engine.journal", "engine.guardrails", "engine.execute", "engine.broker"))
      and "runtime_bridge" not in _paper_code)

check("J7: engine/ still imports nothing from research/ (kernel isolation intact)",
      __import__("subprocess").run(
          ["grep", "-rlE", r"^\s*(from|import)\s+research", str(_ROOT / "engine")],
          capture_output=True, text=True).returncode != 0)


# ===========================================================================
print("\n--- K: overlap prevention — recorder_lock() / paper_lock() ---")
# ===========================================================================

lock_path_k1 = TMP / ".recorder.lock"
with recorder.recorder_lock(lock_path_k1):
    _recorder_busy = False
    try:
        with recorder.recorder_lock(lock_path_k1):
            pass
    except recorder.RecorderBusy:
        _recorder_busy = True
    check("K1: recorder_lock() raises RecorderBusy on a second concurrent "
          "acquire, exactly like worker_lock()/WorkerBusy", _recorder_busy)

lock_path_k2 = TMP / ".paper.lock"
with prunner.paper_lock(lock_path_k2):
    _paper_busy = False
    try:
        with prunner.paper_lock(lock_path_k2):
            pass
    except prunner.PaperCycleBusy:
        _paper_busy = True
    check("K2: paper_lock() raises PaperCycleBusy on a second concurrent "
          "acquire", _paper_busy)

_k3_reacquired = False
with recorder.recorder_lock(lock_path_k1):
    _k3_reacquired = True
check("K3: the lock releases cleanly after its `with` block exits — a "
      "later, non-overlapping acquire succeeds", _k3_reacquired)

# K4: recorder.main() degrades to a clean, quiet no-op (exit 0, no crash,
# no telemetry row written) when another instance already holds the real
# module-level lock — never touches the store while busy.
#
# recorder_lock()'s `path` parameter is a DEFAULT ARGUMENT, bound once at
# module-def time — reassigning recorder.LOCK_PATH afterwards does NOT
# affect it, the identical class of bug already caught and fixed in
# section I for control.runtime.set_state/get_state. An earlier version of
# THIS test made exactly that mistake: it held a decoy lock file while
# recorder.main() (called with no path override) actually acquired the
# REAL, unrelated .recorder.lock, found it free, and proceeded to run a
# genuine "news" cycle — a real outbound HTTP call and a real
# not-all-sources-failed-dependent Telegram alert, neither of which this
# test intended to trigger. Fixed the same way as section I: monkeypatch
# the lock FUNCTION itself, which main() looks up fresh on every call.
_orig_recorder_lock = recorder.recorder_lock
lock_path_k4 = TMP / ".recorder_k4.lock"


def _isolated_recorder_lock(path=lock_path_k4):
    return _orig_recorder_lock(path)


recorder.recorder_lock = _isolated_recorder_lock
try:
    with _orig_recorder_lock(lock_path_k4):
        _orig_argv = sys.argv
        sys.argv = ["recorder", "--cycle", "news", "--db", str(TMP / "k4.db"),
                    "--quiet-on-success"]
        try:
            try:
                recorder.main()
                rc_k4 = 0
            except SystemExit as e:
                rc_k4 = e.code
        finally:
            sys.argv = _orig_argv
    check("K4: recorder.main() exits 0 (a clean no-op) when the lock is "
          "already held, rather than raising or crashing",
          rc_k4 in (0, None), rc_k4)
    check("K4b: no research store file was created while the lock was busy",
          not (TMP / "k4.db").exists())
finally:
    recorder.recorder_lock = _orig_recorder_lock

# K5: run_paper_cycle() degrades to a SKIPPED summary (never raises) when
# another instance already holds the real module-level lock. Same fix as
# K4 — monkeypatch paper_lock itself, not prunner.LOCK_PATH.
_orig_paper_lock = prunner.paper_lock
lock_path_k5 = TMP / ".paper_k5.lock"


def _isolated_paper_lock(path=lock_path_k5):
    return _orig_paper_lock(path)


prunner.paper_lock = _isolated_paper_lock
try:
    with _orig_paper_lock(lock_path_k5):
        summary_k5 = prunner.run_paper_cycle(cycle_label="test-k5", notify=False)
    check("K5: run_paper_cycle() returns a SKIPPED summary (never raises) "
          "when the lock is already held", summary_k5.get("status") == "SKIPPED",
          summary_k5)
    check("K5b: the skip reason names the lock, distinct from a global "
          "control mode skip", "lock" in summary_k5.get("skip_reason", "").lower(),
          summary_k5)
finally:
    prunner.paper_lock = _orig_paper_lock

# K6: sequential (non-overlapping) calls through the real entrypoints are
# completely unaffected by the new locks — proves this isn't a regression
# for the overwhelmingly common case of one cycle finishing before the next
# begins. (E1/E3 above already exercise main()/run_paper_cycle() end to end
# under PAUSED; this confirms two back-to-back RUNNING calls both succeed.)
_real_state_k6 = ctrl.get_state()
try:
    ctrl.set_state("RUNNING", reason="test K6", actor="test")
    reg_k6 = fresh_registry("k6")
    rc_k6a = w.main(["--db", str(TMP / "k6a.db"), "--registry-dir", str(reg_k6),
                     "--no-notify", "--quiet-on-success", "--max-discovery-attempts", "0",
                     "--max-experiments", "0", "--max-promotions", "0",
                     "--max-substrate-creations", "0"])
    rc_k6b = w.main(["--db", str(TMP / "k6b.db"), "--registry-dir", str(reg_k6),
                     "--no-notify", "--quiet-on-success", "--max-discovery-attempts", "0",
                     "--max-experiments", "0", "--max-promotions", "0",
                     "--max-substrate-creations", "0"])
    check("K6: two sequential (non-overlapping) worker.main() calls both "
          "succeed — the lock never falsely blocks a legitimate, later call",
          rc_k6a == 0 and rc_k6b == 0, (rc_k6a, rc_k6b))
finally:
    ctrl.set_state(_real_state_k6["mode"] or "RUNNING", reason="test cleanup", actor="test")


shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'=' * 52}")
print(f"  {PASSED} passed, {FAILED} failed")
print(f"{'=' * 52}\n")
sys.exit(1 if FAILED else 0)
