"""
research/experiments/strategy_backtest.py — Phase 3 Slice Y.

The historical backtest adapter for the Strategy domain Slice X created.
Slice X built Strategy / StrategyVersion / Signal / StrategyContext as a
top-level package that imports nothing from research/ or engine/ — a pure
domain model with no way, on its own, to run against historical data. This
module is the other half of the arrow:

    research/experiments/strategy_backtest.py     <- THIS FILE
            |
            v
    StrategyContext adapter          (ReplayStrategyContext, below)
            |
            v
    strategies.StrategyVersion / Strategy         (Slice X, unmodified)
            |
            v
    Signal

The dependency direction is deliberate and one-way: this file imports
strategies/, research/, and (for costs/universe only, exactly as
research/experiments/runner.py already does) a couple of narrow pieces of
engine/. strategies/ imports none of that back — see
tests/test_strategy_backtest.py's isolation section, which re-confirms this
alongside the check tests/test_kernel_isolation.py already added in Slice X.
Being an adapter is exactly why this file is ALLOWED to import research
infrastructure (Replay, AsOfView, research.memory) where the Strategy
package itself categorically cannot.

What this file is NOT
----------------------
It is not a second experiment runner. It never calls
research.experiments.runner.run_experiment, never writes to
Store.experiment_results (that table is scoped to contract_id-keyed rows
written exclusively by runner.py — see _compute_stats' docstring below for
why blending a Strategy backtest's trades into it would be a mistake, not a
convenience), and never touches a locked research.contracts.Contract. The
Contract-DSL pipeline (hypothesis -> Contract -> runner.run_experiment ->
evaluator/comparison) is completely unchanged and untouched by this slice.

It is also not a validation pipeline. A StrategyBacktestResult with a
positive net_pnl is a data point, not a certification — nothing in this
module or its output claims a Strategy is "validated," "proven," or "ready"
for anything downstream. That judgment (if it is ever made at all) is future
governance work, explicitly out of scope here.

Position/trade semantics (v0, deliberately minimal)
-----------------------------------------------------
A Strategy's Signal carries only strategy_id / strategy_version_id / symbol
/ action / generated_at / optional strength / optional reasons — no stop, no
target, no size. So this adapter's simulated trade model is the simplest
one that is still honest about what a bare BUY/SELL signal stream means:

    BUY  for a symbol with no open position   -> open one long position
    BUY  for a symbol already held             -> ignored (no pyramiding)
    SELL for a symbol currently held            -> close it at that step's price
    SELL for a symbol with no open position     -> ignored (no short-selling)
    still open when the backtest window ends    -> closed at the final step's
                                                    price, exit_reason
                                                    "backtest_end" (never left
                                                    dangling, exactly the
                                                    documented simplification
                                                    runner.simulate() already
                                                    makes for Contract-DSL
                                                    experiments)

No stop-loss, no target, no trailing logic, no partial fills, no slippage
model beyond the existing pure cost function below. This is a "prove the
plumbing" backtest, not an execution simulator — see the module docstring's
opening description and this slice's own explicit non-goals.

Position sizing uses a fixed research notional
(STRATEGY_BACKTEST_POSITION_NOTIONAL below), the same "constant across every
study so studies are comparable" reasoning research.experiments.runner uses
for its own RESEARCH_POSITION_NOTIONAL — defined here as an independent
constant, not imported from runner.py, so a change made to one for its own
reasons can never silently move the other (the same principle
research/experiments/comparison.py's own docstring states for its
threshold constants).

Evidence integration (the documented gap)
--------------------------------------------
research/experiments/evaluator.py and research/experiments/comparison.py are
NOT modified, imported for their computation, or called by this module.
Both are inseparably Contract-and-hypothesis shaped: evaluator.compute_
verdict reads Store.experiment_results(contract_id); comparison.
evaluate_hypothesis_evidence resolves a hypothesis_id via the
hypothesis-claim log, loads Contract objects from a registry directory, and
fingerprints their entry_rule/exit_rule/universe/splits fields to detect
duplicates. None of that exists for a Strategy backtest — there is no
Contract, no hypothesis_id, no entry_rule/exit_rule DSL. Forcing a
StrategyBacktestResult through either function (or writing its trades into
Store.experiment_results so evaluator would read them) would either raise
immediately or silently misattribute a Strategy's trades to the Contract
pipeline's own bookkeeping. So a StrategyBacktestResult's `stats` field
mirrors evaluator.compute_verdict's OUTPUT SHAPE (n_trades, win_rate,
gross_pnl, net_pnl, total_costs, avg_net_pnl, expectancy_r, t_stat) for a
consistent vocabulary across both paths, but is computed independently by
this module — never through evaluator.py itself — and carries no
PROMISING/WEAK/INCONCLUSIVE/CONTRADICTED verdict label at all, since that
label is exactly the Contract-and-hypothesis-specific machinery described
above. A future slice may design a Strategy-shaped evidence model on
purpose; this one does not attempt to force-fit today's.

The one existing research mechanism this module DOES reuse for persistence
is research.memory.record_research_note — a generic, dataset-agnostic,
already-Contract-independent log entry (runner.py uses the exact same
function for its own run-completion notes), used here only to leave a
plain-text record that a backtest ran. No new table, no schema change.

Determinism
-----------
Given the same StrategyVersion, the same historical data in the Store, and
the same backtest parameters (start, end, universe, cost/sizing
assumptions), run_backtest() returns an identical result. Every timestamp
in the result comes from a research.replay.ReplayStep's own `as_of` — never
a wall-clock read — and nothing here uses randomness, network access, a
broker, or live engine state. No execution timestamp ("run at...") is
recorded on the returned result at all, on purpose: adding one would make
the result's own repr depend on when it happened to be produced, which is
exactly the kind of accidental non-determinism a leak-check would otherwise
have to route around rather than catch.
"""

