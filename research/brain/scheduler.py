"""
Experiment Scheduler v0 — Phase 2 Slice R.

The gap this closes: every locked Contract still has to be run by hand, one
`runner.run_experiment(contract_id, store)` call at a time. Slices L-Q built
the review side of the pipeline (duplicate detection, research areas,
discovery provenance, the draft backlog, the research budget) — nothing yet
automates the one step downstream of a human's own approve_and_lock() call:
actually executing the experiments that approval already authorized.

    Human-approved hypothesis
            |
            v
        LOCKED Contract              (hypothesis_intake.approve_and_lock)
            |
            v
        Scheduler                    <- THIS FILE
            |
            v
        bounded selection
            |
            v
    existing runner.run_experiment() (research.experiments.runner — UNCHANGED)
            |
            v
        REPORTED / ABANDONED

This module is orchestration ONLY. It is deliberately as thin as possible:
find already-locked, not-yet-run Contracts (reusing
`runner.runnable_contracts()` verbatim — the same "literal run queue" that
function's own docstring already describes), pick a small, deterministic,
bounded subset of them, and call the existing, completely unmodified
`runner.run_experiment()` once per selected contract_id, sequentially. It
does not simulate anything itself, does not decide what "locked" or
"reported" or "abandoned" mean (those are `research.contracts.VALID_STATUS`
and the runner's own lifecycle, untouched here), and does not invent a
second notion of "eligible to run."

Design principle, same as every Phase 2 brain/ module before it: REUSE,
don't reimplement.
  - which contracts are locked-and-runnable  -> experiments.runner.runnable_contracts()
  - loading + verifying a contract by id     -> experiments.runner.load_runnable_contract()
                                                 (called internally by run_experiment()
                                                 itself — this module never calls it directly)
  - actually running one experiment          -> experiments.runner.run_experiment()
  - what counts as a safe, already-recorded
    per-contract failure                     -> experiments.runner.ExperimentFailed /
                                                 experiments.runner.RunnerRejected
Nothing here re-simulates a contract, re-implements entry/exit logic, writes
a trade row, or decides a verdict — `simulate()`, `evaluator.record_verdict()`,
and every line of `run_experiment()`'s own state-transition logic stay
exactly as Slice D wrote them.

WHAT THIS MODULE NEVER DOES, structurally true by absence (imports nothing
that could do these things, so there is nothing to accidentally call):
  - create a hypothesis or a draft: `hypothesis_intake.create_draft()` is
    never imported here.
  - approve or lock anything: `hypothesis_intake.approve_and_lock()` and
    `Contract.lock()` are never imported or called here. This module's
    entire input is Contracts that are ALREADY locked — it consumes human
    approval, it never grants it.
  - invoke Research AI, the Investigator, or any discovery mechanism:
    `research.brain.investigator` is never imported here. This slice is
    strictly downstream of a human's own approve_and_lock() call; nothing
    here can ever cause a NEW hypothesis or a NEW locked Contract to exist.
  - touch live trading state: no `engine.execute`, `engine.guardrails`,
    `engine.broker*`, or `engine.journal` import; no read of
    `memory/state.json` or broker credentials. This is a research-only
    orchestrator over historical simulation, exactly like the runner it
    calls.
  - run more than one experiment at a time, spawn a worker, a process pool,
    or any concurrency primitive. Execution is strictly sequential — select
    N, run one, run the next, stop. A future parallel-research slice is
    explicit future work, not started here (see the module's own
    non-goals in the Slice R instructions).

Selection order (Slice S update): `eligible_contracts()` now orders its
result via `research.brain.priority.rank_experiments()` — the Research
Priority Engine v0 — instead of the plain age-only sort this module
originally used. That module's own docstring is the authority on what the
ordering actually is (confirmation experiments first, then a round-robin
across research areas, then oldest `locked_at`, then `contract_id` as the
final tie-break); nothing about that rule is reimplemented here, and this
module does not decide what counts as a confirmation experiment, a research
area, or "promising" evidence — it only asks `priority.rank_experiments()`
for an order and uses it. This IS the "future Research Priority module"
this section used to say would replace the original plain age-only
ordering; it now does. Everything else about `eligible_contracts()`
/`select_experiments()`/`run_scheduler()` is otherwise structurally
unchanged: find eligible locked Contracts, rank them, bound the ranked
list, execute sequentially through the unmodified runner.

Bounded execution: `max_experiments` follows the exact same convention as
every other bounded control introduced in this codebase (Slice O's
MAX_LOCKS_PER_PERIOD, Slice P's MAX_PROPOSAL_ATTEMPTS_PER_RUN, Slice Q's
DEFAULT_BACKLOG_LIMIT) — a small, explicit, clearly-named, overridable
constant, never claimed to be production-tuned, checked with `is not None`
(never truthiness) so `max_experiments=0` genuinely means "run nothing."

Failure handling: a per-contract `RunnerRejected` or `ExperimentFailed` is
exactly what `run_experiment()` itself is documented to raise for a
condition it has ALREADY safely handled (see runner.py's own docstrings —
`RunnerRejected` means nothing was touched at all; `ExperimentFailed` means
the contract was already saved as ABANDONED and the failure already
recorded in research memory before the exception was raised). Both are
caught here and the scheduler moves on to the next selected contract — a
single bad experiment must not block the rest of a bounded run. Any OTHER
exception type is, by construction, something that happened OUTSIDE
run_experiment()'s own safety net (e.g. the registry write that flips a
contract to "running" failing outright — disk full, permissions) — a
genuine signal that continuing to hammer the registry/Store for the
remaining selected contracts could be unsafe, so the scheduler stops
selecting further contracts at that point rather than plowing through, but
still returns everything it recorded up to and including that failure
(never swallowed silently, never re-raised in a way that would lose the
partial result — a cron-driven caller still gets a complete, honest
summary of what happened).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import NamedTuple, Optional

from ..contracts import REGISTRY_DIR
from ..store import Store, now_ist, iso
from ..experiments import runner
from . import priority as prio

MAX_EXPERIMENTS_PER_RUN = 3
"""A deliberately conservative placeholder — NOT claimed to be production-
tuned (per this slice's own instructions). Named and overridable per call
(`select_experiments(..., max_experiments=...)` /
`run_scheduler(..., max_experiments=...)` / `--max-experiments` on the CLI),
same posture as MAX_LOCKS_PER_PERIOD (Slice O) and
MAX_PROPOSAL_ATTEMPTS_PER_RUN (Slice P)."""


# ---------------------------------------------------------------------------
# Candidate discovery / selection — read-only throughout.
# ---------------------------------------------------------------------------

def eligible_contracts(store: Store, *, registry_dir: Path = REGISTRY_DIR) -> list:
    """Every locked, not-yet-run Contract — `runner.runnable_contracts()`
    verbatim (status == "locked", exactly the same definition the runner
    itself uses to decide what it will accept) — ordered by the Research
    Priority Engine v0 (`research.brain.priority.rank_experiments()`;
    confirmation first, then a round-robin across research areas, then
    oldest `locked_at`, then `contract_id` as the final tie-break — see
    that module's own docstring for the full rule and rationale).

    Read-only throughout: `runner.runnable_contracts()` only reads the
    registry directory, and `priority.rank_experiments()` only reads
    research memory (hypothesis-claim rows, area tags, already-computed
    evidence summaries) — see that module's own "WHAT THIS MODULE NEVER
    DOES" section. Nothing here mutates a Contract or writes anything.
    """
    contracts = runner.runnable_contracts(registry_dir)
    return prio.rank_experiments(store, contracts, registry_dir=registry_dir)


def select_experiments(
    store: Store,
    *,
    registry_dir: Path = REGISTRY_DIR,
    max_experiments: Optional[int] = MAX_EXPERIMENTS_PER_RUN,
) -> list:
    """The bounded subset of `eligible_contracts()` a scheduler run would
    actually attempt — the first `max_experiments` in priority order.
    `max_experiments=None` means unbounded (every eligible contract);
    `max_experiments=0` means select none — checked with `is not None`,
    never by truthiness, the same convention `digest.py`/`draft_backlog.py`
    already established for every other bounded control in this codebase.
    """
    all_eligible = eligible_contracts(store, registry_dir=registry_dir)
    return all_eligible[:max_experiments] if max_experiments is not None else all_eligible


# ---------------------------------------------------------------------------
# Execution — sequential, bounded, delegating every actual simulation to the
# existing, unmodified runner.run_experiment().
# ---------------------------------------------------------------------------

class ExperimentOutcome(NamedTuple):
    """One selected contract's outcome, already reduced to a JSON-
    serializable shape."""

    contract_id: str
    outcome: str  # "reported" | "abandoned" | "rejected" | "error"
    detail: str


class SchedulerRunResult(NamedTuple):
    started: str
    finished: str
    max_experiments: int
    eligible_contract_ids: list  # every eligible contract, in selection order, BEFORE bounding
    selected_contract_ids: list  # the bounded subset chosen to attempt
    attempted_contract_ids: list  # contract ids the scheduler actually called run_experiment() on
    reported_contract_ids: list
    abandoned_contract_ids: list
    rejected_contract_ids: list  # RunnerRejected at execution time (rare — see module docstring)
    pending_contract_ids: list  # eligible but never attempted (cap-bound or an early stop)
    outcomes: list  # list[ExperimentOutcome]
    n_selected: int
    n_attempted: int
    stopped_early: bool  # True if an unexpected (non-runner) exception halted the run
    error: Optional[str]  # set only if stopped_early


def run_scheduler(
    store: Store,
    *,
    registry_dir: Path = REGISTRY_DIR,
    max_experiments: int = MAX_EXPERIMENTS_PER_RUN,
) -> SchedulerRunResult:
    """Select up to `max_experiments` eligible locked Contracts (oldest
    first) and run each one, sequentially, through the existing, unmodified
    `runner.run_experiment()`. Never creates a hypothesis, never locks a
    Contract, never calls Research AI, never touches engine/* or
    memory/state.json — see the module docstring's safety sections for
    exactly how each of those is true by absence.

    A per-contract `runner.ExperimentFailed` (the contract was already
    safely marked ABANDONED and recorded, by run_experiment() itself, before
    the exception reached here) or `runner.RunnerRejected` (nothing was
    touched at all) is caught and the run continues to the next selected
    contract. Any other exception halts further selection immediately —
    see the module docstring's "Failure handling" section — but the run
    still returns a complete result recording everything attempted so far,
    rather than raising and losing it.
    """
    started = iso(now_ist())

    eligible = eligible_contracts(store, registry_dir=registry_dir)
    eligible_ids = [c.id for c in eligible]
    selected = eligible[:max_experiments] if max_experiments is not None else eligible
    selected_ids = [c.id for c in selected]

    attempted_ids: list = []
    reported_ids: list = []
    abandoned_ids: list = []
    rejected_ids: list = []
    outcomes: list = []
    stopped_early = False
    error: Optional[str] = None

    for contract_id in selected_ids:
        attempted_ids.append(contract_id)
        try:
            result = runner.run_experiment(contract_id, store, registry_dir=registry_dir)
            reported_ids.append(contract_id)
            outcomes.append(ExperimentOutcome(
                contract_id=contract_id, outcome="reported",
                detail=f"{result.get('n_trades', 0)} trade(s) recorded."))
        except runner.ExperimentFailed as e:
            abandoned_ids.append(contract_id)
            outcomes.append(ExperimentOutcome(
                contract_id=contract_id, outcome="abandoned", detail=str(e)))
            continue
        except runner.RunnerRejected as e:
            rejected_ids.append(contract_id)
            outcomes.append(ExperimentOutcome(
                contract_id=contract_id, outcome="rejected", detail=str(e)))
            continue
        except Exception as e:  # noqa: BLE001 — see module docstring's "Failure handling"
            error = f"{type(e).__name__}: {e}"
            outcomes.append(ExperimentOutcome(
                contract_id=contract_id, outcome="error", detail=error))
            stopped_early = True
            break

    pending_ids = [cid for cid in eligible_ids if cid not in set(attempted_ids)]
    finished = iso(now_ist())

    return SchedulerRunResult(
        started=started, finished=finished, max_experiments=max_experiments,
        eligible_contract_ids=eligible_ids, selected_contract_ids=selected_ids,
        attempted_contract_ids=attempted_ids, reported_contract_ids=reported_ids,
        abandoned_contract_ids=abandoned_ids, rejected_contract_ids=rejected_ids,
        pending_contract_ids=pending_ids, outcomes=outcomes,
        n_selected=len(selected_ids), n_attempted=len(attempted_ids),
        stopped_early=stopped_early, error=error,
    )


# ---------------------------------------------------------------------------
# CLI — mirrors research/overnight.py's argparse shape where it fits;
# deliberately no run log, no notification, no scheduler/queue/daemon (none
# of those are asked for by this slice, and adding them would be exactly
# the scope growth the slice discipline exists to prevent).
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Living Quant Experiment Scheduler v0 — runs a small, "
                     "bounded number of already-locked, not-yet-run "
                     "Contracts through the existing runner.run_experiment(). "
                     "Never creates a hypothesis, never locks a Contract.")
    ap.add_argument("--db", default=None)
    ap.add_argument("--registry-dir", default=None)
    ap.add_argument("--max-experiments", type=int, default=MAX_EXPERIMENTS_PER_RUN)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    registry_dir = Path(args.registry_dir) if args.registry_dir else REGISTRY_DIR

    try:
        store = Store.open(args.db) if args.db else Store.open()
    except Exception as e:
        print(f"FATAL: could not open the research store: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        result = run_scheduler(
            store, registry_dir=registry_dir, max_experiments=args.max_experiments)
    finally:
        store.close()

    if args.json:
        print(json.dumps(result._asdict(), indent=2, default=str))
    else:
        print(f"scheduler run {result.started} .. {result.finished}")
        print(f"  eligible: {len(result.eligible_contract_ids)}  "
              f"selected: {result.n_selected}  attempted: {result.n_attempted}")
        for o in result.outcomes:
            print(f"  {o.contract_id}: {o.outcome} — {o.detail}")
        if result.pending_contract_ids:
            print(f"  pending (not attempted this run): "
                  f"{', '.join(result.pending_contract_ids)}")
        if result.stopped_early:
            print(f"  STOPPED EARLY: {result.error}", file=sys.stderr)

    sys.exit(1 if result.stopped_early else 0)


if __name__ == "__main__":
    main()
