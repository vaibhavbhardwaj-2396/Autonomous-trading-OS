"""
Research memory — Phase 1 Slice A.

Thin, typed wrapper functions around the existing bitemporal Store, not a
second storage engine. Everything here writes to the same `observations`
table research/sources/*.py already uses, under new `dataset` values:

    research_anomaly              — observatory.py findings (not built yet)
    research_hypothesis_proposal  — a CLAIM about the world (see below)
    research_experiment_verdict   — evaluator.py's scored result for a
                                     locked research.contracts.Contract
    research_evidence_summary     — comparison.py's cross-experiment read of
                                     one hypothesis's accumulated verdicts
                                     (Phase 1 Slice E)
    research_note                 — free-text research-cycle commentary
    research_area_tag             — hypothesis_id -> research_area label,
                                     purely organizational metadata for
                                     navigating the research space (Phase 2
                                     Slice M, research/brain/research_areas.py)
    research_discovery_search     — provenance: which discovery run (e.g. a
                                     Research AI investigate() call) produced
                                     a given hypothesis_id, against which
                                     research-memory as_of snapshot, using
                                     which implementation/prompt version
                                     (Phase 2 Slice N,
                                     research/brain/discovery_provenance.py)
    research_opportunity_event    — the Autonomous Research Control Plane's
                                     audit trail (research/brain/opportunity.py):
                                     one row per lifecycle transition, user
                                     override (freeze/reopen/retire/force-
                                     reassess), or autonomous promotion —
                                     never research data itself, only a record
                                     of what changed and why

Hypothesis vs. Experiment — kept deliberately distinct, per design review:
a Hypothesis (recorded here) is a claim ("high-volume breakouts may have
higher subsequent expectancy"); a research.contracts.Contract is the precise,
hash-locked test of one specific version of that claim (a particular
threshold, universe, window, exit rule). Several Contracts can trace back to
one Hypothesis. This module only records the claim side; nothing here writes
to research/registry/ or touches Contract — that stays hypothesis_intake.py's
job (not built yet).

This module imports nothing from engine/ — same rule as the rest of research/.
"""

from __future__ import annotations

import uuid
from typing import Optional

from .store import Store, TimeLike, now_ist

DATASET_ANOMALY = "research_anomaly"
DATASET_HYPOTHESIS = "research_hypothesis_proposal"
DATASET_VERDICT = "research_experiment_verdict"
DATASET_EVIDENCE = "research_evidence_summary"
DATASET_NOTE = "research_note"
DATASET_RESEARCH_AREA = "research_area_tag"
DATASET_DISCOVERY_SEARCH = "research_discovery_search"
DATASET_OPPORTUNITY_EVENT = "research_opportunity_event"

MARKET_ENTITY = "_market"  # same convention research/sources already uses


def new_hypothesis_id() -> str:
    """HYP-<date>-<8 hex chars>. Independent of Contract ids (research.contracts
    generates its own EXP-style ids) — see the module docstring for why the two
    are kept separate rather than collapsed into one concept."""
    return f"HYP-{now_ist().strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}"


def record_anomaly(
    store: Store,
    *,
    entity: str,
    metric: str,
    value: float,
    baseline: float,
    z_score: float,
    as_of: TimeLike,
    source: str,
    knowledge_time: Optional[TimeLike] = None,
    extra: Optional[dict] = None,
) -> Optional[int]:
    """Record one observatory finding. `as_of` is when the anomaly was true in
    the world (event_time); knowledge_time defaults to the same instant since
    an observatory query only ever sees data already visible to it — it cannot
    itself introduce a knowledge-time lag."""
    payload = {"metric": metric, "value": value, "baseline": baseline,
              "z_score": z_score, **(extra or {})}
    return store.append(
        dataset=DATASET_ANOMALY, entity=entity, event_time=as_of,
        knowledge_time=knowledge_time or as_of, source=source, payload=payload,
    )


