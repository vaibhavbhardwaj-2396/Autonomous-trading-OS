"""
research/brain/worker.py — Continuous Research Worker, v2: Unified
Opportunity-Driven Worker Selection, on top of the Autonomous Research
Control Plane (research/brain/opportunity.py).

THE GAP THIS CLOSES
-------------------
v1 closed the DRAFT -> LOCKED human-approval gap (autonomous promotion, via
`opportunity.attempt_autonomous_promotion`) but still chose WHAT KIND of
work to do via a FIXED PHASE ORDER every heartbeat: experiments, then
discovery, then promotion, always in that order, regardless of how valuable
any one of them actually was. A brand-new discovery attempt always ran; a
rejected hypothesis whose evidence just turned in its favour had no way to
ever outrank it. v2 removes that fixed order. Every heartbeat now runs a
single, iterative, priority-driven loop:

    ┌─────────────────────────────────────────────────────────────────┐
    │  refresh the opportunity pool  ── opportunity.build_opportunity_ │
    │      pool()  (priority.rank_experiments-style scoring, over      │
    │      EVERY hypothesis: drafts, locked-and-eligible, rejected-    │
    │      but-reassessment-eligible, promising, robust)               │
    │                          │                                       │
    │                          ▼                                       │
    │  rank every EXECUTABLE action on ONE scale ── opportunity.        │
    │      build_action_queue()  — RUN_EXPERIMENT (via scheduler.       │
    │      eligible_contracts), PROMOTE (a promotable draft),          │
    │      CREATE_EXPERIMENT (a substrate for a high-priority          │
    │      opportunity with neither), DISCOVER (one synthetic "run    │
    │      the Research AI" action) — all scored through the SAME      │
    │      priority_score()/_priority_components()                   │
    │                          │                                       │
    │                          ▼                                       │
    │  execute the single highest-priority ELIGIBLE action              │
    │      (per-kind caps still enforced: max_experiments /             │
    │      max_discovery_attempts / max_promotions /                   │
    │      max_substrate_creations)                                    │
    │                          │                                       │
    │                          ▼                                       │
    │  record the outcome, update evidence/lifecycle/priority           │
    │                          │                                       │
    │                          └──── loop, until the runtime budget    │
    │                                is spent or nothing eligible      │
    │                                remains ─────────────────────────┘

Every real step underneath a selected action is exactly the same existing,
unmodified component v1 already used — running an experiment still goes
through `research.brain.scheduler` (a new, narrow, single-contract entry
point, `scheduler.run_one_experiment`, added alongside its existing batch
`run_scheduler`, both delegating to the exact same runner primitive),
discovery still goes through `investigator.investigate`, and promotion
still goes through `opportunity.attempt_autonomous_promotion` (which itself
still calls the EXISTING, UNMODIFIED locking primitive with the EXISTING,
UNMODIFIED research-budget rate limit — see that module's own docstring).
CREATE_EXPERIMENT (Autonomous Research Action Expansion) goes through the
new `opportunity.attempt_create_experiment`, which itself only ever calls
the EXISTING, UNMODIFIED `hypothesis_intake.derive_split_contract` — never
locks anything, produces a DRAFT that a LATER PROMOTE selection (this same
heartbeat or a future one) locks through the unchanged path above. What
changed is ONLY the selection policy: which one of those four kinds of
work is worth doing right now, decided fresh after every single action,
instead of a fixed phase order. See `docs/RESEARCH_CONTROL_PLANE.md` for
the full priority model and `research.brain.opportunity.build_action_queue`'s
own docstring for exactly how the four action kinds are put on one scale.

CADENCE MODEL: cron is a **heartbeat**, this module is the **brain**.
`main()` runs exactly one bounded slice of work and exits. Nothing here
loops, sleeps, daemonises, or schedules itself — the loop above is bounded
by the SAME `WorkerLimits` this module always had (runtime, and one small
per-kind cap each), never unbounded. A cron line every 5–15 minutes invokes
it; the worker decides each time whether any useful work exists and does at
most a configured amount of it.

REUSE, DON'T REIMPLEMENT — every real step is an existing, unmodified component:
    digest                      -> research.brain.digest.build_digest
    discovery (AI proposal)     -> research.brain.investigator.investigate
    duplicate detection         -> research.overnight._existing_duplicate
                                   (itself just research...comparison._rule_fingerprint,
                                   the same check research/brain/similarity.py uses)
    running one experiment      -> research.brain.scheduler.run_one_experiment
                                   (a single-contract entry point onto the SAME
                                   runner primitive scheduler's own batch call uses —
                                   this module never touches the runner module itself)
    verdict / evidence feedback -> research.experiments.evaluator (inside run_experiment)
    provenance                  -> research.memory.record_discovery_search (inside investigate)
    research budgets / areas    -> enforced inside the components above, untouched here
    opportunity pool / priority /
    action ranking / autonomous
    promotion / substrate       -> research.brain.opportunity (composes ALL of the
    creation                       above, reimplements none of it — see that
                                   module's own docstring, in particular
                                   `build_opportunity_pool`, `build_action_queue`,
                                   and `attempt_create_experiment`)
    substrate creation itself   -> research.brain.hypothesis_intake.derive_split_contract
                                   (inside opportunity.attempt_create_experiment;
                                   this module never calls it directly)

WHAT THIS MODULE NEVER DOES — structurally true by absence of the import:
  - lock a Contract through any path OTHER than the existing, unmodified
    locking primitive inside `hypothesis_intake` — this module never imports
    `Contract.lock` and never calls that primitive directly; the ONE call
    site is inside `opportunity.attempt_autonomous_promotion()`, which this
    module only invokes through `opportunity`'s own public API. Locking still
    always uses an explicit, named, auditable approver — a human name for a
    human action, or `opportunity.SYSTEM_APPROVER` for an autonomous one —
    and still always respects the EXISTING, UNMODIFIED research-budget rate
    limit (`hypothesis_intake.check_research_budget`); a budget refusal is a
    clean, logged, non-error outcome, never bypassed or overridden here.
  - call `research.experiments.runner` directly. Running an experiment
    still goes exclusively through `research.brain.scheduler`
    (`run_one_experiment`/`run_scheduler`), which owns the one call to the
    runner primitive; this module has no import of that runner module at all.
  - call `hypothesis_intake.derive_split_contract` (or `create_draft`)
    directly. Every consequential creation — a draft from a discovery
    proposal, or a validation/holdout split derived for a high-priority
    opportunity with no runnable substrate — goes exclusively through
    `investigator.investigate`/`opportunity.attempt_create_experiment`;
    this module never imports `hypothesis_intake.derive_split_contract` by
    name and never calls it.
  - touch engine/*, memory/state.json, or a broker. `engine.execute`,
    `engine.guardrails`, `engine.broker*` are never imported. `research/`
    already never imports a broker (tests/test_broker_probe.py) and
    `engine` never imports `research` (tests/test_kernel_isolation.py) —
    this module adds no edge to either graph. Safe to run during market
    hours precisely because the separation is at the import boundary, not
    a scheduling convention.
  - promote research directly into live trading. Autonomous promotion moves a
    hypothesis from DISCOVERED/TRIAGED to TESTING (a locked, about-to-be-
    backtested Contract) — nowhere near a broker, capital, or an order.
  - create a second source of truth. The only durable state it writes is
    (a) `research/worker_runs.jsonl` — operational telemetry, exactly like
    `overnight_runs.jsonl` / `recorder_runs.jsonl`; (b) `research/.worker_state.json`
    — a two-field cooldown/summary bookmark; (c) via `opportunity`, one new
    append-only `research_opportunity_event` row per lifecycle transition,
    promotion, or user override — the SAME `research.memory` observations
    table every other dataset already lives in. None of these hold research
    data; the research Store remains the sole authority.
  - run more than one experiment, one discovery attempt, or one promotion
    concurrently. Each iteration of the loop executes exactly ONE action and
    waits for it to finish before selecting the next — there is no
    concurrency here, only a different ORDER than v1's fixed three phases.

RESOURCE SAFETY — one process, one action at a time, bounded runtime, no
unbounded scan. All limits are in `WorkerLimits` (env-overridable), not
scattered constants — this slice does not raise or remove any of them, it
only lets the worker choose intelligently AMONG actions within them (see
`docs/RESEARCH_CONTROL_PLANE.md` "remaining gaps" for what a true
resource-aware allocator beyond these per-kind caps would still need).
Overlapping cron triggers are made safe by a single POSIX file lock
(`research/.worker.lock`); a second concurrent invocation exits immediately
without doing anything.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable, Iterator, NamedTuple, Optional

from ..store import Store, TimeLike, now_ist, iso
from ..contracts import REGISTRY_DIR
from .digest import build_digest
from . import investigator as inv
from . import scheduler as sched
from . import hypothesis_intake as hi
from ..experiments import evaluator
# The exact-duplicate pre-flight check for a not-yet-created proposal already
# exists — research/overnight.py wrote it (a throwaway Contract fingerprinted
# with research...comparison._rule_fingerprint, the one authoritative
# definition of "same rule spec"). Reuse it verbatim rather than add a second
# copy; overnight.py imports only from research.brain.{hypothesis_intake,
# investigator,digest}, none of which import this module, so there is no cycle.
from ..overnight import _existing_duplicate as _proposal_duplicate_of
from . import opportunity as opp

RESEARCH_DIR = Path(__file__).resolve().parent.parent
RUN_LOG = RESEARCH_DIR / "worker_runs.jsonl"
LOCK_PATH = RESEARCH_DIR / ".worker.lock"
STATE_PATH = RESEARCH_DIR / ".worker_state.json"


# ---------------------------------------------------------------------------
# Configuration — one place, env-overridable, validated. Same "small,
# explicit, clearly-named, overridable" posture as scheduler.MAX_EXPERIMENTS_
# PER_RUN and overnight.MAX_PROPOSAL_ATTEMPTS_PER_RUN.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WorkerLimits:
    max_discovery_attempts: int = 1
    max_experiments: int = 1
    max_runtime_seconds: float = 300.0
    max_concurrent_experiments: int = 1
    cooldown_seconds: float = 1800.0
    discovery_enabled: bool = True
    summary_interval_seconds: float = 43200.0  # 12h — the periodic Telegram roll-up
    # Autonomous Research Control Plane v1 (research/brain/opportunity.py) — how
    # many DRAFT hypotheses one heartbeat may attempt to autonomously promote
    # (draft -> locked) via opportunity.attempt_autonomous_promotion(). Same
    # "small, explicit, conservative default" posture as every other bound
    # here; the EXISTING research budget (hypothesis_intake.check_research_
    # budget) still governs how many locks may actually succeed regardless of
    # this number.
    max_promotions: int = 1
    # Autonomous Research Action Expansion — how many CREATE_EXPERIMENT
    # substrate-creation attempts (opportunity.attempt_create_experiment(),
    # a validation/holdout split derivation) one heartbeat may make. Same
    # conservative-default posture as max_promotions; creating a substrate
    # never locks anything, so this is NOT gated by the research budget —
    # only by this cap and by whether a substrate is actually available
    # (opportunity._available_split_key()).
    max_substrate_creations: int = 1

    ENV = {
        "max_discovery_attempts": "RESEARCH_WORKER_MAX_DISCOVERY_ATTEMPTS",
        "max_experiments": "RESEARCH_WORKER_MAX_EXPERIMENTS",
        "max_runtime_seconds": "RESEARCH_WORKER_MAX_RUNTIME_SECONDS",
        "max_concurrent_experiments": "RESEARCH_WORKER_MAX_CONCURRENT_EXPERIMENTS",
        "cooldown_seconds": "RESEARCH_WORKER_COOLDOWN_SECONDS",
        "discovery_enabled": "RESEARCH_WORKER_DISCOVERY_ENABLED",
        "summary_interval_seconds": "RESEARCH_WORKER_SUMMARY_INTERVAL_SECONDS",
        "max_promotions": "RESEARCH_WORKER_MAX_PROMOTIONS",
        "max_substrate_creations": "RESEARCH_WORKER_MAX_SUBSTRATE_CREATIONS",
    }

    def __post_init__(self) -> None:
        if self.max_concurrent_experiments != 1:
            raise ValueError(
                "WorkerLimits.max_concurrent_experiments must be 1 in v0 — parallel "
                "experiments are deliberately out of scope until measured safe on the "
                "shared VPS (see docs/RESEARCH_WORKER.md 'Resource safety')")
        for name in ("max_discovery_attempts", "max_experiments", "max_promotions",
                    "max_substrate_creations"):
            if getattr(self, name) < 0:
                raise ValueError(f"WorkerLimits.{name} must be >= 0")
        for name in ("max_runtime_seconds", "cooldown_seconds", "summary_interval_seconds"):
            if getattr(self, name) < 0:
                raise ValueError(f"WorkerLimits.{name} must be >= 0")

    @classmethod
    def from_env(cls, **overrides) -> "WorkerLimits":
        """Build limits from RESEARCH_WORKER_* env vars, then apply any
        explicit `overrides` (CLI flags) on top. An unset/blank/garbage env
        var falls back to the dataclass default, never crashes."""
        base = cls()
        values: dict = {}
        for field_name, env_name in cls.ENV.items():
            raw = (os.environ.get(env_name) or "").strip()
            default = getattr(base, field_name)
            if not raw:
                values[field_name] = default
                continue
            try:
                if isinstance(default, bool):
                    values[field_name] = raw.lower() in ("1", "true", "yes", "on")
                elif isinstance(default, int):
                    values[field_name] = int(raw)
                else:
                    values[field_name] = float(raw)
            except ValueError:
                values[field_name] = default
        values.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**values)


# ---------------------------------------------------------------------------
# Worker-level lock — the ONLY concurrency primitive. A POSIX advisory lock
# the kernel releases automatically on process exit or crash (unlike a lock
# row, which a killed process would leave stuck). If it can't be taken,
# another worker is already running and this invocation is a clean no-op.
# ---------------------------------------------------------------------------

class WorkerBusy(RuntimeError):
    """Another worker process holds the lock — this invocation did nothing."""


@contextmanager
def worker_lock(path: Path = LOCK_PATH) -> Iterator[None]:
    import fcntl
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "w")
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as e:
            raise WorkerBusy(str(e)) from e
        try:
            fh.write(f"{os.getpid()} {iso(now_ist())}\n")
            fh.flush()
        except OSError:
            pass
        yield
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        fh.close()


# ---------------------------------------------------------------------------
# Worker bookmark — NOT research state. Two fields: when discovery may
# resume (cooldown), and when the last periodic Telegram summary went out.
# ---------------------------------------------------------------------------

def _load_state(path: Path = STATE_PATH) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(data: dict, path: Path = STATE_PATH) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, default=str))
        tmp.replace(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Result — every field docs/RESEARCH_WORKER.md's telemetry contract names,
# plus a few extras that make a log line self-describing.
# ---------------------------------------------------------------------------

class WorkerRunResult(NamedTuple):
    started_at: str
    finished_at: str
    runtime_seconds: float
    worker_id: str
    digest_as_of: Optional[str]
    work_selected: list          # e.g. ["experiments", "discovery"]  ([] == idle)
    proposals_created: int
    drafts: list                 # hypothesis_ids created this run
    duplicate_rejections: int
    experiments_run: int
    experiment_outcomes: list    # [{contract_id, outcome, detail}]
    evidence_updates: int        # hypotheses whose verdict was (re)recorded this run
    no_work_reason: Optional[str]
    errors: list                 # [str] — never raised out of run_worker_cycle()
    limits: dict
    notified: bool
    # Autonomous Research Control Plane v1 fields — appended AFTER `notified`
    # with defaults so every pre-existing positional WorkerRunResult(...)
    # construction (including in already-published tests) stays valid.
    opportunities_considered: int = 0
    promotions_attempted: int = 0
    promoted_hypothesis_ids: tuple = ()
    promotion_outcomes: tuple = ()       # [{opportunity_id, outcome, detail}]
    reassessment_eligible_count: int = 0
    # Unified Opportunity-Driven Worker Selection fields — appended AFTER
    # `reassessment_eligible_count`, again with defaults, for the same
    # positional-compatibility reason as the block above.
    action_log: tuple = ()
    """One record per action actually SELECTED and executed this heartbeat,
    in execution order — outcome-aware priority telemetry (not a dashboard
    payload, just enough to later calibrate the priority model from real
    outcomes). Each entry: kind, opportunity_id, hypothesis_id, contract_id,
    priority_score, priority_components, compute_cost_estimate, relevance,
    confidence_before/after, lifecycle_before/after, next_priority (the
    runner-up action's score at selection time, or None if it was the only
    eligible one), outcome, created_substrate_id (set only for a successful
    CREATE_EXPERIMENT), runtime_consumed_seconds. `*_after` fields are
    back-filled from the FOLLOWING iteration's fresh pool build, so the very
    last action of a heartbeat may show `*_after=None` — its effect is
    visible from the next heartbeat's `*_before` instead, never lost, just
    not yet observed."""
    # Autonomous Research Action Expansion fields — appended AFTER
    # `action_log`, again with defaults, same positional-compatibility
    # reason as every block above.
    substrate_creations_attempted: int = 0
    created_substrate_ids: tuple = ()          # new DRAFT contract_ids created this run
    substrate_creation_outcomes: tuple = ()    # [{opportunity_id, outcome, detail}]

    def as_row(self) -> dict:
        return dict(self._asdict())


# ---------------------------------------------------------------------------
# The cycle — bounded, sequential, delegating every real step. `runner` is
# injectable for exactly the reason overnight.run_overnight_cycle takes one:
# no test ever exercises the real `claude` subprocess.
# ---------------------------------------------------------------------------

def run_worker_cycle(
    store: Store,
    as_of: TimeLike,
    *,
    limits: Optional[WorkerLimits] = None,
    registry_dir: Path = REGISTRY_DIR,
    runner: Optional[Callable[[str], str]] = None,
    state_path: Path = STATE_PATH,
    now_fn: Callable[[], float] = time.monotonic,
) -> WorkerRunResult:
    """One heartbeat. Repeatedly selects and executes the single
    highest-priority ELIGIBLE action — RUN_EXPERIMENT, PROMOTE, or
    DISCOVER, ranked together on one scale by
    `opportunity.build_action_queue()` — until `limits.max_runtime_seconds`
    of wall time is spent or nothing eligible remains, and always returns a
    complete result: a component raising is caught, recorded in `errors`,
    and never propagated (see the per-kind handling below for exactly which
    failures stop the whole loop vs. only that one action).

    Unlike v1 (experiments, then discovery, then promotion, always in that
    fixed order), there is no privileged action kind here: a brand-new
    discovery attempt, an already-locked robustness test, and a rejected
    hypothesis whose evidence just turned in its favour all compete on the
    SAME priority score, every iteration, and the winner can change from one
    iteration to the next as soon as an action's own result changes the
    picture (e.g. a draft this cycle just created becomes, one iteration
    later, the single highest-priority PROMOTE candidate, and once promoted
    can itself become the highest-priority RUN_EXPERIMENT candidate — all
    inside the same heartbeat, if the runtime budget allows it). Each of the
    three per-kind caps (`max_experiments` / `max_discovery_attempts` /
    `max_promotions`) is still enforced exactly as before — this slice
    changes ONLY the order/selection policy within those existing bounds,
    never removes or raises them.
    """
    limits = limits or WorkerLimits.from_env()
    started_wall = now_ist()
    t0 = now_fn()
    worker_id = f"worker-{os.getpid()}-{int(time.time())}"

    work_selected: list = []
    errors: list = []
    proposals_created = 0
    drafts: list = []
    duplicate_rejections = 0
    experiments_run = 0
    experiment_outcomes: list = []
    touched_hids: set = set()
    no_work_reason: Optional[str] = None
    action_log: list = []

    opportunities_considered = 0
    promotions_attempted = 0
    promoted_hypothesis_ids: list = []
    promotion_outcomes: list = []
    reassessment_eligible_count = 0
    experiments_attempted = 0
    discovery_attempts = 0
    substrate_creations_attempted = 0
    created_substrate_ids: list = []
    substrate_creation_outcomes: list = []

    state = _load_state(state_path)
    now_epoch = time.time()
    cooldown_until = float(state.get("cooldown_until") or 0.0)
    in_cooldown = now_epoch < cooldown_until

    def budget_left() -> float:
        return limits.max_runtime_seconds - (now_fn() - t0)

    # -- build the digest once, reused by every discovery attempt this
    #    heartbeat (same as overnight.run_overnight_cycle) -----------------
    digest_as_of: Optional[str] = None
    digest: Optional[dict] = None
    try:
        digest = build_digest(store, as_of, registry_dir=registry_dir)
        digest_as_of = digest.get("as_of")
    except Exception as e:  # noqa: BLE001
        errors.append(f"digest: {type(e).__name__}: {e}")

    # -- discovery's own PRECONDITIONS, fixed for the whole heartbeat -------
    # (disabled / capped-at-zero / cooling down / no digest to work from).
    # Whether discovery is actually OFFERED on any given iteration ALSO
    # depends on whether its own attempt cap has already been reached this
    # heartbeat — that part varies per iteration, see `discovery_available`
    # inside the loop below.
    discovery_skip_reason: Optional[str] = None
    if not limits.discovery_enabled:
        discovery_skip_reason = "discovery disabled by config"
    elif limits.max_discovery_attempts <= 0:
        discovery_skip_reason = "max_discovery_attempts is 0"
    elif in_cooldown:
        discovery_skip_reason = (
            f"in cooldown until {dt.datetime.fromtimestamp(cooldown_until).isoformat()}")
    elif digest is None:
        discovery_skip_reason = "digest unavailable"
    discovery_precondition_ok = discovery_skip_reason is None
    dup_check = lambda p: _proposal_duplicate_of(p, registry_dir=registry_dir)  # noqa: E731

    def _has_room(kind: str) -> bool:
        if kind == "RUN_EXPERIMENT":
            return experiments_attempted < limits.max_experiments
        if kind == "PROMOTE":
            return promotions_attempted < limits.max_promotions
        if kind == "DISCOVER":
            return discovery_precondition_ok and discovery_attempts < limits.max_discovery_attempts
        if kind == "CREATE_EXPERIMENT":
            return substrate_creations_attempted < limits.max_substrate_creations
        return False

    _LABEL = {"RUN_EXPERIMENT": "experiments", "DISCOVER": "discovery", "PROMOTE": "promotion",
             "CREATE_EXPERIMENT": "substrate_creation"}

    # -- the unified, priority-driven action loop --------------------------
    # Every iteration: (1) refresh the opportunity pool, (2) rank every
    # currently-executable action on one scale, (3) execute the single
    # highest-priority eligible one, (4) record its result — then loop,
    # until the runtime budget is spent or nothing eligible remains. See
    # research.brain.opportunity.build_action_queue's own docstring for
    # exactly how RUN_EXPERIMENT/PROMOTE/DISCOVER/CREATE_EXPERIMENT are put
    # on one scale.
    iterations = 0
    max_iterations = (limits.max_experiments + limits.max_discovery_attempts
                      + limits.max_promotions + limits.max_substrate_creations + 3)
    # ^ a purely DEFENSIVE hard stop, not the real bounding mechanism — every
    # action already increments its own capped counter above, so ordinary
    # operation can never reach this; it exists only so a genuine bug can
    # never turn into an unbounded loop.

    while budget_left() > 0:
        iterations += 1
        if iterations > max_iterations:
            errors.append("action loop: iteration safety cap reached — stopping defensively")
            break

        try:
            pool = opp.build_opportunity_pool(store, as_of, registry_dir=registry_dir)
        except Exception as e:  # noqa: BLE001
            errors.append(f"opportunity_pool: {type(e).__name__}: {e}")
            pool = []
        opportunities_considered = len(pool)
        reassessment_eligible_count = sum(1 for o in pool if o.reassessment_eligible)
        by_opp_id = {o.id: o for o in pool}

        # Back-fill the PREVIOUS action's after-state now that a fresh pool
        # exists — this is how action_log shows a lifecycle/confidence
        # change caused by the action that just ran (see WorkerRunResult's
        # own docstring for the one case this cannot reach: the very last
        # action of a heartbeat).
        if action_log:
            _prev = action_log[-1]
            if _prev.get("confidence_after") is None and _prev.get("opportunity_id"):
                _prev_opp = by_opp_id.get(_prev["opportunity_id"])
                if _prev_opp is not None:
                    _prev["confidence_after"] = _prev_opp.confidence
                    _prev["lifecycle_after"] = _prev_opp.lifecycle_stage

        discovery_available = _has_room("DISCOVER")
        try:
            queue = opp.build_action_queue(
                store, pool, registry_dir=registry_dir, discovery_available=discovery_available)
        except Exception as e:  # noqa: BLE001
            errors.append(f"action_queue: {type(e).__name__}: {e}")
            queue = []

        eligible_actions = [a for a in queue if _has_room(a.kind)]
        if not eligible_actions:
            break

        action = eligible_actions[0]
        next_priority = eligible_actions[1].priority_score if len(eligible_actions) > 1 else None
        label = _LABEL[action.kind]
        if label not in work_selected:
            work_selected.append(label)

        record = {
            "kind": action.kind, "opportunity_id": action.opportunity_id,
            "contract_id": action.contract_id, "priority_score": action.priority_score,
            "priority_components": action.priority_components,
            "compute_cost_estimate": action.compute_cost_estimate, "relevance": action.relevance,
            "confidence_before": action.confidence,
            "lifecycle_before": (by_opp_id[action.opportunity_id].lifecycle_stage
                                 if action.opportunity_id in by_opp_id else None),
            "confidence_after": None, "lifecycle_after": None,
            "next_priority": next_priority, "outcome": None, "runtime_consumed_seconds": None,
            "created_substrate_id": None,
        }
        t_action_start = now_fn()

        if action.kind == "RUN_EXPERIMENT":
            try:
                outcome = sched.run_one_experiment(store, action.contract_id, registry_dir=registry_dir)
            except Exception as e:  # noqa: BLE001
                # An unexpected (non-runner) failure — the same signal that
                # would have halted a batch run_scheduler() call early; stop
                # selecting further actions this heartbeat rather than keep
                # hammering a Store/registry that just failed unexpectedly.
                errors.append(f"run_one_experiment: {type(e).__name__}: {e}")
                record["outcome"] = "error"
                record["runtime_consumed_seconds"] = round(now_fn() - t_action_start, 4)
                action_log.append(record)
                break
            experiments_attempted += 1
            experiments_run += 1
            experiment_outcomes.append(
                {"contract_id": outcome.contract_id, "outcome": outcome.outcome, "detail": outcome.detail})
            record["outcome"] = outcome.outcome
            if outcome.outcome == "reported":
                # Evidence feedback: a REPORTED experiment recorded a
                # verdict for its hypothesis (evaluator.record_verdict,
                # inside run_experiment) — count the distinct hypothesis.
                try:
                    hyp = evaluator.resolve_hypothesis_id(store, outcome.contract_id)
                except Exception:  # noqa: BLE001
                    hyp = None
                touched_hids.add(hyp or outcome.contract_id)

        elif action.kind == "PROMOTE":
            promotions_attempted += 1
            target_opp = by_opp_id.get(action.opportunity_id)
            if target_opp is None:
                record["outcome"] = "skipped_not_eligible"  # defensive — should not occur;
                # build_action_queue() only ever builds a PROMOTE action from
                # an Opportunity already present in this SAME pool.
            else:
                try:
                    po = opp.attempt_autonomous_promotion(
                        store, target_opp, registry_dir=registry_dir,
                        contract_id=action.contract_id)
                except Exception as e:  # noqa: BLE001 — this primitive is documented to
                    # already reduce every failure to a PromotionOutcome; treat an
                    # unexpected raise as an isolated glitch, not a systemic one.
                    errors.append(f"promotion: {type(e).__name__}: {e}")
                    record["outcome"] = "error"
                else:
                    promotion_outcomes.append(
                        {"opportunity_id": po.opportunity_id, "outcome": po.outcome, "detail": po.detail})
                    record["outcome"] = po.outcome
                    if po.outcome == "promoted":
                        promoted_hypothesis_ids.append(po.hypothesis_id)

        elif action.kind == "DISCOVER":
            discovery_attempts += 1
            try:
                result = inv.investigate(
                    store, as_of, runner=runner, registry_dir=registry_dir,
                    digest=digest, duplicate_check=dup_check)
            except (inv.InvestigatorError, hi.IntakeRejected) as e:
                errors.append(f"discovery: {type(e).__name__}: {str(e)[:200]}")
                record["outcome"] = "error"
            except Exception as e:  # noqa: BLE001
                errors.append(f"discovery: {type(e).__name__}: {str(e)[:200]}")
                record["outcome"] = "error"
            else:
                if isinstance(result, inv.DuplicateProposal):
                    duplicate_rejections += 1
                    record["outcome"] = "duplicate"
                elif isinstance(result, inv.NoProposal):
                    record["outcome"] = "no_proposal"
                elif getattr(result, "hypothesis_id", None):
                    proposals_created += 1
                    drafts.append(result.hypothesis_id)
                    record["outcome"] = "draft_created"
                else:
                    record["outcome"] = "no_proposal"

        elif action.kind == "CREATE_EXPERIMENT":
            substrate_creations_attempted += 1
            target_opp = by_opp_id.get(action.opportunity_id)
            if target_opp is None:
                record["outcome"] = "skipped_not_eligible"  # defensive — should not occur;
                # build_action_queue() only ever builds a CREATE_EXPERIMENT
                # action from an Opportunity already present in this SAME pool.
            else:
                try:
                    so = opp.attempt_create_experiment(store, target_opp, registry_dir=registry_dir)
                except Exception as e:  # noqa: BLE001 — this primitive is documented to
                    # already reduce every failure to a SubstrateOutcome; treat an
                    # unexpected raise as an isolated glitch, not a systemic one.
                    errors.append(f"substrate_creation: {type(e).__name__}: {e}")
                    record["outcome"] = "error"
                else:
                    substrate_creation_outcomes.append(
                        {"opportunity_id": so.opportunity_id, "outcome": so.outcome, "detail": so.detail})
                    record["outcome"] = so.outcome
                    if so.outcome == "created":
                        created_substrate_ids.append(so.created_contract_id)
                        record["created_substrate_id"] = so.created_contract_id

        record["runtime_consumed_seconds"] = round(now_fn() - t_action_start, 4)
        action_log.append(record)

    evidence_updates = len(touched_hids)

    # -- cooldown / no-work bookkeeping -------------------------------------
    did_useful_work = (
        experiments_run > 0 or proposals_created > 0 or len(promoted_hypothesis_ids) > 0
        or len(created_substrate_ids) > 0
    )
    new_state = dict(state)
    if did_useful_work:
        new_state.pop("cooldown_until", None)
    elif not errors:
        # Nothing useful and nothing broke -> back off discovery for a while.
        new_state["cooldown_until"] = time.time() + limits.cooldown_seconds
        if not work_selected:
            if budget_left() <= 0:
                no_work_reason = "runtime budget exhausted before any work could start"
            else:
                no_work_reason = discovery_skip_reason or "no eligible experiments and no discovery work"
        elif "discovery" in work_selected and proposals_created == 0 and duplicate_rejections == 0:
            no_work_reason = "discovery produced no new proposal"
        elif "experiments" in work_selected and experiments_run == 0:
            no_work_reason = "eligible experiments found but none could be attempted"
        elif "promotion" in work_selected and not promoted_hypothesis_ids:
            no_work_reason = (
                f"{promotions_attempted} promotion attempt(s), none succeeded — "
                f"{'; '.join(o['outcome'] for o in promotion_outcomes) or 'no eligible candidates'}")
        elif "substrate_creation" in work_selected and not created_substrate_ids:
            no_work_reason = (
                f"{substrate_creations_attempted} substrate-creation attempt(s), none succeeded — "
                f"{'; '.join(o['outcome'] for o in substrate_creation_outcomes) or 'no eligible candidates'}")
        else:
            no_work_reason = "no useful work this cycle"
    if not work_selected and not no_work_reason:
        no_work_reason = discovery_skip_reason or "idle"

    _save_state(new_state, state_path)

    finished_wall = now_ist()
    runtime = round(now_fn() - t0, 3)

    return WorkerRunResult(
        started_at=iso(started_wall), finished_at=iso(finished_wall),
        runtime_seconds=runtime, worker_id=worker_id, digest_as_of=digest_as_of,
        work_selected=work_selected, proposals_created=proposals_created, drafts=drafts,
        duplicate_rejections=duplicate_rejections, experiments_run=experiments_run,
        experiment_outcomes=experiment_outcomes, evidence_updates=evidence_updates,
        no_work_reason=no_work_reason, errors=errors, limits=asdict(limits),
        notified=False, opportunities_considered=opportunities_considered,
        promotions_attempted=promotions_attempted,
        promoted_hypothesis_ids=tuple(promoted_hypothesis_ids),
        promotion_outcomes=tuple(promotion_outcomes),
        reassessment_eligible_count=reassessment_eligible_count,
        action_log=tuple(action_log),
        substrate_creations_attempted=substrate_creations_attempted,
        created_substrate_ids=tuple(created_substrate_ids),
        substrate_creation_outcomes=tuple(substrate_creation_outcomes),
    )


# ---------------------------------------------------------------------------
# Telemetry + notification
# ---------------------------------------------------------------------------

def _persist_run(result: WorkerRunResult, *, run_log: Path = RUN_LOG) -> None:
    """Append one JSONL row — a worker heartbeat's own record, the same
    shape and purpose as research/overnight_runs.jsonl and
    research/recorder_runs.jsonl. A quiet heartbeat and a broken one look
    different here even when they look the same from outside."""
    try:
        run_log.parent.mkdir(parents=True, exist_ok=True)
        with run_log.open("a") as f:
            f.write(json.dumps(result.as_row(), default=str) + "\n")
    except OSError:
        pass


def _notify(message: str) -> bool:
    """Best-effort Telegram — identical guarded pattern to
    research/overnight.py._notify(). A failure to send never affects the
    run (every write is already durable by the time this is called)."""
    try:
        sys.path.insert(0, str(RESEARCH_DIR.parent / "scripts"))
        from telegram_notify import send_message  # type: ignore
        send_message(message)
        return True
    except Exception:  # noqa: BLE001
        return False


def maybe_notify(result: WorkerRunResult, *, limits: WorkerLimits,
                 state_path: Path = STATE_PATH, now: Optional[float] = None) -> bool:
    """Bounded notification policy (docs/RESEARCH_WORKER.md 'Telegram'):

      * a new DRAFT proposal   -> notify (that is the meaningful event)
      * an error this cycle    -> notify
      * otherwise              -> silent, EXCEPT one periodic roll-up at most
                                  once every `summary_interval_seconds` so a
                                  long quiet stretch still produces a heartbeat

    A routine "ran an experiment, nothing else" cycle sends nothing. The
    first heartbeat after a (re)deploy only *seeds* the summary clock — the
    first periodic roll-up lands one interval later, never as a deploy burst.
    """
    now = now if now is not None else time.time()
    state = _load_state(state_path)
    if "last_summary_at" not in state:
        state["last_summary_at"] = now
        _save_state(state, state_path)

    lines: list = []
    if result.proposals_created:
        lines.append(
            f"🔬 Research worker: {result.proposals_created} new DRAFT hypothesis(es) "
            f"for review — proposals only, not validated, not locked, not tradeable: "
            f"{', '.join(result.drafts)}")
    if result.errors:
        lines.append("⚠️ Research worker errors: " + " | ".join(result.errors[:5]))

    is_summary = False
    _last_summary = float(state.get("last_summary_at") or now)
    if not lines and (now - _last_summary) >= limits.summary_interval_seconds:
        is_summary = True
        lines.append(
            f"🔬 Research worker (periodic): last {_fmt_interval(limits.summary_interval_seconds)} — "
            f"experiments run, drafts created and errors are logged to "
            f"research/worker_runs.jsonl. This cycle: "
            f"{result.experiments_run} experiment(s), {result.proposals_created} draft(s), "
            f"{len(result.errors)} error(s).")

    if not lines:
        return False

    sent = _notify("\n".join(lines))
    if sent and (result.proposals_created or is_summary):
        state["last_summary_at"] = now
        _save_state(state, state_path)
    return sent


def _fmt_interval(seconds: float) -> str:
    h = seconds / 3600.0
    return f"{h:.0f}h" if h >= 1 else f"{seconds/60:.0f}m"


# ---------------------------------------------------------------------------
# Status — a lightweight read-only view of the worker's own telemetry. No
# Store, no lock, no side effect: it only reads worker_runs.jsonl and the
# cooldown bookmark. Answers docs/RESEARCH_WORKER.md's observability
# questions (when did the last heartbeat run, what did it attempt, did
# discovery / an experiment run, what stopped it, was there an error)
# without a dashboard.
# ---------------------------------------------------------------------------

def worker_status(*, run_log: Path = RUN_LOG, state_path: Path = STATE_PATH,
                  tail: int = 5, now_epoch: Optional[float] = None) -> dict:
    now_epoch = now_epoch if now_epoch is not None else time.time()
    rows: list = []
    try:
        for line in run_log.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        rows = []

    state = _load_state(state_path)
    cooldown_until = float(state.get("cooldown_until") or 0.0)
    last = rows[-1] if rows else None

    def _cycle_label(r: dict) -> str:
        if r.get("errors"):
            return "error"
        ws = r.get("work_selected") or []
        if not ws:
            return "idle"
        bits = []
        if "experiments" in ws:
            bits.append(f"exp×{r.get('experiments_run', 0)}")
        if "discovery" in ws:
            bits.append(f"draft×{r.get('proposals_created', 0)}"
                        if r.get("proposals_created") else "disc")
        if "promotion" in ws:
            promoted = len(r.get("promoted_hypothesis_ids") or [])
            bits.append(f"promo×{promoted}" if promoted else "promo-skip")
        if "substrate_creation" in ws:
            created = len(r.get("created_substrate_ids") or [])
            bits.append(f"subst×{created}" if created else "subst-skip")
        return "+".join(bits) or "idle"

    limits = (last or {}).get("limits") or {}
    max_rt = float(limits.get("max_runtime_seconds") or 0.0)
    last_rt = float((last or {}).get("runtime_seconds") or 0.0)

    return {
        "telemetry_file": str(run_log),
        "heartbeats_recorded": len(rows),
        "last_heartbeat_at": (last or {}).get("finished_at"),
        "last_runtime_seconds": last_rt,
        "last_work_selected": (last or {}).get("work_selected") or [],
        "last_experiments_run": (last or {}).get("experiments_run"),
        "last_proposals_created": (last or {}).get("proposals_created"),
        "last_duplicate_rejections": (last or {}).get("duplicate_rejections"),
        "last_opportunities_considered": (last or {}).get("opportunities_considered"),
        "last_promotions_attempted": (last or {}).get("promotions_attempted"),
        "last_promoted_hypothesis_ids": (last or {}).get("promoted_hypothesis_ids") or [],
        "last_reassessment_eligible_count": (last or {}).get("reassessment_eligible_count"),
        "last_substrate_creations_attempted": (last or {}).get("substrate_creations_attempted"),
        "last_created_substrate_ids": (last or {}).get("created_substrate_ids") or [],
        "last_no_work_reason": (last or {}).get("no_work_reason"),
        "last_errors": (last or {}).get("errors") or [],
        "last_runtime_budget_remaining_seconds": (round(max_rt - last_rt, 1)
                                                  if max_rt else None),
        "recent_cycles": [_cycle_label(r) for r in rows[-tail:]],
        "recent_errors": [e for r in rows[-tail:] for e in (r.get("errors") or [])],
        "discovery_cooldown_active": now_epoch < cooldown_until,
        "discovery_cooldown_until": (dt.datetime.fromtimestamp(cooldown_until).isoformat()
                                     if cooldown_until else None),
        "last_summary_at": state.get("last_summary_at"),
        "effective_limits_last_run": limits,
    }


def _print_status(st: dict) -> None:
    print("Research worker status")
    hb = st["last_heartbeat_at"] or "never"
    print(f"  last heartbeat        : {hb}  (runtime {st['last_runtime_seconds']}s, "
          f"{st['heartbeats_recorded']} recorded)")
    print(f"  last cycle            : work_selected={st['last_work_selected'] or '[]'}  "
          f"experiments_run={st['last_experiments_run']}  "
          f"proposals_created={st['last_proposals_created']}  "
          f"duplicates={st['last_duplicate_rejections']}")
    if st["last_no_work_reason"]:
        print(f"  last no_work_reason   : {st['last_no_work_reason']}")
    print(f"  control plane         : opportunities_considered="
          f"{st['last_opportunities_considered']}  promotions_attempted="
          f"{st['last_promotions_attempted']}  promoted={st['last_promoted_hypothesis_ids']}  "
          f"reassessment_eligible={st['last_reassessment_eligible_count']}")
    print(f"  substrate creation    : attempted="
          f"{st['last_substrate_creations_attempted']}  created={st['last_created_substrate_ids']}")
    rem = st["last_runtime_budget_remaining_seconds"]
    if rem is not None:
        print(f"  runtime budget left   : {rem}s of "
              f"{st['effective_limits_last_run'].get('max_runtime_seconds')}s")
    if st["discovery_cooldown_active"]:
        print(f"  discovery             : IN COOLDOWN until {st['discovery_cooldown_until']}")
    else:
        print(f"  discovery             : active (no cooldown)")
    print(f"  recent cycles         : {st['recent_cycles'] or '[]'}")
    if st["recent_errors"]:
        print(f"  recent errors         : {st['recent_errors']}")
    else:
        print(f"  recent errors         : none")
    print(f"  telemetry             : {st['telemetry_file']}")


# ---------------------------------------------------------------------------
# CLI — one heartbeat, then exit. No daemon mode, by design.
# ---------------------------------------------------------------------------

def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Continuous Research Worker v1 — one bounded slice of research "
                    "work, then exit. Invoke from cron as a heartbeat "
                    "(docs/RESEARCH_WORKER.md, docs/RESEARCH_CONTROL_PLANE.md). May "
                    "autonomously lock a DRAFT (research.brain.opportunity, an "
                    "explicit system approver + the existing research budget) — "
                    "never trades, never touches a broker or live state.")
    ap.add_argument("--db", default=None)
    ap.add_argument("--registry-dir", default=None)
    ap.add_argument("--max-discovery-attempts", type=int, default=None)
    ap.add_argument("--max-experiments", type=int, default=None)
    ap.add_argument("--max-promotions", type=int, default=None,
                    help="max DRAFT hypotheses to autonomously promote (lock) this "
                         "heartbeat (default 1; 0 disables autonomous promotion)")
    ap.add_argument("--max-substrate-creations", type=int, default=None,
                    help="max research-substrate creations (a validation/holdout split "
                         "derived for a high-priority opportunity with no runnable "
                         "experiment) this heartbeat (default 1; 0 disables it)")
    ap.add_argument("--max-runtime-seconds", type=float, default=None)
    ap.add_argument("--cooldown-seconds", type=float, default=None)
    ap.add_argument("--no-discovery", action="store_true",
                    help="skip the Research AI step this run (experiments only)")
    ap.add_argument("--no-notify", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet-on-success", action="store_true",
                    help="only print when something went wrong (for cron)")
    ap.add_argument("--status", action="store_true",
                    help="print a read-only summary of recent heartbeats "
                         "(from research/worker_runs.jsonl) and exit — no work is done")
    args = ap.parse_args(argv)

    if args.status:
        st = worker_status()
        if args.json:
            print(json.dumps(st, indent=2, default=str))
        else:
            _print_status(st)
        return 0

    overrides = {
        "max_discovery_attempts": args.max_discovery_attempts,
        "max_experiments": args.max_experiments,
        "max_promotions": args.max_promotions,
        "max_substrate_creations": args.max_substrate_creations,
        "max_runtime_seconds": args.max_runtime_seconds,
        "cooldown_seconds": args.cooldown_seconds,
    }
    if args.no_discovery:
        overrides["discovery_enabled"] = False
    limits = WorkerLimits.from_env(**overrides)

    registry_dir = Path(args.registry_dir) if args.registry_dir else REGISTRY_DIR

    try:
        with worker_lock():
            try:
                store = Store.open(args.db) if args.db else Store.open()
            except Exception as e:  # noqa: BLE001
                print(f"FATAL: could not open the research store: {e}", file=sys.stderr)
                if not args.no_notify:
                    _notify(f"🔴 Research worker: could not open the research store — "
                            f"{type(e).__name__}: {e}")
                return 1

            try:
                result = run_worker_cycle(
                    store, now_ist(), limits=limits, registry_dir=registry_dir)
            finally:
                store.close()

            _persist_run(result)
            notified = False
            if not args.no_notify:
                notified = maybe_notify(result, limits=limits)
            result = result._replace(notified=notified)

            if args.json:
                print(json.dumps(result.as_row(), indent=2, default=str))
            elif not (args.quiet_on_success and not result.errors):
                print(f"research worker {result.started_at} .. {result.finished_at} "
                      f"({result.runtime_seconds}s)")
                print(f"  work_selected={result.work_selected or '[]'} "
                      f"experiments_run={result.experiments_run} "
                      f"proposals_created={result.proposals_created} "
                      f"duplicate_rejections={result.duplicate_rejections} "
                      f"evidence_updates={result.evidence_updates}")
                print(f"  opportunities_considered={result.opportunities_considered} "
                      f"promotions_attempted={result.promotions_attempted} "
                      f"promoted={list(result.promoted_hypothesis_ids)} "
                      f"reassessment_eligible={result.reassessment_eligible_count}")
                print(f"  substrate_creations_attempted={result.substrate_creations_attempted} "
                      f"created_substrate_ids={list(result.created_substrate_ids)}")
                if result.no_work_reason:
                    print(f"  no_work_reason: {result.no_work_reason}")
                for e in result.errors:
                    print(f"  error: {e}")
            return 1 if result.errors else 0
    except WorkerBusy:
        # Another worker is already running — a clean, expected no-op.
        if not args.quiet_on_success:
            print("research worker: another instance holds the lock — nothing to do")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
