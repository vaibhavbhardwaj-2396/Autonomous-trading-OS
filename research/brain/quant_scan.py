"""
Mathematical Discovery Engine v0 — Phase 2 Slice T.

Activates a second, deterministic path into the SAME hypothesis-governance
seam research/brain/investigator.py already activates for the AI path:

    recorded market memory (research.store, via AsOfView — already
    knowledge-time gated, so leakage protection is structural here, not
    something this module has to re-implement)
          |
          v
    bounded quantitative scan                    find_candidate()
        (a small, fixed, pre-declared grid of
         volume-anomaly z-score thresholds x
         forward-return horizons — 2 x 3 = 6
         combinations, nothing adaptive)
          |
          v
    candidate statistical relationship            RelationshipStats /
        (sample count, mean forward return,        CandidateRelationship
         hit rate, stdev, t-stat where the
         sample size justifies one — never a
         claim of "alpha" or "a strategy")
          |
          v
    structured hypothesis proposal                 build_proposal()
        (the SAME dict shape hypothesis_intake.
         validate_proposal()/create_draft() have
         always accepted — no second schema)
          |
          v
    existing duplicate detection                   _existing_duplicate()
        (the SAME research.experiments.comparison.
         _rule_fingerprint() research/brain/
         similarity.py and research/overnight.py
         already treat as the sole definition of
         "this experiment specification already
         exists" — reused here, not reimplemented)
          |
          v
    existing validation + intake                    hi.validate_proposal()
        (hypothesis_intake.py, completely           then hi.create_draft()
         unmodified — the downstream system does
         not, and structurally cannot, know
         whether a proposal came from a human, the
         Research AI, or this module)
          |
          v
    existing research budget                        enforced later, inside
        (checked only at approve_and_lock() time,    hi.approve_and_lock() —
         exactly as for every other proposal —       this module never calls
         create_draft() itself never touches it)      it, so it never bypasses
                                                       the budget either
          |
          v
         DRAFT
          |
          v
    record_discovery_search()                       research.memory's
        (Slice N's EXISTING, unmodified provenance   EXISTING, unmodified
         dataset — discovery_type="mathematical",    function — no schema
         version=QUANT_SCAN_VERSION, plus the         change, no new table
         tested threshold/horizon grid and the
         one selected combination, so a future
         reader can answer "which search produced
         this hypothesis?")

CRITICAL BOUNDARY, enforced structurally (true by absence, the same posture
every research/brain module in this codebase already takes — see
research/brain/investigator.py's and research/overnight.py's own module
docstrings for the identical pattern): this module imports no `engine`
module, no broker module, never imports research.experiments.runner, never
references run_experiment() or simulate(), never imports or calls
hi.approve_and_lock() or Contract.lock(), never touches memory/state.json,
and contains no Claude/LLM call of any kind (no subprocess invocation, no
network call, nothing resembling one). It can PROPOSE a hypothesis. It
cannot approve one, lock one, run one, or place an order. The downstream
governance path (validate_proposal -> create_draft -> [later, separately,
by a human or an explicitly-authorized process] approve_and_lock -> the
scheduler -> the runner) is completely indifferent to where the proposal
came from — that indifference is the entire point of this slice: it proves
a mathematical discovery process can hand the SAME research-governance
front door a human or the Research AI already uses, without opening a
side door.

Why this is a HYPOTHESIS GENERATOR, not a strategy generator, stated
precisely: even a candidate that clears the filter below is reported as
"sessions where X happened were historically followed by a mean return of
Y" — a discovery-window observation over stored data — never as "buy this"
or "this is profitable." The generated proposal's own `hypothesis` and
`abandon_condition` text says exactly that, in plain language, and nothing
downstream of build_proposal() ever runs a trade, computes P&L, or reaches
engine/ in any way. Turning a hypothesis into money requires: an
independent human review of the DRAFT, an explicit approve_and_lock() with
a named approver (subject to the existing research budget), a derived
validation/holdout split (hypothesis_intake.derive_split_contract(), Slice
K, unmodified), a genuine run through research/experiments/runner.py
(unmodified), and evidence scored by research/experiments/comparison.py
(unmodified) — none of which this module can reach.

--------------------------------------------------------------------------
The ONE discovery technique implemented (v0; deliberately not generalized)
--------------------------------------------------------------------------

    volume anomaly z-score  ->  forward close-to-close return

This is research/brain/observatory.py's own `volume_anomaly` detector,
generalized from "flag only the most recent session" to "compute the same
z-score at every historical session that has enough trailing history" —
the exact same statistical core (`observatory._zscore`, imported and
reused verbatim, never reimplemented) applied at every index instead of
only the last one. Reusing `_zscore` also means this module inherits, for
free, observatory's own refusal behavior: a session whose trailing
baseline is smaller than `observatory.MIN_OBSERVATIONS` or has ~zero
variance produces no z-score at all (None), never a fabricated one.

Search space, small and fixed on purpose (2 thresholds x 3 horizons = 6
combinations total, every one of them auditable by eye):

    THRESHOLDS = (3.0, 4.0)   3.0 is observatory.Z_THRESHOLD itself — the
                              same ~3-sigma bar the rest of this codebase
                              already treats as "anomalous"; 4.0 is one
                              stricter increment, this slice's own worked
                              example value. Neither is fit to the data —
                              both are fixed before any scan runs.
    HORIZONS   = (1, 3, 5)    trading days forward — this slice's own
                              worked example values verbatim.

No arbitrary expression generation, no feature mining, no genetic
programming, no parameter optimization loop, no ML of any kind: this is a
plain nested Python loop over 6 fixed tuples, and `THRESHOLDS`/`HORIZONS`
are never touched by anything computed from the scan's own results.

Discovery filter — deterministic, documented, decided BEFORE any data is
looked at (never tuned after seeing what a scan produces):

    n (sample count)              >= MIN_SAMPLES_FOR_CANDIDATE   (10)
    abs(mean forward return)      >= MIN_EFFECT_MAGNITUDE        (0.003)

MIN_SAMPLES_FOR_CANDIDATE reuses this repository's own existing "10"
minimum-sample convention verbatim — the same number as
observatory.MIN_OBSERVATIONS (a baseline needs >= 10 points before a
z-score is trusted at all) and research.experiments.comparison.
MIN_TRADES_FOR_SIGNIFICANCE (an experiment needs >= 10 trades before its
t-stat is trusted). MIN_EFFECT_MAGNITUDE reuses research.experiments.
comparison.ECONOMIC_SIGNIFICANCE_PCT's own number (0.3%) — the same bar
the rest of this codebase already applies to "is this move big enough to
matter economically." Neither constant is imported directly from
comparison.py (this module intentionally has zero dependency on it — see
"Search order" below and the module's own import list) — they are
independently declared, equal-by-design constants, documented here as
exactly that: reuse of an existing convention, not a new number invented
for this slice.

Search order and the one-candidate cap: THRESHOLDS and HORIZONS are walked
in a single fixed nested order — ascending threshold, then ascending
horizon within each threshold — and find_candidate() returns the FIRST
combination (aggregated over ALL scanned symbols) whose statistics clear
the filter above, stopping immediately. This is deliberately NOT "the
best-looking combination out of the 6" — picking whichever result looks
strongest after the fact is exactly the kind of after-the-fact tuning the
filter above is designed to avoid, and this codebase already treats
multiple-comparisons cherry-picking as a real failure mode elsewhere (see
research/contracts.py's comparison_count()). "First, in a fixed order
declared before the scan ran" produces at most one candidate — and
therefore at most one proposal — per call, with no implicit "pick the
winner" step anywhere in the path.

Leakage protection: every AsOfView already refuses to return a row whose
knowledge_ts is after `as_of` (research/store.py, unmodified) — that is
the outer, structural guarantee, and this module never bypasses it (it
only ever reads through `store.view(as_of)`, once, at the very top of
find_candidate()). Inside that already-safe window, `_zscore_at(sessions,
i)` additionally never reads any index beyond `i` — its baseline is
`sessions[max(0, i - WINDOW_DAYS):i]` and its "current" observation is
`sessions[i]`; nothing at `i + 1` or later is ever touched by that
function, by construction. tests/test_research_quant_scan.py's leakage
section proves this empirically, not just by inspection: computing
`_zscore_at` for the same index against a `sessions` list with every row
after that index physically deleted returns an identical result to
computing it against the full, untruncated list.

Determinism: `find_candidate()` takes no wall-clock reading, no random
number, and makes no network or AI call. Its only inputs are the Store,
an explicitly supplied `as_of`, and an explicit `symbols` iterable — the
symbol set is deduplicated and sorted before scanning, so even an
unordered container (e.g. a set) produces identical output across calls.
For a fixed Store, `as_of`, and `symbols`, two calls are guaranteed byte-
for-byte identical.

No AI, structurally: this file contains no import of `subprocess`, no
network client, no reference to any "claude"/"anthropic"/LLM binary or
API, and no injectable "runner" callback of the kind investigator.py
deliberately has for its own (necessarily AI-shaped) boundary. There is
nothing here to inject — the whole module is arithmetic over already-
recorded numbers.

Data model: no new SQLite table, no new dataset. The write side reuses
research.memory.record_discovery_search() exactly as research/brain/
investigator.py already does, under a new `discovery_type="mathematical"`
value that dataset was already documented (Slice N) as designed to accept
without a schema change. research/brain/discovery_provenance.py (the read
side) needs no change either — `discovery_search_for_hypothesis()` already
returns whatever `extra` payload the writer supplied, generically.
"""

