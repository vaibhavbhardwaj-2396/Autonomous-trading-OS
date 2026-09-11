"""
tests/test_research_worker_selection.py — Unified Opportunity-Driven Worker
Selection (research/brain/opportunity.build_action_queue,
research/brain/worker.run_worker_cycle's rewritten unified action loop).

The Control Plane slice (tests/test_research_opportunity.py,
tests/test_research_worker.py §R) already proved the opportunity pool,
priority model, non-terminal rejection, autonomous promotion and user
override model in isolation. This file proves the NEW thing this slice
adds: RUN_EXPERIMENT / PROMOTE / DISCOVER competing on ONE scale, so a new
discovery attempt does not automatically run just because it is discovery —
mirroring the required test matrix from the slice's own spec:

  A. discovery competes directly with an already-locked experiment
  B. a high-priority robustness test beats a lower-priority discovery
  C. a rejected hypothesis re-enters and beats discovery once evidence
     makes it valuable again
  D. an executed action's result changes the very next selection
  E. multiple actions run within one runtime envelope
  F. the worker stops cleanly at its runtime budget, with more work left
  G. no infinite loop, even with abundant available work
  I. a duplicate/low-information action is deprioritized, not merely absent
  J/K. frozen/retired opportunities are unselectable, not just deprioritized
  L. portfolio_relevance stays unfabricated (None)
  M. isolation is unchanged by this slice
  N. the full legacy suites stay green (asserted by the harness that runs
     this file alongside tests/test_research_worker.py and
     tests/test_research_opportunity.py, not re-proven here)

Run with:  python -m tests.test_research_worker_selection
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from research.store import Store, now_ist  # noqa: E402
from research.contracts import Contract  # noqa: E402
from research.brain import hypothesis_intake as hi  # noqa: E402
from research.brain import opportunity as opp  # noqa: E402
from research.brain import worker as w  # noqa: E402
from research.brain import research_areas  # noqa: E402
from research.experiments import evaluator  # noqa: E402

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


TMP = Path(tempfile.mkdtemp(prefix="lq-test-worker-selection-"))


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


def make_proposal(**overrides) -> dict:
    base = {
        "title": "Volume-spike drift",
        "hypothesis": "High-volume anomalies precede a short-term upward drift.",
        "null_hypothesis": "No relationship.",
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


def draft(store, reg, **overrides):
    result = hi.create_draft(store, make_proposal(**overrides), registry_dir=reg)
    return result.hypothesis_id, result.contract.id


def lock(store, reg, contract_id, *, approver="test_human"):
    return hi.approve_and_lock(store, contract_id, approved_by=approver, registry_dir=reg)


def winning_trades(n=12):
    base_pnl = [420, 380, 440, 360, 410, 390, 430, 370, 400, 415, 385, 405]
    base_r = [2.1, 1.9, 2.2, 1.8, 2.05, 1.95, 2.15, 1.85, 2.0, 2.05, 1.95, 2.0]
    return list(zip(base_pnl[:n], base_r[:n]))


def losing_trades(n=12):
    return [(-10.0, -0.5)] * n


def score_it(store, reg, contract_id, *, trades):
    """Push synthetic experiment_results + record a verdict — the same two
    steps runner.run_experiment() performs, done directly so tests control
    the exact win/loss shape. UNLIKE a real run_experiment() call, this does
    NOT itself flip the Contract's own status to "reported" — do that
    explicitly (see `mark_reported` below) whenever a test's fixture also
    cares about scheduler.eligible_contracts()/build_action_queue(), which
    key off status=="locked", not off whether a verdict exists."""
    for i, (net_pnl, r_mult) in enumerate(trades):
        store.append_experiment_result(
            contract_id=contract_id, trade_seq=i, entity="TESTSYM",
            entry_time="2020-01-01", exit_time="2020-01-05",
            entry_price=100.0, exit_price=100.0 + net_pnl, quantity=1,
            gross_pnl=net_pnl, costs=0.0, net_pnl=net_pnl, r_multiple=r_mult,
            exit_reason="target",
        )
    return evaluator.record_verdict(store, contract_id)


def mark_reported(reg, contract_id):
    """Reflect what a real runner.run_experiment() call would already have
    done to the Contract's own status — needed after score_it() so a
    scored-but-fixture-shortcut contract does not also, incorrectly, keep
    showing up as still "locked and runnable" to scheduler.eligible_contracts()."""
    c = Contract.load(contract_id, reg)
    c.status = "reported"
    c.save(reg)


def make_bare_locked_contract(cid, *, registry_dir, locked_at="2024-01-01T00:00:00"):
    """A locked Contract with NO research.memory hypothesis-claim row at all
    — exercises build_action_queue's fallback-scoring path (the same shape
    tests/test_research_worker.py's own make_locked_contract fixture uses)."""
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