from __future__ import annotations

import math
import statistics
import datetime as dt
from dataclasses import dataclass, asdict, field
from typing import Any, Optional, Union

from .. import memory as rm
from ..store import Store
from ..replay import Replay, ReplayStep, DEFAULT_AS_OF_TIME
from strategies.core import (
    Signal, Strategy, StrategyContext, StrategyVersion, StrategyVersionViolation,
)
from strategies import registry as sreg

# A fixed, research-only hypothetical position size — see the module
# docstring's "Position sizing" section for why this is an independent
# constant rather than an import of runner.RESEARCH_POSITION_NOTIONAL.
STRATEGY_BACKTEST_POSITION_NOTIONAL = 100_000.0


class StrategyBacktestError(RuntimeError):
    """A problem with the backtest run itself (not a Strategy's own signal
    logic) — an unresolvable algorithm_id, a Signal for a symbol outside the
    resolved universe, or similar adapter-level failures. Raised before or
    during the walk; nothing partial is ever returned when this is raised."""


class UnknownAlgorithm(StrategyBacktestError):
    """`algorithm_id` has no registered Strategy implementation. Fail
    closed, deliberately: there is no dynamic import, no eval/exec, and no
    fallback interpretation of an unresolvable id — see register_algorithm/
    resolve_algorithm below, and the module docstring's discussion of why
    this is "the safest existing pattern" for this slice."""


# ---------------------------------------------------------------------------
# Algorithm resolution — explicit registration, never eval/exec/dynamic
# import. Slice X's own non-goal ("do NOT add a Turtle/Donchian/RSI/etc.
# example Strategy") means there is nothing built into the repository yet
# for this registry to hold; it exists so a FUTURE Strategy implementation
# (application code, reviewed and committed like any other module) has a
# safe, explicit place to register itself, and so this adapter has a fail-
# closed answer for "I don't recognise that algorithm_id" today.
# ---------------------------------------------------------------------------

ALGORITHM_REGISTRY: dict[str, type] = {}


def register_algorithm(algorithm_id: str, strategy_cls: type) -> None:
    """The one sanctioned way to make a Strategy subclass resolvable by
    algorithm_id. `strategy_cls` must be an actual Strategy subclass — a
    trusted, already-imported, already-reviewed Python class, never a string
    of source to compile or a path to dynamically import."""
    if not (isinstance(strategy_cls, type) and issubclass(strategy_cls, Strategy)):
        raise TypeError(
            f"register_algorithm requires a Strategy subclass, got {strategy_cls!r}")
    ALGORITHM_REGISTRY[algorithm_id] = strategy_cls


