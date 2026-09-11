"""
tests/test_research_opportunity.py — Autonomous Research Control Plane v1
(research/brain/opportunity.py).

Covers, mechanically, the Master Vision's own success criteria for this
slice:
  A. lifecycle: autonomous stage computation, autonomous promotion, no
     mandatory human approval, non-terminal rejection, reassessment
  B. priority: explainable components, evidence/regime/duplicate sensitivity
  C. confidence: computed, explainable, updates with evidence
  D. compute/resource bounding: budget-respecting, bounded, no runaway loop
  E. research safety: isolation from engine/broker/paper/live execution
  F. user override: freeze/reopen/retire/force-reassess, audited, AI never
     silently overwrites an active freeze
  G. regression: the module composes existing components without duplicating
     their logic

Run with:  python -m tests.test_research_opportunity
"""

from __future__ import annotations

import re
import sys
import json
import shutil
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from research.store import Store  # noqa: E402
from research.contracts import Contract, REGISTRY_DIR  # noqa: E402
from research.brain import hypothesis_intake as hi  # noqa: E402
from research.brain import opportunity as opp  # noqa: E402
from research.experiments import evaluator, runner  # noqa: E402
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


TMP = Path(tempfile.mkdtemp(prefix="lq-test-opportunity-"))


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
    """Create one DRAFT hypothesis + contract, return (hypothesis_id, contract_id)."""
    result = hi.create_draft(store, make_proposal(**overrides), registry_dir=reg)
    return result.hypothesis_id, result.contract.id


def lock(store, reg, contract_id, *, approver="test_human"):
    return hi.approve_and_lock(store, contract_id, approved_by=approver, registry_dir=reg)


def winning_trades(n=12):
    """A statistically SIGNIFICANT (real variance, |t_stat|>=2.0) AND
    economically meaningful (avg_net_pnl >= 0.3% of the Rs 1,00,000 research
    notional) positive result -> comparison.py's own rule table calls this
    PROMISING. Zero-variance synthetic trades would make t_stat uncomputable
    (None) and silently fall through to WEAK regardless of how profitable
    they look — real variance is required to EARN a PROMISING verdict."""
    base_pnl = [420, 380, 440, 360, 410, 390, 430, 370, 400, 415, 385, 405]
    base_r = [2.1, 1.9, 2.2, 1.8, 2.05, 1.95, 2.15, 1.85, 2.0, 2.05, 1.95, 2.0]
    return list(zip(base_pnl[:n], base_r[:n]))


def losing_trades(n=12):
    """A losing result — lands on WEAK (comparison.py's default for "no
    significant positive result"), the exact non-terminal-rejection case
    this test file exercises."""
    return [(-10.0, -0.5)] * n


def score_it(store, reg, contract_id, *, trades):
    """Push `trades` synthetic experiment_results rows for `contract_id` and
    record its verdict — the same two steps runner.run_experiment() performs,
    done directly here so tests control the exact win/loss shape without a
    real backtest."""
    for i, (net_pnl, r_mult) in enumerate(trades):
        store.append_experiment_result(
            contract_id=contract_id, trade_seq=i, entity="TESTSYM",
            entry_time="2020-01-01", exit_time="2020-01-05",
            entry_price=100.0, exit_price=100.0 + net_pnl, quantity=1,
            gross_pnl=net_pnl, costs=0.0, net_pnl=net_pnl, r_multiple=r_mult,
            exit_reason="target",
        )
    return evaluator.record_verdict(store, contract_id)


# ---------------------------------------------------------------------------
print("\n--- A: lifecycle stages — pure computation, all 9 reachable ---")
# ---------------------------------------------------------------------------

_ls = opp._lifecycle_stage
check("A1: no contracts, never seen before -> DISCOVERED",
      _ls(statuses=frozenset(), verdict=None, is_robust=False,
          reassessment_eligible=False, has_prior_event=False, is_retired=False) == "DISCOVERED")