from __future__ import annotations

import json
import math
import statistics
from pathlib import Path
from typing import Iterable, NamedTuple, Optional

from .. import memory as rm
from ..contracts import Contract, REGISTRY_DIR, registry as _registry
from ..experiments.comparison import _rule_fingerprint
from ..store import AsOfView, Store, TimeLike, iso
from . import hypothesis_intake as hi
from .observatory import MIN_OBSERVATIONS, WINDOW_DAYS, _zscore

# ---------------------------------------------------------------------------
# This module's own discovery-provenance identity (Slice N convention — see
# research/brain/investigator.py's RESEARCH_AI_VERSION for the precedent).
# Bump QUANT_SCAN_VERSION by hand whenever a change here would matter for
# reproducing a past discovery run (e.g. THRESHOLDS/HORIZONS/the filter
# constants change).
# ---------------------------------------------------------------------------

QUANT_SCAN_VERSION = "quant_scan.v0"
DISCOVERY_TYPE_MATHEMATICAL = "mathematical"
DEFAULT_SOURCE = "quant_scan.mathematical_discovery"
DEFAULT_UNIVERSE_LABEL = "watchlist"

# -- the bounded search space -------------------------------------------------

THRESHOLDS = (3.0, 4.0)
HORIZONS = (1, 3, 5)