def resolve_algorithm(algorithm_id: str,
                      registry: Optional[dict[str, type]] = None) -> type:
    """Look up a registered Strategy class by algorithm_id. Fails closed —
    raises UnknownAlgorithm rather than guessing — when the id has never
    been registered. `registry` defaults to the module-level
    ALGORITHM_REGISTRY but accepts an explicit dict so tests never have to
    mutate (or clean up after mutating) shared global state."""
    reg = ALGORITHM_REGISTRY if registry is None else registry
    try:
        return reg[algorithm_id]
    except KeyError:
        raise UnknownAlgorithm(
            f"no Strategy implementation is registered for "
            f"algorithm_id={algorithm_id!r}. A StrategyVersion names the "
            f"algorithm it was built from; this adapter refuses to guess, "
            f"import dynamically, or otherwise resolve an unregistered id — "
            f"register it explicitly via register_algorithm() first.")


# ---------------------------------------------------------------------------
# StrategyContext adapter — the smallest possible bridge from a
# research.replay.ReplayStep to strategies.core.StrategyContext. All the
# no-look-ahead work is already done by ReplayStep/AsOfView (Slice 4); this
# class adds no gating logic of its own, it only narrows ReplayStep's much
# larger surface (regime(), candidates(), universe()) down to exactly the
# two members StrategyContext promises, so a Strategy implementation has no
# way to reach anything beyond as_of/history even by accident.
# ---------------------------------------------------------------------------

class ReplayStrategyContext:
    """Adapter satisfying strategies.core.StrategyContext, backed by one
    already-point-in-time-bound ReplayStep. A ReplayStep is built once per
    replay step and is never rebound to a later instant, so there is no
    "stale context reused across steps" failure mode to guard against here
    the way research.replay.make_provider must guard its bare callable — the
    object itself is permanently frozen at the instant it was built for.
    """

    __slots__ = ("_step",)

    def __init__(self, step: ReplayStep) -> None:
        self._step = step

    @property
    def as_of(self) -> dt.datetime:
        return self._step.as_of

    def history(self, symbol: str, days: int) -> Any:
        return self._step.history(symbol, days=days)


# ---------------------------------------------------------------------------
# Universe resolution — lives entirely here, in the adapter. A Strategy is
# handed a plain list[str] and never asked to know index membership,
# watchlist definitions, or survivorship rules — see the module docstring
# and Slice Y's own "Universe" section.
# ---------------------------------------------------------------------------

def _resolve_universe(step: ReplayStep, universe: Union[str, list[str]]) -> list[str]:
    """Three accepted shapes for `universe`:

        "watchlist"     -> engine.watchlist.UNIVERSE (static, liquid Nifty
                            large-caps; the same list engine/screener.py
                            itself trades against)
        an index name   -> step.universe(name), the point-in-time,
                            as-of-correct constituent list a Contract-DSL
                            experiment already uses via
                            research.experiments.runner._universe_for_step
        a literal list   -> used as-is, every step — an already-resolved,
                            fixed universe the caller supplied directly;
                            deliberately not survivorship-adjusted, since a
                            caller passing a literal list is explicitly
                            saying "this is the universe, don't recompute it"

    Defined independently here rather than importing runner.py's private
    helper of the same shape — this slice is told not to modify runner.py,
    and depending on one of its underscore-prefixed internals would be a
    tighter coupling than either module actually needs.
    """
    if isinstance(universe, str):
        if universe == "watchlist":
            from engine.watchlist import UNIVERSE
            return list(UNIVERSE)
        return step.universe(universe)
    return list(universe)


# ---------------------------------------------------------------------------
# Simulated trades and the backtest result
# ---------------------------------------------------------------------------

