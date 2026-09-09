"""
Research Digest — Phase 1 Slice C.

A deterministic, bounded snapshot of the research state: what the Observatory
has flagged, and what the contract registry already knows. This is the ONLY
thing a future research-investigator Claude call (not built yet — that is the
next slice's Digest-to-Claude wiring, still unbuilt here) would ever be shown.
That makes what this module leaves OUT at least as important as what it
includes:

    INCLUDED                              EXCLUDED, structurally (this module
    - Observatory anomalies               imports none of the modules that
      (research.memory, DATASET_ANOMALY)  could reach these, so there is
    - the contract registry                nothing to accidentally include):
      (research.contracts.registry)       - engine/* (guardrails, execute,
    - the multiple-comparisons count        broker, live positions/orders)
      (research.contracts.comparison_       - memory/state.json, strategy.md
      count)                               - broker credentials / .env
    - which contracts were already        - raw source code of the trading
      tested (status reported/abandoned/    system
      superseded), so a proposal doesn't
      rediscover a dead idea

Determinism is the whole point of building this now, before anything reads
it: build_digest(store, as_of) called twice against an unchanged store must
produce an identical result, byte for byte once rendered. No wall-clock, no
randomness, no set-ordering. Every list here is explicitly sorted before it
is returned.

This module performs zero interpretation — it does not decide whether an
anomaly is interesting, does not rank hypotheses, does not summarize in
prose. It assembles facts. Judgment is the investigator's job, in a later
slice, and only after a human has decided the investigator is allowed to
exist.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .. import memory as rm
from ..contracts import Contract, REGISTRY_DIR, comparison_count, registry as _registry
from ..experiments import comparison, evaluator
from ..store import Store, TimeLike, iso, to_dt
from . import discovery_provenance, research_areas, similarity

DEFAULT_ANOMALY_LIMIT = 50
DEFAULT_DUPLICATE_GROUP_LIMIT = 20
DEFAULT_AREA_LIMIT = 50
DEFAULT_EVIDENCE_LIMIT = 20
TESTED_STATUSES = ("reported", "abandoned", "superseded")

# Slice V — the ordering rule for the new `evidence` section (see
# build_digest's evidence block below for the full rationale): rank by how
# informative/actionable the hypothesis's aggregate verdict already is,
# according to comparison.py's own existing verdict semantics — nothing new
# is being scored or invented here, this is just an ordering over the fixed
# set of strings VALID_VERDICTS already defines. PROMISING (a positive,
# actionable finding) and CONTRADICTED (a definite, actionable "the evidence
# disagrees with itself" finding) both represent the evidence actually
# resolving to *something*; WEAK and INCONCLUSIVE both represent "no
# resolved signal yet", with INCONCLUSIVE strictly less informative than
# WEAK (WEAK means enough was tested to rule out easy wins; INCONCLUSIVE
# means there isn't even enough of a sample to say that much).
# `evaluate_hypothesis_evidence()` can also return REDUNDANT for a specific
# duplicate *contract*, but every hypothesis entry below is deliberately
# evaluated through its earliest-locked scored contract (see
# `_representative_contract_id`), which by construction can never itself be
# a duplicate — so REDUNDANT should never actually appear as a hypothesis-
# level verdict here. It is still given a rank (lowest) purely so sorting
# never raises on an unexpected/legacy value.
_VERDICT_RANK = {"PROMISING": 0, "CONTRADICTED": 1, "WEAK": 2, "INCONCLUSIVE": 3, "REDUNDANT": 4}

# Fields surfaced per contract — bounded and deliberately excludes nothing
# from the hashed hypothesis itself (a reader needs the actual entry/exit
# rules to judge "has this already been tried"), but never includes internal
# bookkeeping like file paths.
_CONTRACT_FIELDS = (
    "id", "title", "hypothesis", "status", "universe", "signal",
    "evaluation_start", "evaluation_end", "llm_features", "locked_hash",
    "falsification", "abandon_condition",
)


def _contract_summary(c: Contract) -> dict:
    d = {k: getattr(c, k) for k in _CONTRACT_FIELDS}
    for rule_field in ("entry_rule", "exit_rule"):
        raw = getattr(c, rule_field)
        try:
            d[rule_field] = json.loads(raw)
        except (TypeError, ValueError):
            d[rule_field] = raw  # pre-Slice-C contracts may hold free text
    return d


def _anomaly_item(row: dict) -> dict:
    payload = row.get("payload") or {}
    return {
        "anomaly_id": row["id"],
        "dataset": payload.get("source_dataset"),
        "entity": row["entity"],
        "metric": payload.get("metric"),
        "value": payload.get("value"),
        "baseline": payload.get("baseline"),
        "z_score": payload.get("z_score"),
        "as_of": row["event_time"],
    }


def _representative_contract_id(
    store: Store, scored_contract_ids: list[str], *, registry_dir: Path,
) -> Optional[str]:
    """Pick the one scored contract whose evaluate_hypothesis_evidence() call
    represents the WHOLE hypothesis, not just one variant of it.

    Why this matters: evaluate_hypothesis_evidence(store, contract_id) always
    aggregates over every scored sibling of contract_id's hypothesis — the
    aggregate verdict itself (PROMISING/WEAK/INCONCLUSIVE/CONTRADICTED) does
    not depend on which sibling you pass in. The one thing that DOES depend
    on the choice is whether that specific contract_id itself happens to be
    an exact-duplicate variant under comparison.py's own within-hypothesis
    fingerprint check — in which case the call returns "REDUNDANT" for THAT
    contract, which would misrepresent the hypothesis as a whole if it has
    other, non-duplicate, independently evaluated variants.

    The earliest-locked scored contract can never be marked a duplicate by
    that check (duplicate status is only ever assigned to the later members
    of a fingerprint group — see comparison.py's own docstring/logic), so
    picking it deterministically avoids an arbitrary REDUNDANT reading
    without reimplementing any of comparison.py's fingerprint logic here."""
    def _sort_key(cid: str) -> tuple:
        try:
            c = Contract.load(cid, registry_dir)
        except FileNotFoundError:
            return ("", cid)  # missing registry file — sorts first, harmless
        return (c.locked_at or "", cid)

    if not scored_contract_ids:
        return None
    return min(scored_contract_ids, key=_sort_key)


def _evidence_item(
    store: Store, hypothesis_id: str, *, registry_dir: Path,
    duplicate_member_ids: set,
) -> Optional[dict]:
    """One bounded evidence-feedback entry for `hypothesis_id`, or None if it
    turns out to have no scored contract after all (defensive — every caller
    already filtered to hypothesis_ids that appeared in at least one recorded
    verdict, so this should not normally happen).

    Every number/string here comes from machinery that already exists and is
    already relied on elsewhere (evaluator.py, comparison.py,
    research_areas.py, discovery_provenance.py, similarity.py) — nothing is
    recomputed from raw trades, and nothing here is written back anywhere."""
    sibling_ids = evaluator.contract_ids_for_hypothesis(store, hypothesis_id)
    scored_ids = [
        cid for cid in sibling_ids
        if evaluator.verdict_for_contract(store, cid) is not None
    ]
    representative_id = _representative_contract_id(
        store, scored_ids, registry_dir=registry_dir)
    if representative_id is None:
        return None

    try:
        evidence = comparison.evaluate_hypothesis_evidence(
            store, representative_id, registry_dir=registry_dir)
    except comparison.ComparisonRejected:
        return None  # defensive; representative_id was just confirmed scored

    try:
        title = Contract.load(representative_id, registry_dir).title
    except FileNotFoundError:
        title = None

    # Research-area tags (Slice M) and discovery provenance (Slice N) are
    # both recorded with the real wall clock, not the bitemporal research
    # clock `as_of` gates elsewhere in this digest (see the module docstring
    # note in build_digest's evidence block) — read "as of now" here,
    # deliberately un-gated, exactly like evaluator/comparison's own verdict
    # and evidence reads already are.
    area = research_areas.area_of(store, hypothesis_id)
    provenance_rows = discovery_provenance.discovery_search_for_hypothesis(
        store, hypothesis_id)
    provenance_ref = None
    if provenance_rows:
        first = provenance_rows[0]
        provenance_ref = {
            "discovery_type": first.get("discovery_type"),
            "version": first.get("version"),
            "as_of": first.get("as_of"),
        }

    try:
        locked_at = Contract.load(representative_id, registry_dir).locked_at
    except FileNotFoundError:
        locked_at = None

    return {
        "hypothesis_id": hypothesis_id,
        "title": title,
        "verdict": evidence["verdict"],
        "contract_count": len(sibling_ids),
        "n_variants_scored": evidence["n_variants_scored"],
        "positive_count": evidence["positive_count"],
        "negative_count": evidence["negative_count"],
        "evidence_summary": evidence["rationale"],
        "research_area": area,
        "discovery_provenance": provenance_ref,
        "has_exact_duplicate": any(cid in duplicate_member_ids for cid in sibling_ids),
        "_locked_at": locked_at,  # sort key only — stripped before returning
    }


def build_digest(
    store: Store,
    as_of: TimeLike,
    *,
    anomaly_limit: Optional[int] = DEFAULT_ANOMALY_LIMIT,
    duplicate_limit: Optional[int] = DEFAULT_DUPLICATE_GROUP_LIMIT,
    area_limit: Optional[int] = DEFAULT_AREA_LIMIT,
    evidence_limit: Optional[int] = DEFAULT_EVIDENCE_LIMIT,
    registry_dir: Path = REGISTRY_DIR,
) -> dict:
    """Assemble the digest. Every collection is sorted by a stable key before
    being returned, independent of sqlite row order or filesystem glob order,
    so the result is reproducible across processes and machines."""
    all_anomalies = rm.query_research_log(store, rm.DATASET_ANOMALY, as_of=as_of)
    all_anomalies = sorted(all_anomalies, key=lambda r: (r["event_ts"], r["id"]))
    shown = all_anomalies[-anomaly_limit:] if anomaly_limit else all_anomalies
    anomaly_items = [_anomaly_item(r) for r in shown]

    contracts = sorted(_registry(registry_dir), key=lambda c: c.id)
    contract_items = [_contract_summary(c) for c in contracts]
    tested_items = [_contract_summary(c) for c in contracts if c.status in TESTED_STATUSES]

    # Exact-duplicate experiment specifications across the WHOLE registry,
    # independent of hypothesis_id (Slice L) — already deterministically
    # sorted by fingerprint by similarity.duplicate_groups() itself. Bounded
    # the same way `anomalies` is bounded, and deliberately kept to
    # id/hypothesis_id/status per member rather than repeating each
    # contract's full rule — that detail is already available by
    # cross-referencing contract_registry.contracts above, so this section
    # doesn't dump the registry a second time.
    all_dup_groups = similarity.duplicate_groups(store, registry_dir=registry_dir)
    # `duplicate_limit=0` must genuinely mean "show none" rather than being
    # treated as falsy-and-therefore-unbounded — distinct from `None`, which
    # means "no limit". Checked explicitly rather than by truthiness.
    dup_shown = all_dup_groups[:duplicate_limit] if duplicate_limit is not None else all_dup_groups
    duplicate_items = [g.to_dict() for g in dup_shown]

    # Research-area / hypothesis-family map (Slice M) — a bounded summary of
    # research/brain/research_areas.py's grouping, kept terse (name + count,
    # not the full hypothesis_id membership already available from that
    # module directly) so this section stays a navigation aid rather than a
    # second full listing of the registry. Sorted by area name inside
    # research_areas.groups() itself, so this is deterministic for free.
    all_area_groups = research_areas.groups(store, as_of=as_of)
    area_shown = all_area_groups[:area_limit] if area_limit is not None else all_area_groups
    area_items = [{"name": g.name, "hypothesis_count": len(g.hypothesis_ids)} for g in area_shown]

    # Research-evidence feedback loop (Slice V). The read-side half of
    # "Experiment -> Verdict -> Evidence -> Digest -> Research AI": a
    # bounded, per-hypothesis summary of what evaluator.py/comparison.py
    # already know, so the next discovery cycle can see what has already
    # been tried and how it turned out, rather than treating every digest as
    # a blank slate. Deliberately NOT gated by `as_of` — like
    # contract_registry/previously_tested/exact_duplicates above (and unlike
    # anomalies/research_areas, which read genuinely bitemporal market/
    # tagging data), verdicts and evidence summaries are live/operational
    # research-pipeline state with no bitemporal concept of their own
    # (evaluator.verdict_for_contract and comparison.evaluate_hypothesis_
    # evidence take no `as_of` parameter at all) — so this section reads
    # "as of right now", the same convention those other un-gated sections
    # already use for the same reason.
    #
    # duplicate_member_ids reuses all_dup_groups (computed above for
    # exact_duplicates) rather than a second call into similarity.py, so
    # flagging a hypothesis's exact-duplicate status here costs no extra
    # registry scan.
    duplicate_member_ids = {m.contract_id for g in all_dup_groups for m in g.members}
    verdict_rows = rm.query_research_log(store, rm.DATASET_VERDICT)
    hypothesis_ids_with_verdicts = sorted({
        r["payload"].get("hypothesis_id") for r in verdict_rows
        if r["payload"].get("hypothesis_id")
    })
    all_evidence_items = [
        item for hid in hypothesis_ids_with_verdicts
        if (item := _evidence_item(
            store, hid, registry_dir=registry_dir,
            duplicate_member_ids=duplicate_member_ids)) is not None
    ]

    # Ordering rule (documented once, here, rather than re-derived per call):
    # 1. verdict informativeness (_VERDICT_RANK — PROMISING/CONTRADICTED
    #    ahead of WEAK/INCONCLUSIVE, see the module-level comment on
    #    _VERDICT_RANK for the full rationale);
    # 2. most recent evidence, using the representative contract's
    #    `locked_at` (Contract carries no separate "verdict recorded at"
    #    timestamp, and locked_at is already the timestamp this system uses
    #    elsewhere — e.g. research/brain/priority.py's own age tie-break —
    #    as "when this experiment entered the pipeline");
    # 3. hypothesis_id, for a fully deterministic tie-break.
    # Implemented as three stable sorts, lowest-priority first, exploiting
    # Python's guaranteed sort stability rather than hand-building a single
    # composite key that has to invert a string timestamp for "most recent
    # first" ordering.
    all_evidence_items.sort(key=lambda e: e["hypothesis_id"])
    all_evidence_items.sort(key=lambda e: e["_locked_at"] or "", reverse=True)
    all_evidence_items.sort(key=lambda e: _VERDICT_RANK.get(e["verdict"], 99))
    for item in all_evidence_items:
        del item["_locked_at"]

    evidence_shown = (
        all_evidence_items[:evidence_limit] if evidence_limit is not None
        else all_evidence_items
    )

    return {
        "as_of": iso(to_dt(as_of, end_of_day=True)),
        "provenance": {
            "includes": [
                "research.memory Observatory anomalies (dataset=research_anomaly)",
                "research.contracts registry (hypothesis/contract records)",
                "research.brain.similarity exact-duplicate experiment specifications "
                "(same universe/entry_rule/exit_rule/splits/evaluation window, across "
                "ALL hypotheses and statuses — not near-duplicates, not semantic overlap)",
                "research.brain.research_areas hypothesis-to-research-area tags "
                "(explicit labels only — never inferred from hypothesis text)",
                "research.experiments.evaluator / research.experiments.comparison "
                "existing verdicts and cross-experiment evidence summaries, per "
                "hypothesis (bounded — no raw trade-level results)",
            ],
            "excludes": [
                "engine/* (guardrails, execute, broker, live positions/orders)",
                "memory/state.json and other live trading state",
                "broker credentials / .env",
                "raw source code of the trading system",
            ],
            "data_firewall": "as_of_view — every anomaly gated on knowledge_time <= as_of",
        },
        "anomalies": {
            "shown": anomaly_items,
            "shown_count": len(anomaly_items),
            "total_count": len(all_anomalies),
            "truncated": len(all_anomalies) > len(anomaly_items),
        },
        "contract_registry": {
            "contracts": contract_items,
            "count": len(contract_items),
            "comparison_count": comparison_count(registry_dir),
        },
        "previously_tested": {
            "contracts": tested_items,
            "count": len(tested_items),
        },
        "exact_duplicates": {
            "groups": duplicate_items,
            "shown_count": len(duplicate_items),
            "total_count": len(all_dup_groups),
            "truncated": len(all_dup_groups) > len(duplicate_items),
        },
        "research_areas": {
            "areas": area_items,
            "shown_count": len(area_items),
            "total_count": len(all_area_groups),
            "truncated": len(all_area_groups) > len(area_items),
        },
        "evidence": {
            "hypotheses": evidence_shown,
            "shown_count": len(evidence_shown),
            "total_count": len(all_evidence_items),
            "truncated": len(all_evidence_items) > len(evidence_shown),
        },
    }


def to_json(digest: dict) -> str:
    """A deterministic, bounded text rendering — sort_keys=True so the output
    string is identical run to run regardless of any incidental dict-
    construction ordering, not just dict-equal."""
    return json.dumps(digest, indent=2, sort_keys=True, default=str)
