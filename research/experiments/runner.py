"""
The Experiment Runner — Phase 1 Slice D. The first sanctioned execution
boundary in the Living Quant. Nothing before this slice ever turned a
hypothesis into simulated trades; nothing after it should exist outside this
package's discipline.

The mental model this module exists to enforce (Vaibhav's framing, kept
verbatim because it's exactly right):

    PROPOSAL
       |
       v
    DRAFT                          research/brain/hypothesis_intake.py
       |
       v
    HUMAN APPROVAL
       |
       v
    LOCK + HASH
       |
       v
    VERIFY
       |
    ──────── EXECUTION BOUNDARY ────────      <- THIS FILE starts here
       |
       v
    REPLAY
       |
       v
    RESULTS
       |
       v
    VERDICT                        research/experiments/evaluator.py

Trust is established BEFORE execution, not by execution. The runner does not
decide whether a contract is trustworthy — that was already decided by the
draft/approve/lock/verify pipeline. The runner's only job is to refuse
anything that didn't come through that pipeline correctly, then simulate
exactly what the locked rules say, honestly.

THE EXECUTION BOUNDARY: id in, verified contract out
------------------------------------------------------
`run_experiment()` takes a `contract_id: str`, never a `Contract` object. This
is deliberate and load-bearing, not a style choice: if the public API
accepted a `Contract`, any caller could do

    contract = Contract(id="EXP-X", status="locked", locked_hash="whatever", ...)
    run_experiment(contract)

and skip every check `load_runnable_contract()` performs — the hash is just a
field on a dataclass, not something Python enforces. Taking only an id means
the ONLY way to get a runnable contract is through the loader below, which:

    1. loads it from the registry directory (the one place `approve_and_lock`
       ever writes a locked contract to),
    2. requires status == "locked" EXACTLY — not "running" (a contract already
       mid-execution), not "draft", not "reported"/"abandoned"/"superseded"
       (already-finished studies, never silently re-run),
    3. calls `Contract.verify()`, which re-hashes and re-checks the model
       firewall — catching any edit made to the file after it was locked.

Lifecycle this module manages:

    LOCKED --[load_runnable_contract + verify]--> RUNNING --[simulate]-->
        --success--> REPORTED
        --failure--> ABANDONED

RUNNING is explicitly a transitional state with NO crash-recovery semantics
in this slice. If the process dies while a contract is RUNNING, that contract
is left RUNNING — there is no resume, no retry-detection, nothing. Handling
that is an explicit non-goal here (Vaibhav's call, Slice C/D review) — adding
it now would be exactly the kind of quiet scope growth the whole slice
discipline exists to prevent. A RUNNING contract found later is a human
signal ("something died mid-run"), not a state this module knows how to
recover from.

What this module deliberately does NOT do:
  - accept a caller-supplied Contract object (see above)
  - call engine.execute, any engine.broker_*, engine.guardrails, or
    engine.journal — this is a simulation over historical data, not a path to
    a real order, and none of those modules are imported here
  - reuse engine.stats — it defaults to reading memory/trades.jsonl, the LIVE
    trade journal; see research/experiments/evaluator.py's docstring for why
    that's refused outright rather than worked around with a path parameter
  - size positions using engine.guardrails' live 2%-risk sizing. A fixed,
    explicitly-labeled research notional (RESEARCH_POSITION_NOTIONAL below)
    is used instead — comparing strategies on equal footing across a
    discovery/validation split needs a constant, not whatever the live
    account happens to hold that day.
  - add a hypothesis_id field to Contract. The link from a contract back to
    its hypothesis is resolved through research memory
    (evaluator.resolve_hypothesis_id), exactly as hypothesis_intake.py
    already does internally — a second, Contract-side copy of that link would
    be a second source of truth for the same fact.
  - call eval, exec, or compile, anywhere.

Metric evaluation note (why this file computes its own z-scores rather than
calling research.brain.observatory's detectors): observatory.py's
volume_anomaly / price_move_anomaly / event_frequency_anomaly are
THRESHOLD-GATED — they return a record only when |z| >= Z_THRESHOLD, which is
exactly right for "what's unusual" but wrong for "evaluate this arbitrary
condition", since a locked contract's entry_rule can compare against any
threshold (z > 1.5 is a legal, if aggressive, condition). This module reuses
observatory's shared statistical core (`_zscore`) and its window/minimum-
observations constants directly, and re-implements only the baseline-
windowing shape observatory's public functions don't expose raw. Recorded as
a known, deliberate duplication — a future slice could factor a shared
"compute this metric's raw value" primitive both modules call; not done here
to keep this slice's diff to the files it actually needs.

This module contains zero calls to eval, exec, or compile, and reads market
data exclusively through research.replay.Replay / research.store.AsOfView —
see tests/test_research_runner.py for the leak-check proving it.
"""