@dataclass
class SimulatedTrade:
    """One long round trip, opened by a BUY signal and closed by either a
    SELL signal or the end of the backtest window. See the module
    docstring's "Position/trade semantics" section for the full v0 model —
    there is no representation here for a short, a partial fill, or a
    stop/target exit, because nothing upstream of this dataclass (Signal
    itself) carries that information yet."""

    symbol: str
    entry_time: Any
    exit_time: Any
    entry_price: float
    exit_price: float
    quantity: int
    gross_pnl: float
    costs: float
    net_pnl: float
    exit_reason: str  # "signal_exit" | "backtest_end"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class StrategyBacktestResult:
    """Enough information to establish what was tested and what happened —
    deliberately NOT a claim that the Strategy is "validated," "proven," or
    ready for anything downstream. See the module docstring's second
    paragraph. Every field here is a pure function of
    (StrategyVersion, the Store's historical data, and the backtest
    parameters given to run_backtest()) — no execution timestamp is
    recorded, on purpose (see "Determinism" in the module docstring)."""

    strategy_id: str
    version_id: str
    algorithm_id: str
    start: str
    end: str
    n_steps: int
    universe_size_last_step: int
    signals: list[dict]
    trades: list[dict]
    stats: dict
    cost_assumptions: dict
    completed: bool

    def to_dict(self) -> dict:
        return asdict(self)


def _close_trade(symbol: str, pos: dict, exit_price: float, exit_time: dt.datetime,
                 exit_reason: str, intraday: bool) -> SimulatedTrade:
    from engine.costs import equity_round_trip  # pure function, no live state

    qty = pos["quantity"]
    entry_price = pos["entry_price"]
    gross_pnl = (exit_price - entry_price) * qty
    costs = equity_round_trip(entry_price, qty, intraday=intraday).total
    net_pnl = gross_pnl - costs
    return SimulatedTrade(
        symbol=symbol, entry_time=pos["entry_time"], exit_time=exit_time,
        entry_price=entry_price, exit_price=exit_price, quantity=qty,
        gross_pnl=gross_pnl, costs=costs, net_pnl=net_pnl, exit_reason=exit_reason,
    )


def _compute_stats(trades: list[SimulatedTrade]) -> dict:
    """Mirrors research.experiments.evaluator's compute_verdict OUTPUT SHAPE
    for a consistent vocabulary across the Contract-DSL and Strategy
    backtest paths, but is computed independently — see the module
    docstring's "Evidence integration" section for exactly why this is not
    a call to evaluator.compute_verdict itself. expectancy_r is always None
    in v0: an r-multiple needs a stop distance, and a bare Signal carries
    none — recording a fabricated one would be worse than admitting it is
    not knowable yet."""
    n = len(trades)
    if n == 0:
        return {"n_trades": 0, "win_rate": None, "gross_pnl": 0.0, "net_pnl": 0.0,
                "total_costs": 0.0, "avg_net_pnl": None, "expectancy_r": None,
                "t_stat": None}

    net_pnls = [t.net_pnl for t in trades]
    gross_pnls = [t.gross_pnl for t in trades]
    costs = [t.costs for t in trades]
    wins = sum(1 for p in net_pnls if p > 0)

    return {
        "n_trades": n,
        "win_rate": wins / n,
        "gross_pnl": sum(gross_pnls),
        "net_pnl": sum(net_pnls),
        "total_costs": sum(costs),
        "avg_net_pnl": statistics.fmean(net_pnls),
        "expectancy_r": None,
        "t_stat": _t_stat(net_pnls),
    }


def _t_stat(sample: list[float]) -> Optional[float]:
    """The same one-sample-vs-zero t-statistic evaluator.py computes,
    reimplemented locally rather than imported across the module boundary —
    a few lines of ordinary statistics, not worth coupling two otherwise-
    independent modules over."""
    n = len(sample)
    if n < 2:
        return None
    mean = statistics.fmean(sample)
    sd = statistics.stdev(sample)
    if sd <= 1e-12:
        return None
    return mean / (sd / math.sqrt(n))


# ---------------------------------------------------------------------------
# The backtest itself
# ---------------------------------------------------------------------------

