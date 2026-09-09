"""
paper/runner.py — the bounded paper cycle. One call in, one summary out,
nothing left running afterward. No queue, no daemon, no infinite loop —
this is meant to be invoked by cron (see docs/PAPER_TRADING.md) exactly the
way run_cycle.sh invokes a live trading cycle, just without the Claude Code
agent step (signal generation here is pure, deterministic Python — see
strategies/core.py — so there is no judgment step to hand to a model).

The nine steps (AA spec section 14), in order:

    1. load eligible paper strategies        -> paper.eligibility
    2. obtain market data/state                -> paper.context (per symbol)
    3. generate signals                        -> strategies.core.Strategy
    4. validate the signal structurally        -> universe membership check
    5. simulate paper fills                    -> paper.fills / paper.portfolio
    6. update paper portfolio                  -> paper.portfolio / paper.store
    7. persist state                           -> paper.store (already atomic
                                                   per order/lot as each signal
                                                   is processed — nothing here
                                                   is held only in memory)
    8. record orders/trades                    -> paper.store
    9. emit summary information                -> the returned dict + optional
                                                   Telegram (paper.notify)

Idempotency: `cycle_id` is the unit of "have I already done this." A
`cycle_id` already marked COMPLETED in paper_cycles makes this function an
immediate, cheap no-op that returns the stored summary — see
paper/store.py's paper_cycles table and tests/test_paper_runner.py.
"""

from __future__ import annotations

import datetime as dt
import json
import traceback
from typing import Any, Optional, Union

from strategies import registry as sreg
from strategies.core import StrategyVersionViolation

from . import algorithms as palgo
from . import config as paper_config
from . import eligibility as pelig
from . import notify as pnotify
from .clock import Clock, resolve_clock
from .context import HistoryProvider, PaperStrategyContext
from .portfolio import PaperPortfolio
from .store import PaperStore