# ---------------------------------------------------------------------------
print("\n--- A: discovery competes directly with an already-locked experiment ---")
# ---------------------------------------------------------------------------

s_a = fresh_store("a")
reg_a = fresh_registry("a")
make_bare_locked_contract("EXP-A-0", registry_dir=reg_a)
pool_a = opp.build_opportunity_pool(s_a, "2024-05-12", registry_dir=reg_a)
queue_a = opp.build_action_queue(s_a, pool_a, registry_dir=reg_a, discovery_available=True)
kinds_a = {a.kind for a in queue_a}
check("A1: both RUN_EXPERIMENT and DISCOVER are present in the SAME action queue",
      {"RUN_EXPERIMENT", "DISCOVER"} <= kinds_a, str(kinds_a))
check("A2: every action in the queue is comparable on ONE numeric priority_score",
      all(isinstance(a.priority_score, float) for a in queue_a))
check("A3: the queue is sorted strictly by descending priority_score",
      all(queue_a[i].priority_score >= queue_a[i + 1].priority_score
          for i in range(len(queue_a) - 1)), str([a.priority_score for a in queue_a]))
check("A4: the top of the queue is genuinely the max-scoring action, regardless of kind",
      queue_a[0].priority_score == max(a.priority_score for a in queue_a))


# ---------------------------------------------------------------------------
print("\n--- B: a high-priority robustness test beats a lower-priority discovery ---")
# ---------------------------------------------------------------------------

s_b = fresh_store("b")
reg_b = fresh_registry("b")
hid_b, cid_b = draft(s_b, reg_b, title="Parent idea (about to be promising)",
                     splits={"discovery": ["2019-01-01", "2021-12-31"],
                             "validation": ["2022-01-01", "2022-12-31"]})
lock(s_b, reg_b, cid_b)
score_it(s_b, reg_b, cid_b, trades=winning_trades())  # PROMISING
mark_reported(reg_b, cid_b)  # the parent is no longer "locked and runnable"

deriv_b = hi.derive_split_contract(s_b, cid_b, "validation", hid_b, registry_dir=reg_b)
lock(s_b, reg_b, deriv_b.contract.id)  # LOCKED, not yet run — the robustness test itself

pool_b = opp.build_opportunity_pool(s_b, "2024-05-12", registry_dir=reg_b)
o_b = [o for o in pool_b if o.hypothesis_id == hid_b][0]
check("B1: the parent hypothesis is now typed ROBUSTNESS_TEST (a validation sibling "
      "is locked, waiting to run, against an already-PROMISING parent)",
      o_b.type == "ROBUSTNESS_TEST", o_b.type)

queue_b = opp.build_action_queue(s_b, pool_b, registry_dir=reg_b, discovery_available=True)
run_exp_b = [a for a in queue_b if a.kind == "RUN_EXPERIMENT" and a.contract_id == deriv_b.contract.id]
discover_b = [a for a in queue_b if a.kind == "DISCOVER"]
check("B2: the robustness-test RUN_EXPERIMENT action exists in the queue",
      len(run_exp_b) == 1, str(queue_b))