from __future__ import annotations

import json
import operator as _op
import datetime as dt
from pathlib import Path
from typing import Any, Optional

from .. import memory as rm
from ..contracts import Contract, ContractViolation, REGISTRY_DIR, registry as _registry
from ..replay import Replay, ReplayStep
from ..store import Store, AsOfView
from ..brain.observatory import _zscore, WINDOW_DAYS, MIN_OBSERVATIONS
from . import evaluator

# A fixed, research-only hypothetical position size. NOT live capital, NOT
# derived from engine.guardrails' account-aware sizing. Every simulated trade
# in every experiment uses the same notional so studies are comparable to
# each other regardless of what the live account happens to hold.
RESEARCH_POSITION_NOTIONAL = 100_000.0

_OPERATORS = {
    ">": _op.gt, ">=": _op.ge, "<": _op.lt, "<=": _op.le, "==": _op.eq, "!=": _op.ne,
}


class RunnerRejected(RuntimeError):
    """The execution boundary refused a contract_id before touching any
    state. Nothing is written anywhere when this is raised."""

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = list(reasons)
        super().__init__("; ".join(self.reasons) or "rejected (no reason given)")


class ExperimentFailed(RuntimeError):
    """Simulation started (the contract was already flipped to RUNNING) and
    raised. The contract has already been saved as ABANDONED with the error
    recorded, both on the contract's own `notes` and in research memory,
    before this is raised — this exception is a signal to the caller, not the
    only record that the failure happened."""


# ---------------------------------------------------------------------------
# The execution boundary
# ---------------------------------------------------------------------------

def load_runnable_contract(contract_id: str, registry_dir: Path = REGISTRY_DIR) -> Contract:
    """The ONLY sanctioned way to get a Contract this module will execute.
    Loads from disk, requires status == "locked" exactly, then calls
    Contract.verify() (hash + model-firewall re-check). Raises RunnerRejected
    — without changing anything — on any failure."""
    if not isinstance(contract_id, str):
        raise RunnerRejected([
            f"contract_id must be a string, got {type(contract_id).__name__}. "
            f"run_experiment() takes an id and loads the contract itself — "
            f"passing an already-constructed Contract object is refused, not "
            f"accepted as a shortcut."])

    try:
        contract = Contract.load(contract_id, registry_dir)
    except FileNotFoundError:
        raise RunnerRejected([f"no contract {contract_id!r} found in {registry_dir}"])

    if contract.status != "locked":
        raise RunnerRejected([
            f"{contract_id} is not LOCKED (status={contract.status!r}). Only a "
            f"locked contract may enter the runner. A draft was never approved; "
            f"a running/reported/abandoned/superseded contract is already "
            f"mid-execution or finished and is never silently re-run."])

    try:
        contract.verify()
    except ContractViolation as e:
        raise RunnerRejected([f"{contract_id} failed verify(): {e}"])

    return contract


def runnable_contracts(registry_dir: Path = REGISTRY_DIR) -> list[Contract]:
    """Every contract currently eligible to enter the runner — the literal
    run queue, symmetric to hypothesis_intake.pending_drafts()."""
    return [c for c in _registry(registry_dir) if c.status == "locked"]


def run_experiment(contract_id: str, store: Store, *, registry_dir: Path = REGISTRY_DIR) -> dict:
    """Run one locked, verified experiment end to end: simulate, persist
    every trade, compute and record a verdict, and transition the contract to
    REPORTED (success) or ABANDONED (any exception during simulation).

    Takes a contract_id, never a Contract — see the module docstring's
    "THE EXECUTION BOUNDARY" section for why that's load-bearing, not
    stylistic.
    """
    contract = load_runnable_contract(contract_id, registry_dir)

    contract.status = "running"
    contract.save(registry_dir)

    try:
        trades = simulate(contract, store)
        for i, trade in enumerate(trades, start=1):
            store.append_experiment_result(contract_id=contract.id, trade_seq=i, **trade)

        verdict = evaluator.record_verdict(store, contract.id)

        contract.status = "reported"
        summary = (f"Run completed: {verdict['n_trades']} trades, "
                   f"net_pnl={verdict['net_pnl']:.2f}.")
        contract.notes = f"{contract.notes}\n{summary}".strip() if contract.notes else summary
        contract.save(registry_dir)
        rm.record_research_note(
            store, note=summary, source="research.experiments.runner",
            extra={"contract_id": contract.id},
        )
        return {"status": "reported", "contract_id": contract.id,
                "n_trades": len(trades), "verdict": verdict}

    except Exception as e:
        contract.status = "abandoned"
        failure = f"Run failed: {type(e).__name__}: {e}"
        contract.notes = f"{contract.notes}\n{failure}".strip() if contract.notes else failure
        contract.save(registry_dir)
        rm.record_research_note(
            store, note=failure, source="research.experiments.runner",
            extra={"contract_id": contract.id, "error_type": type(e).__name__},
        )
        raise ExperimentFailed(f"{contract.id} failed and was marked abandoned: {e}") from e