# -- the discovery filter, fixed before any scan runs — see module docstring -

MIN_SAMPLES_FOR_CANDIDATE = 10
MIN_EFFECT_MAGNITUDE = 0.003


# ---------------------------------------------------------------------------
# Step 1 — per-session z-score, generalized from observatory.volume_anomaly
# (which only ever evaluates the LAST session) to an arbitrary index. Reuses
# observatory._zscore verbatim: same refusal behavior, same statistical core.
# ---------------------------------------------------------------------------

def _zscore_at(sessions: list[dict], i: int):
    """observatory._zscore applied to sessions[i]'s volume against its own
    trailing baseline sessions[max(0, i-WINDOW_DAYS):i].

    Reads ONLY sessions[0 .. i] inclusive — index i+1 and beyond are never
    touched anywhere in this function body. That is the leakage guarantee
    tests/test_research_quant_scan.py's section N verifies empirically (by
    truncating `sessions` after index i and confirming an identical result),
    not just by this docstring's claim.

    Returns None — never a fabricated value — exactly when observatory._
    zscore itself would: fewer than MIN_OBSERVATIONS baseline points, a
    ~zero-variance baseline, or a missing volume figure anywhere in the
    window (missing data is never interpolated or guessed).
    """
    baseline = sessions[max(0, i - WINDOW_DAYS): i]
    if len(baseline) < MIN_OBSERVATIONS:
        return None
    current_vol = sessions[i].get("volume")
    baseline_vols = [s.get("volume") for s in baseline]
    if current_vol is None or any(v is None for v in baseline_vols):
        return None
    return _zscore([float(v) for v in baseline_vols], float(current_vol))


def _forward_return(sessions: list[dict], i: int, horizon: int) -> Optional[float]:
    """Close-to-close return from sessions[i] to sessions[i + horizon].
    Returns None — never an invented figure — if that forward session
    doesn't exist yet (the series simply hasn't run that far) or either
    close is missing/zero."""
    j = i + horizon
    if j >= len(sessions):
        return None
    c0, c1 = sessions[i].get("close"), sessions[j].get("close")
    if c0 is None or c1 is None or c0 == 0:
        return None
    return (c1 - c0) / c0