def run_backtest(
    version: Union[StrategyVersion, str],
    store: Store,
    *,
    start,
    end,
    universe: Union[str, list[str]] = "watchlist",
    algorithm_registry: Optional[dict[str, type]] = None,
    registry_dir=sreg.REGISTRY_DIR,
    as_of_time: dt.time = DEFAULT_AS_OF_TIME,
    intraday: bool = False,
    position_notional: float = STRATEGY_BACKTEST_POSITION_NOTIONAL,
    record_note: bool = True,
) -> StrategyBacktestResult:
    """Run one StrategyVersion over historical Replay steps from `start` to
    `end` and return a StrategyBacktestResult.

    `version` may be an already-constructed StrategyVersion, or a
    version_id string to load (read-only — see strategies/registry.py's own
    load_version()) from `registry_dir`. Either way, the version is verified
    twice before a single step is walked: StrategyVersion.verify() (internal
    self-consistency) and StrategyVersion.verify_implementation() against
    the freshly-resolved algorithm class (has the algorithm's own source
    drifted since this version was registered?) — both fail closed, raising
    StrategyVersionViolation, with nothing simulated and nothing written.

    No Strategy code is modified during the run: the exact class resolved
    from `version.algorithm_id` is instantiated once, with `version` itself,
    and called unchanged at every step.
    """
    if isinstance(version, str):
        version = sreg.load_version(version, directory=registry_dir)

    strategy_cls = resolve_algorithm(version.algorithm_id, algorithm_registry)
    version.verify()
    version.verify_implementation(strategy_cls)
    strategy = strategy_cls(version)

    replay = Replay(store, as_of_time=as_of_time)
    steps = list(replay.walk(start, end))

    all_signals: list[Signal] = []
    open_positions: dict[str, dict] = {}
    trades: list[SimulatedTrade] = []
    last_universe: list[str] = []

    for step in steps:
        resolved_universe = _resolve_universe(step, universe)
        last_universe = resolved_universe
        context = ReplayStrategyContext(step)

        signals = strategy.generate_signal(context, list(resolved_universe))
        all_signals.extend(signals)

        for sig in signals:
            if sig.symbol not in resolved_universe:
                raise StrategyBacktestError(
                    f"{version.strategy_id} produced a Signal for "
                    f"{sig.symbol!r}, which is not in the universe it was "
                    f"given at {step.as_of.isoformat()} ({resolved_universe}). "
                    f"A Strategy may only signal on symbols from the "
                    f"universe it was handed.")

            bar_rows = step.view.prices(sig.symbol, days=1)
            if not bar_rows or bar_rows[-1].get("close") is None:
                continue  # no priceable bar this step — skipped, never guessed

            price = bar_rows[-1]["close"]

            if sig.action == "BUY":
                if sig.symbol in open_positions:
                    continue  # already held — no pyramiding in v0
                quantity = max(int(position_notional / price), 1)
                open_positions[sig.symbol] = {
                    "entry_price": price, "entry_time": step.as_of,
                    "quantity": quantity,
                }
            elif sig.action == "SELL":
                pos = open_positions.pop(sig.symbol, None)
                if pos is None:
                    continue  # no open long to close — no short-selling in v0
                trades.append(_close_trade(
                    sig.symbol, pos, price, step.as_of, "signal_exit", intraday))

    # Anything still open when the window ends is closed at the final step's
    # price rather than left dangling — the same documented simplification
    # research.experiments.runner.simulate() already makes.
    if steps:
        last_step = steps[-1]
        for symbol, pos in list(open_positions.items()):
            bar_rows = last_step.view.prices(symbol, days=1)
            if bar_rows and bar_rows[-1].get("close") is not None:
                trades.append(_close_trade(
                    symbol, pos, bar_rows[-1]["close"], last_step.as_of,
                    "backtest_end", intraday))

    stats = _compute_stats(trades)

    result = StrategyBacktestResult(
        strategy_id=version.strategy_id,
        version_id=version.version_id,
        algorithm_id=version.algorithm_id,
        start=str(start),
        end=str(end),
        n_steps=len(steps),
        universe_size_last_step=len(last_universe),
        signals=[s.to_dict() for s in all_signals],
        trades=[t.to_dict() for t in trades],
        stats=stats,
        cost_assumptions={
            "model": "engine.costs.equity_round_trip",
            "intraday": intraday,
            "position_notional": position_notional,
        },
        completed=True,
    )

    if record_note:
        rm.record_research_note(
            store,
            note=(f"Strategy backtest completed: {version.strategy_id} "
                  f"{version.version_id} ({start} to {end}) — "
                  f"{len(trades)} trade(s), net_pnl={stats['net_pnl']:.2f}."),
            source="research.experiments.strategy_backtest",
            extra={"strategy_id": version.strategy_id,
                   "version_id": version.version_id,
                   "algorithm_id": version.algorithm_id,
                   "n_trades": len(trades)},
        )

    return result
