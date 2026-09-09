"""
Tests for Phase 2 Slice R: Experiment Scheduler v0 (research/brain/scheduler.py).

The scheduler is orchestration only: find already-locked, not-yet-run
Contracts (reusing research.experiments.runner.runnable_contracts()
verbatim), pick a small deterministic bounded subset, and call the
existing, completely unmodified runner.run_experiment() once per selected
contract_id, sequentially. This file does not re-test the runner's own
simulation correctness (that's tests/test_research_runner.py's job) — it
tests only what this slice adds: discovery/exclusion via the runner's own
status semantics, deterministic ordering, bounding, runner delegation
(never re-implemented), partial-failure continuation, and read-only
candidate selection.

Run with:  python -m tests.test_research_scheduler
"""

import re
import sys
import json
import shutil
import hashlib
import tempfile
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from research.store import Store  # noqa: E402
from research.contracts import Contract, REGISTRY_DIR, registry as _registry  # noqa: E402
from research.brain import hypothesis_intake as hi  # noqa: E402
from research.experiments import runner  # noqa: E402
from research.brain import scheduler as sch  # noqa: E402

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


def _code_only(source: str) -> str:
    """Strip every triple-double-quoted docstring, leaving only real code —
    the convention established in test_research_investigator.py and reused
    by every isolation check since."""
    return re.sub(r'"""[\s\S]*?"""', "", source)


TMP = Path(tempfile.mkdtemp(prefix="lq-test-scheduler-"))
START = dt.date(2020, 1, 1)


def fresh_store(name) -> Store:
    p = TMP / f"{name}.db"
    if p.exists():
        p.unlink()
    return Store.open(p)


def fresh_registry(name) -> Path:
    d = TMP / f"registry-{name}"
    if d.exists():
        shutil.rmtree(d)
    return d