def record_hypothesis_proposal(
    store: Store,
    *,
    claim: str,
    source: str,
    entity: str = MARKET_ENTITY,
    hypothesis_id: Optional[str] = None,
    extra: Optional[dict] = None,
) -> tuple[str, Optional[int]]:
    """Record a claim about the world (not yet a locked, testable Contract).

    Returns (hypothesis_id, row_id). Pass an existing hypothesis_id to add a
    later note against the same claim rather than mint a new one — useful once
    several Contracts test variations of the same underlying idea (see the
    EXP-001-A/B/C example in the design review).
    """
    hid = hypothesis_id or new_hypothesis_id()
    now = now_ist()
    payload = {"hypothesis_id": hid, "claim": claim, **(extra or {})}
    row_id = store.append(
        dataset=DATASET_HYPOTHESIS, entity=entity, event_time=now,
        knowledge_time=now, source=source, payload=payload,
    )
    return hid, row_id


def record_experiment_verdict(
    store: Store,
    *,
    contract_id: str,
    verdict: dict,
    hypothesis_id: Optional[str] = None,
    entity: str = MARKET_ENTITY,
    source: str = "research.experiments.evaluator",
) -> Optional[int]:
    """Record evaluator.py's scored result for one locked contract (not built
    yet). `verdict` is whatever evaluator.py computes — expectancy, win rate,
    t-stat, sample size, comparison_count() at time of testing — stored as-is
    so the schema here never has to anticipate every statistic in advance."""
    now = now_ist()
    payload = {"contract_id": contract_id, "hypothesis_id": hypothesis_id, **verdict}
    return store.append(
        dataset=DATASET_VERDICT, entity=entity, event_time=now,
        knowledge_time=now, source=source, payload=payload,
    )


def record_evidence_summary(
    store: Store,
    *,
    hypothesis_id: str,
    contract_id: str,
    summary: dict,
    entity: str = MARKET_ENTITY,
    source: str = "research.experiments.comparison",
) -> Optional[int]:
    """Record comparison.py's cross-experiment read of one hypothesis (Phase 1
    Slice E) — the deterministic verdict ("PROMISING"/"WEAK"/"INCONCLUSIVE"/
    "CONTRADICTED"/"REDUNDANT") produced by comparing this contract's result
    against every other scored variant of the same hypothesis. `summary` is
    stored as-is, same pattern as record_experiment_verdict, so this schema
    doesn't have to anticipate every field comparison.py computes."""
    now = now_ist()
    payload = {"hypothesis_id": hypothesis_id, "contract_id": contract_id, **summary}
    return store.append(
        dataset=DATASET_EVIDENCE, entity=entity, event_time=now,
        knowledge_time=now, source=source, payload=payload,
    )


def record_research_note(
    store: Store,
    *,
    note: str,
    source: str,
    entity: str = MARKET_ENTITY,
    extra: Optional[dict] = None,
) -> Optional[int]:
    """Free-text commentary from a research cycle — the equivalent of an entry
    in memory/research_log.md, but bitemporal and queryable."""
    now = now_ist()
    payload = {"note": note, **(extra or {})}
    return store.append(
        dataset=DATASET_NOTE, entity=entity, event_time=now,
        knowledge_time=now, source=source, payload=payload,
    )


def record_research_area_tag(
    store: Store,
    *,
    hypothesis_id: str,
    research_area: str,
    source: str,
    entity: str = MARKET_ENTITY,
) -> Optional[int]:
    """Record which research area/topic a hypothesis belongs to (Phase 2
    Slice M) — purely organizational metadata for navigating the research
    space ("have we already looked at momentum?"), never anything that feeds
    Contract identity, hashing, or locking. Contracts and hypotheses know
    nothing about this dataset; it exists only so research/brain/
    research_areas.py can group hypothesis_ids for the digest.

    Append-only, same as everything else here: tagging a hypothesis into a
    new area does not erase the old tag, it appends a new row. The most
    recent row (by event_ts, then id — the order query_research_log already
    returns) is the one callers should treat as the current tag, exactly the
    same "last write wins over an append-only log" pattern hypothesis_intake
    uses for split derivations.
    """
    now = now_ist()
    payload = {"hypothesis_id": hypothesis_id, "research_area": research_area}
    return store.append(
        dataset=DATASET_RESEARCH_AREA, entity=entity, event_time=now,
        knowledge_time=now, source=source, payload=payload,
    )