def _iso(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat(timespec="seconds")
    return str(value)


def _resolve_universe(universe: Union[str, list[str], None]) -> list[str]:
    """"watchlist" (the default) resolves to engine.watchlist.UNIVERSE — a
    plain, static list read, not a broker call — exactly the same
    resolution research/experiments/strategy_backtest.py's own
    _resolve_universe() makes for the "watchlist" case; every other
    resolution mode that module supports (a point-in-time index
    constituent list) is research-only and deliberately not reachable from
    here (see paper/algorithms.py's module docstring on why paper/ does
    not depend on that module at all). A literal list is used as-is."""
    universe = universe if universe is not None else paper_config.universe()
    if isinstance(universe, str):
        if universe == "watchlist":
            from engine.watchlist import UNIVERSE
            return list(UNIVERSE)
        raise ValueError(
            f"paper/runner.py only resolves universe='watchlist' or an explicit "
            f"list — got {universe!r}. Point-in-time index membership is a "
            f"research-only concept (research.replay.ReplayStep.universe()); "
            f"pass an explicit list if you need something else.")
    return list(universe)


def default_cycle_id(cycle_label: str, clock: Clock) -> str:
    """`paper-<label>-<date>` — one cycle per label per calendar day by
    default (this is a daily-bar system; see docs/PAPER_TRADING.md). A
    caller wanting finer granularity (e.g. more than one manual run in a
    day for testing) should pass an explicit cycle_id instead."""
    as_of = clock()
    return f"paper-{cycle_label}-{as_of.date().isoformat()}"


def run_paper_cycle(
    *,
    cycle_id: Optional[str] = None,
    cycle_label: str = "manual",
    clock: Optional[Clock] = None,
    history_provider: Optional[HistoryProvider] = None,
    universe: Union[str, list[str], None] = None,
    store: Optional[PaperStore] = None,
    registry_dir: Optional[Any] = None,
    eligibility_dir: Optional[Any] = None,
    algorithm_registry: Optional[dict[str, type]] = None,
    notify: bool = True,
) -> dict:
    """Run one bounded paper cycle and return its summary dict. Never
    raises for an ordinary per-strategy or per-signal problem (those are
    recorded and skipped — see the module docstring's step 1-9 list and the
    AA spec section 20); only re-raises for a genuine, unexpected failure
    (e.g. the paper store itself cannot be opened), after first recording
    the cycle as FAILED and sending a best-effort Telegram alert.
    """
    resolved_clock = resolve_clock(clock)
    owns_store = store is None
    store = store if store is not None else PaperStore.open()

    resolved_cycle_id = cycle_id or default_cycle_id(cycle_label, resolved_clock)
    now = _iso(resolved_clock())

    existing = store.get_cycle(resolved_cycle_id)
    if existing is not None and existing.get("status") == "COMPLETED":
        # Idempotency: already done. Return the stored summary rather than
        # reprocessing anything — see tests/test_paper_runner.py.
        summary = json.loads(existing["summary_json"]) if existing.get("summary_json") else {}
        summary["cycle_id"] = resolved_cycle_id
        summary["idempotent_replay"] = True
        if owns_store:
            store.close()
        return summary

    store.start_cycle(resolved_cycle_id, now)

    try:
        summary = _run_cycle_body(
            store=store, cycle_id=resolved_cycle_id, clock=resolved_clock,
            history_provider=history_provider, universe=universe,
            registry_dir=registry_dir, eligibility_dir=eligibility_dir,
            algorithm_registry=algorithm_registry,
        )
    except Exception as e:  # genuinely unexpected — record and re-raise
        note = f"{type(e).__name__}: {e}"
        store.fail_cycle(resolved_cycle_id, note, _iso(resolved_clock()))
        if notify:
            try:
                pnotify.notify_paper_cycle_failure(cycle_id=resolved_cycle_id, error=note)
            except Exception:
                pass  # best-effort — see paper/notify.py's module docstring
        if owns_store:
            store.close()
        raise

    finished_at = _iso(resolved_clock())
    store.complete_cycle(resolved_cycle_id, summary, finished_at)
    summary["cycle_id"] = resolved_cycle_id
    summary["idempotent_replay"] = False

    if notify:
        try:
            pnotify.notify_paper_cycle_summary(cycle_id=resolved_cycle_id, summary=summary)
        except Exception:
            pass  # best-effort

    if owns_store:
        store.close()
    return summary


def _run_cycle_body(
    *, store: PaperStore, cycle_id: str, clock: Clock,
    history_provider: Optional[HistoryProvider], universe: Union[str, list[str], None],
    registry_dir, eligibility_dir, algorithm_registry: Optional[dict[str, type]],
) -> dict:
    portfolio = PaperPortfolio(store)
    reg_dir = registry_dir if registry_dir is not None else sreg.REGISTRY_DIR
    universe_list = _resolve_universe(universe)

    # One context per cycle: every eligible Strategy sees the identical
    # as_of, the same "one bound instant per step" discipline
    # ReplayStrategyContext already follows (see paper/context.py).
    context = PaperStrategyContext(clock=clock, history_provider=history_provider)
    now = _iso(context.as_of)

    eligible = pelig.list_paper_eligible(directory=eligibility_dir)

    skipped_versions: list[dict] = []
    signals_evaluated = 0
    orders_filled = 0
    orders_rejected = 0
    marked_symbols: set[str] = set()

    for event in eligible:
        version_id = event["version_id"]
        try:
            version = sreg.load_version(version_id, directory=reg_dir)
            version.verify()
        except (FileNotFoundError, OSError, StrategyVersionViolation) as e:
            skipped_versions.append({"version_id": version_id, "reason": f"registry_error: {e}"})
            continue

        try:
            strategy_cls = palgo.resolve_algorithm(version.algorithm_id, algorithm_registry)
        except palgo.UnknownAlgorithm as e:
            skipped_versions.append({"version_id": version_id, "reason": str(e)})
            continue

        try:
            version.verify_implementation(strategy_cls)
        except StrategyVersionViolation as e:
            skipped_versions.append({"version_id": version_id, "reason": f"implementation_drift: {e}"})
            continue

        strategy = strategy_cls(version)

        try:
            signals = strategy.generate_signal(context, list(universe_list))
        except Exception as e:
            # A Strategy's own bug must never take down the whole cycle —
            # see the AA spec section 20's general fail-safe posture.
            skipped_versions.append({
                "version_id": version_id,
                "reason": f"generate_signal raised {type(e).__name__}: {e}",
            })
            continue

        for signal in signals:
            signals_evaluated += 1

            if signal.symbol not in universe_list:
                outcome = portfolio.reject_malformed(
                    signal, cycle_id=cycle_id,
                    reason="symbol_outside_universe", now=now)
                if outcome.is_new:
                    orders_rejected += 1
                    _maybe_notify_rejection(signal, outcome)
                continue

            lookback = strategy.required_lookback or paper_config.default_lookback_days()
            history_df = context.history(signal.symbol, lookback)

            outcome = portfolio.process_signal(
                signal, history_df, cycle_id=cycle_id, now=now)
            if not outcome.is_new:
                continue  # duplicate signal within this cycle — no-op

            if outcome.order and outcome.order["status"] == "FILLED":
                orders_filled += 1
                _maybe_notify_fill(signal, outcome)
            elif outcome.order:
                orders_rejected += 1
                _maybe_notify_rejection(signal, outcome)

            marked_symbols.add(signal.symbol)

    # Mark every symbol currently held (across all versions), plus anything
    # touched by a signal this cycle, so /paper/positions and
    # /paper/performance stay reasonably fresh without ever fetching the
    # entire universe's history on every cycle regardless of position size.
    held_symbols = {p["symbol"] for p in store.list_positions()}
    for symbol in held_symbols | marked_symbols:
        history_df = context.history(symbol, paper_config.default_lookback_days())
        portfolio.mark_symbol(symbol, history_df, now)

    perf = portfolio.performance_summary()
    return {
        "as_of": now,
        "eligible_version_count": len(eligible),
        "skipped_versions": skipped_versions,
        "signals_evaluated": signals_evaluated,
        "orders_filled": orders_filled,
        "orders_rejected": orders_rejected,
        **perf,
    }


def _maybe_notify_fill(signal, outcome) -> None:
    try:
        pnotify.notify_paper_trade(
            side=signal.action, strategy_id=signal.strategy_id,
            version_id=signal.strategy_version_id, symbol=signal.symbol,
            quantity=outcome.order.get("requested_quantity") or 0,
            fill_price=outcome.order["fill_price"],
        )
    except Exception:
        pass  # best-effort — see paper/notify.py


def _maybe_notify_rejection(signal, outcome) -> None:
    try:
        pnotify.notify_paper_rejection(
            side=signal.action, strategy_id=signal.strategy_id,
            version_id=signal.strategy_version_id, symbol=signal.symbol,
            reason=(outcome.order or {}).get("reason") or "unknown",
        )
    except Exception:
        pass  # best-effort


# ---------------------------------------------------------------------------
# CLI — python -m paper.runner run [--cycle-id ID] [--cycle-label LABEL] [--no-notify]
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Run one bounded paper/shadow cycle (Slice AA)")
    sub = parser.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="Run one paper cycle")
    r.add_argument("--cycle-id", default=None)
    r.add_argument("--cycle-label", default="manual")
    r.add_argument("--no-notify", action="store_true")

    args = parser.parse_args()

    if args.command == "run":
        summary = run_paper_cycle(
            cycle_id=args.cycle_id, cycle_label=args.cycle_label,
            notify=not args.no_notify,
        )
        print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