# ---------------------------------------------------------------------------
# Metric evaluation — the per-step, stateless read of "what does this
# condition's metric equal right now, honestly, from only what's knowable at
# this as_of". This is the ONLY place the runner reads market data, and it
# reads it exclusively through the AsOfView handed to it by a ReplayStep.
# ---------------------------------------------------------------------------

def _metric_value(view: AsOfView, symbol: str, metric: str,
                  window_days: Optional[int] = None) -> Optional[float]:
    window = window_days or WINDOW_DAYS

    if metric == "close":
        rows = view.prices(symbol, days=1)
        return rows[-1]["close"] if rows and rows[-1].get("close") is not None else None

    if metric == "return_1d":
        rows = view.prices(symbol, days=2)
        if len(rows) < 2:
            return None
        prev, cur = rows[-2].get("close"), rows[-1].get("close")
        if prev in (None, 0) or cur is None:
            return None
        return (cur - prev) / prev

    if metric == "volume_zscore":
        rows = view.prices(symbol, days=window + 1)
        if len(rows) < MIN_OBSERVATIONS + 1:
            return None
        current, baseline = rows[-1], rows[:-1]
        cv = current.get("volume")
        bv = [r.get("volume") for r in baseline]
        if cv is None or any(v is None for v in bv):
            return None
        z = _zscore([float(v) for v in bv], float(cv))
        return z.z if z is not None else None

    if metric == "price_move_zscore":
        rows = view.prices(symbol, days=window + 2)
        if len(rows) < MIN_OBSERVATIONS + 2:
            return None
        closes = [r.get("close") for r in rows]
        if any(c is None for c in closes):
            return None
        returns: list[float] = []
        for prev, cur in zip(closes, closes[1:]):
            if prev == 0:
                return None
            returns.append((cur - prev) / prev)
        z = _zscore(returns[:-1], returns[-1])
        return z.z if z is not None else None

    if metric == "event_frequency_zscore":
        rows = view.prices(symbol, days=window + 1)
        if len(rows) < MIN_OBSERVATIONS + 1:
            return None
        current_day = rows[-1]["session_date"]
        baseline_days = [r["session_date"] for r in rows[:-1]]
        obs_rows = view.observations(
            "bse_announcement", entity=symbol.upper(),
            event_from=baseline_days[0], event_to=current_day, latest_only=False,
        )
        counts: dict[str, int] = {}
        for r in obs_rows:
            day = str(r["event_time"])[:10]
            counts[day] = counts.get(day, 0) + 1
        current_count = counts.get(current_day, 0)
        baseline_counts = [float(counts.get(d, 0)) for d in baseline_days]
        z = _zscore(baseline_counts, float(current_count))
        return z.z if z is not None else None

    # Unreachable given hypothesis_intake's SUPPORTED_METRICS whitelist — a
    # contract cannot lock with a metric outside that set. Refusing loudly
    # rather than returning None if it somehow happens anyway (e.g. a
    # hand-edited registry file bypassing intake entirely).
    raise ValueError(f"unsupported metric {metric!r} — this should be unreachable "
                     f"for any contract that went through hypothesis_intake")


def _condition_met(view: AsOfView, symbol: str, condition: dict) -> bool:
    value = _metric_value(view, symbol, condition["metric"], condition.get("window_days"))
    if value is None:
        return False  # insufficient history / missing data -> condition not met, never guessed
    return _OPERATORS[condition["op"]](value, condition["value"])


def entry_signal(view: AsOfView, symbol: str, entry_rule: dict) -> bool:
    """True iff every (ANDed) condition in entry_rule is met for `symbol` at
    the instant `view` is bound to. The single function leak_check is run
    against in tests/test_research_runner.py — see that file for why testing
    this function is equivalent to testing the whole simulation loop's data
    access."""
    return all(_condition_met(view, symbol, c) for c in entry_rule["conditions"])


# ---------------------------------------------------------------------------
# Universe resolution — per step, so index-membership rules (Nifty 50/500)
# stay point-in-time correct across a multi-year evaluation window.
# ---------------------------------------------------------------------------

def _universe_for_step(step: ReplayStep, universe_name: str) -> list[str]:
    if universe_name == "watchlist":
        from engine.watchlist import UNIVERSE  # read-only, static — see hypothesis_intake.py
        return UNIVERSE
    return step.universe(universe_name)


