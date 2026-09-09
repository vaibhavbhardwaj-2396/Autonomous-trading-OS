"""
paper/algorithms.py — explicit Strategy-class resolution for the paper
engine, by algorithm_id.

Deliberately its own small registry, not an import of
research.experiments.strategy_backtest.ALGORITHM_REGISTRY / register_
algorithm / resolve_algorithm. Two independent reasons, both already
established conventions elsewhere in this repository:

  1. Importing that module would pull research/experiments/
     strategy_backtest.py's own research/ dependencies (Replay, Store,
     research.memory) into paper/, which would blur the isolation boundary
     paper/__init__.py documents (paper/ imports only engine.market_data
     and engine.costs from engine/, and nothing from research/ at all —
     see the AA spec section 23, "research remains upstream").
  2. A small, independent registry-of-the-same-shape is exactly the
     pattern this project already uses whenever two adapters need the same
     KIND of lookup for different purposes — e.g.
     STRATEGY_BACKTEST_POSITION_NOTIONAL vs. runner.RESEARCH_POSITION_
     NOTIONAL, or this file's own resolve_algorithm() vs. that module's.
     Duplicating ~15 lines of "explicit registration, fail closed" is a
     far smaller coupling than importing a research-heavy adapter module.

Same safety posture as the original: explicit registration only, no
eval/exec, no dynamic import, fail closed (UnknownAlgorithm) on an
unresolved id.
"""

from __future__ import annotations

from strategies.core import Strategy

ALGORITHM_REGISTRY: dict[str, type] = {}


class UnknownAlgorithm(RuntimeError):
    """No Strategy implementation is registered for a given algorithm_id.
    paper/runner.py treats this as a per-version skip (see the AA spec
    section 20's "missing strategy -> skip and record reason"), never as a
    reason to abort the whole cycle."""


def register_algorithm(algorithm_id: str, strategy_cls: type) -> None:
    if not (isinstance(strategy_cls, type) and issubclass(strategy_cls, Strategy)):
        raise TypeError(
            f"register_algorithm requires a Strategy subclass, got {strategy_cls!r}")
    ALGORITHM_REGISTRY[algorithm_id] = strategy_cls


def resolve_algorithm(algorithm_id: str, registry: dict[str, type] = None) -> type:
    reg = ALGORITHM_REGISTRY if registry is None else registry
    try:
        return reg[algorithm_id]
    except KeyError:
        raise UnknownAlgorithm(
            f"no Strategy implementation is registered for algorithm_id="
            f"{algorithm_id!r} in paper/algorithms.py's registry. Register it "
            f"explicitly via paper.algorithms.register_algorithm() before "
            f"marking a StrategyVersion built from it paper-eligible.")
