"""Regression coverage for the discovery -> confirmation -> strategy seam.

This is intentionally isolated from the real registry, market store, paper
ledger and broker.  It proves the production dead-end fixed in October 2026:
legacy discovery-only work can receive one frozen-rule reserved holdout, and
only that structurally linked confirmation can use the five-slot reserve.
"""

import datetime as dt
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from research import memory as rm
from research.brain import hypothesis_intake as hi
from research.brain import opportunity
from research.contracts import Contract
from research import strategy_factory
from research.store import Store


PASSED = FAILED = 0
TMP = Path(tempfile.mkdtemp(prefix="lq-test-confirmation-lane-"))


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


def fresh(name):
    db = TMP / f"{name}.db"
    reg = TMP / f"registry-{name}"
    shutil.rmtree(reg, ignore_errors=True)
    return Store.open(db), reg


def verdict(t_stat=5.0):
    return {"n_trades": 40, "win_rate": 0.6, "gross_pnl": 20_000.0,
            "net_pnl": 20_000.0, "total_costs": 0.0,
            "avg_net_pnl": 500.0, "expectancy_r": 0.2, "t_stat": t_stat}


def reported_parent(store, reg, cid="EXP-LEGACY-A", hid="HYP-LEGACY"):
    c = Contract(
        id=cid, title="legacy promising rule", hypothesis="volume drift persists",
        null_hypothesis="no drift", universe="watchlist",
        signal="observatory.volume_zscore",
        entry_rule=json.dumps({"conditions": [
            {"metric": "volume_zscore", "op": ">", "value": 3.0}]}),
        exit_rule=json.dumps({"max_hold_days": 5}),
        splits={"discovery": ["2022-01-01", "2024-12-31"]},
        independence="one event per symbol", falsification="t_stat < 2",
        abandon_condition="expectancy_r <= 0", evaluation_start="2022-01-01",
        evaluation_end="2024-12-31")
    c.lock()
    c.status = "reported"
    c.save(reg)
    rm.record_hypothesis_proposal(store, claim=c.hypothesis, source="test",
                                  hypothesis_id=hid,
                                  extra={"contract_id": cid})
    rm.record_experiment_verdict(store, contract_id=cid, hypothesis_id=hid,
                                 verdict=verdict())
    store.append_price(symbol="TEST", session_date=dt.date(2026, 6, 30),
                       knowledge_time=dt.date(2026, 6, 30), source="test",
                       open_=100, high=101, low=99, close=100, volume=1000)
    return c


def opp(hid, cid):
    return opportunity.Opportunity(
        id=f"OPP-{hid}", type="RETEST_HYPOTHESIS", hypothesis_id=hid,
        contract_id=cid, title="test", lifecycle_stage="PROMISING",
        priority_score=1.0, priority_components={}, confidence=0.9,
        evidence_verdict="PROMISING", research_area=None, is_duplicate=False,
        reassessment_eligible=False, override_state={"frozen": False, "retired": False},
        compute_cost_estimate=1.0, contract_statuses=("reported",),
        evidence_signature="test")


print("\n--- A: autonomous ResearchPacket proposals must reserve a real holdout ---")
proposal = {
    "title": "test", "hypothesis": "testable claim", "null_hypothesis": "no effect",
    "universe": "watchlist", "signal": "observatory.volume_zscore",
    "entry_rule": {"conditions": [{"metric": "volume_zscore", "op": ">", "value": 3}]},
    "exit_rule": {"max_hold_days": 5},
    "splits": {"discovery": ["2022-01-01", "2024-12-31"]},
    "independence": "one event", "falsification": "t_stat < 2",
    "abandon_condition": "expectancy_r <= 0", "evaluation_start": "2022-01-01",
    "evaluation_end": "2024-12-31", "source_research_packet": "PKT-1",
    "economic_mechanism": "attention", "expected_effect": "positive drift",
    "features": ["volume_zscore"], "time_horizon": "5 sessions",
    "regime_assumption": "all", "required_datasets": ["prices_eod"],
    "related_research": [],
}
check("A1: discovery-only autonomous proposal is rejected",
      any("cannot ever reach ROBUST" in p for p in hi.validate_proposal(proposal)))
proposal["splits"]["holdout"] = ["2025-01-01", "2026-06-30"]
check("A2: a non-overlapping pre-registered holdout is accepted",
      hi.validate_proposal(proposal) == [], hi.validate_proposal(proposal))


print("\n--- B: a legacy promising parent receives one immutable reserved holdout ---")
store, reg = fresh("derive")
parent = reported_parent(store, reg)
created = hi.derive_reserved_holdout_contract(store, parent.id, "HYP-LEGACY",
                                               registry_dir=reg)
child = created.contract
check("B1: sibling is a new draft on the fixed reserved window",
      child.id != parent.id and child.status == "draft"
      and (child.evaluation_start, child.evaluation_end) == hi.LEGACY_RESERVED_HOLDOUT)
check("B2: universe and frozen rules are byte-identical to the parent",
      (child.universe, child.entry_rule, child.exit_rule)
      == (parent.universe, parent.entry_rule, parent.exit_rule))