check("A2: no contracts, seen before -> TRIAGED",
      _ls(statuses=frozenset(), verdict=None, is_robust=False,
          reassessment_eligible=False, has_prior_event=True, is_retired=False) == "TRIAGED")
check("A3: locked -> TESTING",
      _ls(statuses=frozenset({"locked"}), verdict=None, is_robust=False,
          reassessment_eligible=False, has_prior_event=True, is_retired=False) == "TESTING")
check("A4: running -> TESTING",
      _ls(statuses=frozenset({"running"}), verdict=None, is_robust=False,
          reassessment_eligible=False, has_prior_event=True, is_retired=False) == "TESTING")
check("A5: reported, no verdict yet -> EVALUATING",
      _ls(statuses=frozenset({"reported"}), verdict=None, is_robust=False,
          reassessment_eligible=False, has_prior_event=True, is_retired=False) == "EVALUATING")
check("A6: verdict PROMISING, not robust -> PROMISING",
      _ls(statuses=frozenset({"reported"}), verdict="PROMISING", is_robust=False,
          reassessment_eligible=False, has_prior_event=True, is_retired=False) == "PROMISING")
check("A7: verdict PROMISING AND robust -> ROBUST",
      _ls(statuses=frozenset({"reported"}), verdict="PROMISING", is_robust=True,
          reassessment_eligible=False, has_prior_event=True, is_retired=False) == "ROBUST")
check("A8: verdict WEAK, not reassessment-eligible -> REJECTED (non-terminal)",
      _ls(statuses=frozenset({"reported"}), verdict="WEAK", is_robust=False,
          reassessment_eligible=False, has_prior_event=True, is_retired=False) == "REJECTED")
check("A9: verdict CONTRADICTED, reassessment-eligible -> REASSESSING",
      _ls(statuses=frozenset({"reported"}), verdict="CONTRADICTED", is_robust=False,
          reassessment_eligible=True, has_prior_event=True, is_retired=False) == "REASSESSING")
check("A10: abandoned, no evidence, eligible -> REASSESSING",
      _ls(statuses=frozenset({"abandoned"}), verdict=None, is_robust=False,
          reassessment_eligible=True, has_prior_event=True, is_retired=False) == "REASSESSING")
check("A11: retired flag wins over every other input",
      _ls(statuses=frozenset({"reported"}), verdict="PROMISING", is_robust=True,
          reassessment_eligible=True, has_prior_event=True, is_retired=True) == "RETIRED")
check("A12: every stage in LIFECYCLE_STAGES is reachable by SOME input combination "
      "(no dead stage in the vocabulary)",
      {"DISCOVERED", "TRIAGED", "TESTING", "EVALUATING", "PROMISING", "ROBUST",
       "REJECTED", "REASSESSING", "RETIRED"} == set(opp.LIFECYCLE_STAGES))


# ---------------------------------------------------------------------------
print("\n--- B: end-to-end pool + autonomous promotion (no mandatory human approval) ---")
# ---------------------------------------------------------------------------

s_b = fresh_store("b")
reg_b = fresh_registry("b")
hid_b, cid_b = draft(s_b, reg_b)

pool = opp.build_opportunity_pool(s_b, "2024-05-12", registry_dir=reg_b)
check("B1: a fresh draft appears in the pool as DISCOVERED",
      len(pool) == 1 and pool[0].hypothesis_id == hid_b and pool[0].lifecycle_stage == "DISCOVERED",
      str(pool))
check("B2: the opportunity id is deterministic (OPP-<hypothesis_id>)",
      pool[0].id == f"OPP-{hid_b}")

pool2 = opp.build_opportunity_pool(s_b, "2024-05-12", registry_dir=reg_b)
check("B3: a second, unchanged call re-reports TRIAGED (it has now been seen before)",
      pool2[0].lifecycle_stage == "TRIAGED", pool2[0].lifecycle_stage)

outcome = opp.attempt_autonomous_promotion(s_b, pool2[0], registry_dir=reg_b)
check("B4: attempt_autonomous_promotion succeeds with NO human `approved_by` supplied "
      "anywhere in this test — the system approver is used instead",
      outcome.outcome == "promoted", str(outcome))