check("B3: a synthetic DISCOVER action also exists (discovery genuinely available)",
      len(discover_b) == 1)
check("B4: the robustness test's priority is strictly HIGHER than discovery's baseline",
      run_exp_b[0].priority_score > discover_b[0].priority_score,
      (run_exp_b[0].priority_score, discover_b[0].priority_score))
check("B5: the robustness test is ranked BEFORE discovery in the sorted queue",
      queue_b.index(run_exp_b[0]) < queue_b.index(discover_b[0]))
check("B6: the top of the queue is the robustness test, not discovery",
      queue_b[0].kind == "RUN_EXPERIMENT" and queue_b[0].contract_id == deriv_b.contract.id)


# ---------------------------------------------------------------------------
print("\n--- C: a rejected hypothesis re-enters and beats discovery once evidence "
      "makes it valuable again ---")
# ---------------------------------------------------------------------------

s_c = fresh_store("c")
reg_c = fresh_registry("c")
hid_c, cid_c = draft(s_c, reg_c, title="Weak idea")
lock(s_c, reg_c, cid_c)
score_it(s_c, reg_c, cid_c, trades=losing_trades())  # WEAK -> REJECTED
mark_reported(reg_c, cid_c)

# baseline: plain REJECTED, no reassessment trigger yet
pool_c0 = opp.build_opportunity_pool(s_c, "2024-05-12", registry_dir=reg_c)
o_c0 = [o for o in pool_c0 if o.hypothesis_id == hid_c][0]
check("C0: freshly WEAK, with nothing to compare against, it is plain REJECTED",
      o_c0.lifecycle_stage == "REJECTED", o_c0.lifecycle_stage)

# a sibling in the same research area turns PROMISING -> a non-calendar
# reassessment trigger (the exact Master Vision example: "related strategy succeeds")
research_areas.tag_hypothesis(s_c, hypothesis_id=hid_c, research_area="momentum", source="test")
hid_sib_c, cid_sib_c = draft(
    s_c, reg_c, title="Sibling idea", signal="observatory.price_move_zscore",
    entry_rule={"conditions": [{"metric": "price_move_zscore", "op": ">", "value": 2.0}]})
research_areas.tag_hypothesis(s_c, hypothesis_id=hid_sib_c, research_area="momentum", source="test")
lock(s_c, reg_c, cid_sib_c)
score_it(s_c, reg_c, cid_sib_c, trades=winning_trades())  # PROMISING
mark_reported(reg_c, cid_sib_c)

# and a NEW, already-locked retest variant now exists for the rejected
# hypothesis — the concrete, runnable substrate the reassessment can act on
_, cid_retest = draft(
    s_c, reg_c, hypothesis_id=hid_c, title="Retest of the weak idea, wider stop",
    entry_rule={"conditions": [{"metric": "volume_zscore", "op": ">", "value": 3.5}]})
lock(s_c, reg_c, cid_retest)  # LOCKED, not yet run

pool_c = opp.build_opportunity_pool(s_c, "2024-05-12", registry_dir=reg_c)
o_c = [o for o in pool_c if o.hypothesis_id == hid_c][0]
check("C1: the rejected hypothesis is now REASSESSING — value-driven, not calendar-driven",
      o_c.lifecycle_stage == "REASSESSING", o_c.lifecycle_stage)
check("C2: its priority score rose relative to when it was plain REJECTED",
      o_c.priority_score > o_c0.priority_score, (o_c.priority_score, o_c0.priority_score))

queue_c = opp.build_action_queue(s_c, pool_c, registry_dir=reg_c, discovery_available=True)
run_exp_c = [a for a in queue_c if a.kind == "RUN_EXPERIMENT" and a.contract_id == cid_retest]
discover_c = [a for a in queue_c if a.kind == "DISCOVER"]
check("C3: the retest RUN_EXPERIMENT action for the reassessment-eligible hypothesis "
      "is in the queue, typed REASSESS_REJECTED",
      len(run_exp_c) == 1 and run_exp_c[0].priority_components is not None, str(queue_c))
