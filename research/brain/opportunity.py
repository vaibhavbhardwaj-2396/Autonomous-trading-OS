"""
Autonomous Research Control Plane v1 — research/brain/opportunity.py.

Answers, mechanically and explainably: "what should be researched next, given
everything currently known?" This is orchestration and scoring ONLY — every
real capability it uses already exists and is reused verbatim:

    which drafts are pending            -> hypothesis_intake.pending_drafts()
    which locked contracts are runnable -> scheduler.eligible_contracts()
                                            (itself already priority.rank_experiments()-ordered)
    hypothesis <-> contract linkage     -> experiments.evaluator.{resolve_hypothesis_id,
                                            contract_ids_for_hypothesis}
    per-hypothesis evidence/verdict     -> brain.digest.build_digest()'s own
                                            `evidence` section (unbounded here)
    exact-duplicate detection           -> brain.similarity.is_duplicate()
    research-area grouping              -> brain.research_areas.groups()/area_of()
    confirmation/robustness signal      -> brain.priority.is_confirmation_experiment()
    variant direction (pos/neg/flat)    -> experiments.comparison.classify_variant()
    locking a draft                     -> hypothesis_intake.approve_and_lock() — UNCHANGED
                                            signature, UNCHANGED research-budget gate
                                            (hypothesis_intake.check_research_budget()),
                                            called here with an explicit, named,
                                            auditable SYSTEM approver — never a human
                                            name forged, never a blank approver, never a
                                            second locking code path

Nothing here reimplements duplicate detection, evidence aggregation, research-area
tagging, priority-within-locked-contracts, or the locking primitive itself. The only
genuinely new logic in this file is: (1) a unified, typed "research opportunity"
representation spanning drafts/locked-eligible/rejected-for-reassessment work, (2) a
transparent, versioned, multi-component priority score over that pool, (3) a
non-terminal rejection / reassessment-eligibility rule with no calendar dependency,
(4) an autonomous promotion action (draft -> locked) bounded by the EXISTING research
budget, (5) a durable, auditable event log (research.memory, one new `dataset`
value — no new table, no second source of truth) backing lifecycle transitions and
user overrides (freeze/reopen/retire/force-reassess), and (6) `build_action_queue()`
— a single ranked queue of concrete, executable Actions (RUN_EXPERIMENT / PROMOTE /
DISCOVER) spanning discovery, already-locked experiments and autonomous promotion,
all scored through the SAME priority_score/`_priority_components` this file already
uses for the opportunity pool itself. This is the mechanism behind the Unified
Opportunity-Driven Worker Selection slice: a new discovery attempt competes
directly, on the same numeric scale, against an existing robustness test or a
rejected hypothesis whose evidence just turned in its favour — there is no second,
competing ranking system, and no fixed phase order privileges one action kind over
another. `research.brain.worker` composes the pool + queue every iteration of its
own bounded loop; this module never decides WHEN to stop, only what is currently
the single highest-value thing to do next.

LIFECYCLE (the 9 stages this slice computes; a DERIVED, READ-ONLY projection over
existing Contract.status + evidence verdicts + the override event log — never a new
field stored on Contract, so it can never drift out of sync with the data it is
computed from):

    DISCOVERED -> TRIAGED -> TESTING -> EVALUATING -> PROMISING -> ROBUST
                                            |
                                            v
                                        REJECTED <-> REASSESSING
                                            |
                                            v
                                         RETIRED (explicit override only, always
                                                  user-reopenable)

"REJECTED" is never terminal: it only means "no rising value detected yet". An
opportunity becomes REASSESSING (not by a fixed retest period — see
`_evidence_signature`) the moment something in its actual evidence picture changes
since it was last looked at: its own new variant scored, a sibling hypothesis in the
same research area turned PROMISING, etc. "RETIRED" is the only stage this module
will not re-enter automatically; it exists solely via an explicit `retire()` call
(human or AI, both audited) and a human can always `reopen()` it.

TESTING and EVALUATING already happen fully autonomously TODAY, with no change here:
research.brain.scheduler / research.experiments.runner already run every locked
contract, and research.experiments.evaluator already scores it, on every worker
heartbeat, with no human step in between. The ONE human-gated step in the existing
pipeline is DRAFT -> LOCKED (hypothesis_intake.approve_and_lock's mandatory
`approved_by`). This module adds ONE new action, `attempt_autonomous_promotion()`,
that calls that exact same function with an explicit system identity — the lock
primitive, its audit note, and its research-budget rate limit are all completely
unmodified. A promotion the budget refuses is a clean, expected, non-error outcome,
not a bypass of the budget.

WHAT THIS MODULE NEVER DOES, structurally true by absence (the same "true by
absence, not by a check that could be forgotten" pattern every research/brain/
module in this codebase already uses):
  - import engine, engine.execute, engine.guardrails, engine.broker*, engine.journal,
    or any broker module. No memory/state.json read. No live trading state of any
    kind is reachable from this file.
  - import research.experiments.runner or call run_experiment()/simulate() — running
    an experiment stays entirely research.brain.scheduler's job, unmodified. This
    module imports `research.brain.scheduler` for exactly ONE read-only call,
    `scheduler.eligible_contracts()`, to RANK candidate RUN_EXPERIMENT actions in
    `build_action_queue()` — it never calls `scheduler.run_scheduler()` and never
    executes anything itself; deciding an action is highest-priority and actually
    running it are two different steps, owned by two different modules.
  - call Contract.lock() directly, or write a Contract file directly. The ONLY path
    to locking anything is the existing, unmodified hypothesis_intake.approve_and_lock().
  - create a second source of truth. The only durable writes are (a) research memory
    rows under the new `research_opportunity_event` dataset — the SAME append-only
    `observations` table every other research/memory.py dataset already uses, via the
    SAME typed-wrapper convention — and (b) whatever approve_and_lock() itself already
    writes when a promotion succeeds. No new SQLite table, no new file, no new state.json.
  - import paper/ or anything execution-adjacent.
  - promote research directly into live trading. There is no path from this file to
    a broker, to memory/state.json, or to engine.execute at any distance.

Isolation is enforced mechanically by tests/test_research_opportunity.py (import
scan + behavioural proof), the same way tests/test_research_worker.py §J and
tests/test_kernel_isolation.py already do for the rest of research/brain.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple, Optional

from .. import memory as rm
from ..contracts import REGISTRY_DIR, Contract
from ..experiments import evaluator
from ..experiments.comparison import classify_variant
from ..store import Store, TimeLike
from . import hypothesis_intake as hi
from . import priority as prio
from . import research_areas
from . import scheduler as sched
from . import similarity
from .digest import build_digest

# ---------------------------------------------------------------------------
# Vocabulary — extensible by design (§2 of the Master Vision: "do not overbuild
# every type immediately; create an extensible representation"). The auto-
# classifier below (`_opportunity_type`) only ever actively assigns a handful
# of these; the rest exist so a future, narrower caller (a human, the Research
# AI, or a later slice) can construct an Opportunity of that type explicitly
# without a schema change. Adding a member here is additive and safe.
# ---------------------------------------------------------------------------

OPPORTUNITY_TYPES = frozenset({
    "NEW_HYPOTHESIS", "RETEST_HYPOTHESIS", "REASSESS_REJECTED", "ROBUSTNESS_TEST",
    "OUT_OF_SAMPLE_TEST", "REGIME_TEST", "COST_SENSITIVITY", "DATA_TEST",
    "FEATURE_RESEARCH", "STRATEGY_VARIANT", "ENTRY_RESEARCH", "EXIT_RESEARCH",
    "SIZING_RESEARCH", "PORTFOLIO_INTERACTION", "CORRELATION_RESEARCH",
    "HEDGE_RESEARCH", "OPTIONS_RESEARCH", "FUTURES_RESEARCH", "STAT_ARB_RESEARCH",
    "OTHER_RESEARCH",
})

LIFECYCLE_STAGES = (
    "DISCOVERED", "TRIAGED", "TESTING", "EVALUATING", "PROMISING", "ROBUST",
    "REJECTED", "REASSESSING", "RETIRED",
)

# Versioned, deliberately conservative placeholders — the SAME "small, explicit,
# clearly-named, overridable, NOT claimed to be production-tuned" posture every
# other bounded constant in this codebase takes (WorkerLimits, MAX_LOCKS_PER_PERIOD,
# MAX_EXPERIMENTS_PER_RUN, MAX_PROPOSAL_ATTEMPTS_PER_RUN, ...). Recalibrating these
# from real outcome data is explicit future work, not attempted here.
PROMOTION_POLICY_VERSION = "v1"
PRIORITY_MODEL_VERSION = "v1"

# The explicit, named, auditable approver identity used for AUTONOMOUS locking —
# never a forged human name, never blank. hypothesis_intake.approve_and_lock()'s
# own mandatory-approver check ("a lock with no named approver is refused outright")
# is satisfied by this being a real, non-empty, greppable, versioned string — the
# audit note approve_and_lock() itself writes records exactly this value, so "who
# approved this" is always answerable and never ambiguous with a human approval.
SYSTEM_APPROVER = f"autonomous_control_plane.{PROMOTION_POLICY_VERSION}"

DATASET = rm.DATASET_OPPORTUNITY_EVENT

# Evidence-strength component (§4) — a plain, documented lookup, not a model.
# None (no verdict yet — a fresh, unscored idea) is deliberately mid-scale: an
# untested idea is unknown, not bad, and should stay explorable rather than
# being penalized like a hypothesis that was actually tested and disagreed with.
_EVIDENCE_STRENGTH = {
    "PROMISING": 1.0, "CONTRADICTED": 0.6, "WEAK": 0.3,
    "INCONCLUSIVE": 0.2, "REDUNDANT": 0.0, None: 0.5,
}

# Compute-cost component (§4/§8) — a relative, static per-type estimate, not a
# real profiler (explicitly out of scope for v1 — "add resource-awareness
# INTERFACES", not a resource model). A discovery/new-hypothesis proposal costs
# an AI subprocess call (research.brain.investigator._default_runner); running
# an already-locked experiment or a robustness/confirmation test costs one
# bounded backtest (the existing experiment runner, unchanged); re-litigating
# a rejected idea
# with no new evidence is intentionally the most expensive per unit of value.
_TYPE_COMPUTE_COST = {
    "NEW_HYPOTHESIS": 3.0, "REASSESS_REJECTED": 2.0, "ROBUSTNESS_TEST": 1.0,
    "RETEST_HYPOTHESIS": 1.5,
}
_DEFAULT_COMPUTE_COST = 1.5

# Small, explicit, named weights (§4: "prefer a transparent score with
# explainable components... do NOT make a single giant opaque formula"). Each
# term is independently inspectable via Opportunity.priority_components; this
# is a sum of named, documented pieces, not a black box. NOT claimed to be
# calibrated or optimal — see PRIORITY_MODEL_VERSION above.
PRIORITY_WEIGHTS = {
    "evidence_strength": 3.0,
    "novelty": 2.0,
    "confirmation_bonus": 2.5,
    "research_area_balance": 1.0,
    "reassessment_rising_value": 2.0,
    "compute_cost_penalty": -1.5,
    "age_bonus": 0.5,
}

DEFAULT_MAX_REASSESSMENT_EVENTS_PER_CYCLE = 5
"""Bounded, same convention as every other per-run cap in this codebase — how
many newly-eligible-for-reassessment transitions build_opportunity_pool() will
persist as audit events in one call. Does not limit how many opportunities are
SCORED or RETURNED, only how many NEW event rows one call may write, so a large
backlog of simultaneously-newly-eligible items cannot flood research memory."""


# ---------------------------------------------------------------------------
# Opportunity — the unit of work (§2, §7 of the Master Vision).
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Opportunity:
    id: str                              # "OPP-<hypothesis_id>" — deterministic, 1:1
    type: str
    hypothesis_id: str
    contract_id: Optional[str]           # the most relevant contract, if any
    title: Optional[str]
    lifecycle_stage: str
    priority_score: float
    priority_components: dict
    confidence: Optional[float]
    evidence_verdict: Optional[str]
    research_area: Optional[str]
    is_duplicate: bool
    reassessment_eligible: bool
    override_state: dict                 # {"frozen": bool, "retired": bool}
    compute_cost_estimate: float
    contract_statuses: tuple             # every status among this hypothesis's contracts
    evidence_signature: str
    # Explicit placeholder — NOT computed in v1 ("do not introduce a fake
    # portfolio engine just to populate this field", Master Vision §10). Ready
    # for a future Autonomous Portfolio Engine slice to populate.
    portfolio_relevance: Optional[dict] = None
    rationale: str = ""

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["contract_statuses"] = list(self.contract_statuses)
        return d


class PromotionOutcome(NamedTuple):
    opportunity_id: str
    hypothesis_id: str
    contract_id: Optional[str]
    outcome: str  # "promoted" | "skipped_frozen" | "skipped_not_eligible" |
                  # "skipped_duplicate" | "skipped_budget" | "rejected" | "error"
    detail: str


# ---------------------------------------------------------------------------
# Audit trail — research.memory.record_opportunity_event(), read back and
# folded here. Append-only; "current" state is always a fold over history,
# the SAME "last write wins over an append-only log" pattern
# research_areas.py's own _latest_tags() already uses (see that module).
# ---------------------------------------------------------------------------

def _events_for(store: Store, opportunity_id: str) -> list[dict]:
    rows = rm.query_research_log(store, DATASET)
    return [r["payload"] for r in rows if r["payload"].get("opportunity_id") == opportunity_id]


def _fold_events(events: list[dict]) -> dict:
    """One pass over an opportunity's ordered event history -> its current
    override state PLUS the last-recorded evidence signature/stage/priority/
    confidence a lifecycle transition committed. Pure; no store I/O. Order
    matters (ascending, as query_research_log already returns) — a later
    event always wins over an earlier one for every field it touches."""
    state = {
        "frozen": False, "retired": False, "force_reassess_pending": False,
        "last_signature": None, "last_stage": None,
        "last_priority": None, "last_confidence": None, "events_count": len(events),
    }
    for e in events:
        etype = e.get("event_type")
        if etype == "FREEZE":
            state["frozen"] = True
        elif etype == "UNFREEZE":
            state["frozen"] = False
        elif etype == "RETIRE":
            state["retired"] = True
        elif etype == "REOPEN":
            state["retired"] = False
        elif etype == "FORCE_REASSESS":
            state["force_reassess_pending"] = True
        elif etype == "LIFECYCLE_TRANSITION":
            state["force_reassess_pending"] = False
            state["last_signature"] = e.get("evidence_signature")
            state["last_stage"] = e.get("new_state")
            state["last_priority"] = e.get("priority_after")
            state["last_confidence"] = e.get("confidence_after")
    return state


def override_history(store: Store, opportunity_id: str) -> list[dict]:
    """Every recorded event for one opportunity, oldest first — the raw
    audit trail a hypothesis dossier (§13) would render."""
    return _events_for(store, opportunity_id)


def _record_event(
    store: Store, *, opportunity_id: str, hypothesis_id: str, event_type: str,
    actor: str, reason: Optional[str] = None, previous_state: Optional[str] = None,
    new_state: Optional[str] = None, evidence_signature: Optional[str] = None,
    priority_before: Optional[float] = None, priority_after: Optional[float] = None,
    confidence_before: Optional[float] = None, confidence_after: Optional[float] = None,
    source: str,
) -> Optional[int]:
    return rm.record_opportunity_event(
        store, opportunity_id=opportunity_id, hypothesis_id=hypothesis_id,
        event_type=event_type, actor=actor, reason=reason,
        previous_state=previous_state, new_state=new_state,
        evidence_signature=evidence_signature,
        priority_before=priority_before, priority_after=priority_after,
        confidence_before=confidence_before, confidence_after=confidence_after,
        source=source,
    )


def freeze(store: Store, opportunity_id: str, *, by: str, reason: str = "") -> Optional[int]:
    """User override (§11/§14): suspend autonomous promotion/reassessment of
    this opportunity until reopen()'d. Never called automatically — only ever
    by an explicit human (or a future explicit AI escalation) action."""
    hid = opportunity_id[len("OPP-"):] if opportunity_id.startswith("OPP-") else opportunity_id
    return _record_event(store, opportunity_id=opportunity_id, hypothesis_id=hid,
                         event_type="FREEZE", actor=by, reason=reason, source="user_override")


def reopen(store: Store, opportunity_id: str, *, by: str, reason: str = "") -> Optional[int]:
    """Clears BOTH frozen and retired — reopening is the one override that
    always wins, by design (§11: "user authority should be stronger than
    ordinary AI preference"; §14 example: AI:REJECTED -> User:REOPENED)."""
    hid = opportunity_id[len("OPP-"):] if opportunity_id.startswith("OPP-") else opportunity_id
    _record_event(store, opportunity_id=opportunity_id, hypothesis_id=hid,
                  event_type="UNFREEZE", actor=by, reason=reason, source="user_override")
    return _record_event(store, opportunity_id=opportunity_id, hypothesis_id=hid,
                         event_type="REOPEN", actor=by, reason=reason, source="user_override")


def retire(store: Store, opportunity_id: str, *, by: str, reason: str) -> Optional[int]:
    """The one non-automatic-re-entry state (§8). `reason` is required —
    retirement without a stated reason is refused, the same "no anonymous,
    unexplained consequential action" posture approve_and_lock() already
    enforces for locking."""
    if not reason or not reason.strip():
        raise ValueError("retire() requires a non-empty reason")
    hid = opportunity_id[len("OPP-"):] if opportunity_id.startswith("OPP-") else opportunity_id
    return _record_event(store, opportunity_id=opportunity_id, hypothesis_id=hid,
                         event_type="RETIRE", actor=by, reason=reason, source="user_override")


def force_reassess(store: Store, opportunity_id: str, *, by: str, reason: str = "") -> Optional[int]:
    """User override: make this opportunity REASSESSING on the very next
    build_opportunity_pool() call, regardless of whether its evidence
    signature has actually changed. Self-consuming — the next lifecycle
    transition this produces clears it (see _fold_events)."""
    hid = opportunity_id[len("OPP-"):] if opportunity_id.startswith("OPP-") else opportunity_id
    return _record_event(store, opportunity_id=opportunity_id, hypothesis_id=hid,
                         event_type="FORCE_REASSESS", actor=by, reason=reason,
                         source="user_override")


# ---------------------------------------------------------------------------
# Pure scoring functions — no store I/O, fully unit-testable in isolation.
# ---------------------------------------------------------------------------

def _evidence_signature(verdict: Optional[str], n_variants_scored: int,
                        sibling_promising_count: int) -> str:
    """A plain, inspectable string (not an opaque hash — §4's transparency
    requirement) summarizing "everything about this hypothesis's evidence
    picture that would make it worth looking at again". Two calls with an
    unchanged signature mean nothing has actually changed since the last
    time this opportunity was assessed — the entire mechanism behind
    non-calendar-based reassessment (Master Vision §3/§5/§9): compare
    signatures, not dates."""
    return f"v={verdict};n={n_variants_scored};siblings_promising={sibling_promising_count}"


def _lifecycle_stage(
    *, statuses: frozenset, verdict: Optional[str], is_robust: bool,
    reassessment_eligible: bool, has_prior_event: bool, is_retired: bool,
) -> str:
    """Pure: the computed lifecycle stage given already-resolved inputs. No
    store access, no wall clock — same discipline research.brain.priority's
    own priority_key() already keeps."""
    if is_retired:
        return "RETIRED"
    if verdict == "PROMISING":
        return "ROBUST" if is_robust else "PROMISING"
    if verdict in ("WEAK", "CONTRADICTED", "REDUNDANT"):
        return "REASSESSING" if reassessment_eligible else "REJECTED"
    if "locked" in statuses or "running" in statuses:
        return "TESTING"
    if "reported" in statuses:
        # reported but no resolvable verdict (e.g. zero trades) — evaluated,
        # inconclusive by construction rather than mis-labelled REJECTED
        return "EVALUATING"
    if "abandoned" in statuses:
        return "REASSESSING" if reassessment_eligible else "REJECTED"
    return "TRIAGED" if has_prior_event else "DISCOVERED"


def _opportunity_type(
    *, verdict: Optional[str], reassessment_eligible: bool,
    is_confirmation_sibling: bool,
) -> str:
    """A small, honest, deterministic classifier — NOT an attempt to infer
    every type in OPPORTUNITY_TYPES from heuristics (Master Vision §2: "do
    not overbuild every type immediately"). Finer types (SIZING_RESEARCH,
    HEDGE_RESEARCH, OPTIONS_RESEARCH, ...) exist in the vocabulary for an
    explicit caller to use directly; this function only ever assigns the
    handful that are genuinely derivable from data already on hand."""
    if is_confirmation_sibling:
        return "ROBUSTNESS_TEST"
    if verdict in ("WEAK", "CONTRADICTED", "REDUNDANT") and reassessment_eligible:
        return "REASSESS_REJECTED"
    if verdict is not None:
        return "RETEST_HYPOTHESIS"
    return "NEW_HYPOTHESIS"


def _priority_components(
    *, evidence_verdict: Optional[str], is_duplicate: bool, is_confirmation_sibling: bool,
    area_hypothesis_count: Optional[int], max_area_hypothesis_count: int,
    reassessment_eligible: bool, opportunity_type: str, locked_at: Optional[str],
) -> dict:
    """Every named component PRIORITY_WEIGHTS sums over — see that constant's
    docstring for what each one means and why. Returns raw [0, 1]-ish values
    (priority_score() applies the weights); kept separate from the weighted
    sum so `explain` can show BOTH the raw signal and its weighted
    contribution, never just the opaque total."""
    evidence_strength = _EVIDENCE_STRENGTH.get(evidence_verdict, 0.5)
    novelty = 0.0 if is_duplicate else 1.0
    confirmation_bonus = 1.0 if is_confirmation_sibling else 0.0
    if area_hypothesis_count is None or max_area_hypothesis_count <= 0:
        research_area_balance = 0.5  # untagged/neutral — never penalized, never favoured
    else:
        research_area_balance = 1.0 - (area_hypothesis_count / max_area_hypothesis_count)
    reassessment_rising_value = 1.0 if reassessment_eligible else 0.0
    compute_cost = _TYPE_COMPUTE_COST.get(opportunity_type, _DEFAULT_COMPUTE_COST)
    compute_cost_penalty = compute_cost / max(_TYPE_COMPUTE_COST.values())
    age_bonus = 1.0 if locked_at is None else 0.0  # a still-unlocked draft never starves
    return {
        "evidence_strength": round(evidence_strength, 4),
        "novelty": novelty,
        "confirmation_bonus": confirmation_bonus,
        "research_area_balance": round(research_area_balance, 4),
        "reassessment_rising_value": reassessment_rising_value,
        "compute_cost_penalty": round(compute_cost_penalty, 4),
        "age_bonus": age_bonus,
    }


def priority_score(components: dict) -> float:
    """The one place PRIORITY_WEIGHTS is actually applied — rank_experiments()
    equivalent for the whole opportunity pool. Deterministic, no store access."""
    return round(sum(PRIORITY_WEIGHTS.get(k, 0.0) * v for k, v in components.items()), 4)


# ---------------------------------------------------------------------------
# Store-reading assembly — composes every module named in the file docstring.
# ---------------------------------------------------------------------------

def _all_hypotheses(store: Store) -> dict:
    """hypothesis_id -> its first-recorded claim text. Reads
    research.memory's own DATASET_HYPOTHESIS log directly — the SAME rows
    research.experiments.evaluator.resolve_hypothesis_id() and
    research.brain.priority._split_metadata() already read, just grouped the
    other direction (by hypothesis_id, collecting every one that exists)."""
    rows = rm.query_research_log(store, rm.DATASET_HYPOTHESIS)
    out: dict = {}
    for r in rows:
        payload = r.get("payload") or {}
        hid = payload.get("hypothesis_id")
        if not hid:
            continue
        out.setdefault(hid, payload.get("claim"))
    return out


def _evidence_index(store: Store, as_of: TimeLike, *, registry_dir: Path) -> dict:
    """hypothesis_id -> its digest evidence dict, UNBOUNDED (digest.py bounds
    this section by default for the AI prompt's sake — evidence_limit=None
    here asks the exact same, unmodified build_digest() for the full set).
    Anomalies/duplicate-groups/area sections are also fetched unbounded
    (duplicate/area) or suppressed (anomaly_limit=0, not needed here) so this
    one call is the complete input the opportunity pool needs."""
    digest = build_digest(
        store, as_of, anomaly_limit=0, duplicate_limit=None,
        area_limit=None, evidence_limit=None, registry_dir=registry_dir,
    )
    return {e["hypothesis_id"]: e for e in digest["evidence"]["hypotheses"]}


def _is_robust(store: Store, hypothesis_id: str, verdict: Optional[str], *, registry_dir: Path) -> bool:
    """PROMISING AND at least one validation/holdout sibling (reusing
    priority.is_confirmation_experiment — the SAME confirmation-experiment
    concept priority.py already established) has itself been scored with a
    positive direction (reusing comparison.classify_variant)."""
    if verdict != "PROMISING":
        return False
    for cid in evaluator.contract_ids_for_hypothesis(store, hypothesis_id):
        if not prio.is_confirmation_experiment(store, cid, registry_dir=registry_dir):
            continue
        v = evaluator.verdict_for_contract(store, cid)
        if v is None:
            continue
        if classify_variant(cid, v)["direction"] == "positive":
            return True
    return False


def build_opportunity_pool(
    store: Store, as_of: TimeLike, *, registry_dir: Path = REGISTRY_DIR,
    log_events: bool = True, max_reassessment_events: int = DEFAULT_MAX_REASSESSMENT_EVENTS_PER_CYCLE,
) -> list[Opportunity]:
    """Every research opportunity currently in the universe — pending drafts,
    locked-and-eligible experiments, and every hypothesis with recorded
    evidence (PROMISING/ROBUST, and non-terminally REJECTED/REASSESSING) —
    scored and sorted by priority_score, descending, contract_id tie-break.

    Read-mostly: the pool itself is computed purely from existing state.
    `log_events=True` (the default) additionally appends a
    `LIFECYCLE_TRANSITION` audit row (§12) for any opportunity whose computed
    stage differs from the last one recorded for it — bounded to at most
    `max_reassessment_events` NEW-reassessment-eligible transitions per call
    (existing/unchanged-stage opportunities are never re-logged, so a stable
    pool costs zero writes). Pass `log_events=False` for a read-only caller
    (e.g. a future dashboard) that must never write.
    """
    hypotheses = _all_hypotheses(store)
    evidence_by_hid = _evidence_index(store, as_of, registry_dir=registry_dir)
    area_groups = research_areas.groups(store, as_of=as_of)
    area_counts = {g.name: len(g.hypothesis_ids) for g in area_groups}
    max_area_count = max(area_counts.values()) if area_counts else 0

    reassessment_events_written = 0
    opportunities: list[Opportunity] = []

    for hid, claim in hypotheses.items():
        contract_ids = evaluator.contract_ids_for_hypothesis(store, hid)
        statuses = []
        locked_at = None
        representative_contract_id = None
        for cid in contract_ids:
            try:
                c = Contract.load(cid, registry_dir)
            except FileNotFoundError:
                continue
            statuses.append(c.status)
            if c.status != "draft":
                locked_at = c.locked_at if locked_at is None else min(locked_at, c.locked_at or locked_at)
            if representative_contract_id is None or c.status != "draft":
                representative_contract_id = cid
        statuses_frozen = frozenset(statuses)

        ev = evidence_by_hid.get(hid)
        verdict = ev["verdict"] if ev else None
        n_variants_scored = ev["n_variants_scored"] if ev else 0
        area = ev["research_area"] if ev else research_areas.area_of(store, hid)
        title = ev["title"] if ev else None
        if ev:
            is_dup = ev["has_exact_duplicate"]
        elif representative_contract_id:
            is_dup = similarity.is_duplicate(store, representative_contract_id, registry_dir=registry_dir)
        else:
            is_dup = False

        sibling_promising = sum(
            1 for other_hid, other_ev in evidence_by_hid.items()
            if other_hid != hid and other_ev.get("research_area") == area
            and area is not None and other_ev.get("verdict") == "PROMISING"
        )
        signature = _evidence_signature(verdict, n_variants_scored, sibling_promising)

        opp_id = f"OPP-{hid}"
        # Folded regardless of log_events — reassessment_eligible/override
        # state are READ from history even when this call is not permitted
        # to WRITE new history (log_events=False, e.g. a read-only caller).
        folded = _fold_events(_events_for(store, opp_id))
        has_prior_event = folded["events_count"] > 0
        reassessment_eligible = folded["force_reassess_pending"] or (
            folded["last_signature"] is not None and folded["last_signature"] != signature
        )
        is_robust = _is_robust(store, hid, verdict, registry_dir=registry_dir)

        stage = _lifecycle_stage(
            statuses=statuses_frozen, verdict=verdict, is_robust=is_robust,
            reassessment_eligible=reassessment_eligible, has_prior_event=has_prior_event,
            is_retired=folded["retired"],
        )
        is_confirmation_sibling = any(
            prio.is_confirmation_experiment(store, cid, registry_dir=registry_dir)
            for cid in contract_ids
        )
        opp_type = _opportunity_type(
            verdict=verdict, reassessment_eligible=reassessment_eligible,
            is_confirmation_sibling=is_confirmation_sibling,
        )
        components = _priority_components(
            evidence_verdict=verdict, is_duplicate=is_dup,
            is_confirmation_sibling=is_confirmation_sibling,
            area_hypothesis_count=area_counts.get(area) if area else None,
            max_area_hypothesis_count=max_area_count,
            reassessment_eligible=reassessment_eligible, opportunity_type=opp_type,
            locked_at=locked_at,
        )
        # a frozen or retired opportunity is suppressed to zero priority — the
        # user override always wins over the computed score (Master Vision §11)
        score = 0.0 if (folded["frozen"] or folded["retired"]) else priority_score(components)
        confidence = None if verdict is None else round(
            _EVIDENCE_STRENGTH.get(verdict, 0.5) * (1.15 if is_robust else 1.0), 4)
        if confidence is not None:
            confidence = min(confidence, 1.0)

        opp = Opportunity(
            id=opp_id, type=opp_type, hypothesis_id=hid, contract_id=representative_contract_id,
            title=title, lifecycle_stage=stage, priority_score=score,
            priority_components=components, confidence=confidence, evidence_verdict=verdict,
            research_area=area, is_duplicate=is_dup, reassessment_eligible=reassessment_eligible,
            override_state={"frozen": folded["frozen"], "retired": folded["retired"]},
            compute_cost_estimate=_TYPE_COMPUTE_COST.get(opp_type, _DEFAULT_COMPUTE_COST),
            contract_statuses=tuple(sorted(statuses)), evidence_signature=signature,
            rationale=f"{stage} — verdict={verdict}, area={area}, reassessment_eligible="
                      f"{reassessment_eligible}, duplicate={is_dup}",
        )
        opportunities.append(opp)

        if log_events and folded["last_stage"] != stage:
            if stage == "REASSESSING" and reassessment_events_written >= max_reassessment_events:
                continue  # bounded — see DEFAULT_MAX_REASSESSMENT_EVENTS_PER_CYCLE
            _record_event(
                store, opportunity_id=opp_id, hypothesis_id=hid, event_type="LIFECYCLE_TRANSITION",
                actor="system", reason="opportunity pool refresh",
                previous_state=folded["last_stage"], new_state=stage,
                evidence_signature=signature, priority_before=folded["last_priority"],
                priority_after=score, confidence_before=folded["last_confidence"],
                confidence_after=confidence, source="research.brain.opportunity",
            )
            if stage == "REASSESSING":
                reassessment_events_written += 1

    opportunities.sort(key=lambda o: (-o.priority_score, o.contract_id or "", o.id))
    return opportunities


# ---------------------------------------------------------------------------
# Autonomous promotion — the ONE new consequential action (§6). Everything
# downstream of a successful lock (TESTING/EVALUATING/PROMISING/ROBUST) was
# ALREADY fully autonomous before this module existed.
# ---------------------------------------------------------------------------

def attempt_autonomous_promotion(
    store: Store, opportunity: Opportunity, *, registry_dir: Path = REGISTRY_DIR,
    now=None, approver: str = SYSTEM_APPROVER,
) -> PromotionOutcome:
    """Try to move ONE opportunity from DISCOVERED/TRIAGED to TESTING by
    calling the existing, unmodified hypothesis_intake.approve_and_lock()
    with an explicit system approver identity. Every refusal path here is a
    clean, expected, non-error outcome — never a bypass of an existing gate:

      - frozen/retired (user override)      -> skipped_frozen
      - not a draft / no contract_id         -> skipped_not_eligible
      - an exact duplicate elsewhere         -> skipped_duplicate
      - over the EXISTING research budget    -> skipped_budget (checked via
        (hypothesis_intake.check_research_budget()) — a read-only check,        the SAME check_research_budget() approve_and_lock() itself
        performed BEFORE ever calling approve_and_lock, so a budget refusal    would apply — never bypassed, never overridden here
        never even attempts the call)
      - hypothesis_intake.IntakeRejected     -> rejected (a real validation
        failure surfaced by the existing firewall — e.g. the contract fails
        Contract.check())

    A successful promotion is recorded as a LIFECYCLE_TRANSITION audit event
    (previous_state=the opportunity's own stage, new_state="TESTING") in
    addition to whatever audit note approve_and_lock() itself already writes.
    """
    if opportunity.override_state.get("frozen") or opportunity.override_state.get("retired"):
        return PromotionOutcome(opportunity.id, opportunity.hypothesis_id,
                                opportunity.contract_id, "skipped_frozen",
                                "opportunity is frozen or retired by user override")
    if opportunity.lifecycle_stage not in ("DISCOVERED", "TRIAGED") or not opportunity.contract_id:
        return PromotionOutcome(opportunity.id, opportunity.hypothesis_id,
                                opportunity.contract_id, "skipped_not_eligible",
                                f"lifecycle_stage={opportunity.lifecycle_stage} is not promotable")
    if opportunity.is_duplicate:
        return PromotionOutcome(opportunity.id, opportunity.hypothesis_id,
                                opportunity.contract_id, "skipped_duplicate",
                                "an exact-duplicate rule specification already exists")

    within_budget, count = hi.check_research_budget(registry_dir, now=now)
    if not within_budget:
        return PromotionOutcome(opportunity.id, opportunity.hypothesis_id,
                                opportunity.contract_id, "skipped_budget",
                                f"research budget exhausted ({count} locks already this period)")

    try:
        hi.approve_and_lock(
            store, opportunity.contract_id, approved_by=approver,
            registry_dir=registry_dir, now=now,
        )
    except hi.IntakeRejected as e:
        return PromotionOutcome(opportunity.id, opportunity.hypothesis_id,
                                opportunity.contract_id, "rejected", "; ".join(e.reasons))
    except Exception as e:  # noqa: BLE001 — never propagate out of a bounded promotion attempt
        return PromotionOutcome(opportunity.id, opportunity.hypothesis_id,
                                opportunity.contract_id, "error", f"{type(e).__name__}: {e}")

    _record_event(
        store, opportunity_id=opportunity.id, hypothesis_id=opportunity.hypothesis_id,
        event_type="LIFECYCLE_TRANSITION", actor="system",
        reason="autonomous promotion: draft -> locked", previous_state=opportunity.lifecycle_stage,
        new_state="TESTING", evidence_signature=opportunity.evidence_signature,
        priority_before=opportunity.priority_score, priority_after=opportunity.priority_score,
        source="research.brain.opportunity",
    )
    return PromotionOutcome(opportunity.id, opportunity.hypothesis_id, opportunity.contract_id,
                            "promoted", f"locked by {approver}")


# ---------------------------------------------------------------------------
# Unified action queue — Unified Opportunity-Driven Worker Selection slice.
#
# THE GAP THIS CLOSES: build_opportunity_pool() already scores every research
# opportunity on one scale, but the worker (v1, Control Plane slice) still
# chose WHAT KIND of work to do via a fixed phase order (experiments, then
# discovery, then promotion) — a new discovery attempt could never actually
# be outranked by, say, a rejected hypothesis whose evidence just turned in
# its favour; it simply ran on every heartbeat regardless of anything else's
# priority. `build_action_queue()` removes that fixed ordering: it turns the
# CURRENT opportunity pool into a single list of concrete, executable
# Actions — one per genuinely runnable experiment, one per promotable draft,
# and (at most) one synthetic "run the Research AI" action — all scored
# through the SAME priority_score() / _priority_components() this file
# already uses for the pool itself, then sorted together. Whichever action
# has the highest priority_score, of ANY kind, is the one thing most worth
# doing next. There is no second, competing formula: a RUN_EXPERIMENT action
# is scored by its hypothesis's own Opportunity (the pool already computed
# it); a PROMOTE action is scored the same way; DISCOVER is scored by
# handing the exact same pure function a neutral, synthetic input.
#
# Some OPPORTUNITY_TYPES (REASSESS_REJECTED chief among them) do not yet
# have an executable substrate in v1 — a hypothesis that already ran and was
# rejected has no pending draft and no still-locked contract to point an
# action at, only a history. Such an opportunity stays fully visible and
# correctly scored in the pool (a future dossier, or a human, can act on it
# right now via force_reassess()/reopen()) but simply produces no Action
# here — per the slice's own instruction, "unsupported types may remain
# represented but ineligible; do not require every type to be executable
# yet." The moment a reassessment-eligible hypothesis DOES have a runnable
# substrate (e.g. a retest contract already locked for it), it is picked up
# automatically here, scored via its own REASSESS_REJECTED/ROBUSTNESS_TEST
# Opportunity — which is exactly how a rejected hypothesis "returns to the
# pool" and can outrank a fresh discovery attempt: not a calendar, not a
# special-cased branch, just the same priority score everything else gets.
# ---------------------------------------------------------------------------

ACTION_KINDS = ("RUN_EXPERIMENT", "PROMOTE", "DISCOVER")


class Action(NamedTuple):
    """One concrete, executable unit of research work, ready to be run by
    research.brain.worker's own bounded loop. Self-contained and
    explainable — every field a caller needs to answer "why this, and not
    something else" is here, without a second lookup, even though
    RUN_EXPERIMENT/PROMOTE actions are also traceable back to a full
    Opportunity via `opportunity_id`."""

    kind: str                          # one of ACTION_KINDS
    opportunity_id: Optional[str]      # None only for the synthetic DISCOVER action
    hypothesis_id: Optional[str]
    contract_id: Optional[str]
    priority_score: float
    priority_components: dict
    compute_cost_estimate: float
    confidence: Optional[float]
    novelty: float
    evidence_value: float              # named, explainable "expected information gain" proxy
    relevance: str                     # why this action is eligible right now
    rationale: str


def _evidence_value_estimate(*, confidence: Optional[float], reassessment_eligible: bool) -> float:
    """A small, named, honest proxy for "how much would running this action
    likely teach us" (Master Vision: "expected information gain") — NOT a
    formal information-theoretic estimate; that is explicitly out of scope
    for v1, the same posture every other priority component in this module
    already takes. An untested idea (confidence is None) or a
    reassessment-eligible one (its evidence picture just changed) scores
    high; a well-confirmed, unchanged result scores low — there is little
    left to learn by re-running it."""
    unknown = 0.5 if confidence is None else round(max(0.0, 1.0 - confidence), 4)
    return round(min(1.0, unknown + (0.25 if reassessment_eligible else 0.0)), 4)


def _discovery_priority_components() -> dict:
    """The SAME priority formula every other opportunity type uses
    (_priority_components) — never a second, competing formula — applied to
    the one synthetic "run the Research AI and see what it proposes" action
    every heartbeat may offer. A discovery attempt has no verdict yet
    (evidence_strength stays at the neutral 0.5, exactly like any
    not-yet-scored idea), is never a duplicate of itself, is never a
    confirmation sibling of itself, is treated as area-neutral (there is no
    specific research area to balance against before it exists), is never
    reassessment-eligible (there is nothing to reassess yet), and is always
    "fresh" (age_bonus=1.0, locked_at=None) — so a long-idle worker never
    silently stops exploring."""
    return _priority_components(
        evidence_verdict=None, is_duplicate=False, is_confirmation_sibling=False,
        area_hypothesis_count=None, max_area_hypothesis_count=0,
        reassessment_eligible=False, opportunity_type="NEW_HYPOTHESIS", locked_at=None,
    )


def build_action_queue(
    store: Store, pool: list, *, registry_dir: Path = REGISTRY_DIR,
    discovery_available: bool = True,
) -> list:
    """Turn the current opportunity pool into a single, ranked queue of
    Actions — RUN_EXPERIMENT (one per genuinely runnable locked contract),
    PROMOTE (one per promotable draft), and at most one synthetic DISCOVER.
    Sorted by priority_score, descending — the "which one thing is most
    worth doing right now" answer the unified worker loop consumes directly.

    Read-only throughout: `scheduler.eligible_contracts()` only reads the
    registry, and everything else here reads the already-built `pool` and
    `evaluator.resolve_hypothesis_id()` (a Store read). Nothing here
    mutates a Contract, locks anything, or runs an experiment — deciding
    an action is highest-priority and actually executing it are two
    different steps, owned by two different modules.

    A frozen or retired opportunity is excluded outright, not merely
    zero-scored — a user override must be unselectable, never just
    outranked (Master Vision §11/§14; the same rule
    attempt_autonomous_promotion() itself re-checks before ever promoting).
    """
    by_hid = {o.hypothesis_id: o for o in pool}
    actions: list = []

    try:
        eligible_contracts = sched.eligible_contracts(store, registry_dir=registry_dir)
    except Exception:  # noqa: BLE001 — a read-only ranking helper never raises
        eligible_contracts = []
    for c in eligible_contracts:
        try:
            hid = evaluator.resolve_hypothesis_id(store, c.id)
        except Exception:  # noqa: BLE001
            hid = None
        o = by_hid.get(hid) if hid else None
        if o is not None and (o.override_state.get("frozen") or o.override_state.get("retired")):
            continue
        if o is not None:
            actions.append(Action(
                kind="RUN_EXPERIMENT", opportunity_id=o.id, hypothesis_id=o.hypothesis_id,
                contract_id=c.id, priority_score=o.priority_score,
                priority_components=o.priority_components, compute_cost_estimate=o.compute_cost_estimate,
                confidence=o.confidence, novelty=o.priority_components.get("novelty", 0.0),
                evidence_value=_evidence_value_estimate(
                    confidence=o.confidence, reassessment_eligible=o.reassessment_eligible),
                relevance=f"locked contract {c.id} is eligible to run ({o.type}, {o.lifecycle_stage})",
                rationale=o.rationale,
            ))
            continue
        # No hypothesis row (research.memory's DATASET_HYPOTHESIS) exists for
        # this contract yet — e.g. a contract locked by a path other than
        # hypothesis_intake.create_draft. `scheduler.eligible_contracts()`
        # still says it is genuinely locked and runnable, so it MUST still
        # get an Action — a legitimate, already-approved experiment must
        # never silently disappear from consideration just because its
        # provenance row is missing. Score it with the SAME formula, using
        # the same read-only fallbacks build_opportunity_pool() itself
        # already uses when it has no digest evidence entry to work from.
        is_dup = similarity.is_duplicate(store, c.id, registry_dir=registry_dir)
        is_conf_sib = prio.is_confirmation_experiment(store, c.id, registry_dir=registry_dir)
        fallback_type = _opportunity_type(
            verdict=None, reassessment_eligible=False, is_confirmation_sibling=is_conf_sib)
        fallback_components = _priority_components(
            evidence_verdict=None, is_duplicate=is_dup, is_confirmation_sibling=is_conf_sib,
            area_hypothesis_count=None, max_area_hypothesis_count=0,
            reassessment_eligible=False, opportunity_type=fallback_type, locked_at=c.locked_at,
        )
        actions.append(Action(
            kind="RUN_EXPERIMENT", opportunity_id=None, hypothesis_id=hid, contract_id=c.id,
            priority_score=priority_score(fallback_components), priority_components=fallback_components,
            compute_cost_estimate=_TYPE_COMPUTE_COST.get(fallback_type, _DEFAULT_COMPUTE_COST),
            confidence=None, novelty=fallback_components.get("novelty", 0.0),
            evidence_value=_evidence_value_estimate(confidence=None, reassessment_eligible=False),
            relevance=f"locked contract {c.id} is eligible to run (no linked opportunity record)",
            rationale="runnable per scheduler.eligible_contracts(); no matching Opportunity in the pool",
        ))

    for o in pool:
        if o.lifecycle_stage not in ("DISCOVERED", "TRIAGED") or not o.contract_id:
            continue
        if o.override_state.get("frozen") or o.override_state.get("retired"):
            continue
        actions.append(Action(
            kind="PROMOTE", opportunity_id=o.id, hypothesis_id=o.hypothesis_id,
            contract_id=o.contract_id, priority_score=o.priority_score,
            priority_components=o.priority_components, compute_cost_estimate=o.compute_cost_estimate,
            confidence=o.confidence, novelty=o.priority_components.get("novelty", 0.0),
            evidence_value=_evidence_value_estimate(
                confidence=o.confidence, reassessment_eligible=o.reassessment_eligible),
            relevance=f"{o.lifecycle_stage} draft ready for autonomous promotion",
            rationale=o.rationale,
        ))

    if discovery_available:
        components = _discovery_priority_components()
        actions.append(Action(
            kind="DISCOVER", opportunity_id=None, hypothesis_id=None, contract_id=None,
            priority_score=priority_score(components), priority_components=components,
            compute_cost_estimate=_TYPE_COMPUTE_COST["NEW_HYPOTHESIS"], confidence=None,
            novelty=1.0,
            evidence_value=_evidence_value_estimate(confidence=None, reassessment_eligible=False),
            relevance="no comparable existing opportunity outranks a fresh Research AI pass",
            rationale="run the Research AI once and evaluate whatever it proposes",
        ))

    actions.sort(key=lambda a: (-a.priority_score, a.kind, a.contract_id or "", a.opportunity_id or ""))
    return actions