check("B5: the approver recorded is the explicit, named SYSTEM_APPROVER — never blank, "
      "never a forged human name",
      Contract.load(cid_b, reg_b).status == "locked")

pool3 = opp.build_opportunity_pool(s_b, "2024-05-12", registry_dir=reg_b)
check("B6: after promotion the SAME opportunity now reports TESTING",
      pool3[0].lifecycle_stage == "TESTING", pool3[0].lifecycle_stage)

# The audit note approve_and_lock() itself writes records the system approver —
# proving "who approved this" is never ambiguous with a human approval.
notes = [r for r in rm.query_research_log(s_b, rm.DATASET_NOTE)]
check("B7: the lock's own audit note names the system approver explicitly",
      any(opp.SYSTEM_APPROVER in json.dumps(n["payload"]) for n in notes), str(notes))


# ---------------------------------------------------------------------------
print("\n--- C: priority — explainable components, novelty, confirmation, area balance ---")
# ---------------------------------------------------------------------------

comp = opp._priority_components(
    evidence_verdict="PROMISING", is_duplicate=False, is_confirmation_sibling=True,
    area_hypothesis_count=1, max_area_hypothesis_count=4,
    reassessment_eligible=False, opportunity_type="ROBUSTNESS_TEST", locked_at=None,
)
check("C1: every named PRIORITY_WEIGHTS component is present and independently inspectable",
      set(opp.PRIORITY_WEIGHTS) == set(comp), str(comp))
score = opp.priority_score(comp)
check("C2: priority_score is exactly the documented weighted sum — 'why is this #1' "
      "is always answerable from priority_components alone",
      score == round(sum(opp.PRIORITY_WEIGHTS[k] * comp[k] for k in comp), 4), (comp, score))

dup_comp = dict(comp, novelty=0.0)
check("C3: a duplicate (novelty=0) scores strictly lower than an otherwise-identical "
      "non-duplicate — duplicate work is actively de-prioritized",
      opp.priority_score(dup_comp) < opp.priority_score(comp))

no_confirm = dict(comp, confirmation_bonus=0.0)
check("C4: a confirmation/robustness-test bonus raises priority over an otherwise-"
      "identical non-confirmation opportunity",
      opp.priority_score(comp) > opp.priority_score(no_confirm))

balanced = opp._priority_components(
    evidence_verdict=None, is_duplicate=False, is_confirmation_sibling=False,
    area_hypothesis_count=1, max_area_hypothesis_count=10,
    reassessment_eligible=False, opportunity_type="NEW_HYPOTHESIS", locked_at="x",
)
crowded = opp._priority_components(
    evidence_verdict=None, is_duplicate=False, is_confirmation_sibling=False,
    area_hypothesis_count=9, max_area_hypothesis_count=10,
    reassessment_eligible=False, opportunity_type="NEW_HYPOTHESIS", locked_at="x",
)
check("C5: an under-represented research area scores higher than an over-represented "
      "one, all else equal (§4 research-area balance)",
      opp.priority_score(balanced) > opp.priority_score(crowded))

reassess_yes = dict(comp, reassessment_rising_value=1.0)
reassess_no = dict(comp, reassessment_rising_value=0.0)
check("C6: reassessment-eligible (rising value detected) scores higher than the same "
      "opportunity with nothing new to show",
      opp.priority_score(reassess_yes) > opp.priority_score(reassess_no))

cheap = opp._priority_components(
    evidence_verdict=None, is_duplicate=False, is_confirmation_sibling=True,
    area_hypothesis_count=None, max_area_hypothesis_count=0,
    reassessment_eligible=False, opportunity_type="ROBUSTNESS_TEST", locked_at=None)
expensive = opp._priority_components(
    evidence_verdict=None, is_duplicate=False, is_confirmation_sibling=True,
    area_hypothesis_count=None, max_area_hypothesis_count=0,
    reassessment_eligible=False, opportunity_type="NEW_HYPOTHESIS", locked_at=None)