# ---------------------------------------------------------------------------
# Simulation — walks the contract's evaluation window via Replay/ReplayStep,
# tracks at most one open position per symbol, and closes it on stop/target/
# max_hold_days exactly as declared in the locked exit_rule. Every price read
# goes through step.view (an AsOfView); nothing else is consulted.
# ---------------------------------------------------------------------------

def simulate(contract: Contract, store: Store) -> list[dict]:
    """Pure(ish) simulation: reads `contract`'s already-validated, already-
    whitelisted entry_rule/exit_rule/universe/evaluation window and the
    store's price/observation history; returns a list of trade dicts ready
    to hand to Store.append_experiment_result (minus contract_id/trade_seq,
    which run_experiment assigns). Writes nothing itself — run_experiment is
    the only function that persists anything.
    """
    from engine.costs import equity_round_trip  # pure function, no live state

    entry_rule = json.loads(contract.entry_rule)
    exit_rule = json.loads(contract.exit_rule)

    replay = Replay(store)
    steps = list(replay.walk(contract.evaluation_start, contract.evaluation_end))

    open_positions: dict[str, dict] = {}
    trades: list[dict] = []

    def close_trade(symbol: str, pos: dict, exit_price: float, exit_time: dt.datetime,
                    exit_reason: str) -> None:
        qty = pos["quantity"]
        entry_price = pos["entry_price"]
        gross_pnl = (exit_price - entry_price) * qty
        costs = equity_round_trip(entry_price, qty).total
        net_pnl = gross_pnl - costs
        r_multiple = None
        if pos.get("stop_price") is not None:
            risk_per_share = entry_price - pos["stop_price"]
            if risk_per_share > 0:
                r_multiple = (exit_price - entry_price) / risk_per_share
        trades.append({
            "entity": symbol, "entry_time": pos["entry_time"], "exit_time": exit_time,
            "entry_price": entry_price, "exit_price": exit_price, "quantity": qty,
            "gross_pnl": gross_pnl, "costs": costs, "net_pnl": net_pnl,
            "r_multiple": r_multiple, "exit_reason": exit_reason,
        })

    for step in steps:
        symbols = _universe_for_step(step, contract.universe)

        # -- exits first: never let an entry and an exit for the SAME symbol
        # on the SAME day pretend to be simultaneous; a held position is
        # resolved before a fresh entry on that symbol is even considered.
        for symbol in list(open_positions):
            pos = open_positions[symbol]
            bar_rows = step.view.prices(symbol, days=1)
            if not bar_rows:
                continue
            bar = bar_rows[-1]
            exit_price, exit_reason = None, None

            if (pos.get("stop_price") is not None and bar.get("low") is not None
                    and bar["low"] <= pos["stop_price"]):
                exit_price, exit_reason = pos["stop_price"], "stop"
            elif (pos.get("target_price") is not None and bar.get("high") is not None
                  and bar["high"] >= pos["target_price"]):
                exit_price, exit_reason = pos["target_price"], "target"
            elif ("max_hold_days" in exit_rule and pos["days_held"] >= exit_rule["max_hold_days"]
                  and bar.get("close") is not None):
                exit_price, exit_reason = bar["close"], "time_exit"

            if exit_price is not None:
                close_trade(symbol, pos, exit_price, step.as_of, exit_reason)
                del open_positions[symbol]
            else:
                pos["days_held"] += 1

        # -- entries
        for symbol in symbols:
            if symbol in open_positions:
                continue
            if not entry_signal(step.view, symbol, entry_rule):
                continue
            bar_rows = step.view.prices(symbol, days=1)
            if not bar_rows or bar_rows[-1].get("close") is None:
                continue
            entry_price = bar_rows[-1]["close"]
            quantity = max(int(RESEARCH_POSITION_NOTIONAL / entry_price), 1)
            open_positions[symbol] = {
                "entry_price": entry_price, "entry_time": step.as_of, "quantity": quantity,
                "stop_price": (entry_price * (1 - exit_rule["stop_loss_pct"] / 100)
                              if "stop_loss_pct" in exit_rule else None),
                "target_price": (entry_price * (1 + exit_rule["target_pct"] / 100)
                                 if "target_pct" in exit_rule else None),
                "days_held": 0,
            }

    # Evaluation window ended with positions still open: close them at the
    # last step's close rather than leaving them dangling. This is a
    # documented simplification, not a silent one.
    if steps:
        last_step = steps[-1]
        for symbol, pos in list(open_positions.items()):
            bar_rows = last_step.view.prices(symbol, days=1)
            if bar_rows and bar_rows[-1].get("close") is not None:
                close_trade(symbol, pos, bar_rows[-1]["close"], last_step.as_of, "evaluation_end")

    return trades
