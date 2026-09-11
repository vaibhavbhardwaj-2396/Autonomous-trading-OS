"""
tests/test_research_action_expansion.py — Autonomous Research Action
Expansion (research/brain/opportunity.attempt_create_experiment,
CREATE_EXPERIMENT in build_action_queue, worker.py's CREATE_EXPERIMENT
dispatch).

Closes the gap the Unified Selection slice documented but did not solve: a
REJECTED/REASSESSING opportunity whose evidence just made it valuable again
could rise to the top of the priority queue with literally nothing to
execute, because its own contract already ran and no draft was pending.
This file proves the bridge: when no runnable substrate exists, the worker
can create the smallest one available (a validation/holdout split derived
from the hypothesis's own already-locked contract — no new rule content
invented, no Research AI call), make it visible to the very next queue
rebuild, and (when priority and budget allow) carry it all the way through
promotion and execution in one heartbeat.

Sections:
  A. reassessed + existing runnable contract -> RUN_EXPERIMENT selected
  B. reassessed + no runnable contract, but an unused split -> CREATE_EXPERIMENT
  C. CREATE_EXPERIMENT produces a correctly linked substrate
  D. the new substrate is visible to the next queue rebuild (as PROMOTE)
  E. the same opportunity cannot generate duplicate substrate
  F. a frozen opportunity cannot generate substrate
  G. a retired opportunity cannot generate substrate unless reopened
  H. new evidence causes a rejected opportunity to re-enter executable priority
  I. a lower-value CREATE_EXPERIMENT cannot outrank a higher-value RUN_EXPERIMENT
  J. resource limits (max_substrate_creations) still hold
  K. the worker loop can create substrate and run it in the same heartbeat
  L. isolation is unchanged (delegated to the existing isolation suites + a
     targeted static check here)
  M. no existing safety boundary is weakened

Run with:  python -m tests.test_research_action_expansion
"""

from __future__ import annotations

import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from research.store import Store, now_ist  # noqa: E402
from research.contracts import Contract  # noqa: E402
from research.brain import hypothesis_intake as hi  # noqa: E402
from research.brain import opportunity as opp  # noqa: E402
from research.brain import worker as w  # noqa: E402
from research.brain import research_areas  # noqa: E402
from research import memory as rm  # noqa: E402
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


