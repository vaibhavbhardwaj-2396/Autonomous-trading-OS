"""Acceptance coverage for the Phase 5-10 integrated release."""

import datetime as dt
import json
import shutil
import sys
import tempfile
import pandas as pd
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from paper import algorithms as paper_algorithms
from paper.runner import _paper_exit_reason
from research.data_quality import DataRequirement, build_data_quality_report
from research.experiments import strategy_backtest as sb
from research.store import IST, Store
from research.contracts import Contract
from research import strategy_factory as factory
from strategies.spec import (ALGORITHM_ID, ContractRuleStrategy, ExitPolicy,
                             RuleCondition, StrategySpec, StrategySpecViolation,
                             compile_spec)

PASSED = FAILED = 0


def check(name, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  ✓ {name}")
    else:
        FAILED += 1
        print(f"  ✗ {name}: {detail}")


tmp = Path(tempfile.mkdtemp(prefix="lq-phase-5-10-"))
store = Store.open(tmp / "research.db")
try:
    spec = StrategySpec(
        strategy_id="STRAT-H1",
        conditions=(RuleCondition("close", ">=", 1.0, 2),),
        exit=ExitPolicy(stop_loss_pct=3, target_pct=5, max_hold_days=5),
        universe="watchlist")
    version = compile_spec(spec, derived_from_hypothesis_id="H1")
    check("typed compilation is deterministic", version == compile_spec(
        spec, derived_from_hypothesis_id="H1"))
    check("compiled provenance is immutable", version.derived_from_hypothesis_id == "H1")
    try:
        RuleCondition("__import__", ">", 0)
        refused = False
    except StrategySpecViolation:
        refused = True
    check("unknown DSL metrics fail closed", refused)
    check("production algorithm is explicitly registered in backtest and paper",
          sb.ALGORITHM_REGISTRY.get(ALGORITHM_ID) is ContractRuleStrategy and
          paper_algorithms.ALGORITHM_REGISTRY.get(ALGORITHM_ID) is ContractRuleStrategy)

    days = [dt.date(2026, 1, 5) + dt.timedelta(days=i) for i in range(4)]
    for day, close in zip(days, [100, 101, 107, 108]):
        knowledge = dt.datetime.combine(day, dt.time(18), tzinfo=IST)
        store.append_price("AAA", day, knowledge, "fixture", open_=close,
                           high=close, low=close, close=close, volume=1000)
    result = sb.run_backtest(version, store, start=days[0], end=days[-1],
                             universe=["AAA"], record_note=False, slippage_bps=10)
    check("realistic backtest applies typed target exits",
          any(t["exit_reason"] == "target" for t in result.trades), str(result.trades))
    check("realistic backtest declares two-sided slippage",
          result.cost_assumptions["slippage_bps_each_side"] == 10.0)

    report = build_data_quality_report(
        store, dt.datetime(2026, 1, 8, 20, tzinfo=IST),
        requirements=(DataRequirement("prices_eod", 48),))
    check("data quality proves append-only protection", report["append_only_enforced"])
    check("fresh required market data reports READY", report["status"] == "READY", str(report))
    historical = store.quality_inventory(dt.datetime(2026, 1, 6, 20, tzinfo=IST))
    check("quality inventory is knowledge-time gated",
          historical["prices_eod"]["n"] == 2, str(historical["prices_eod"]))

    contract_dir, strategy_dir = tmp / "contracts", tmp / "strategies"
    contract = Contract(
        id="C-H1", title="H1", hypothesis="edge", null_hypothesis="none",
        universe="watchlist", signal="typed rule",
        entry_rule=json.dumps({"conditions": [{"metric": "close", "op": ">=", "value": 1}]}),
        exit_rule=json.dumps({"stop_loss_pct": 3, "target_pct": 5, "max_hold_days": 5}),
        splits={"discovery": ["2026-01-01", "2026-01-31"]}, independence="daily",
        falsification="negative net expectancy", abandon_condition="negative holdout",
        evaluation_start="2026-01-01", evaluation_end="2026-02-01")
    contract.lock()
    contract.status = "reported"
    contract.save(contract_dir)
    original_pool = factory.build_opportunity_pool
    import research.experiments.evaluator as evaluator
    original_ids = evaluator.contract_ids_for_hypothesis
    factory.build_opportunity_pool = lambda *a, **k: [SimpleNamespace(
        hypothesis_id="H1", lifecycle_stage="ROBUST")]
    evaluator.contract_ids_for_hypothesis = lambda *a, **k: ["C-H1"]
    try:
        promoted = factory.promote_robust_hypothesis(
            store, "H1", as_of=dt.datetime(2026, 2, 2, tzinfo=IST),
            contract_registry_dir=contract_dir, strategy_registry_dir=strategy_dir)
        repeated = factory.promote_robust_hypothesis(
            store, "H1", as_of=dt.datetime(2026, 2, 2, tzinfo=IST),
            contract_registry_dir=contract_dir, strategy_registry_dir=strategy_dir)
    finally:
        factory.build_opportunity_pool = original_pool
        evaluator.contract_ids_for_hypothesis = original_ids
    check("ROBUST evidence promotes to an immutable registry version", promoted.created)
    check("strategy promotion is idempotent", not repeated.created and
          repeated.version_id == promoted.version_id)

    # _paper_exit_reason calls the ordinary fill adapter; a tiny list of bars
    # exercises it without touching the paper database or any broker.
    exit_reason = _paper_exit_reason(
        {"entry_price": 100.0, "opened_at": "2026-01-01T09:00:00+05:30"},
        pd.DataFrame([{"close": 94.0}], index=[dt.datetime(2026, 1, 2, tzinfo=IST)]),
        dt.datetime(2026, 1, 2, tzinfo=IST),
        {"stop_loss_pct": 5})
    check("paper runner reconciles typed stop exits", exit_reason == "stop_loss")

    source = (Path(__file__).parent.parent / "research" / "strategy_factory.py").read_text()
    check("factory has no broker or live execution path",
          "engine.execute" not in source and "engine.broker" not in source)
finally:
    store.close()
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{PASSED} passed, {FAILED} failed")
sys.exit(1 if FAILED else 0)