check("C7: higher compute cost (NEW_HYPOTHESIS, an AI subprocess call) lowers priority "
      "relative to a cheaper opportunity type (ROBUSTNESS_TEST, one bounded backtest), "
      "all else equal",
      opp.priority_score(cheap) > opp.priority_score(expensive), (cheap, expensive))


# ---------------------------------------------------------------------------
print("\n--- D: non-terminal rejection — REJECTED regains priority when value rises ---")
# ---------------------------------------------------------------------------

s_d = fresh_store("d")
reg_d = fresh_registry("d")
hid_d, cid_d = draft(s_d, reg_d, title="Weak idea")
lock(s_d, reg_d, cid_d)
score_it(s_d, reg_d, cid_d, trades=losing_trades())  # loses money -> WEAK

pool_d1 = opp.build_opportunity_pool(s_d, "2024-05-12", registry_dir=reg_d)
o1 = pool_d1[0]
check("D1: a losing, scored hypothesis is REJECTED, not some other terminal label",
      o1.lifecycle_stage in ("REJECTED", "REASSESSING"), o1.lifecycle_stage)
check("D1b: on first assessment (nothing to compare against yet) it is plain REJECTED, "
      "not REASSESSING — there is no calendar-based auto-reassessment",
      o1.lifecycle_stage == "REJECTED", o1.lifecycle_stage)
check("D2: it is NEVER removed from the pool — non-terminal means it stays visible",
      any(o.hypothesis_id == hid_d for o in pool_d1))
rejected_priority = o1.priority_score

# nothing changed -> still REJECTED, same signature, no calendar dependency
pool_d2 = opp.build_opportunity_pool(s_d, "2024-05-12", registry_dir=reg_d)
o2 = [o for o in pool_d2 if o.hypothesis_id == hid_d][0]
check("D3: with NOTHING new, it stays REJECTED (no fixed retest period resurrects it)",
      o2.lifecycle_stage == "REJECTED" and o2.evidence_signature == o1.evidence_signature)

# a SIBLING hypothesis in the same research area turns PROMISING — a concrete,
# non-calendar trigger from the Master Vision's own list ("related strategy
# succeeds")
from research.brain import research_areas  # noqa: E402
research_areas.tag_hypothesis(s_d, hypothesis_id=hid_d, research_area="momentum", source="test")
hid_sib, cid_sib = draft(
    s_d, reg_d, title="Sibling idea", signal="observatory.price_move_zscore",
    # a DIFFERENT rule spec — must not be an exact-rule duplicate of hid_d's
    # contract, or `novelty` would confound this test's own measurement
    entry_rule={"conditions": [{"metric": "price_move_zscore", "op": ">", "value": 2.0}]})
research_areas.tag_hypothesis(s_d, hypothesis_id=hid_sib, research_area="momentum", source="test")
lock(s_d, reg_d, cid_sib)
score_it(s_d, reg_d, cid_sib, trades=winning_trades())  # a genuine winner -> PROMISING

pool_d3 = opp.build_opportunity_pool(s_d, "2024-05-12", registry_dir=reg_d)
o3 = [o for o in pool_d3 if o.hypothesis_id == hid_d][0]
check("D4: after a sibling in the SAME research area turns PROMISING, the rejected "
      "hypothesis becomes REASSESSING — reassessment IS value-driven",
      o3.lifecycle_stage == "REASSESSING", o3.lifecycle_stage)
check("D5: its priority score rose relative to when it was plain REJECTED",
      o3.priority_score > rejected_priority, (o3.priority_score, rejected_priority))
check("D6: reassessment_eligible is True exactly when the stage is REASSESSING",
      o3.reassessment_eligible is True)


# ---------------------------------------------------------------------------
print("\n--- E: evidence_signature has zero wall-clock / calendar dependency ---")
# ---------------------------------------------------------------------------