claims = [r["payload"] for r in rm.query_research_log(store, rm.DATASET_HYPOTHESIS)]
lineage = next(p for p in claims if p.get("contract_id") == child.id)
check("B3: structural holdout lineage is explicit and auditable",
      lineage.get("split_of") == parent.id and lineage.get("split") == "holdout")
try:
    hi.derive_reserved_holdout_contract(store, parent.id, "HYP-LEGACY", registry_dir=reg)
    refused = False
except hi.IntakeRejected:
    refused = True
check("B4: the same parent cannot receive a second confirmation attempt", refused)


print("\n--- C: the reserve admits confirmation, never ordinary discovery ---")
now = dt.datetime.now()
for i in range(hi.MAX_LOCKS_PER_PERIOD):
    c = Contract(
        id=f"EXP-CAP-{i:02d}", title="capacity", hypothesis="capacity",
        null_hypothesis="none", universe="watchlist", signal="test",
        entry_rule=json.dumps({"conditions": []}),
        exit_rule=json.dumps({"max_hold_days": 1}),
        splits={"discovery": ["2022-01-01", "2022-02-01"]},
        independence="n/a", falsification="n/a", abandon_condition="n/a",
        evaluation_start="2022-01-01", evaluation_end="2022-02-01")
    c.lock()
    c.locked_at = now.isoformat(timespec="seconds")
    c.save(reg)
out = opportunity.attempt_autonomous_promotion(
    store, opp("HYP-LEGACY", parent.id), registry_dir=reg, now=now,
    contract_id=child.id)
check("C1: structural confirmation can use the reserved capacity lane",
      out.outcome == "promoted", out)

ordinary = Contract(
    id="EXP-ORDINARY", title="ordinary", hypothesis="ordinary",
    null_hypothesis="none", universe="watchlist", signal="test",
    entry_rule=json.dumps({"conditions": [{"metric": "return_1d", "op": ">", "value": 0.01}]}),
    exit_rule=json.dumps({"max_hold_days": 2}),
    splits={"discovery": ["2022-01-01", "2022-12-31"]},
    independence="n/a", falsification="n/a", abandon_condition="n/a",
    evaluation_start="2022-01-01", evaluation_end="2022-12-31")
ordinary.save(reg)
ordinary_opp = opp("HYP-ORDINARY", ordinary.id)
ordinary_opp = opportunity.Opportunity(**{**ordinary_opp.__dict__,
    "lifecycle_stage": "DISCOVERED", "evidence_verdict": None,
    "contract_statuses": ("draft",)})
blocked = opportunity.attempt_autonomous_promotion(
    store, ordinary_opp, registry_dir=reg, now=now)
check("C2: ordinary discovery remains blocked after the general 20-lock ceiling",
      blocked.outcome == "skipped_budget", blocked)


print("\n--- D: strategy compilation uses the confirmed parent rule ---")
confirmed = Contract.load(child.id, reg)
confirmed.status = "reported"
confirmed.save(reg)
rm.record_experiment_verdict(store, contract_id=child.id, hypothesis_id="HYP-LEGACY",
                             verdict=verdict())
strategy_reg = TMP / "strategy-registry"
result = strategy_factory.promote_robust_hypothesis(
    store, "HYP-LEGACY", as_of=dt.datetime.now(dt.timezone.utc),
    contract_registry_dir=reg, strategy_registry_dir=strategy_reg)
check("D1: a positive structural holdout makes the hypothesis promotable",
      result.created and result.version_id)
check("D2: the strategy records the frozen parent, not an arbitrary sibling",
      result.contract_id == parent.id, result)


print("\n--- E: positive but noisy holdout evidence does not become ROBUST ---")
weak_store, weak_reg = fresh("weak-confirmation")
weak_parent = reported_parent(weak_store, weak_reg, cid="EXP-WEAK-A", hid="HYP-WEAK")
weak_child = hi.derive_reserved_holdout_contract(
    weak_store, weak_parent.id, "HYP-WEAK", registry_dir=weak_reg).contract
weak_child.lock()
weak_child.status = "reported"
weak_child.save(weak_reg)
rm.record_experiment_verdict(weak_store, contract_id=weak_child.id,
                             hypothesis_id="HYP-WEAK", verdict=verdict(1.7))
weak_opportunity = next(o for o in opportunity.build_opportunity_pool(
    weak_store, dt.datetime.now(dt.timezone.utc), registry_dir=weak_reg,
    log_events=False) if o.hypothesis_id == "HYP-WEAK")
check("E1: positive P&L below adjusted significance remains PROMISING",
      weak_opportunity.lifecycle_stage == "PROMISING", weak_opportunity.lifecycle_stage)
try:
    strategy_factory.promote_robust_hypothesis(
        weak_store, "HYP-WEAK", as_of=dt.datetime.now(dt.timezone.utc),
        contract_registry_dir=weak_reg, strategy_registry_dir=TMP / "weak-strategies")
    weak_refused = False
except strategy_factory.StrategyPromotionRefused:
    weak_refused = True
check("E2: the strategy factory independently refuses that noisy holdout", weak_refused)


print("\n====================================================")
print(f"  {PASSED} passed, {FAILED} failed")
print("====================================================")
raise SystemExit(1 if FAILED else 0)
