"""Validated, deterministic strategy specification and compiler.

This module is deliberately broker- and portfolio-blind.  It translates the
small pre-registered research rule vocabulary into immutable StrategyVersion
parameters and provides the one reviewed implementation that can execute those
parameters in backtest and paper contexts.  Unknown fields or metrics fail
closed; no expression is ever evaluated as Python.
"""

from __future__ import annotations

import math
import operator
from dataclasses import dataclass
from typing import Any, Iterable

from .core import Strategy, StrategyContext, StrategyVersion, create_strategy_version

ALGORITHM_ID = "contract_rule.v1"
SUPPORTED_METRICS = frozenset({"close", "return_1d", "volume_zscore", "price_move_zscore"})
SUPPORTED_OPERATORS = frozenset({">", ">=", "<", "<=", "==", "!="})
_OPS = {">": operator.gt, ">=": operator.ge, "<": operator.lt,
        "<=": operator.le, "==": operator.eq, "!=": operator.ne}


class StrategySpecViolation(ValueError):
    """The proposed strategy cannot be represented safely and deterministically."""


@dataclass(frozen=True)
class RuleCondition:
    metric: str
    op: str
    value: float
    window: int = 20

    def __post_init__(self) -> None:
        if self.metric not in SUPPORTED_METRICS:
            raise StrategySpecViolation(f"unsupported strategy metric: {self.metric!r}")
        if self.op not in SUPPORTED_OPERATORS:
            raise StrategySpecViolation(f"unsupported strategy operator: {self.op!r}")
        if not isinstance(self.value, (int, float)) or not math.isfinite(float(self.value)):
            raise StrategySpecViolation("condition value must be finite")
        if not isinstance(self.window, int) or not 2 <= self.window <= 252:
            raise StrategySpecViolation("condition window must be an integer in [2, 252]")

    def to_dict(self) -> dict:
        return {"metric": self.metric, "op": self.op, "value": float(self.value),
                "window": self.window}


@dataclass(frozen=True)
class ExitPolicy:
    stop_loss_pct: float | None = None
    target_pct: float | None = None
    max_hold_days: int | None = None

    def __post_init__(self) -> None:
        if all(v is None for v in (self.stop_loss_pct, self.target_pct, self.max_hold_days)):
            raise StrategySpecViolation("at least one exit rule is required")
        for name, value in (("stop_loss_pct", self.stop_loss_pct), ("target_pct", self.target_pct)):
            if value is not None and (not isinstance(value, (int, float)) or
                                      not math.isfinite(float(value)) or not 0 < float(value) <= 50):
                raise StrategySpecViolation(f"{name} must be a percentage in (0, 50]")
        if self.max_hold_days is not None and (
                not isinstance(self.max_hold_days, int) or not 1 <= self.max_hold_days <= 365):
            raise StrategySpecViolation("max_hold_days must be an integer in [1, 365]")

    def to_dict(self) -> dict:
        return {k: v for k, v in {
            "stop_loss_pct": self.stop_loss_pct, "target_pct": self.target_pct,
            "max_hold_days": self.max_hold_days}.items() if v is not None}


@dataclass(frozen=True)
class StrategySpec:
    strategy_id: str
    conditions: tuple[RuleCondition, ...]
    exit: ExitPolicy
    universe: str

    def __post_init__(self) -> None:
        if not self.strategy_id.strip():
            raise StrategySpecViolation("strategy_id is required")
        if not self.conditions:
            raise StrategySpecViolation("at least one entry condition is required")
        if not self.universe.strip():
            raise StrategySpecViolation("universe is required")

    @property
    def required_lookback(self) -> int:
        return max(c.window for c in self.conditions) + 2

    def to_parameters(self) -> dict:
        return {"schema_version": 1, "universe": self.universe,
                "entry": {"all": [c.to_dict() for c in self.conditions]},
                "exit": self.exit.to_dict()}

    @classmethod
    def from_parameters(cls, strategy_id: str, raw: dict) -> "StrategySpec":
        if set(raw) != {"schema_version", "universe", "entry", "exit"} or raw["schema_version"] != 1:
            raise StrategySpecViolation("unknown or missing top-level strategy fields")
        entry = raw["entry"]
        if not isinstance(entry, dict) or set(entry) != {"all"} or not isinstance(entry["all"], list):
            raise StrategySpecViolation("entry must contain exactly an 'all' condition list")
        conditions = []
        for item in entry["all"]:
            if not isinstance(item, dict) or not set(item) <= {"metric", "op", "value", "window"}:
                raise StrategySpecViolation("invalid entry condition fields")
            conditions.append(RuleCondition(**item))
        if not isinstance(raw["exit"], dict) or not set(raw["exit"]) <= {
                "stop_loss_pct", "target_pct", "max_hold_days"}:
            raise StrategySpecViolation("invalid exit policy fields")
        return cls(strategy_id=strategy_id, conditions=tuple(conditions),
                   exit=ExitPolicy(**raw["exit"]), universe=str(raw["universe"]))


class ContractRuleStrategy(Strategy):
    """Long-entry signal generator for the reviewed contract-rule vocabulary."""

    def __init__(self, version: StrategyVersion) -> None:
        self.spec = StrategySpec.from_parameters(version.strategy_id, version.parameters)
        super().__init__(version, required_lookback=self.spec.required_lookback,
                         required_data=("prices_eod",))

    def generate_signal(self, context: StrategyContext, universe: list[str]) -> list:
        signals = []
        for symbol in universe:
            history = context.history(symbol, self.required_lookback)
            if history is None or len(history) < 2:
                continue
            values = [self._metric(history, condition) for condition in self.spec.conditions]
            if all(value is not None and _OPS[c.op](value, float(c.value))
                   for c, value in zip(self.spec.conditions, values)):
                reasons = tuple(f"{c.metric}={value:.6g} {c.op} {c.value:g}"
                                for c, value in zip(self.spec.conditions, values))
                signals.append(self._make_signal(symbol=symbol, action="BUY",
                                                 generated_at=context.as_of, reasons=reasons))
        return signals

    @staticmethod
    def _metric(history: Any, condition: RuleCondition) -> float | None:
        def series(name: str) -> list[float]:
            try:
                raw: Iterable[Any] = history[name].tolist()
            except Exception:
                raw = [row.get(name) for row in history]
            return [float(v) for v in raw if v is not None]

        closes = series("close")
        if not closes:
            return None
        if condition.metric == "close":
            return closes[-1]
        returns = [(b / a) - 1.0 for a, b in zip(closes, closes[1:]) if a]
        if not returns:
            return None
        if condition.metric == "return_1d":
            return returns[-1]
        base = returns[-(condition.window + 1):-1]
        if condition.metric == "volume_zscore":
            volumes = series("volume")
            base = volumes[-(condition.window + 1):-1]
            current = volumes[-1] if volumes else None
        else:
            current = returns[-1]
        if current is None or len(base) < 2:
            return None
        mean = sum(base) / len(base)
        variance = sum((x - mean) ** 2 for x in base) / (len(base) - 1)
        return None if variance <= 1e-24 else (current - mean) / math.sqrt(variance)


def compile_spec(spec: StrategySpec, *, derived_from_hypothesis_id: str | None = None) -> StrategyVersion:
    """Compile a validated spec into a source-locked, immutable version."""
    return create_strategy_version(strategy_id=spec.strategy_id, algorithm_id=ALGORITHM_ID,
                                   parameters=spec.to_parameters(), implementation=ContractRuleStrategy,
                                   derived_from_hypothesis_id=derived_from_hypothesis_id)