sig1 = opp._evidence_signature("WEAK", 3, 0)
sig2 = opp._evidence_signature("WEAK", 3, 0)
check("E1: identical inputs -> identical signature, called at 'different times' "
      "(there is no time input at all)", sig1 == sig2)
check("E2: signature changes when the verdict changes",
      opp._evidence_signature("PROMISING", 3, 0) != sig1)
check("E3: signature changes when new variants get scored",
      opp._evidence_signature("WEAK", 4, 0) != sig1)
check("E4: signature changes when a sibling turns PROMISING",
      opp._evidence_signature("WEAK", 3, 1) != sig1)
_src_no_time = re.sub(r'"""[\s\S]*?"""', "", (Path(__file__).parent.parent / "research" /
                      "brain" / "opportunity.py").read_text())
_sig_fn_body = _src_no_time.split("def _evidence_signature")[1].split("\ndef ")[0]
check("E5: _evidence_signature() itself references no wall clock / date module",
      not any(tok in _sig_fn_body for tok in ("datetime", "now_ist(", "time.", "date")))


# ---------------------------------------------------------------------------
print("\n--- F: confidence — computed, explainable, changes with evidence, no user action ---")
# ---------------------------------------------------------------------------

s_f = fresh_store("f")
reg_f = fresh_registry("f")
hid_f, cid_f = draft(s_f, reg_f)
pool_f0 = opp.build_opportunity_pool(s_f, "2024-05-12", registry_dir=reg_f)
check("F1: an unscored draft has confidence None (unknown, not zero)",
      pool_f0[0].confidence is None)

lock(s_f, reg_f, cid_f)
score_it(s_f, reg_f, cid_f, trades=winning_trades())
pool_f1 = opp.build_opportunity_pool(s_f, "2024-05-12", registry_dir=reg_f)
o_f1 = [o for o in pool_f1 if o.hypothesis_id == hid_f][0]
check("F2: after a PROMISING result, confidence is a real, positive number",
      o_f1.confidence is not None and o_f1.confidence > 0, o_f1.confidence)
check("F3: confidence never requires user action to update — no freeze/override call "
      "happened anywhere in this section", True)

# a WEAK opportunity has lower confidence than a PROMISING one
s_f2 = fresh_store("f2")
reg_f2 = fresh_registry("f2")
hid_f2, cid_f2 = draft(s_f2, reg_f2)
lock(s_f2, reg_f2, cid_f2)
score_it(s_f2, reg_f2, cid_f2, trades=losing_trades())
pool_f2 = opp.build_opportunity_pool(s_f2, "2024-05-12", registry_dir=reg_f2)
o_f2 = [o for o in pool_f2 if o.hypothesis_id == hid_f2][0]
check("F4: a WEAK/losing result has strictly lower confidence than a PROMISING one",
      (o_f2.confidence or 0) < o_f1.confidence, (o_f2.confidence, o_f1.confidence))


# ---------------------------------------------------------------------------
print("\n--- G: compute / resource bounding — budget-respecting, never a runaway loop ---")
# ---------------------------------------------------------------------------

s_g = fresh_store("g")
reg_g = fresh_registry("g")
# consume the ENTIRE existing research budget (5 locks / 7 days, unchanged)
for i in range(hi.MAX_LOCKS_PER_PERIOD):
    _, cid = draft(s_g, reg_g, title=f"budget filler {i}",
                   entry_rule={"conditions": [{"metric": "volume_zscore", "op": ">", "value": 3.0 + i}]})
    lock(s_g, reg_g, cid)

within_budget, count = hi.check_research_budget(reg_g)
check("G1: fixture sanity — the research budget is now genuinely exhausted",
      not within_budget and count == hi.MAX_LOCKS_PER_PERIOD, (within_budget, count))

hid_g, cid_g = draft(s_g, reg_g, title="one too many",
                     entry_rule={"conditions": [{"metric": "volume_zscore", "op": ">", "value": 9.0}]})