class AnomalyInstance(NamedTuple):
    """One (symbol, session) where a z-score was computable — regardless of
    whether it clears any threshold; thresholding happens later, in
    _aggregate(), the same "detect first, decide later" separation
    observatory.py itself uses between its detectors and run()."""

    symbol: str
    session_date: str
    z_score: float
    forward_returns: dict  # {horizon (int): forward_return (float)} — only
                            # horizons actually computable are present; a
                            # missing horizon is omitted, never invented.


def _instances_for_symbol(sessions: list[dict], symbol: str) -> list[AnomalyInstance]:
    """Every AnomalyInstance computable from `sessions` (already fetched
    through an AsOfView, already knowledge-time gated, already ascending by
    session_date). An instance with no computable forward return at any
    declared horizon is dropped — it can never contribute to any
    aggregate, so keeping it would just be dead weight, never a source of
    invented data."""
    out: list[AnomalyInstance] = []
    for i in range(len(sessions)):
        z = _zscore_at(sessions, i)
        if z is None:
            continue
        forward = {}
        for h in HORIZONS:
            r = _forward_return(sessions, i, h)
            if r is not None:
                forward[h] = r
        if not forward:
            continue
        out.append(AnomalyInstance(
            symbol=symbol, session_date=sessions[i]["session_date"],
            z_score=z.z, forward_returns=forward,
        ))
    return out


def _scan_symbol(view: AsOfView, symbol: str) -> list[AnomalyInstance]:
    """The only place this module reads prices: the whole visible history
    for one symbol, ascending, through the supplied (already as_of-gated)
    view. `days=None` means "no LIMIT" — every session AsOfView.prices()
    will return, i.e. everything knowledge-time-visible at as_of."""
    sessions = view.prices(symbol, days=None)
    return _instances_for_symbol(sessions, symbol.upper())


# ---------------------------------------------------------------------------
# Step 2 — aggregate statistics for one (threshold, horizon) combination.
# Transparent, basic statistics only — no invented corrections.
# ---------------------------------------------------------------------------

class RelationshipStats(NamedTuple):
    threshold: float
    horizon: int
    n: int
    mean_return: float
    hit_rate: float
    stdev: float
    t_stat: Optional[float]
    first_observed: Optional[str]  # earliest session_date contributing, or None if n == 0
    last_observed: Optional[str]   # latest session_date contributing, or None if n == 0


def _aggregate(
    by_symbol: dict, threshold: float, horizon: int,
) -> RelationshipStats:
    """Every forward_returns[horizon] from every instance (across every
    symbol, symbols walked in sorted order for determinism) whose z_score
    clears `threshold` — one-directional (an unusually HIGH volume session,
    z_score >= threshold), matching the entry_rule this candidate would
    eventually propose (a single `metric >= value` condition). n=0 is a
    normal, valid result (it will simply fail the filter below), not an
    error."""
    returns: list[float] = []
    dates: list[str] = []
    for symbol in sorted(by_symbol):
        for inst in by_symbol[symbol]:
            if inst.z_score >= threshold and horizon in inst.forward_returns:
                returns.append(inst.forward_returns[horizon])
                dates.append(inst.session_date)

    n = len(returns)
    if n == 0:
        return RelationshipStats(
            threshold=threshold, horizon=horizon, n=0, mean_return=0.0,
            hit_rate=0.0, stdev=0.0, t_stat=None,
            first_observed=None, last_observed=None,
        )

    mean = statistics.fmean(returns)
    hit_rate = sum(1 for r in returns if r > 0) / n
    stdev = statistics.stdev(returns) if n > 1 else 0.0
    t_stat = (mean / (stdev / math.sqrt(n))) if (n > 1 and stdev > 1e-12) else None

    return RelationshipStats(
        threshold=threshold, horizon=horizon, n=n, mean_return=mean,
        hit_rate=hit_rate, stdev=stdev, t_stat=t_stat,
        first_observed=min(dates), last_observed=max(dates),
    )