check("C3b: the underlying opportunity is genuinely typed REASSESS_REJECTED",
      o_c.type == "REASSESS_REJECTED", o_c.type)
check("C4: its priority beats the synthetic discovery baseline",
      run_exp_c[0].priority_score > discover_c[0].priority_score,
      (run_exp_c[0].priority_score, discover_c[0].priority_score))
check("C5: it is ranked ahead of discovery in the sorted queue — a rejected "
      "hypothesis, made valuable again, beats a brand-new discovery attempt",
      queue_c.index(run_exp_c[0]) < queue_c.index(discover_c[0]))


# ---------------------------------------------------------------------------
print("\n--- D/E: an executed action's result changes the very next selection; "
      "multiple actions run within one runtime envelope ---")
# ---------------------------------------------------------------------------

s_de = fresh_store("de")
reg_de = fresh_registry("de")
st_de = fresh_state("de")
raw_de = json.dumps(make_proposal(title="Fresh idea from discovery"))
lim_de = w.WorkerLimits(max_discovery_attempts=1, cooldown_seconds=0)  # max_promotions/experiments default 1
r_de = w.run_worker_cycle(s_de, now_ist(), limits=lim_de, registry_dir=reg_de,
                          runner=lambda p: raw_de, state_path=st_de)
check("D1: the heartbeat ran more than one action (discover, then promote, "
      "then — because promoting just made a new experiment eligible — run it)",
      len(r_de.action_log) >= 3, str(r_de.action_log))
kinds_de = [a["kind"] for a in r_de.action_log]
check("D2: the sequence is DISCOVER -> PROMOTE -> RUN_EXPERIMENT — each step only "
      "became possible because of the one before it",
      kinds_de[:3] == ["DISCOVER", "PROMOTE", "RUN_EXPERIMENT"], str(kinds_de))
check("D3: the RUN_EXPERIMENT action's contract is exactly the one PROMOTE just locked "
      "— its eligibility did not exist before that promotion happened",
      r_de.action_log[2]["contract_id"] == r_de.action_log[1]["contract_id"], str(r_de.action_log))
check("D4: the PROMOTE action's own after-state was back-filled once the pool was "
      "rebuilt for the next iteration — lifecycle really did change, observably",
      r_de.action_log[1]["lifecycle_before"] == "DISCOVERED"
      and r_de.action_log[1]["lifecycle_after"] == "TESTING", str(r_de.action_log[1]))
check("E1: multiple DIFFERENT kinds of action ran within the SAME runtime envelope",
      len(set(kinds_de)) >= 2, str(kinds_de))
check("E2: every action_log entry carries its own priority/compute-cost/outcome — "
      "explainable, not just a count",
      all({"priority_score", "priority_components", "compute_cost_estimate", "outcome"} <= set(a)
          for a in r_de.action_log))


# ---------------------------------------------------------------------------
print("\n--- F: the worker stops cleanly at its runtime budget, with eligible "
      "work still left ---")
# ---------------------------------------------------------------------------

s_f = fresh_store("f")
reg_f = fresh_registry("f")
st_f = fresh_state("f")
for i in range(5):
    make_bare_locked_contract(f"EXP-F-{i}", registry_dir=reg_f, locked_at=f"2024-03-0{i+1}T00:00:00")


def _fake_clock():
    """A deterministic, hand-advanced clock — 5 seconds per call — so the
    runtime budget is exhausted after a KNOWN, reproducible number of
    actions, never by real wall-clock flakiness."""
    state = {"t": 0.0}

    def _tick():
        state["t"] += 5.0
        return state["t"]
    return _tick


lim_f = w.WorkerLimits(max_experiments=5, max_discovery_attempts=0, max_promotions=0,
                       max_runtime_seconds=22.0, cooldown_seconds=0)