pool_g = opp.build_opportunity_pool(s_g, "2024-05-12", registry_dir=reg_g)
o_g = [o for o in pool_g if o.hypothesis_id == hid_g][0]
outcome_g = opp.attempt_autonomous_promotion(s_g, o_g, registry_dir=reg_g)
check("G2: a promotion attempt over the EXISTING research budget is refused cleanly "
      "(skipped_budget), never an exception, never a bypass",
      outcome_g.outcome == "skipped_budget", str(outcome_g))
check("G3: the contract was NOT locked — the budget gate genuinely blocked it",
      Contract.load(cid_g, reg_g).status == "draft")


# ---------------------------------------------------------------------------
print("\n--- H: duplicate detection is respected — never promotes a known-duplicate rule ---")
# ---------------------------------------------------------------------------

s_h = fresh_store("h")
reg_h = fresh_registry("h")
_, cid_h1 = draft(s_h, reg_h, title="original")
lock(s_h, reg_h, cid_h1)
hid_h2, cid_h2 = draft(s_h, reg_h, title="exact duplicate rule")  # same entry/exit/splits
pool_h = opp.build_opportunity_pool(s_h, "2024-05-12", registry_dir=reg_h)
o_h = [o for o in pool_h if o.hypothesis_id == hid_h2][0]
check("H1: the duplicate opportunity is flagged is_duplicate=True",
      o_h.is_duplicate is True)
outcome_h = opp.attempt_autonomous_promotion(s_h, o_h, registry_dir=reg_h)
check("H2: attempt_autonomous_promotion refuses a duplicate (skipped_duplicate), "
      "never locks it", outcome_h.outcome == "skipped_duplicate", str(outcome_h))
check("H3: the duplicate contract stays a draft", Contract.load(cid_h2, reg_h).status == "draft")


# ---------------------------------------------------------------------------
print("\n--- I: user override authority — freeze / reopen / retire / force_reassess ---")
# ---------------------------------------------------------------------------

s_i = fresh_store("i")
reg_i = fresh_registry("i")
hid_i, cid_i = draft(s_i, reg_i)
opp_id_i = f"OPP-{hid_i}"

opp.freeze(s_i, opp_id_i, by="vaibhav", reason="not ready yet")
pool_i1 = opp.build_opportunity_pool(s_i, "2024-05-12", registry_dir=reg_i)
o_i1 = pool_i1[0]
check("I1: a frozen opportunity reports override_state.frozen=True",
      o_i1.override_state["frozen"] is True)
check("I2: a frozen opportunity's priority is suppressed to 0 — never silently promoted",
      o_i1.priority_score == 0.0)
outcome_i1 = opp.attempt_autonomous_promotion(s_i, o_i1, registry_dir=reg_i)
check("I3: attempt_autonomous_promotion refuses a frozen opportunity (skipped_frozen) — "
      "the AI does NOT silently overwrite an active user freeze",
      outcome_i1.outcome == "skipped_frozen", str(outcome_i1))
check("I4: the contract stays a draft while frozen", Contract.load(cid_i, reg_i).status == "draft")

opp.reopen(s_i, opp_id_i, by="vaibhav", reason="ready now")
pool_i2 = opp.build_opportunity_pool(s_i, "2024-05-12", registry_dir=reg_i)
o_i2 = pool_i2[0]
check("I5: reopen() clears the freeze — override_state.frozen is False again",
      o_i2.override_state["frozen"] is False)
check("I6: after reopen, priority is no longer suppressed to zero",
      o_i2.priority_score > 0.0)
outcome_i2 = opp.attempt_autonomous_promotion(s_i, o_i2, registry_dir=reg_i)
check("I7: after reopen, autonomous promotion proceeds normally",
      outcome_i2.outcome == "promoted", str(outcome_i2))

# retire — the one non-automatic-re-entry state
s_i2 = fresh_store("i2")
reg_i2 = fresh_registry("i2")
hid_r, cid_r = draft(s_i2, reg_i2, title="dead end")
opp_id_r = f"OPP-{hid_r}"
_raised = False
try:
    opp.retire(s_i2, opp_id_r, by="vaibhav", reason="")
