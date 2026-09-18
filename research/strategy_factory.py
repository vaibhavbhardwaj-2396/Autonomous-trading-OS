"""One-way, evidence-gated promotion from research to immutable strategies.

The factory is intentionally incapable of paper approval, live enablement or
broker access.  Its only write is an idempotent StrategyVersion registry file
plus an append-only provenance note.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from strategies import registry as strategy_registry
from strategies.spec import ExitPolicy, RuleCondition, StrategySpec, compile_spec

from . import memory as rm
from .brain.opportunity import build_opportunity_pool
from .contracts import REGISTRY_DIR, Contract
from .store import Store, TimeLike


class StrategyPromotionRefused(RuntimeError):
    """Evidence or contract state is insufficient for deterministic promotion."""


@dataclass(frozen=True)
class PromotionResult:
    hypothesis_id: str
    contract_id: str
    strategy_id: str
    version_id: str
    registry_path: str
    created: bool


def promote_robust_hypothesis(
    store: Store, hypothesis_id: str, *, as_of: TimeLike,
    contract_registry_dir: Path = REGISTRY_DIR,
    strategy_registry_dir: Path = strategy_registry.REGISTRY_DIR,
) -> PromotionResult:
    """Compile one ROBUST hypothesis, refusing every weaker lifecycle stage."""
    opportunity = next((o for o in build_opportunity_pool(
        store, as_of, registry_dir=contract_registry_dir, log_events=False)
                        if o.hypothesis_id == hypothesis_id), None)
    if opportunity is None or opportunity.lifecycle_stage != "ROBUST":
        actual = opportunity.lifecycle_stage if opportunity else "MISSING"
        raise StrategyPromotionRefused(
            f"hypothesis {hypothesis_id!r} is {actual}, not ROBUST")

    candidates = []
    from .experiments.evaluator import contract_ids_for_hypothesis
    for contract_id in contract_ids_for_hypothesis(store, hypothesis_id):
        try:
            contract = Contract.load(contract_id, contract_registry_dir)
            contract.verify()
        except (FileNotFoundError, OSError):
            continue
        if contract.status == "reported":
            candidates.append(contract)
    if not candidates:
        raise StrategyPromotionRefused("no verified reported contract backs the hypothesis")
    contract = sorted(candidates, key=lambda c: c.id)[0]

    try:
        entry = json.loads(contract.entry_rule)
        exit_rule = json.loads(contract.exit_rule)
        raw_conditions = entry["conditions"]
    except (TypeError, KeyError, json.JSONDecodeError) as exc:
        raise StrategyPromotionRefused(f"contract rules are not valid typed JSON: {exc}") from exc

    conditions = tuple(RuleCondition(
        metric=item["metric"], op=item["op"], value=item["value"],
        window=item.get("window_days", 20)) for item in raw_conditions)
    strategy_id = "STRAT-" + re.sub(r"[^A-Za-z0-9_.-]+", "-", hypothesis_id).strip("-")
    spec = StrategySpec(strategy_id=strategy_id, conditions=conditions,
                        exit=ExitPolicy(**exit_rule), universe=contract.universe)
    version = compile_spec(spec, derived_from_hypothesis_id=hypothesis_id)
    path = strategy_registry_dir / f"{version.version_id}.json"
    created = not path.exists()
    saved = strategy_registry.save_version(version, directory=strategy_registry_dir)
    if created:
        rm.record_research_note(
            store, source="research.strategy_factory",
            note=(f"Promoted ROBUST hypothesis {hypothesis_id} through contract "
                  f"{contract.id} to immutable strategy version {version.version_id}."),
            extra={"hypothesis_id": hypothesis_id, "contract_id": contract.id,
                   "strategy_id": strategy_id, "version_id": version.version_id,
                   "lifecycle_stage": "ROBUST"})
    return PromotionResult(hypothesis_id, contract.id, strategy_id, version.version_id,
                           str(saved), created)