r_f = w.run_worker_cycle(s_f, now_ist(), limits=lim_f, registry_dir=reg_f,
                         runner=lambda p: "unused", state_path=st_f, now_fn=_fake_clock())
check("F1: the worker did NOT run all 5 available experiments — it stopped at its budget",
      0 < r_f.experiments_run < 5, str(r_f))
check("F2: it exited cleanly (no errors) rather than hanging or crashing",
      r_f.errors == [], str(r_f.errors))
check("F3: the run genuinely completed (returned), proving the budget check is honoured "
      "even with MORE eligible work available than the budget allows",
      isinstance(r_f, w.WorkerRunResult))


# ---------------------------------------------------------------------------
print("\n--- G: no infinite loop, even with abundant available work ---")
# ---------------------------------------------------------------------------

s_g = fresh_store("g")
reg_g = fresh_registry("g")
st_g = fresh_state("g")
N_G = 15
for i in range(N_G):
    make_bare_locked_contract(f"EXP-G-{i:02d}", registry_dir=reg_g, locked_at=f"2024-04-{i+1:02d}T00:00:00")
lim_g = w.WorkerLimits(max_experiments=N_G, max_discovery_attempts=0, max_promotions=0, cooldown_seconds=0)
_t_start = time.time()
r_g = w.run_worker_cycle(s_g, now_ist(), limits=lim_g, registry_dir=reg_g,
                         runner=lambda p: "unused", state_path=st_g)
_elapsed = time.time() - _t_start
check(f"G1: with {N_G} eligible experiments, the cycle terminates (does not hang) "
      f"— took {_elapsed:.2f}s", _elapsed < 60.0, f"{_elapsed}s")
check("G2: it ran exactly N_G experiments — every one, then stopped (no infinite re-selection)",
      r_g.experiments_run == N_G, str(r_g.experiments_run))
check("G3: no defensive iteration-safety-cap error was needed for ordinary operation",
      not any("iteration safety cap" in e for e in r_g.errors), str(r_g.errors))


# ---------------------------------------------------------------------------
print("\n--- I: a duplicate action is deprioritized relative to an equivalent "
      "non-duplicate one ---")
# ---------------------------------------------------------------------------

s_i = fresh_store("i")
reg_i = fresh_registry("i")
hid_i1, cid_i1 = draft(s_i, reg_i, title="Original rule")
lock(s_i, reg_i, cid_i1)
hid_i2, cid_i2 = draft(s_i, reg_i, title="Exact same rule, different title")  # same entry/exit_rule
lock(s_i, reg_i, cid_i2)

pool_i = opp.build_opportunity_pool(s_i, "2024-05-12", registry_dir=reg_i)
o_i1 = [o for o in pool_i if o.hypothesis_id == hid_i1][0]
o_i2 = [o for o in pool_i if o.hypothesis_id == hid_i2][0]
check("I1: both siblings sharing an identical rule spec are flagged as duplicates",
      o_i1.is_duplicate and o_i2.is_duplicate, (o_i1.is_duplicate, o_i2.is_duplicate))
check("I2: the novelty component is zeroed for a duplicate — the mechanism behind "
      "deprioritization, not exclusion",
      o_i1.priority_components["novelty"] == 0.0)

# a genuinely unique third hypothesis, otherwise identical treatment
hid_i3, cid_i3 = draft(s_i, reg_i, title="Unique rule", signal="observatory.price_move_zscore",
                       entry_rule={"conditions": [{"metric": "price_move_zscore", "op": ">", "value": 5.0}]})
lock(s_i, reg_i, cid_i3)
pool_i2 = opp.build_opportunity_pool(s_i, "2024-05-12", registry_dir=reg_i)
o_i3 = [o for o in pool_i2 if o.hypothesis_id == hid_i3][0]
o_i1b = [o for o in pool_i2 if o.hypothesis_id == hid_i1][0]
check("I3: the unique hypothesis is NOT flagged as a duplicate", not o_i3.is_duplicate)
check("I4: its priority score is strictly HIGHER than an otherwise-equivalent duplicate's "
      "— more useful information is rewarded, not merely 'more experiments'",
      o_i3.priority_score > o_i1b.priority_score, (o_i3.priority_score, o_i1b.priority_score))