def _passes_filter(stats: RelationshipStats) -> bool:
    """The one deterministic minimum bar — see module docstring. Fixed
    before any scan runs; never adjusted based on what a particular scan's
    numbers happen to look like."""
    return (stats.n >= MIN_SAMPLES_FOR_CANDIDATE
            and abs(stats.mean_return) >= MIN_EFFECT_MAGNITUDE)


# ---------------------------------------------------------------------------
# Step 3 — the bounded search itself. At most one CandidateRelationship out,
# ever, per call — see module docstring's "Search order and the
# one-candidate cap".
# ---------------------------------------------------------------------------

class CandidateRelationship(NamedTuple):
    threshold: float
    horizon: int
    stats: RelationshipStats
    symbols_examined: tuple  # sorted, deduplicated — the exact universe scanned


def find_candidate(
    store: Store, as_of: TimeLike, symbols: Iterable[str],
) -> Optional[CandidateRelationship]:
    """Run the entire bounded 6-combination search and return the first
    (threshold, horizon) pair, in fixed ascending order, whose aggregate
    statistics clear the filter — or None if nothing in the declared search
    space does. Pure and deterministic: the only Store access is the single
    `store.view(as_of)` call, and every read after that goes through that
    one already-gated view."""
    view = store.view(as_of)
    syms = tuple(sorted({s.upper() for s in symbols}))
    by_symbol = {s: _scan_symbol(view, s) for s in syms}

    for threshold in THRESHOLDS:
        for horizon in HORIZONS:
            stats = _aggregate(by_symbol, threshold, horizon)
            if _passes_filter(stats):
                return CandidateRelationship(
                    threshold=threshold, horizon=horizon, stats=stats,
                    symbols_examined=syms,
                )
    return None


# ---------------------------------------------------------------------------
# Step 4 — structured hypothesis proposal. Same shape hypothesis_intake.
# validate_proposal()/create_draft() have always accepted; no second schema.
# ---------------------------------------------------------------------------