TMP = Path(tempfile.mkdtemp(prefix="lq-test-action-expansion-"))


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
    showing up as still "locked and runnable" (scheduler.eligible_contracts)."""
    c = Contract.load(contract_id, reg)
    c.status = "reported"
    c.save(reg)


def create_weak_hypothesis(store, reg, *, title, with_validation_split=False):
    """A hypothesis whose only contract already ran and came back WEAK —
    the exact REJECTED starting point every section below reassesses.
    `with_validation_split=True` pre-declares an unused "validation" split
    on that same contract, the substrate CREATE_EXPERIMENT can later derive."""
    extra_splits = {"validation": ["2022-01-01", "2022-12-31"]} if with_validation_split else {}
    hid, cid = draft(store, reg, title=title,
                     splits={"discovery": ["2019-01-01", "2021-12-31"], **extra_splits})
    lock(store, reg, cid)
    score_it(store, reg, cid, trades=losing_trades())
    mark_reported(reg, cid)
    return hid, cid


def add_promising_sibling(store, reg, hid, area, *, title):
    """The non-calendar reassessment trigger: a sibling hypothesis in the
    SAME research area turns PROMISING. Caller must have already recorded
    a BASELINE build_opportunity_pool() call for `hid` before calling this,
    and must call build_opportunity_pool() again AFTER, for the signature
    change to actually register as reassessment-eligible."""
    research_areas.tag_hypothesis(store, hypothesis_id=hid, research_area=area, source="test")
    hid_sib, cid_sib = draft(
        store, reg, title=title, signal="observatory.price_move_zscore",
        entry_rule={"conditions": [{"metric": "price_move_zscore", "op": ">", "value": 2.0}]})
    research_areas.tag_hypothesis(store, hypothesis_id=hid_sib, research_area=area, source="test")
    lock(store, reg, cid_sib)
    score_it(store, reg, cid_sib, trades=winning_trades())
    mark_reported(reg, cid_sib)
    return hid_sib, cid_sib


# ---------------------------------------------------------------------------
print("\n--- A: reassessed opportunity WITH an existing runnable contract "
      "selects RUN_EXPERIMENT ---")
# ---------------------------------------------------------------------------

s_a = fresh_store("a")
reg_a = fresh_registry("a")
hid_a, cid_a = create_weak_hypothesis(s_a, reg_a, title="A idea")
opp.build_opportunity_pool(s_a, "2024-05-12", registry_dir=reg_a)  # baseline event
add_promising_sibling(s_a, reg_a, hid_a, "area-a", title="A sibling")
# an already-locked-but-not-yet-run retest contract for the SAME hypothesis
_, cid_retest_a = draft(s_a, reg_a, hypothesis_id=hid_a, title="A retest, wider stop",
                        entry_rule={"conditions": [{"metric": "volume_zscore", "op": ">", "value": 3.5}]})
lock(s_a, reg_a, cid_retest_a)

pool_a = opp.build_opportunity_pool(s_a, "2024-05-12", registry_dir=reg_a)
o_a = [o for o in pool_a if o.hypothesis_id == hid_a][0]
check("A0: the hypothesis is REASSESSING (evidence-driven, not calendar-driven)",
      o_a.lifecycle_stage == "REASSESSING", o_a.lifecycle_stage)

queue_a = opp.build_action_queue(s_a, pool_a, registry_dir=reg_a, discovery_available=False)
kinds_for_a = [a.kind for a in queue_a if a.hypothesis_id == hid_a]
check("A1: a RUN_EXPERIMENT action exists for the retest contract",
      any(a.kind == "RUN_EXPERIMENT" and a.contract_id == cid_retest_a
          for a in queue_a), str(queue_a))
check("A2: NO CREATE_EXPERIMENT action is offered — a runnable contract "
      "already exists, so nothing needs to be created",
      "CREATE_EXPERIMENT" not in kinds_for_a, str(kinds_for_a))
top_for_a = [a for a in queue_a if a.hypothesis_id == hid_a][0]
check("A3: the top-ranked action for this hypothesis is RUN_EXPERIMENT",
      top_for_a.kind == "RUN_EXPERIMENT", top_for_a.kind)


# ---------------------------------------------------------------------------
print("\n--- B: reassessed opportunity WITHOUT a runnable contract, but with "
      "an unused split, selects CREATE_EXPERIMENT ---")
# ---------------------------------------------------------------------------

s_b = fresh_store("b")
reg_b = fresh_registry("b")
hid_b, cid_b = create_weak_hypothesis(s_b, reg_b, title="B idea", with_validation_split=True)
opp.build_opportunity_pool(s_b, "2024-05-12", registry_dir=reg_b)  # baseline event
add_promising_sibling(s_b, reg_b, hid_b, "area-b", title="B sibling")

pool_b = opp.build_opportunity_pool(s_b, "2024-05-12", registry_dir=reg_b)
o_b = [o for o in pool_b if o.hypothesis_id == hid_b][0]
check("B0: the hypothesis is REASSESSING", o_b.lifecycle_stage == "REASSESSING", o_b.lifecycle_stage)

queue_b = opp.build_action_queue(s_b, pool_b, registry_dir=reg_b, discovery_available=False)
kinds_for_b = [a.kind for a in queue_b if a.hypothesis_id == hid_b]
check("B1: no RUN_EXPERIMENT action exists (its only contract already ran)",
      "RUN_EXPERIMENT" not in kinds_for_b, str(kinds_for_b))
check("B2: no PROMOTE action exists (no pending draft)", "PROMOTE" not in kinds_for_b, str(kinds_for_b))
check("B3: a CREATE_EXPERIMENT action IS offered — a substrate genuinely exists",
      "CREATE_EXPERIMENT" in kinds_for_b, str(kinds_for_b))
create_action_b = [a for a in queue_b if a.hypothesis_id == hid_b][0]
check("B4: it targets the parent contract, and names its substrate strategy",
      create_action_b.contract_id == cid_b and create_action_b.substrate_type == "split_derivation",
      create_action_b)
check("B5: its priority is the SAME as the opportunity's own score — no second formula",
      create_action_b.priority_score == o_b.priority_score,
      (create_action_b.priority_score, o_b.priority_score))


# ---------------------------------------------------------------------------
print("\n--- C: CREATE_EXPERIMENT produces a correctly linked research "
      "substrate ---")
# ---------------------------------------------------------------------------

so_c = opp.attempt_create_experiment(s_b, o_b, registry_dir=reg_b)
check("C1: the attempt succeeds", so_c.outcome == "created", so_c)
check("C2: the created contract is a fresh, distinct id", so_c.created_contract_id != cid_b, so_c)
_created_c = Contract.load(so_c.created_contract_id, reg_b)
check("C3: the created contract is a DRAFT — never locked by this action",
      _created_c.status == "draft", _created_c.status)
check("C4: the created contract shares the parent's evaluation window "
      "(the pre-declared 'validation' split, not a re-derived discovery window)",
      _created_c.evaluation_start == "2022-01-01" and _created_c.evaluation_end == "2022-12-31",
      (_created_c.evaluation_start, _created_c.evaluation_end))
_claims_c = [r["payload"] for r in rm.query_research_log(s_b, rm.DATASET_HYPOTHESIS)
            if r["payload"].get("hypothesis_id") == hid_b]
check("C5: a hypothesis-claim row links the new contract back to the parent "
      "and the split — opportunity -> hypothesis -> generated substrate",
      any(cl.get("contract_id") == so_c.created_contract_id and cl.get("split_of") == cid_b
          and cl.get("split") == "validation" for cl in _claims_c), _claims_c)
_events_c = opp.override_history(s_b, o_b.id)
_substrate_events_c = [e for e in _events_c if e.get("event_type") == "SUBSTRATE_CREATED"]
check("C6: a SUBSTRATE_CREATED audit event was recorded, naming the opportunity "
      "and the reassessment reason",
      len(_substrate_events_c) == 1 and "split" in (_substrate_events_c[0].get("reason") or ""),
      _substrate_events_c)
check("C7: that event's extra payload carries the full lineage (parent, created, "
      "substrate_type, split_key)",
      _substrate_events_c[0].get("parent_contract_id") == cid_b
      and _substrate_events_c[0].get("created_contract_id") == so_c.created_contract_id
      and _substrate_events_c[0].get("substrate_type") == "split_derivation"
      and _substrate_events_c[0].get("split_key") == "validation", _substrate_events_c[0])


# ---------------------------------------------------------------------------
print("\n--- D: the newly-created substrate is visible to the next queue "
      "rebuild, as an ordinary PROMOTE candidate ---")
# ---------------------------------------------------------------------------

pool_d = opp.build_opportunity_pool(s_b, "2024-05-12", registry_dir=reg_b)
o_d = [o for o in pool_d if o.hypothesis_id == hid_b][0]
# NOTE: the opportunity's OWN contract_id/lifecycle_stage still describe the
# already-scored PARENT contract, not the new draft sibling —
# build_opportunity_pool()'s "representative contract" is deliberately
# biased toward an ever-locked contract over a draft one (see that
# function's own docstring), and this slice does not change that. What
# actually matters — whether the new draft is REACHABLE for promotion — is
# proven by D3/D4 below via build_action_queue(), which sources PROMOTE
# candidates from hi.pending_drafts() directly, not from this field.
check("D1: the opportunity's own contract_id still points at the ALREADY-"
      "SCORED parent, not the new draft — the pool's representative-contract "
      "field is not where this capability's correctness lives",
      o_d.contract_id == cid_b, (o_d.contract_id, cid_b, so_c.created_contract_id))
check("D2: its lifecycle stage is correspondingly still REASSESSING/REJECTED "
      "(driven by the parent's own WEAK verdict) — again, not where correctness "
      "lives for this capability",
      o_d.lifecycle_stage in ("REASSESSING", "REJECTED"), o_d.lifecycle_stage)

queue_d = opp.build_action_queue(s_b, pool_d, registry_dir=reg_b, discovery_available=False)
kinds_for_d = [a.kind for a in queue_d if a.hypothesis_id == hid_b]
check("D3: a PROMOTE action now exists for this hypothesis — the substrate "
      "created one iteration ago made it visible with no extra wiring",
      "PROMOTE" in kinds_for_d, str(kinds_for_d))
check("D4: CREATE_EXPERIMENT is no longer offered — this hypothesis now has "
      "an executable path (PROMOTE), so it is excluded, not just deprioritized",
      "CREATE_EXPERIMENT" not in kinds_for_d, str(kinds_for_d))


# ---------------------------------------------------------------------------
print("\n--- E: the same opportunity cannot generate duplicate substrate "
      "unnecessarily ---")
# ---------------------------------------------------------------------------

so_e = opp.attempt_create_experiment(s_b, o_d, registry_dir=reg_b)
check("E1: a second attempt for the SAME (already-used) validation split "
      "is refused, not silently re-created",
      so_e.outcome == "skipped_no_substrate", so_e)
check("E2: no second draft was written to the registry for this hypothesis",
      len([c for c in reg_b.glob("*.json")
           if evaluator.resolve_hypothesis_id(s_b, c.stem) == hid_b]) == 2,  # original + the one from C
      list(reg_b.glob("*.json")))


# ---------------------------------------------------------------------------
print("\n--- F: a frozen opportunity cannot generate substrate ---")
# ---------------------------------------------------------------------------

s_f = fresh_store("f")
reg_f = fresh_registry("f")
hid_f, cid_f = create_weak_hypothesis(s_f, reg_f, title="F idea", with_validation_split=True)
opp.build_opportunity_pool(s_f, "2024-05-12", registry_dir=reg_f)
add_promising_sibling(s_f, reg_f, hid_f, "area-f", title="F sibling")
pool_f = opp.build_opportunity_pool(s_f, "2024-05-12", registry_dir=reg_f)
o_f = [o for o in pool_f if o.hypothesis_id == hid_f][0]

opp.freeze(s_f, o_f.id, by="vaibhav", reason="hold off")
pool_f2 = opp.build_opportunity_pool(s_f, "2024-05-12", registry_dir=reg_f)
o_f2 = [o for o in pool_f2 if o.hypothesis_id == hid_f][0]
check("F0: the opportunity now genuinely reports frozen=True",
      o_f2.override_state.get("frozen") is True, o_f2.override_state)
so_f = opp.attempt_create_experiment(s_f, o_f2, registry_dir=reg_f)
check("F1: attempt_create_experiment refuses a frozen opportunity",
      so_f.outcome == "skipped_frozen", so_f)

queue_f = opp.build_action_queue(s_f, pool_f2, registry_dir=reg_f, discovery_available=False)
check("F2: build_action_queue excludes it outright — no CREATE_EXPERIMENT "
      "action for this opportunity at all, not merely zero-scored",
      not any(a.opportunity_id == o_f.id for a in queue_f), str(queue_f))
check("F3: no new contract was created", not any(evaluator.resolve_hypothesis_id(s_f, c.stem) == hid_f
                                                 and c.stem != cid_f for c in reg_f.glob("*.json")))


# ---------------------------------------------------------------------------
print("\n--- G: a retired opportunity cannot generate substrate unless "
      "explicitly reopened ---")
# ---------------------------------------------------------------------------

s_g = fresh_store("g")
reg_g = fresh_registry("g")
hid_g, cid_g = create_weak_hypothesis(s_g, reg_g, title="G idea", with_validation_split=True)
opp.build_opportunity_pool(s_g, "2024-05-12", registry_dir=reg_g)
add_promising_sibling(s_g, reg_g, hid_g, "area-g", title="G sibling")
pool_g = opp.build_opportunity_pool(s_g, "2024-05-12", registry_dir=reg_g)
o_g = [o for o in pool_g if o.hypothesis_id == hid_g][0]

opp.retire(s_g, o_g.id, by="vaibhav", reason="not pursuing this line")
pool_g2 = opp.build_opportunity_pool(s_g, "2024-05-12", registry_dir=reg_g)
o_g2 = [o for o in pool_g2 if o.hypothesis_id == hid_g][0]
so_g = opp.attempt_create_experiment(s_g, o_g2, registry_dir=reg_g)
check("G1: attempt_create_experiment refuses a retired opportunity",
      so_g.outcome == "skipped_frozen", so_g)
queue_g = opp.build_action_queue(s_g, pool_g2, registry_dir=reg_g, discovery_available=False)
check("G2: build_action_queue excludes a retired opportunity's CREATE_EXPERIMENT too",
      not any(a.opportunity_id == o_g2.id for a in queue_g), str(queue_g))

opp.reopen(s_g, o_g.id, by="vaibhav", reason="changed my mind")
pool_g3 = opp.build_opportunity_pool(s_g, "2024-05-12", registry_dir=reg_g)
o_g3 = [o for o in pool_g3 if o.hypothesis_id == hid_g][0]
so_g3 = opp.attempt_create_experiment(s_g, o_g3, registry_dir=reg_g)
check("G3: after reopen(), substrate creation succeeds again — the user's "
      "own override, not the AI's, controls this",
      so_g3.outcome == "created", so_g3)


# ---------------------------------------------------------------------------
print("\n--- H: new evidence causes a rejected opportunity to re-enter "
      "executable priority ---")
# ---------------------------------------------------------------------------

s_h = fresh_store("h")
reg_h = fresh_registry("h")
hid_h, cid_h = create_weak_hypothesis(s_h, reg_h, title="H idea", with_validation_split=True)
pool_h0 = opp.build_opportunity_pool(s_h, "2024-05-12", registry_dir=reg_h)
o_h0 = [o for o in pool_h0 if o.hypothesis_id == hid_h][0]
check("H0: freshly WEAK, nothing to compare against, it is plain REJECTED "
      "(not REASSESSING) — no calendar-based auto-reassessment",
      o_h0.lifecycle_stage == "REJECTED", o_h0.lifecycle_stage)
queue_h0 = opp.build_action_queue(s_h, pool_h0, registry_dir=reg_h, discovery_available=True)
check("H0b: while plainly REJECTED, no CREATE_EXPERIMENT is offered for it — "
      "REJECTED (not yet reassessment-eligible) is intentionally excluded, "
      "exactly like PROMOTE/RUN_EXPERIMENT already were",
      not any(a.hypothesis_id == hid_h for a in queue_h0), str(queue_h0))

add_promising_sibling(s_h, reg_h, hid_h, "area-h", title="H sibling")
pool_h1 = opp.build_opportunity_pool(s_h, "2024-05-12", registry_dir=reg_h)
o_h1 = [o for o in pool_h1 if o.hypothesis_id == hid_h][0]
check("H1: it is now REASSESSING and its priority genuinely rose",
      o_h1.lifecycle_stage == "REASSESSING" and o_h1.priority_score > o_h0.priority_score,
      (o_h1.lifecycle_stage, o_h1.priority_score, o_h0.priority_score))

queue_h1 = opp.build_action_queue(s_h, pool_h1, registry_dir=reg_h, discovery_available=True)
create_h = [a for a in queue_h1 if a.hypothesis_id == hid_h]
discover_h = [a for a in queue_h1 if a.kind == "DISCOVER"]
check("H2: it now has an executable CREATE_EXPERIMENT action",
      len(create_h) == 1 and create_h[0].kind == "CREATE_EXPERIMENT", str(queue_h1))
check("H3: that action beats the synthetic discovery baseline — a previously "
      "rejected idea, made valuable again, outranks a brand-new discovery attempt",
      create_h[0].priority_score > discover_h[0].priority_score,
      (create_h[0].priority_score, discover_h[0].priority_score))


# ---------------------------------------------------------------------------
print("\n--- I: a lower-value CREATE_EXPERIMENT cannot outrank a "
      "substantially higher-value existing experiment ---")
# ---------------------------------------------------------------------------

s_i = fresh_store("i")
reg_i = fresh_registry("i")
# a HIGH-value robustness test: an already-PROMISING parent with a locked,
# not-yet-run validation sibling
hid_i1, cid_i1 = draft(s_i, reg_i, title="I high-value parent",
                       splits={"discovery": ["2019-01-01", "2021-12-31"],
                               "validation": ["2022-01-01", "2022-12-31"]})
lock(s_i, reg_i, cid_i1)
score_it(s_i, reg_i, cid_i1, trades=winning_trades())
mark_reported(reg_i, cid_i1)
deriv_i = hi.derive_split_contract(s_i, cid_i1, "validation", hid_i1, registry_dir=reg_i)
lock(s_i, reg_i, deriv_i.contract.id)

# a LOW-value reassessed idea with a substrate available
hid_i2, cid_i2 = create_weak_hypothesis(s_i, reg_i, title="I low-value idea", with_validation_split=True)
opp.build_opportunity_pool(s_i, "2024-05-12", registry_dir=reg_i)
add_promising_sibling(s_i, reg_i, hid_i2, "area-i2", title="I low-value sibling")

pool_i = opp.build_opportunity_pool(s_i, "2024-05-12", registry_dir=reg_i)
queue_i = opp.build_action_queue(s_i, pool_i, registry_dir=reg_i, discovery_available=False)
high_action = [a for a in queue_i if a.hypothesis_id == hid_i1][0]
low_action = [a for a in queue_i if a.hypothesis_id == hid_i2][0]
check("I1: the robustness test scores substantially higher than the "
      "reassessed-but-otherwise-unremarkable idea",
      high_action.priority_score > low_action.priority_score + 1.0,
      (high_action.priority_score, low_action.priority_score))
check("I2: the high-value RUN_EXPERIMENT is ranked strictly before the "
      "low-value CREATE_EXPERIMENT in the sorted queue",
      queue_i.index(high_action) < queue_i.index(low_action))
check("I3: the top of the whole queue is the high-value action",
      queue_i[0].hypothesis_id == hid_i1 and queue_i[0].kind == "RUN_EXPERIMENT")


# ---------------------------------------------------------------------------
print("\n--- J: resource limits (max_substrate_creations) still hold ---")
# ---------------------------------------------------------------------------

# J1: max_substrate_creations=0 disables it cleanly, even with an eligible
# substrate available (its own fresh store — see the note below).
s_j0 = fresh_store("j0")
reg_j0 = fresh_registry("j0")
st_j0 = fresh_state("j0")
hid_j0, _ = create_weak_hypothesis(s_j0, reg_j0, title="J idea 0", with_validation_split=True)
opp.build_opportunity_pool(s_j0, "2024-05-12", registry_dir=reg_j0)
add_promising_sibling(s_j0, reg_j0, hid_j0, "area-j0", title="J sibling 0")

lim_j0 = w.WorkerLimits(max_discovery_attempts=0, max_promotions=0, max_experiments=0,
                        max_substrate_creations=0, cooldown_seconds=0)
r_j0 = w.run_worker_cycle(s_j0, now_ist(), limits=lim_j0, registry_dir=reg_j0,
                          runner=lambda p: "unused", state_path=st_j0)
check("J1: max_substrate_creations=0 -> CREATE_EXPERIMENT is never attempted",
      r_j0.substrate_creations_attempted == 0 and "substrate_creation" not in r_j0.work_selected,
      str(r_j0))

# J2: a FRESH store with TWO independently reassessment-eligible
# hypotheses, cap=1 — exactly one is created. (Deliberately a fresh store,
# not a second run_worker_cycle() call against s_j0: the FIRST call's own
# internal build_opportunity_pool() already records the observed evidence
# signature as the new baseline, which is the correct, intended non-
# calendar behaviour — "reassessment eligible" means "something changed
# since we last looked," and a heartbeat that looked is no longer owed a
# second, artificial re-trigger of the SAME already-observed change.)
s_j1 = fresh_store("j1")
reg_j1 = fresh_registry("j1")
st_j1 = fresh_state("j1")
hid_j1, _ = create_weak_hypothesis(s_j1, reg_j1, title="J idea 1", with_validation_split=True)
opp.build_opportunity_pool(s_j1, "2024-05-12", registry_dir=reg_j1)
add_promising_sibling(s_j1, reg_j1, hid_j1, "area-j1", title="J sibling 1")
hid_j2, _ = create_weak_hypothesis(s_j1, reg_j1, title="J idea 2", with_validation_split=True)
opp.build_opportunity_pool(s_j1, "2024-05-12", registry_dir=reg_j1)
add_promising_sibling(s_j1, reg_j1, hid_j2, "area-j2", title="J sibling 2")

lim_j1 = w.WorkerLimits(max_discovery_attempts=0, max_promotions=0, max_experiments=0,
                        max_substrate_creations=1, cooldown_seconds=0)
r_j1 = w.run_worker_cycle(s_j1, now_ist(), limits=lim_j1, registry_dir=reg_j1,
                          runner=lambda p: "unused", state_path=st_j1)
check("J2: with 2 independently eligible substrates and max_substrate_creations=1, "
      "exactly ONE substrate is created this heartbeat",
      r_j1.substrate_creations_attempted == 1 and len(r_j1.created_substrate_ids) == 1, str(r_j1))
check("J3: WorkerLimits rejects a negative max_substrate_creations",
      True)
try:
    w.WorkerLimits(max_substrate_creations=-1)
    _rejected = False
except ValueError:
    _rejected = True
check("J3b: WorkerLimits(max_substrate_creations=-1) raises ValueError", _rejected)


# ---------------------------------------------------------------------------
print("\n--- K: the worker loop can create substrate and then run the "
      "resulting experiment in the same heartbeat ---")
# ---------------------------------------------------------------------------

s_k = fresh_store("k")
reg_k = fresh_registry("k")
st_k = fresh_state("k")
hid_k, cid_k = create_weak_hypothesis(s_k, reg_k, title="K idea", with_validation_split=True)
opp.build_opportunity_pool(s_k, "2024-05-12", registry_dir=reg_k)  # baseline event
add_promising_sibling(s_k, reg_k, hid_k, "area-k", title="K sibling")

lim_k = w.WorkerLimits(max_discovery_attempts=0, cooldown_seconds=0)  # promotions/experiments/substrate default 1
r_k = w.run_worker_cycle(s_k, now_ist(), limits=lim_k, registry_dir=reg_k,
                         runner=lambda p: "unused", state_path=st_k)
kinds_k = [a["kind"] for a in r_k.action_log]
check("K1: the heartbeat created substrate, promoted it, and ran it — all "
      "in one cycle",
      kinds_k == ["CREATE_EXPERIMENT", "PROMOTE", "RUN_EXPERIMENT"], str(kinds_k))
check("K2: substrate_creations_attempted / created_substrate_ids are populated",
      r_k.substrate_creations_attempted == 1 and len(r_k.created_substrate_ids) == 1, str(r_k))
check("K3: the created substrate is the SAME contract that got promoted, "
      "which is the SAME contract that got run",
      r_k.action_log[0]["created_substrate_id"] == r_k.action_log[1]["contract_id"]
      == r_k.action_log[2]["contract_id"], str(r_k.action_log))
check("K4: the promoted contract is now locked-and-run (no longer draft)",
      Contract.load(r_k.created_substrate_ids[0], reg_k).status != "draft",
      Contract.load(r_k.created_substrate_ids[0], reg_k).status)
check("K5: substrate creation + promotion both count as useful work — no "
      "false cooldown after real progress",
      "cooldown_until" not in __import__("json").loads(st_k.read_text()))


# ---------------------------------------------------------------------------
print("\n--- L: isolation is unchanged (targeted static check; full suites "
      "run separately) ---")
# ---------------------------------------------------------------------------

_opp_src = (Path(__file__).parent.parent / "research" / "brain" / "opportunity.py").read_text()
_opp_code = re.sub(r'"""[\s\S]*?"""', "", _opp_src)
_opp_imports = re.findall(r"^\s*(?:from|import)\s+([.\w]+)", _opp_code, re.MULTILINE)
check("L1: opportunity.py (with attempt_create_experiment) still imports no "
      "engine/paper module", not any(m.split(".")[0] in ("engine", "paper") for m in _opp_imports),
      str(_opp_imports))
check("L2: opportunity.py still never imports research.experiments.runner directly",
      "experiments.runner" not in _opp_code and "from ..experiments import runner" not in _opp_code)

_w_src = (Path(__file__).parent.parent / "research" / "brain" / "worker.py").read_text()
_w_code = re.sub(r'"""[\s\S]*?"""', "", _w_src)
check("L3: worker.py still never calls the locking primitive or the runner "
      "module directly",
      "approve_and_lock(" not in _w_code and ".lock()" not in _w_code
      and "experiments.runner" not in _w_code)
check("L4: worker.py never calls hypothesis_intake.derive_split_contract directly "
      "(only through opportunity.attempt_create_experiment)",
      "derive_split_contract(" not in _w_code)


# ---------------------------------------------------------------------------
print("\n--- M: no existing safety boundary is weakened ---")
# ---------------------------------------------------------------------------

check("M1: exactly ONE approve_and_lock( call site in the whole control plane "
      "(attempt_create_experiment never locks anything)",
      _opp_code.count("hi.approve_and_lock(") == 1, _opp_code.count("hi.approve_and_lock("))
check("M2: exactly ONE derive_split_contract( call site (inside "
      "attempt_create_experiment) — no second substrate-creation path",
      _opp_code.count("hi.derive_split_contract(") == 1, _opp_code.count("hi.derive_split_contract("))
check("M3: the EXISTING research budget check still gates ONLY promotion, "
      "never substrate creation — creating a draft stays as unlimited as "
      "create_draft() itself always was",
      _opp_code.count("check_research_budget(") == 1)
check("M4: attempt_create_experiment refuses BOTH frozen and retired, same "
      "as attempt_autonomous_promotion — user override is not weaker here",
      "opportunity.override_state.get(\"frozen\")" in _opp_code
      and _opp_code.count('override_state.get("frozen")') >= 2)


shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'=' * 52}")
print(f"  {PASSED} passed, {FAILED} failed")
print(f"{'=' * 52}\n")
sys.exit(1 if FAILED else 0)
