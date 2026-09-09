"""
The Evaluator — Phase 1 Slice D. Pure statistics over one contract's
`experiment_results` rows. Nothing here simulates anything (that's
runner.py) and nothing here touches live data.

Why this does NOT reuse engine.stats
-------------------------------------
`engine.stats.compute()` defaults to reading `memory/trades.jsonl` — the
LIVE trade journal, real money's own history. Reusing that function here,
even by passing it a different path, creates exactly the live/research
ambiguity Vaibhav flagged in the Slice D review: a caller who forgets the
override, or a future refactor that changes the default, silently computes
"research" statistics from real trading history — or the reverse, a live
report that accidentally reads simulated data. The fix isn't a parameter;
it's not sharing the function at all. This module is a small, independent,
research-only statistics layer that reads exclusively from
`Store.experiment_results(contract_id)`, which is written ONLY by
research/experiments/runner.py and scoped to one contract at a time. There
is no path from here to memory/trades.jsonl, structurally — this file does
not import engine.stats, engine.journal, or anything else that could reach
it.

No engine import of any kind lives in this file. The runner already priced
every trade (via engine.costs) before persisting it; this module only
aggregates numbers already sitting in the database.
"""

from __future__ import annotations

import math
import statistics
from typing import Optional

from .. import memory as rm
from ..store import Store


def compute_verdict(store: Store, contract_id: str) -> dict:
    """Aggregate statistics over every persisted trade for `contract_id`.
    Returns a plain dict — the same shape research.memory.record_experiment_
    verdict already expects (it stores whatever dict it's given, by design,
    so this schema can evolve without touching that module)."""
    trades = store.experiment_results(contract_id)
    n = len(trades)

    if n == 0:
        return {
            "n_trades": 0, "win_rate": None, "gross_pnl": 0.0, "net_pnl": 0.0,
            "total_costs": 0.0, "avg_net_pnl": None, "expectancy_r": None,
            "t_stat": None,
        }

    net_pnls = [t["net_pnl"] for t in trades]
    gross_pnls = [t["gross_pnl"] for t in trades]
    costs = [t["costs"] for t in trades]
    r_multiples = [t["r_multiple"] for t in trades if t["r_multiple"] is not None]
    wins = sum(1 for p in net_pnls if p > 0)

    return {
        "n_trades": n,
        "win_rate": wins / n,
        "gross_pnl": sum(gross_pnls),
        "net_pnl": sum(net_pnls),
        "total_costs": sum(costs),
        "avg_net_pnl": statistics.fmean(net_pnls),
        "expectancy_r": statistics.fmean(r_multiples) if r_multiples else None,
        "t_stat": _t_stat(r_multiples),
    }


def _t_stat(sample: list[float]) -> Optional[float]:
    """One-sample t-statistic of `sample` against a mean of zero — "is this
    edge distinguishable from noise", the same question falsification is
    ultimately in service of. None when it isn't computable (fewer than 2
    trades, or zero variance) rather than a fabricated number."""
    n = len(sample)
    if n < 2:
        return None
    mean = statistics.fmean(sample)
    sd = statistics.stdev(sample)
    if sd <= 1e-12:
        return None
    return mean / (sd / math.sqrt(n))


def resolve_hypothesis_id(store: Store, contract_id: str) -> Optional[str]:
    """Contract carries no hypothesis_id field — a deliberate Slice C/D
    decision (see hypothesis_intake.py's module docstring) to avoid a second
    source of truth for that link. The only record of it is the
    hypothesis-claim row research.memory wrote when the contract was
    drafted, which carries the contract_id in its payload. This walks that
    log the same way hypothesis_intake._hypothesis_claims() does, just keyed
    the other direction (by contract_id instead of hypothesis_id)."""
    rows = rm.query_research_log(store, rm.DATASET_HYPOTHESIS)
    for r in rows:
        if r["payload"].get("contract_id") == contract_id:
            return r["payload"].get("hypothesis_id")
    return None


def contract_ids_for_hypothesis(store: Store, hypothesis_id: str) -> list[str]:
    """Every contract_id ever drafted against `hypothesis_id`, sorted for a
    deterministic order. The forward direction of resolve_hypothesis_id,
    added for Slice E (research/experiments/comparison.py) so it can find
    every sibling variant of a hypothesis without duplicating this scan.
    Same source of truth, same caveats: reads the hypothesis-claim log,
    nothing else."""
    rows = rm.query_research_log(store, rm.DATASET_HYPOTHESIS)
    ids = {
        r["payload"].get("contract_id")
        for r in rows
        if r["payload"].get("hypothesis_id") == hypothesis_id and r["payload"].get("contract_id")
    }
    return sorted(ids)


def verdict_for_contract(store: Store, contract_id: str) -> Optional[dict]:
    """The most recently recorded verdict payload for `contract_id`, or None
    if it has never been scored. `record_verdict` writes at most one verdict
    per successful run (run_experiment refuses to re-run a reported
    contract), so in the ordinary case there is exactly one; if a caller
    invokes record_verdict directly more than once, the latest write wins —
    the log itself is append-only, this just picks the newest row from it."""
    rows = rm.query_research_log(store, rm.DATASET_VERDICT)
    matches = [r for r in rows if r["payload"].get("contract_id") == contract_id]
    return matches[-1]["payload"] if matches else None


def record_verdict(store: Store, contract_id: str) -> dict:
    """Compute the verdict and record it via research.memory — the one
    function research/experiments/runner.py calls after a successful run."""
    verdict = compute_verdict(store, contract_id)
    hypothesis_id = resolve_hypothesis_id(store, contract_id)
    rm.record_experiment_verdict(
        store, contract_id=contract_id, hypothesis_id=hypothesis_id, verdict=verdict,
    )
    return verdict