def build_proposal(
    candidate: CandidateRelationship, *, universe: str = DEFAULT_UNIVERSE_LABEL,
) -> dict:
    """Turn one CandidateRelationship into a plain dict proposal, ready for
    hi.validate_proposal()/hi.create_draft() — no field here is anything
    hypothesis_intake.py doesn't already know how to validate.

    `universe` is the categorical label hypothesis_intake.VALID_UNIVERSES
    expects ("watchlist" by default — the symbols this module is normally
    pointed at, via engine.watchlist.UNIVERSE, ARE that watchlist); a
    caller scanning a different symbol set (e.g. the Nifty 50 index
    membership) should pass the matching label explicitly.

    The proposed entry_rule is deliberately ONE condition
    (`volume_zscore >= threshold`) and the exit_rule is deliberately just
    `max_hold_days: horizon` — a pure "hold for the tested forward
    horizon, then exit" specification, mirroring exactly what the scan
    itself measured. No stop_loss_pct/target_pct is invented; the scan
    never tested one, so proposing one would misrepresent what was found.

    evaluation_start/evaluation_end (and splits["discovery"], the same
    window) are the actual first/last calendar dates on which a qualifying
    instance was observed for this exact (threshold, horizon) — the
    discovery window this candidate's own statistics were computed over,
    not the full history scanned and not padded for horizon lookout (how a
    downstream Contract handles a trade whose exit falls after
    evaluation_end is research/experiments/runner.py's existing, unchanged
    concern via max_hold_days, not something this discovery step manages).
    """
    stats = candidate.stats
    threshold, horizon = candidate.threshold, candidate.horizon
    t_stat_str = f"{stats.t_stat:.3f}" if stats.t_stat is not None else "n/a"

    title = (
        f"Volume z-score >= {threshold:g} vs {horizon}-day forward return "
        f"(mathematical discovery)"
    )
    hypothesis = (
        f"This is a hypothesis, generated by a deterministic quantitative "
        f"scan of recorded price history (research.brain.quant_scan, "
        f"{QUANT_SCAN_VERSION}), not a proven result: across {stats.n} "
        f"historical session(s) where the {WINDOW_DAYS}-session trailing "
        f"volume z-score reached at least {threshold:g}, the mean "
        f"{horizon}-trading-day forward close-to-close return was "
        f"{stats.mean_return:.4%} (hit rate {stats.hit_rate:.2%}, t-stat "
        f"{t_stat_str}). This candidate relationship requires independent "
        f"testing — an independently specified, hash-locked Contract with "
        f"its own out-of-sample split — before it may be treated as a "
        f"validated trading edge."
    )
    null_hypothesis = (
        f"The {horizon}-day forward return following a session where the "
        f"volume z-score reaches at least {threshold:g} is drawn from the "
        f"same distribution as the forward return following an ordinary, "
        f"non-anomalous session — the discovery-window mean reported above "
        f"reflects noise, not a real relationship."
    )
    signal = (
        f"volume_zscore >= {threshold:g} over trailing {WINDOW_DAYS} "
        f"sessions as a leading indicator of {horizon}-day forward return"
    )
    independence = (
        "Generated by a deterministic quantitative scan reading only "
        "already-recorded research market memory (research.brain."
        "quant_scan) — no human or Research AI judgment selected this "
        "specific relationship. It is independent only in the sense of "
        "not being human- or AI-curated; it is still a single discovery-"
        "window observation and needs its own validation or holdout test "
        "before being treated as confirmed."
    )
    falsification = (
        "Falsified if a locked Contract testing this exact entry/exit "
        "specification over an independent evaluation window shows no "
        "statistically or economically meaningful edge, evaluated by the "
        "existing research evidence rules."
    )
    abandon_condition = (
        "Abandon if a derived validation or holdout split shows no "
        "meaningful forward-return edge, or if its sign or magnitude "
        "differs materially from the discovery-window statistics recorded "
        "in this proposal's own notes."
    )
    notes = (
        f"quant_scan {QUANT_SCAN_VERSION} candidate: threshold={threshold:g}, "
        f"horizon={horizon}d, n={stats.n}, mean_return={stats.mean_return:.6f}, "
        f"hit_rate={stats.hit_rate:.4f}, stdev={stats.stdev:.6f}, "
        f"t_stat={t_stat_str}. Search space tested: thresholds="
        f"{list(THRESHOLDS)}, horizons={list(HORIZONS)}. Symbols examined: "
        f"{list(candidate.symbols_examined)}. Discovery window: "
        f"{stats.first_observed} to {stats.last_observed}."
    )

    return {
        "title": title,
        "hypothesis": hypothesis,
        "null_hypothesis": null_hypothesis,
        "universe": universe,
        "signal": signal,
        "entry_rule": {"conditions": [
            {"metric": "volume_zscore", "op": ">=", "value": float(threshold)},
        ]},
        "exit_rule": {"max_hold_days": int(horizon)},
        "splits": {"discovery": [stats.first_observed, stats.last_observed]},
        "independence": independence,
        "falsification": falsification,
        "abandon_condition": abandon_condition,
        "evaluation_start": stats.first_observed,
        "evaluation_end": stats.last_observed,
        "notes": notes,
        "source": DEFAULT_SOURCE,
    }


# ---------------------------------------------------------------------------
# Step 5 — duplicate pre-check, mirroring research/overnight.py's own
# _existing_duplicate() (which this module deliberately does NOT import:
# overnight.py is a protected, do-not-modify file for this slice, and its
# helper is private to it — the same reasoning research/brain/similarity.py
# already documents for importing comparison._rule_fingerprint directly
# despite its leading underscore applies again here: reuse the ONE
# authoritative definition of "duplicate", never invent a second one).
# Nothing here writes, locks, or executes anything — a throwaway Contract
# object is built purely to compute its fingerprint, then discarded.
# ---------------------------------------------------------------------------

def _existing_duplicate(proposal: dict, *, registry_dir: Path) -> Optional[str]:
    """Would a Contract built from this not-yet-created proposal share
    research.experiments.comparison._rule_fingerprint() with a Contract
    already in the registry? Returns the existing contract_id if so, else
    None. Fails OPEN (returns None) on any malformed proposal shape — this
    is a pre-flight optimization, not a validation boundary; hi.
    validate_proposal() (invoked downstream, inside hi.create_draft())
    remains the real, authoritative gate for a malformed proposal."""
    try:
        candidate = Contract(
            id="__quant_scan_duplicate_preview__",
            title="", hypothesis="", null_hypothesis="",
            universe=proposal["universe"], signal="",
            entry_rule=json.dumps(proposal["entry_rule"], sort_keys=True, separators=(",", ":")),
            exit_rule=json.dumps(proposal["exit_rule"], sort_keys=True, separators=(",", ":")),
            splits=proposal["splits"], independence="", falsification="",
            abandon_condition="x",
            evaluation_start=proposal["evaluation_start"],
            evaluation_end=proposal["evaluation_end"],
        )
        target_fp = _rule_fingerprint(candidate)
    except Exception:
        return None

    for c in _registry(registry_dir):
        if _rule_fingerprint(c) == target_fp:
            return c.id
    return None