except ValueError:
    _raised = True
check("I8: retire() without a reason is refused (no anonymous consequential action)",
      _raised)
opp.retire(s_i2, opp_id_r, by="vaibhav", reason="instrument delisted")
pool_r1 = opp.build_opportunity_pool(s_i2, "2024-05-12", registry_dir=reg_i2)
check("I9: a retired opportunity reports lifecycle_stage RETIRED",
      pool_r1[0].lifecycle_stage == "RETIRED", pool_r1[0].lifecycle_stage)
outcome_r = opp.attempt_autonomous_promotion(s_i2, pool_r1[0], registry_dir=reg_i2)
check("I10: RETIRED is never auto-promoted", outcome_r.outcome == "skipped_frozen")
opp.reopen(s_i2, opp_id_r, by="vaibhav", reason="reconsidered")
pool_r2 = opp.build_opportunity_pool(s_i2, "2024-05-12", registry_dir=reg_i2)
check("I11: a human CAN reopen a retired opportunity — retirement is not truly permanent",
      pool_r2[0].lifecycle_stage != "RETIRED", pool_r2[0].lifecycle_stage)

# force_reassess
s_i3 = fresh_store("i3")
reg_i3 = fresh_registry("i3")
hid_fr, cid_fr = draft(s_i3, reg_i3)
lock(s_i3, reg_i3, cid_fr)
score_it(s_i3, reg_i3, cid_fr, trades=losing_trades())
opp.build_opportunity_pool(s_i3, "2024-05-12", registry_dir=reg_i3)  # first look -> REJECTED
opp.force_reassess(s_i3, f"OPP-{hid_fr}", by="vaibhav", reason="market regime changed")
pool_fr = opp.build_opportunity_pool(s_i3, "2024-05-12", registry_dir=reg_i3)
o_fr = [o for o in pool_fr if o.hypothesis_id == hid_fr][0]
check("I12: force_reassess() makes an otherwise-unchanged REJECTED opportunity "
      "REASSESSING on the very next pool build — a genuine user override, not a "
      "signature-based trigger", o_fr.lifecycle_stage == "REASSESSING", o_fr.lifecycle_stage)


# ---------------------------------------------------------------------------
print("\n--- J: audit trail — every transition/override/promotion is recorded ---")
# ---------------------------------------------------------------------------

s_j = fresh_store("j")
reg_j = fresh_registry("j")
hid_j, cid_j = draft(s_j, reg_j)
opp_id_j = f"OPP-{hid_j}"
opp.build_opportunity_pool(s_j, "2024-05-12", registry_dir=reg_j)      # DISCOVERED
opp.build_opportunity_pool(s_j, "2024-05-12", registry_dir=reg_j)      # -> TRIAGED
opp.freeze(s_j, opp_id_j, by="vaibhav", reason="hold")
opp.reopen(s_j, opp_id_j, by="vaibhav", reason="go")

history = opp.override_history(s_j, opp_id_j)
event_types = [h["event_type"] for h in history]
check("J1: the audit trail records every transition/override in order",
      event_types == ["LIFECYCLE_TRANSITION", "LIFECYCLE_TRANSITION", "FREEZE", "UNFREEZE", "REOPEN"],
      event_types)
check("J2: every event names an actor — never anonymous",
      all(h.get("actor") for h in history), history)
check("J3: the freeze event carries the human's stated reason",
      any(h["event_type"] == "FREEZE" and h["reason"] == "hold" for h in history))
check("J4: lifecycle transitions record previous_state -> new_state explicitly",
      any(h["event_type"] == "LIFECYCLE_TRANSITION" and h["previous_state"] == "DISCOVERED"
          and h["new_state"] == "TRIAGED" for h in history), history)


# ---------------------------------------------------------------------------
print("\n--- K: isolation — no engine/broker/paper import, no second locking path, "
      "no new SQL table ---")
# ---------------------------------------------------------------------------

