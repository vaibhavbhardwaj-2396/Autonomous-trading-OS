"""
Research Priority Engine v0 — Phase 2 Slice S.

The gap this closes: Slice R's Experiment Scheduler selects which eligible
locked Contracts to run using a deliberately neutral rule — oldest
`locked_at` first, `contract_id` tie-break — and its own module docstring
says outright that "a future Research Priority module replaces this
ordering; it is not built here." This module is that replacement, and
nothing more than what its own name says: it decides RESEARCH EXECUTION
ORDER among Contracts a human has already approved and locked. It is not a
trading signal, not a portfolio priority, not an expected-return model, and
it has no path — structurally, by absence — to influence live trading in
any way. See "WHAT THIS MODULE NEVER DOES" below.

    eligible locked Contracts (research.experiments.runner.runnable_contracts)
                |
                v
        THIS MODULE (priority ranking)
                |
                v
        scheduler.py's bounded selection (unchanged: first N, sequential)
                |
                v
        existing runner.run_experiment()   <- UNCHANGED

Design principle, same as every Phase 2 brain/ module before it: REUSE,
don't reimplement.
  - which contracts are locked-and-runnable  -> experiments.runner.runnable_contracts()
                                                 (called by scheduler.py, not here —
                                                 this module ranks a list it's handed,
                                                 it never discovers candidates itself)
  - which hypothesis a contract belongs to   -> experiments.evaluator.resolve_hypothesis_id()
  - a contract's research-area tag           -> brain.research_areas.area_of() (Slice M)
  - whether a contract is a validation/
    holdout sibling of a parent contract     -> the SAME hypothesis-claim-log walk
                                                 hypothesis_intake._prior_split_derivation()
                                                 (Slice K) already does (payload's
                                                 `split_of`/`split` extras) — not a new
                                                 validation model
  - whether that parent's evidence is
    actually promising                       -> experiments.comparison.
                                                 evaluate_hypothesis_evidence() (Slice E) —
                                                 the SAME PROMISING/WEAK/INCONCLUSIVE/
                                                 CONTRADICTED/REDUNDANT verdict every other
                                                 evidence-reading module already uses
Nothing here defines a second notion of "research area", "validation
sibling", or "promising" — every one of those words means exactly what the
module that already owns it says it means. The only genuinely new logic in
this file is the priority ordering itself.

WHAT THIS MODULE NEVER DOES, structurally true by absence:
  - call `run_experiment()`, `simulate()`, `load_runnable_contract()`, or any
    other execution-boundary function — `research.experiments.runner` is
    never imported here at all. This module only ever receives a list of
    already-selected Contract objects and hands back an ordering; it cannot
    cause an experiment to run.
  - create a hypothesis, a draft, or lock a Contract — `hypothesis_intake`
    is never imported here.
  - call Research AI, an LLM, or any external service — nothing here makes
    a network call, and no AI/embedding/ML library is imported.
  - touch live trading state — no `engine.execute`, `engine.guardrails`,
    `engine.broker*`, or `engine.journal` import; no read of
    `memory/state.json` or broker credentials. This is pure research
    metadata ranking, nothing else.
  - read raw historical market data or run a backtest — the only I/O here
    is a handful of small, already-existing research-memory reads (the
    hypothesis-claim log, an area tag, an evidence summary already computed
    from verdicts that already exist). No `Replay`, no `AsOfView.prices()`,
    no price/observation table touched at all.
  - write anything, anywhere. Every function in this module is a pure read
    over the Store/registry state it's handed, computing and returning a
    value — no `store.append()`, no `Contract.save()`, no `Contract.lock()`.
  - use the wall clock, randomness, or any other non-deterministic input.
    `Contract.locked_at` (a value already recorded on the Contract, at lock
    time, by code this module never calls) is the only notion of "time"
    anywhere in this file — never `datetime.now()`, never a caller-supplied
    "now" either, since research priority orders by WHEN something was
    locked, not relative to any reference instant. Same (contracts, store)
    input always produces the same output.

Priority dimensions and the exact ordering rule (v0 — deliberately NOT a
calibrated score): a plain, 4-part deterministic lexicographic key, sorted
ascending (lower sorts first = higher priority):

    1. CONFIRMATION — a validation/holdout sibling of a parent contract
       whose accumulated evidence is already PROMISING outranks an ordinary
       exploratory contract. (0 if confirmation, else 1.)
    2. RESEARCH-AREA BALANCE — a contract's rank among only the OTHER
       eligible contracts sharing its own research area (oldest-in-area
       first, 0-indexed) — see `_area_ranks()` below for exactly why
       ranking on this number, globally, is a round-robin: every area's
       single oldest pending contract (area_rank 0) outranks EVERY area's
       second-oldest (area_rank 1), so one area with 100 pending
       experiments can never crowd out an area with only 2 — each gets a
       turn every round, deterministically, with no configuration and no
       artificially forced diversity beyond that.
    3. AGE — older `locked_at` first, globally — a further tie-break among
       contracts that landed on the same area_rank (e.g. the "first pick"
       from two different areas), so the actually-older one still goes
       first between them.
    4. CONTRACT_ID — ascending, the final deterministic tie-break.

This is deliberately a small, transparent, inspectable tuple with an
obvious meaning per component — not a weighted score with invented
coefficients. `explain()` below exposes every one of these four raw values
per contract, in final order, so "why was A selected before B" always has a
concrete, non-AI-generated answer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .. import memory as rm
from ..contracts import REGISTRY_DIR
from ..experiments import evaluator
from ..experiments.comparison import ComparisonRejected, evaluate_hypothesis_evidence
from ..store import Store
from . import research_areas

UNASSIGNED_AREA = "__unassigned__"
"""Neutral fallback bucket for a contract whose hypothesis has never been
tagged via research_areas.tag_hypothesis() (Slice M). An untagged contract
is never excluded, penalized, or treated as an error — it simply competes
for its turn in the same round-robin as every tagged area, grouped together
with every other untagged contract as one bucket, so a tagging gap can
never cause starvation (Slice S's own "F. Missing area" requirement)."""


# ---------------------------------------------------------------------------
# Per-contract dimension resolution — each one a thin, defensive read over
# an already-existing, already-tested module. None of these can raise for
# any input a caller would realistically hand them; each documents its own
# fallback explicitly rather than letting an edge case propagate.
# ---------------------------------------------------------------------------

def _split_metadata(store: Store, contract_id: str) -> tuple:
    """(split_of, split) for `contract_id` if it was created via
    hypothesis_intake.derive_split_contract() (Slice K) — i.e. it is a
    validation/holdout sibling of an earlier parent contract, not a fresh,
    independently-proposed experiment. This is the exact same
    DATASET_HYPOTHESIS walk (payload.contract_id == contract_id)
    hypothesis_intake._prior_split_derivation() and
    draft_backlog._creation_row() already perform, kept local here rather
    than imported from either (both are private helpers of modules on this
    slice's do-not-modify list) — same rows, same filter, no new dataset,
    no new write path. Returns (None, None) for an ordinary contract, or
    for one this walk cannot find a creation row for at all."""
    rows = rm.query_research_log(store, rm.DATASET_HYPOTHESIS)
    for r in rows:
        payload = r.get("payload") or {}
        if payload.get("contract_id") == contract_id:
            return payload.get("split_of"), payload.get("split")
    return None, None


def is_confirmation_experiment(
    store: Store, contract_id: str, *, registry_dir: Path = REGISTRY_DIR,
) -> bool:
    """True iff `contract_id` is a validation/holdout sibling (Slice K) of a
    parent contract whose OWN accumulated evidence is already classified
    PROMISING by `research.experiments.comparison.
    evaluate_hypothesis_evidence()` — the existing, unmodified evidence
    semantics, reused rather than reimplemented (no new validation model,
    per this slice's own instructions).

    Deliberately narrower than "is structurally a validation/holdout
    sibling at all": a sibling of a parent that has never been run, was
    scored WEAK/INCONCLUSIVE/CONTRADICTED, or has no recorded verdict yet
    is NOT treated as a confirmation experiment for priority purposes —
    this dimension exists to move a hypothesis that already looks
    promising's own validation step ahead of the queue, exactly as the
    slice's own framing describes ("more valuable as a confirmation
    experiment AFTER a promising discovery"), not to elevate every
    structurally-a-sibling contract regardless of what its parent showed.
    `evaluate_hypothesis_evidence()` raising `ComparisonRejected` (parent
    never run, or never linked to a hypothesis) is treated as "not yet
    confirmable" — False, not an error.
    """
    split_of, split = _split_metadata(store, contract_id)
    if not split_of or split not in ("validation", "holdout"):
        return False
    try:
        summary = evaluate_hypothesis_evidence(store, split_of, registry_dir=registry_dir)
    except ComparisonRejected:
        return False  # parent never run / never scored yet — nothing to confirm yet
    return summary["verdict"] == "PROMISING"


def research_area_for(store: Store, contract_id: str) -> str:
    """The research area (Slice M) of the hypothesis `contract_id` belongs
    to, or UNASSIGNED_AREA if the contract has no resolvable hypothesis or
    the hypothesis was never tagged. Never raises."""
    hypothesis_id = evaluator.resolve_hypothesis_id(store, contract_id)
    if not hypothesis_id:
        return UNASSIGNED_AREA
    area = research_areas.area_of(store, hypothesis_id)
    return area or UNASSIGNED_AREA


def _area_ranks(contracts: list, areas: dict) -> dict:
    """contract_id -> its 0-indexed position among ONLY the other eligible
    contracts sharing its research area (oldest `locked_at` first,
    contract_id tie-break within the area). See the module docstring's
    "Priority dimensions" section for why ranking GLOBALLY on this number
    is exactly a round-robin across areas — the oldest pending contract in
    every area ties for rank 0, so no single area's backlog size can push
    every other area's oldest contract out of the front of the queue."""
    by_area: dict = {}
    for c in contracts:
        by_area.setdefault(areas[c.id], []).append(c)
    ranks: dict = {}
    for group in by_area.values():
        ordered = sorted(group, key=lambda c: (c.locked_at is None, c.locked_at or "", c.id))
        for i, c in enumerate(ordered):
            ranks[c.id] = i
    return ranks


def priority_key(contract, *, area_rank: int, is_confirmation: bool) -> tuple:
    """The pure, explainable v0 ordering key for one contract, given its
    already-resolved per-contract dimension values — this function itself
    needs neither a Store nor the rest of the eligible set; every
    cross-contract dimension (area_rank) is computed once, up front, by
    whichever caller assembled it (`explain()` below). Ascending sort on
    this tuple is the complete v0 priority rule — see the module docstring
    for what each of the four components means and why.

    `locked_at` is compared as the plain string `Contract.lock()` already
    writes (`dt.datetime.now().isoformat(timespec="seconds")`, the same
    naive, non-IST-aware convention `hypothesis_intake.locks_in_period()`
    and `scheduler.eligible_contracts()` already treat as authoritative) —
    not re-parsed into a datetime. ISO-8601-shaped timestamps compare
    correctly as plain strings, and treating a locked_at that somehow isn't
    one as an opaque, still-comparable string is the same defensive
    posture `scheduler.py` already established rather than risking a
    parse-time exception on a malformed value ("G. Missing/odd
    timestamps" — follow existing repository conventions defensively,
    verbatim).
    """
    return (
        0 if is_confirmation else 1,
        area_rank,
        contract.locked_at is None, contract.locked_at or "",
        contract.id,
    )


def explain(
    store: Store, contracts: list, *, registry_dir: Path = REGISTRY_DIR,
) -> list:
    """Every contract in `contracts`, annotated with the exact dimension
    values that decided its place in the queue, already sorted into final
    v0 priority order — the concrete, inspectable answer to "why was
    Contract A selected before Contract B" this slice's "Explainability"
    section asks for. Generates no prose and calls no AI: every field is a
    plain fact this module (or one it reuses) already computed.

    This is the ONE place the ranking is actually computed —
    `rank_experiments()` below is a thin wrapper that derives Contract
    order from this function's own output, so the two can never disagree
    (the "no duplicated priority logic" requirement, honored by
    construction rather than by convention).
    """
    areas = {c.id: research_area_for(store, c.id) for c in contracts}
    area_ranks = _area_ranks(contracts, areas)
    confirmations = {
        c.id: is_confirmation_experiment(store, c.id, registry_dir=registry_dir)
        for c in contracts
    }

    rows = [
        {
            "contract_id": c.id,
            "is_confirmation": confirmations[c.id],
            "research_area": areas[c.id],
            "area_rank": area_ranks[c.id],
            "locked_at": c.locked_at,
            "priority_key": priority_key(
                c, area_rank=area_ranks[c.id], is_confirmation=confirmations[c.id]),
        }
        for c in contracts
    ]
    rows.sort(key=lambda r: r["priority_key"])
    return rows


def rank_experiments(
    store: Store, contracts: list, *, registry_dir: Path = REGISTRY_DIR,
) -> list:
    """Re-order `contracts` (any list of locked Contract objects — normally
    `scheduler.py`'s own eligible-contracts list) into v0 research priority
    order: confirmation first, underrepresented research areas next, oldest
    waiting experiment next, contract_id tie-break.

    Pure and deterministic: reads research memory (the hypothesis-claim
    log, an area tag, an evidence summary already computed from existing
    verdicts) but writes nothing, calls no AI, makes no network call, and
    never touches `engine/*`, a broker, or live state. Identical
    `(store, contracts)` always produces an identical result — no wall
    clock, no randomness.
    """
    rows = explain(store, contracts, registry_dir=registry_dir)
    by_id = {c.id: c for c in contracts}
    return [by_id[r["contract_id"]] for r in rows]