# ---------------------------------------------------------------------------
# Orchestration — the one public entry point most callers want. Mirrors
# research/brain/investigator.py's investigate() shape exactly: a result, a
# "nothing found" outcome, or a "would duplicate" outcome, all normal —
# never an exception for any of the three.
# ---------------------------------------------------------------------------

class ScanResult(NamedTuple):
    hypothesis_id: str
    contract_id: str
    candidate: CandidateRelationship


class NoCandidate(NamedTuple):
    """No (threshold, horizon) combination in the declared search space
    cleared the deterministic minimum bar this run. Not an error — a scan
    finding nothing is a normal, expected outcome, exactly like
    investigator.NoProposal is for the AI path."""

    reason: str


class DuplicateCandidate(NamedTuple):
    """The candidate's proposal would exactly duplicate an existing
    Contract (research.experiments.comparison._rule_fingerprint()) — caught
    BEFORE create_draft() is ever called. Not an error, the same way
    investigator.DuplicateProposal is not one."""

    existing_contract_id: str
    candidate: CandidateRelationship


def run_scan(
    store: Store,
    as_of: TimeLike,
    symbols: Iterable[str],
    *,
    universe: str = DEFAULT_UNIVERSE_LABEL,
    registry_dir: Path = REGISTRY_DIR,
    persist_draft: bool = True,
):
    """The whole seam, end to end — see the module docstring's diagram.

    Returns a ScanResult on a successful DRAFT, a NoCandidate when nothing
    in the bounded search space cleared the filter, or a DuplicateCandidate
    when the one surviving candidate would exactly duplicate an existing
    Contract. All three are normal outcomes.

    Raises hi.IntakeRejected only if a produced proposal somehow fails
    hypothesis_intake's own content validation — every field build_proposal()
    fills in is already schema-shaped, so this should not happen in
    practice, but this function makes no separate promise beyond delegating
    to the existing, unmodified create_draft(). Never calls, imports, or
    references approve_and_lock, run_experiment, or anything under engine/.

    At most one hypothesis proposal is ever produced per call — find_
    candidate() returns at most one CandidateRelationship, and this function
    calls hi.create_draft() at most once.
    """
    candidate = find_candidate(store, as_of, symbols)
    if candidate is None:
        return NoCandidate(
            reason="no threshold/horizon combination in the declared search "
                   "space cleared the minimum sample/effect-magnitude bar"
        )

    proposal = build_proposal(candidate, universe=universe)

    existing_id = _existing_duplicate(proposal, registry_dir=registry_dir)
    if existing_id:
        return DuplicateCandidate(existing_contract_id=existing_id, candidate=candidate)

    result = hi.create_draft(
        store, proposal, default_source=DEFAULT_SOURCE,
        registry_dir=registry_dir, persist_draft=persist_draft,
    )

    # Slice N provenance — recorded only now that create_draft() has
    # genuinely succeeded (it raises hi.IntakeRejected, before writing
    # anything, for any proposal that fails validation, so this line is
    # never reached for a rejected proposal). Carries enough to answer
    # "which search produced this hypothesis?": the full tested grid, the
    # one selected combination, and the exact symbol universe scanned.
    rm.record_discovery_search(
        store,
        hypothesis_id=result.hypothesis_id,
        discovery_type=DISCOVERY_TYPE_MATHEMATICAL,
        as_of=iso(as_of),
        version=QUANT_SCAN_VERSION,
        source=DEFAULT_SOURCE,
        extra={
            "thresholds_tested": list(THRESHOLDS),
            "horizons_tested": list(HORIZONS),
            "selected_threshold": candidate.threshold,
            "selected_horizon": candidate.horizon,
            "n_observations": candidate.stats.n,
            "symbols_examined": list(candidate.symbols_examined),
        },
    )

    return ScanResult(
        hypothesis_id=result.hypothesis_id,
        contract_id=result.contract.id,
        candidate=candidate,
    )