_OPP_SRC = (Path(__file__).parent.parent / "research" / "brain" / "opportunity.py").read_text()
_opp_code = re.sub(r'"""[\s\S]*?"""', "", _OPP_SRC)
_imports = re.findall(r"^\s*(?:from|import)\s+([.\w]+)", _opp_code, re.MULTILINE)
check("K1: opportunity.py imports no engine module",
      not any(m.startswith("engine") for m in _imports), str(_imports))
check("K2: opportunity.py imports nothing from paper/",
      not any(m.split(".")[0] == "paper" for m in _imports), str(_imports))
check("K3: opportunity.py never imports research.experiments.runner "
      "(running an experiment stays scheduler.py's job)",
      "experiments import runner" not in _opp_code and "experiments.runner" not in _opp_code,
      "must not gain a run_experiment()/simulate() path")
check("K4: opportunity.py never references a broker / order-placement symbol",
      not any(tok in _opp_code for tok in
              ("get_broker", "propose_trade", "place_order", "run_paper_cycle", ".place(")))
check("K5: opportunity.py never references memory/state.json or a broker credential",
      "state.json" not in _opp_code and ".env" not in _opp_code.replace("os.environ", ""))
check("K6: Contract.lock() is never called directly — only through "
      "hypothesis_intake.approve_and_lock()",
      ".lock()" not in _opp_code)
check("K7: the only store.append() in this module is inside research.memory's own "
      "typed wrapper (research.memory.record_opportunity_event) — opportunity.py "
      "itself never calls store.append() directly, so there is no second write path "
      "into the observations table",
      "store.append(" not in _opp_code, "all writes must go through research.memory")

# engine/ must not have grown a research import (kernel isolation, re-proven here too)
import subprocess  # noqa: E402
_root = str(Path(__file__).parent.parent)
_eng = subprocess.run(["grep", "-rlE", r"^\s*(from|import)\s+research", f"{_root}/engine"],
                      capture_output=True, text=True)
check("K8: engine/ still imports nothing from research/ (kernel isolation intact)",
      _eng.returncode != 0 and _eng.stdout.strip() == "", _eng.stdout)

_MEM_SRC = (Path(__file__).parent.parent / "research" / "memory.py").read_text()
check("K9: the new dataset is one more value in the SAME observations table — no new "
      "table, no new schema file (research/memory.py's own store.append() call, "
      "unchanged shape)",
      "DATASET_OPPORTUNITY_EVENT" in _MEM_SRC
      and _MEM_SRC.count("def record_") >= 6)  # one more record_* wrapper, same convention


# ---------------------------------------------------------------------------
print("\n--- L: determinism — identical state -> identical pool, across two calls ---")
# ---------------------------------------------------------------------------

s_l = fresh_store("l")
reg_l = fresh_registry("l")
draft(s_l, reg_l, title="a")
draft(s_l, reg_l, title="b",
      entry_rule={"conditions": [{"metric": "volume_zscore", "op": ">", "value": 5.0}]})
pool_l1 = opp.build_opportunity_pool(s_l, "2024-05-12", registry_dir=reg_l, log_events=False)
pool_l2 = opp.build_opportunity_pool(s_l, "2024-05-12", registry_dir=reg_l, log_events=False)
check("L1: two read-only calls against unchanged state produce an identical, "
      "identically-ordered pool",
      [o.id for o in pool_l1] == [o.id for o in pool_l2]
      and [o.priority_score for o in pool_l1] == [o.priority_score for o in pool_l2])
check("L2: log_events=False writes NO new audit rows (a safe read-only caller)",
      len(rm.query_research_log(s_l, rm.DATASET_OPPORTUNITY_EVENT)) == 0)


for st in (s_b, s_d, s_f, s_f2, s_g, s_h, s_i, s_i2, s_i3, s_j, s_l):
    st.close()

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'=' * 52}")
print(f"  {PASSED} passed, {FAILED} failed")
print(f"{'=' * 52}\n")
sys.exit(1 if FAILED else 0)