def make_locked_contract(cid, *, locked_at=None, status="locked", registry_dir, **overrides):
    """A locked Contract, constructed directly (same make_contract-then-
    lock()-then-mutate-status pattern test_research_digest.py's own
    'previously tested' section already uses) rather than through the full
    create_draft()/approve_and_lock() pipeline — this file is testing the
    SCHEDULER's selection/execution logic, not hypothesis_intake's own
    budget/validation machinery, so the lighter-weight construction is the
    right tool. No price data is seeded for most of these on purpose: with
    no price data, runner.simulate() legitimately returns zero trades and
    run_experiment() still completes as 'reported' — proven directly in
    section A below — so most tests here don't need to seed a market."""
    fields = dict(
        id=cid, title=f"idea {cid}", hypothesis=f"claim behind {cid}",
        null_hypothesis="no effect", universe="watchlist", signal="observatory.volume_zscore",
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


def seed_prices(store, symbol, start, closes, vols):
    for i, (cl, v) in enumerate(zip(closes, vols)):
        d = start + dt.timedelta(days=i)
        store.append_price(symbol=symbol, session_date=d, knowledge_time=d, source="test",
                            open_=cl, high=cl, low=cl, close=cl, volume=v)


def flat_series(n, base_close=100.0, base_vol=100_000, spike_at=None, spike_vol=5_000_000):
    closes = [base_close for _ in range(n)]
    vols = [base_vol + (i % 5) * 1000 for i in range(n)]
    if spike_at is not None:
        vols[spike_at] = spike_vol
    return closes, vols


def make_proposal(**overrides):
    base = {
        "title": "runner smoke test", "hypothesis": "volume spikes precede continuation",
        "null_hypothesis": "no effect", "universe": "watchlist",
        "signal": "observatory.volume_zscore",
        "entry_rule": {"conditions": [{"metric": "volume_zscore", "op": ">", "value": 3.0}]},
        "exit_rule": {"stop_loss_pct": 5.0, "target_pct": 8.0, "max_hold_days": 15},
        "splits": {"discovery": ["2020-01-01", "2020-12-31"]},
        "independence": "one entry per symbol", "falsification": "t_stat < 2.0",
        "abandon_condition": "expectancy_r <= 0",
        "evaluation_start": "2020-01-01", "evaluation_end": "2020-06-30",
    }
    base.update(overrides)
    return base


def locked_contract_via_pipeline(store, reg, **overrides):
    """The FULL pipeline (proposal -> draft -> approve_and_lock), same
    fixture shape as test_research_runner.py's own locked_contract() — used
    once (section A2) to prove the scheduler's delegation produces a
    genuine, realistically-derived REPORTED contract, not just a status
    flip on a synthetically constructed one."""
    proposal = make_proposal(**overrides)
    result = hi.create_draft(store, proposal, registry_dir=reg)
    return hi.approve_and_lock(store, result.contract.id, approved_by="Vaibhav",
                                registry_dir=reg, max_locks=50)


def _registry_fingerprint(reg_dir: Path) -> str:
    h = hashlib.sha256()
    if not reg_dir.exists():
        return h.hexdigest()
    for f in sorted(reg_dir.glob("**/*")):
        if f.is_file():
            h.update(f.name.encode())
            h.update(f.read_bytes())
    return h.hexdigest()


# ---------------------------------------------------------------------------
print("\n--- A: finds locked, unrun Contracts ---")
# ---------------------------------------------------------------------------

store_a = fresh_store("a")
reg_a = fresh_registry("a")

c_a1 = make_locked_contract("EXP-A-1", registry_dir=reg_a)
eligible_a = sch.eligible_contracts(store_a, registry_dir=reg_a)
check("A1: a freshly locked, never-run contract is found",
      [c.id for c in eligible_a] == ["EXP-A-1"])

# no price data at all: run_experiment() still completes as REPORTED with
# zero trades — establishing that most of this file's fixtures don't need
# to seed a market to prove selection/execution-cap/ordering behavior.
result_a = sch.run_scheduler(store_a, registry_dir=reg_a, max_experiments=5)
check("A2: the scheduler actually ran it via the real runner (status now reported)",
      Contract.load("EXP-A-1", reg_a).status == "reported")
check("A2: zero trades is a legitimate, non-error outcome with no seeded price data",
      result_a.outcomes[0].outcome == "reported")

# A3 — full pipeline, real price data, a genuine end-to-end reported run.
store_a3 = fresh_store("a3")
reg_a3 = fresh_registry("a3")
seed_prices(store_a3, "RELIANCE", START, *flat_series(40, spike_at=25))
locked_a3 = locked_contract_via_pipeline(store_a3, reg_a3)
result_a3 = sch.run_scheduler(store_a3, registry_dir=reg_a3, max_experiments=5)
check("A3: a realistic end-to-end locked contract, run through the real "
      "pipeline, is found and executed",
      result_a3.reported_contract_ids == [locked_a3.id])
check("A3: real trades were recorded for the spike (via the genuine runner, "
      "not a scheduler-internal reimplementation)",
      result_a3.outcomes[0].detail != "0 trade(s) recorded.")


# ---------------------------------------------------------------------------
print("\n--- B: ignores drafts ---")
# ---------------------------------------------------------------------------

store_b = fresh_store("b")
reg_b = fresh_registry("b")
draft_b = hi.create_draft(store_b, make_proposal(), registry_dir=reg_b)
check("B: a draft (never locked) does not appear in eligible_contracts()",
      sch.eligible_contracts(store_b, registry_dir=reg_b) == [])
result_b = sch.run_scheduler(store_b, registry_dir=reg_b, max_experiments=5)
check("B: a scheduler run over a registry with only a draft attempts nothing",
      result_b.n_selected == 0 and result_b.n_attempted == 0)
check("B: the draft's status is untouched by a scheduler run",
      Contract.load(draft_b.contract.id, reg_b).status == "draft")


# ---------------------------------------------------------------------------
print("\n--- C: ignores already-reported Contracts ---")
# ---------------------------------------------------------------------------

store_c = fresh_store("c")
reg_c = fresh_registry("c")
make_locked_contract("EXP-C-1", status="reported", registry_dir=reg_c)
make_locked_contract("EXP-C-2", status="locked", registry_dir=reg_c)
check("C: a reported contract is excluded; an actually-locked one is included "
      "— verified against runner.runnable_contracts()'s own existing "
      "status filter, not a scheduler-invented one",
      [c.id for c in sch.eligible_contracts(store_c, registry_dir=reg_c)] == ["EXP-C-2"]
      == [c.id for c in runner.runnable_contracts(reg_c)])


# ---------------------------------------------------------------------------
print("\n--- D: ignores abandoned/terminal Contracts (per existing runner semantics) ---")
# ---------------------------------------------------------------------------

store_d = fresh_store("d")
reg_d = fresh_registry("d")
for i, status in enumerate(("abandoned", "reported", "superseded", "running")):
    make_locked_contract(f"EXP-D-{i}", status=status, registry_dir=reg_d)
make_locked_contract("EXP-D-OK", status="locked", registry_dir=reg_d)

eligible_d = sch.eligible_contracts(store_d, registry_dir=reg_d)
check("D: every terminal/in-flight status (abandoned/reported/superseded/"
      "running) is excluded — exactly runner.runnable_contracts()'s own "
      "'status == locked, exactly' rule, not a new policy",
      [c.id for c in eligible_d] == ["EXP-D-OK"])
check("D: this matches runner.runnable_contracts() called directly, "
      "byte for byte",
      [c.id for c in eligible_d] == [c.id for c in runner.runnable_contracts(reg_d)])


# ---------------------------------------------------------------------------
print("\n--- E: deterministic selection order ---")
# ---------------------------------------------------------------------------

store_e = fresh_store("e")
reg_e = fresh_registry("e")
# Deliberately scrambled locked_at values and a tie, to prove the ORDER
# reflects locked_at (oldest first) + contract_id tie-break, not creation/
# glob order.
make_locked_contract("EXP-E-D", locked_at="2024-01-03T00:00:00", registry_dir=reg_e)
make_locked_contract("EXP-E-B", locked_at="2024-01-01T00:00:00", registry_dir=reg_e)
make_locked_contract("EXP-E-C", locked_at="2024-01-02T00:00:00", registry_dir=reg_e)
make_locked_contract("EXP-E-A", locked_at="2024-01-01T00:00:00", registry_dir=reg_e)  # ties EXP-E-B

order1 = [c.id for c in sch.eligible_contracts(store_e, registry_dir=reg_e)]
order2 = [c.id for c in sch.eligible_contracts(store_e, registry_dir=reg_e)]
check("E: two calls against an unchanged registry return identical order",
      order1 == order2)
check("E: oldest locked_at first, with a stable contract_id tie-break "
      "(EXP-E-A ties EXP-E-B on locked_at and sorts first alphabetically)",
      order1 == ["EXP-E-A", "EXP-E-B", "EXP-E-C", "EXP-E-D"], order1)


# ---------------------------------------------------------------------------
print("\n--- F: execution cap ---")
# ---------------------------------------------------------------------------

store_f = fresh_store("f")
reg_f = fresh_registry("f")
for i in range(10):
    make_locked_contract(f"EXP-F-{i:02d}", locked_at=f"2024-01-{i+1:02d}T00:00:00",
                          registry_dir=reg_f)

check("F: 10 eligible contracts exist", len(sch.eligible_contracts(store_f, registry_dir=reg_f)) == 10)
result_f = sch.run_scheduler(store_f, registry_dir=reg_f, max_experiments=2)
check("F: exactly 2 are selected", result_f.n_selected == 2)
check("F: exactly 2 are attempted", result_f.n_attempted == 2)
check("F: the other 8 remain pending (never attempted this run)",
      len(result_f.pending_contract_ids) == 8)
check("F: pending + attempted accounts for every eligible contract",
      set(result_f.pending_contract_ids) | set(result_f.attempted_contract_ids)
      == set(result_f.eligible_contract_ids))


# ---------------------------------------------------------------------------
print("\n--- G: runner delegation — the scheduler calls the existing "
      "runner.run_experiment(), it does not reimplement execution ---")
# ---------------------------------------------------------------------------

SCHED_SRC = Path("research/brain/scheduler.py").read_text()
SCHED_BODY = _code_only(SCHED_SRC)
check("G: scheduler.py never defines its own simulate(",
      "def simulate(" not in SCHED_BODY)
check("G: scheduler.py never calls entry_signal( or _metric_value( directly "
      "(no reimplemented signal evaluation)",
      "entry_signal(" not in SCHED_BODY and "_metric_value(" not in SCHED_BODY)
check("G: scheduler.py does not import research.replay (no direct market "
      "replay — that stays entirely inside the runner)",
      "from ..replay" not in SCHED_SRC and "from research.replay" not in SCHED_SRC)
check("G: scheduler.py DOES call the real runner.run_experiment(",
      "runner.run_experiment(" in SCHED_BODY)

store_g = fresh_store("g")
reg_g = fresh_registry("g")
for i in range(3):
    make_locked_contract(f"EXP-G-{i}", locked_at=f"2024-02-0{i+1}T00:00:00", registry_dir=reg_g)

calls = []
real_run_experiment = sch.runner.run_experiment


def spy_run_experiment(contract_id, store, *, registry_dir=REGISTRY_DIR):
    calls.append((contract_id, store, registry_dir))
    return {"status": "reported", "contract_id": contract_id, "n_trades": 0, "verdict": {}}


try:
    sch.runner.run_experiment = spy_run_experiment
    result_g = sch.run_scheduler(store_g, registry_dir=reg_g, max_experiments=3)
finally:
    sch.runner.run_experiment = real_run_experiment

check("G: the scheduler called runner.run_experiment() exactly once per "
      "selected contract, in the deterministic selection order",
      [c[0] for c in calls] == result_g.selected_contract_ids
      == ["EXP-G-0", "EXP-G-1", "EXP-G-2"])
check("G: each call was passed the SAME store object the scheduler itself "
      "received, not a copy or a new one", all(c[1] is store_g for c in calls))
check("G: each call was passed the scheduler's own registry_dir",
      all(c[2] == reg_g for c in calls))


# ---------------------------------------------------------------------------
print("\n--- H: partial failure — one failure doesn't block subsequent bounded runs ---")
# ---------------------------------------------------------------------------

store_h = fresh_store("h")
reg_h = fresh_registry("h")
for i in range(3):
    make_locked_contract(f"EXP-H-{i}", locked_at=f"2024-03-0{i+1}T00:00:00", registry_dir=reg_h)


def failing_first_run_experiment(contract_id, store, *, registry_dir=REGISTRY_DIR):
    if contract_id == "EXP-H-0":
        raise runner.ExperimentFailed(f"{contract_id} failed and was marked abandoned: boom")
    return {"status": "reported", "contract_id": contract_id, "n_trades": 0, "verdict": {}}


try:
    sch.runner.run_experiment = failing_first_run_experiment
    result_h = sch.run_scheduler(store_h, registry_dir=reg_h, max_experiments=3)
finally:
    sch.runner.run_experiment = real_run_experiment

check("H: all 3 selected contracts were still attempted despite the first failing",
      result_h.n_attempted == 3)
check("H: the failing contract is recorded as abandoned",
      result_h.abandoned_contract_ids == ["EXP-H-0"])
check("H: the remaining two still succeeded and are recorded as reported",
      result_h.reported_contract_ids == ["EXP-H-1", "EXP-H-2"])
check("H: nothing is pending — the bounded run completed fully",
      result_h.pending_contract_ids == [])
check("H: stopped_early is False — ExperimentFailed is a known-safe, "
      "continuable outcome, not a halt condition",
      result_h.stopped_early is False and result_h.error is None)

# H2 — an unexpected (non-runner) exception DOES stop further selection,
# but the run still returns everything recorded up to that point.
store_h2 = fresh_store("h2")
reg_h2 = fresh_registry("h2")
for i in range(3):
    make_locked_contract(f"EXP-H2-{i}", locked_at=f"2024-03-1{i+1}T00:00:00", registry_dir=reg_h2)


def weird_failure_run_experiment(contract_id, store, *, registry_dir=REGISTRY_DIR):
    if contract_id == "EXP-H2-0":
        raise RuntimeError("something outside the runner's own safety net")
    return {"status": "reported", "contract_id": contract_id, "n_trades": 0, "verdict": {}}


try:
    sch.runner.run_experiment = weird_failure_run_experiment
    result_h2 = sch.run_scheduler(store_h2, registry_dir=reg_h2, max_experiments=3)
finally:
    sch.runner.run_experiment = real_run_experiment

check("H2: an exception type the runner itself never documents halts "
      "further selection (stopped_early=True)", result_h2.stopped_early is True)
check("H2: the error is captured, not swallowed", result_h2.error is not None
      and "RuntimeError" in result_h2.error)
check("H2: only the one contract that raised was attempted",
      result_h2.attempted_contract_ids == ["EXP-H2-0"])
check("H2: the other two remain pending rather than silently skipped",
      result_h2.pending_contract_ids == ["EXP-H2-1", "EXP-H2-2"])


# ---------------------------------------------------------------------------
print("\n--- I: no path to auto-locking ---")
# ---------------------------------------------------------------------------

check("I: scheduler.py never CALLS create_draft(", "create_draft(" not in SCHED_BODY)
check("I: scheduler.py never CALLS approve_and_lock(", "approve_and_lock(" not in SCHED_BODY)
check("I: scheduler.py never calls Contract.lock() (or any .lock() call)",
      ".lock(" not in SCHED_BODY)
check("I: scheduler.py never imports hypothesis_intake at all",
      not re.search(r"^\s*(?:from|import)\s+.*hypothesis_intake", SCHED_SRC, re.MULTILINE))
check("I: scheduler.py never imports the Research AI investigator",
      not re.search(r"^\s*(?:from|import)\s+.*investigator", SCHED_SRC, re.MULTILINE))


# ---------------------------------------------------------------------------
print("\n--- J: no live-engine access ---")
# ---------------------------------------------------------------------------

check("J: scheduler.py never imports an engine module",
      not re.search(r"^\s*(?:from|import)\s+engine\b", SCHED_SRC, re.MULTILINE))
check("J: scheduler.py never imports a broker module",
      not re.search(r"^\s*(?:from|import)\s+broker\w*", SCHED_SRC, re.MULTILINE))
check("J: scheduler.py's code (outside prose/docstrings) never references "
      "memory/state.json", "state.json" not in SCHED_BODY)
check("J: scheduler.py's code (outside prose/docstrings) never references "
      "broker credentials", "credential" not in SCHED_BODY.lower())


# ---------------------------------------------------------------------------
print("\n--- K: read-only candidate selection ---")
# ---------------------------------------------------------------------------

store_k = fresh_store("k")
reg_k = fresh_registry("k")
for i in range(4):
    make_locked_contract(f"EXP-K-{i}", locked_at=f"2024-04-0{i+1}T00:00:00", registry_dir=reg_k)

before_fp = _registry_fingerprint(reg_k)
before_statuses = {c.id: c.status for c in _registry(reg_k)}

_ = sch.eligible_contracts(store_k, registry_dir=reg_k)
_ = sch.select_experiments(store_k, registry_dir=reg_k, max_experiments=2)
_ = sch.eligible_contracts(store_k, registry_dir=reg_k)

after_fp = _registry_fingerprint(reg_k)
after_statuses = {c.id: c.status for c in _registry(reg_k)}

check("K: registry files are byte-for-byte unchanged after selecting candidates",
      before_fp == after_fp)
check("K: no Contract's status changed merely from being selected",
      before_statuses == after_statuses)


# ---------------------------------------------------------------------------
print("\n====================================================")
print(f"  {PASSED} passed, {FAILED} failed")
print("====================================================")
sys.exit(1 if FAILED else 0)
