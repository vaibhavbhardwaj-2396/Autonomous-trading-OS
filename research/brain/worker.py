"""
research/brain/worker.py — Continuous Research Worker v0.

THE GAP THIS CLOSES
-------------------
The research pipeline was only alive at 01:00 (`research/overnight.py`).
Everything downstream of a human's `approve_and_lock()` — actually running
the locked experiments, folding their verdicts back into the evidence the
next digest reads — had no cron at all. This module makes the research loop
*continuously* alive:

    data ingestion (research/recorder.py, unchanged)
        │
        ▼
    research state / digest  ── digest.build_digest() ──┐
        │                                               │
        ▼                                               │
    bounded discovery work   ── investigator.investigate()   (proposal-only)
        │                                               │
        ▼                                               │
    DRAFT hypotheses  (hypothesis_intake.create_draft, via investigate)
        │                                               │
        ▼                                               │
    priority selection ── priority.rank_experiments() (via scheduler)
        │                                               │
        ▼                                               │
    bounded experiments ── scheduler.run_scheduler() ── runner.run_experiment()
        │                                               │
        ▼                                               │
    evidence  ── evaluator.record_verdict() (inside run_experiment) ──────────┘
        │
        ▼
    memory  ── research.memory / the research Store (the ONE source of truth)

CADENCE MODEL: cron is a **heartbeat**, this module is the **brain**.
`main()` runs exactly one bounded slice of work and exits. Nothing here
loops, sleeps, daemonises, or schedules itself. A cron line every 5–15
minutes invokes it; the worker decides each time whether any useful work
exists and does at most a configured amount of it. See
`docs/RESEARCH_WORKER.md` for the recommended cron.

REUSE, DON'T REIMPLEMENT — every real step is an existing, unmodified component:
    digest                      -> research.brain.digest.build_digest
    discovery (AI proposal)     -> research.brain.investigator.investigate
    duplicate detection         -> research.overnight._existing_duplicate
                                   (itself just research...comparison._rule_fingerprint,
                                   the same check research/brain/similarity.py uses)
    experiment selection/order  -> research.brain.scheduler + research.brain.priority
    running one experiment      -> research.experiments.runner.run_experiment
    verdict / evidence feedback -> research.experiments.evaluator (inside run_experiment)
    provenance                  -> research.memory.record_discovery_search (inside investigate)
    research budgets / areas    -> enforced inside the components above, untouched here

WHAT THIS MODULE NEVER DOES — structurally true by absence of the import:
  - approve or lock a hypothesis. `hypothesis_intake.approve_and_lock` and
    `Contract.lock` are never imported. Discovery ends at DRAFT; a human
    still approves before anything is locked.
  - touch engine/*, memory/state.json, or a broker. `engine.execute`,
    `engine.guardrails`, `engine.broker*` are never imported. `research/`
    already never imports a broker (tests/test_broker_probe.py) and
    `engine` never imports `research` (tests/test_kernel_isolation.py) —
    this module adds no edge to either graph. Safe to run during market
    hours precisely because the separation is at the import boundary, not
    a scheduling convention.
  - create a second source of truth. The only durable state it writes is
    (a) `research/worker_runs.jsonl` — operational telemetry, exactly like
    `overnight_runs.jsonl` / `recorder_runs.jsonl`; (b) `research/.worker_state.json`
    — a two-field cooldown/summary bookmark. Neither holds research data;
    the research Store remains the sole authority.

RESOURCE SAFETY — one process, one experiment at a time, bounded runtime,
no unbounded scan. All limits are in `WorkerLimits` (env-overridable), not
scattered constants. Overlapping cron triggers are made safe by a single
POSIX file lock (`research/.worker.lock`); a second concurrent invocation
exits immediately without doing anything.
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

    ENV = {
        "max_discovery_attempts": "RESEARCH_WORKER_MAX_DISCOVERY_ATTEMPTS",
        "max_experiments": "RESEARCH_WORKER_MAX_EXPERIMENTS",
        "max_runtime_seconds": "RESEARCH_WORKER_MAX_RUNTIME_SECONDS",
        "max_concurrent_experiments": "RESEARCH_WORKER_MAX_CONCURRENT_EXPERIMENTS",
        "cooldown_seconds": "RESEARCH_WORKER_COOLDOWN_SECONDS",
        "discovery_enabled": "RESEARCH_WORKER_DISCOVERY_ENABLED",
        "summary_interval_seconds": "RESEARCH_WORKER_SUMMARY_INTERVAL_SECONDS",
    }

    def __post_init__(self) -> None:
        if self.max_concurrent_experiments != 1:
            raise ValueError(
                "WorkerLimits.max_concurrent_experiments must be 1 in v0 — parallel "
                "experiments are deliberately out of scope until measured safe on the "
                "shared VPS (see docs/RESEARCH_WORKER.md 'Resource safety')")
        for name in ("max_discovery_attempts", "max_experiments"):
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
    """One heartbeat. Runs at most `limits.max_experiments` locked
    experiments and at most `limits.max_discovery_attempts` discovery
    attempts, stops starting new work once `limits.max_runtime_seconds` of
    wall time has elapsed, and always returns a complete result — a
    component raising is caught, recorded in `errors`, and never propagated.

    Order is deliberate: experiments first (cheap, deterministic, and a
    human may have just locked one), then discovery (may shell out to the
    Research AI, up to 5 min). Discovery is skipped entirely while a
    cooldown from a previous "no useful work" tick is still in effect.
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
    evidence_updates = 0
    no_work_reason: Optional[str] = None

    state = _load_state(state_path)
    now_epoch = time.time()
    cooldown_until = float(state.get("cooldown_until") or 0.0)
    in_cooldown = now_epoch < cooldown_until

    def budget_left() -> float:
        return limits.max_runtime_seconds - (now_fn() - t0)

    # -- build the digest once, reused by every discovery attempt (same as
    #    overnight.run_overnight_cycle) -----------------------------------
    digest_as_of: Optional[str] = None
    digest: Optional[dict] = None
    try:
        digest = build_digest(store, as_of, registry_dir=registry_dir)
        digest_as_of = digest.get("as_of")
    except Exception as e:  # noqa: BLE001
        errors.append(f"digest: {type(e).__name__}: {e}")

    # -- 1. bounded experiments -------------------------------------------
    if limits.max_experiments > 0 and budget_left() > 0:
        try:
            eligible = sched.eligible_contracts(store, registry_dir=registry_dir)
        except Exception as e:  # noqa: BLE001
            eligible = []
            errors.append(f"eligible_contracts: {type(e).__name__}: {e}")
        if eligible:
            work_selected.append("experiments")
            try:
                sr = sched.run_scheduler(
                    store, registry_dir=registry_dir,
                    max_experiments=limits.max_experiments)
                experiments_run = len(sr.attempted_contract_ids)
                experiment_outcomes = [
                    {"contract_id": o.contract_id, "outcome": o.outcome, "detail": o.detail}
                    for o in sr.outcomes
                ]
                # Evidence feedback: every REPORTED experiment recorded a
                # verdict for its hypothesis (evaluator.record_verdict, inside
                # run_experiment). Count the distinct hypotheses touched.
                touched: set = set()
                for cid in sr.reported_contract_ids:
                    try:
                        hyp = evaluator.resolve_hypothesis_id(store, cid)
                    except Exception:  # noqa: BLE001
                        hyp = None
                    touched.add(hyp or cid)
                evidence_updates = len(touched)
                if sr.error:
                    errors.append(f"scheduler: {sr.error}")
            except Exception as e:  # noqa: BLE001
                errors.append(f"run_scheduler: {type(e).__name__}: {e}")

    # -- 2. bounded discovery -------------------------------------------------
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
    elif budget_left() <= 0:
        discovery_skip_reason = "runtime budget exhausted before discovery"

    if discovery_skip_reason is None:
        work_selected.append("discovery")
        dup_check = lambda p: _proposal_duplicate_of(p, registry_dir=registry_dir)  # noqa: E731
        for _ in range(limits.max_discovery_attempts):
            if budget_left() <= 0:
                errors.append("discovery: stopped — runtime budget exhausted mid-loop")
                break
            try:
                result = inv.investigate(
                    store, as_of, runner=runner, registry_dir=registry_dir,
                    digest=digest, duplicate_check=dup_check)
            except (inv.InvestigatorError, hi.IntakeRejected) as e:
                errors.append(f"discovery: {type(e).__name__}: {str(e)[:200]}")
                continue
            except Exception as e:  # noqa: BLE001
                errors.append(f"discovery: {type(e).__name__}: {str(e)[:200]}")
                continue

            if isinstance(result, inv.DuplicateProposal):
                duplicate_rejections += 1
            elif isinstance(result, inv.NoProposal):
                pass
            elif getattr(result, "hypothesis_id", None):
                proposals_created += 1
                drafts.append(result.hypothesis_id)

    # -- cooldown / no-work bookkeeping -------------------------------------
    did_useful_work = experiments_run > 0 or proposals_created > 0
    new_state = dict(state)
    if did_useful_work:
        new_state.pop("cooldown_until", None)
    elif not errors:
        # Nothing useful and nothing broke -> back off discovery for a while.
        new_state["cooldown_until"] = time.time() + limits.cooldown_seconds
        if not work_selected:
            no_work_reason = discovery_skip_reason or "no eligible experiments and no discovery work"
        elif "discovery" in work_selected and proposals_created == 0 and duplicate_rejections == 0:
            no_work_reason = "discovery produced no new proposal"
        elif "experiments" in work_selected and experiments_run == 0:
            no_work_reason = "eligible experiments found but none could be attempted"
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
        notified=False,
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
        description="Continuous Research Worker v0 — one bounded slice of research "
                    "work, then exit. Invoke from cron as a heartbeat "
                    "(docs/RESEARCH_WORKER.md). PROPOSAL-ONLY: never locks a "
                    "hypothesis, never trades, never touches a broker or live state.")
    ap.add_argument("--db", default=None)
    ap.add_argument("--registry-dir", default=None)
    ap.add_argument("--max-discovery-attempts", type=int, default=None)
    ap.add_argument("--max-experiments", type=int, default=None)
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