def record_discovery_search(
    store: Store,
    *,
    hypothesis_id: str,
    discovery_type: str,
    as_of: str,
    version: str,
    source: str,
    entity: str = MARKET_ENTITY,
    extra: Optional[dict] = None,
) -> Optional[int]:
    """Record a minimal, queryable provenance link from one discovery run to
    the hypothesis_id it produced (Phase 2 Slice N) — the first connection
    in the eventual "digest snapshot -> discovery process -> hypothesis ->
    contracts -> evidence" lineage, and for now ONLY that first connection.

    Deliberately NOT a copy of the digest body: `as_of` (the same string the
    caller's `build_digest(store, as_of)` used) plus `version` (which
    discovery implementation/prompt produced this) is the minimum needed to
    reconstruct "what was the system looking at" — `build_digest(store,
    as_of)` reproduces the exact same digest later, so persisting the digest
    itself here would just be a second, driftable copy of something already
    reproducible from `as_of` alone.

    `discovery_type` distinguishes which kind of discovery process ran (the
    only value that exists yet is "research_ai", from
    research/brain/investigator.py — this dataset is intentionally generic
    enough that a future discovery mechanism, e.g. a mathematical discovery
    engine, could record its own runs here too, under its own
    discovery_type/version, without a schema change). `version` is a plain
    string identifier the discovery mechanism itself defines and owns (see
    research/brain/investigator.py's RESEARCH_AI_VERSION) — this module does
    not interpret or validate it, on purpose: introducing a general AI/
    discovery-mechanism registry here is explicitly out of scope for this
    slice.
    """
    now = now_ist()
    payload = {
        "hypothesis_id": hypothesis_id,
        "discovery_type": discovery_type,
        "as_of": as_of,
        "version": version,
        **(extra or {}),
    }
    return store.append(
        dataset=DATASET_DISCOVERY_SEARCH, entity=entity, event_time=now,
        knowledge_time=now, source=source, payload=payload,
    )


def record_opportunity_event(
    store: Store,
    *,
    opportunity_id: str,
    hypothesis_id: str,
    event_type: str,
    actor: str,
    source: str,
    reason: Optional[str] = None,
    previous_state: Optional[str] = None,
    new_state: Optional[str] = None,
    evidence_signature: Optional[str] = None,
    priority_before: Optional[float] = None,
    priority_after: Optional[float] = None,
    confidence_before: Optional[float] = None,
    confidence_after: Optional[float] = None,
    entity: str = MARKET_ENTITY,
    extra: Optional[dict] = None,
) -> Optional[int]:
    """Record one Autonomous Research Control Plane event (research/brain/
    opportunity.py) — a lifecycle transition, a user override
    (freeze/reopen/retire/force-reassess), or an autonomous promotion
    attempt. Append-only, same pattern as every other dataset in this
    module: the CURRENT state of an opportunity (frozen? retired? what stage
    was it last computed as?) is always a fold over its own history, never a
    separately-maintained field that could drift out of sync with it — see
    research.brain.opportunity._fold_events().

    `actor` is a free string identifying who/what caused this event — a
    human's name/handle for a user override, or the explicit system approver
    identity (research.brain.opportunity.SYSTEM_APPROVER) for an autonomous
    action. Never anonymous, never blank; this is the audit trail §12 of the
    Master Vision asks for.
    """
    now = now_ist()
    payload = {
        "opportunity_id": opportunity_id, "hypothesis_id": hypothesis_id,
        "event_type": event_type, "actor": actor, "reason": reason,
        "previous_state": previous_state, "new_state": new_state,
        "evidence_signature": evidence_signature,
        "priority_before": priority_before, "priority_after": priority_after,
        "confidence_before": confidence_before, "confidence_after": confidence_after,
        **(extra or {}),
    }
    return store.append(
        dataset=DATASET_OPPORTUNITY_EVENT, entity=entity, event_time=now,
        knowledge_time=now, source=source, payload=payload,
    )


def query_research_log(
    store: Store,
    dataset: str,
    *,
    as_of: Optional[TimeLike] = None,
    entity: Optional[str] = None,
    limit: Optional[int] = None,
) -> list[dict]:
    """Read back any of the four datasets above, gated at `as_of` (defaults to
    now — there is no reason a research cycle should see its own future notes
    either). A thin convenience so callers don't have to construct a view by
    hand for a simple lookback."""
    view = store.view(as_of or now_ist())
    return view.observations(dataset, entity=entity, limit=limit, latest_only=False)