# ---------------------------------------------------------------------------
print("\n--- J/K: frozen and retired opportunities are UNSELECTABLE, not merely "
      "deprioritized — user override remains authoritative ---")
# ---------------------------------------------------------------------------

s_jk = fresh_store("jk")
reg_jk = fresh_registry("jk")
hid_jk, cid_jk = draft(s_jk, reg_jk, title="About to be frozen")
opp.freeze(s_jk, f"OPP-{hid_jk}", by="vaibhav", reason="hold off for now")

pool_jk = opp.build_opportunity_pool(s_jk, "2024-05-12", registry_dir=reg_jk)
queue_jk = opp.build_action_queue(s_jk, pool_jk, registry_dir=reg_jk, discovery_available=True)
check("J1: no PROMOTE action exists anywhere in the queue for the frozen opportunity — "
      "unselectable, not just zero-scored",
      not any(a.opportunity_id == f"OPP-{hid_jk}" for a in queue_jk), str(queue_jk))

opp.reopen(s_jk, f"OPP-{hid_jk}", by="vaibhav", reason="changed my mind")
pool_jk2 = opp.build_opportunity_pool(s_jk, "2024-05-12", registry_dir=reg_jk)
queue_jk2 = opp.build_action_queue(s_jk, pool_jk2, registry_dir=reg_jk, discovery_available=True)
check("K1: after reopen(), the SAME opportunity is selectable again — the user's own "
      "override, not the AI's, decides this",
      any(a.opportunity_id == f"OPP-{hid_jk}" for a in queue_jk2), str(queue_jk2))


# ---------------------------------------------------------------------------
print("\n--- L: portfolio_relevance stays unfabricated ---")
# ---------------------------------------------------------------------------

check("L1: every opportunity in a real pool still reports portfolio_relevance=None "
      "— the field exists for a future Portfolio Engine slice, nothing fakes it here",
      all(o.portfolio_relevance is None for o in pool_i2 + pool_c + pool_b))


# ---------------------------------------------------------------------------
print("\n--- M: isolation is unchanged by this slice ---")
# ---------------------------------------------------------------------------

_opp_src = (Path(__file__).parent.parent / "research" / "brain" / "opportunity.py").read_text()
import re as _re  # noqa: E402
_opp_code = _re.sub(r'"""[\s\S]*?"""', "", _opp_src)
_opp_imports = _re.findall(r"^\s*(?:from|import)\s+([.\w]+)", _opp_code, _re.MULTILINE)
check("M1: opportunity.py (with build_action_queue) still imports no engine/paper module",
      not any(m.split(".")[0] in ("engine", "paper") for m in _opp_imports), str(_opp_imports))
check("M2: opportunity.py still never imports research.experiments.runner directly",
      "experiments.runner" not in _opp_code and "from ..experiments import runner" not in _opp_code)

_w_src = (Path(__file__).parent.parent / "research" / "brain" / "worker.py").read_text()
_w_code = _re.sub(r'"""[\s\S]*?"""', "", _w_src)
check("M3: worker.py still never imports research.experiments.runner directly "
      "(RUN_EXPERIMENT dispatch stays entirely inside scheduler.run_one_experiment)",
      "experiments.runner" not in _w_code and "from ..experiments import runner" not in _w_code)
check("M4: worker.py still never calls the locking primitive directly",
      "approve_and_lock(" not in _w_code and ".lock()" not in _w_code)


shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'=' * 52}")
print(f"  {PASSED} passed, {FAILED} failed")
print(f"{'=' * 52}\n")
sys.exit(1 if FAILED else 0)
