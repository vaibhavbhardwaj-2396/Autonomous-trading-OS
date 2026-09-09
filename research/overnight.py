"""
Overnight Autonomous Research Cadence — Phase 2 Slice P.

    01:00 IST cron entry, its own — NEVER inside run_cycle.sh
          |
          v
    main() / run_overnight_cycle()
          |
          v
    as_of = now_ist()                  "what does research memory know right
          |                            now" — this run has no backtest
          |                            horizon of its own to inherit.
          v
    store = Store.open()
          |
          v
    digest = build_digest(store, as_of)    built EXACTLY ONCE for the whole
          |                                run and reused by every attempt
          |                                below — see MAX_PROPOSAL_ATTEMPTS_
          |                                PER_RUN and run_overnight_cycle()'s
          |                                own docstring for why.
          v
    up to MAX_PROPOSAL_ATTEMPTS_PER_RUN bounded attempts, each one:
        research.brain.investigator.investigate(
            store, as_of, digest=digest, duplicate_check=_existing_duplicate)
          |
          v
    InvestigatorResult (a genuine new DRAFT + Slice N provenance, both via
                         the existing, UNMODIFIED create_draft()/
                         record_discovery_search() machinery)
    | NoProposal          (the AI declined — normal, not a failure)
    | DuplicateProposal   (would exactly duplicate an existing Contract —
                            caught BEFORE create_draft() is ever called,
                            reusing research.experiments.comparison.
                            _rule_fingerprint(), the same function Slice L's
                            global duplicate detection already uses)
    | hi.IntakeRejected   (malformed/invalid proposal — caught, logged,
                            does not abort the run)
    | inv.InvestigatorError (AI timeout/malformed output/boundary failure —
                            caught, logged, does not abort the run)
          |
          v
    this module's own JSONL run log (research/overnight_runs.jsonl — same
    convention as research/recorder_runs.jsonl) + one best-effort Telegram
    notification (same _notify() pattern research/recorder.py already uses)
          |
          v
    exit 0 — even a run producing zero drafts is a successful run, exactly
             like the recorder's own "0 new rows" is not a failure. exit 1
             is reserved for the run failing OUTRIGHT (the Store could not
             even be opened) — never for "the Research AI had nothing to
             propose tonight."

VERY IMPORTANT SAFETY BOUNDARY — PROPOSAL ONLY, enforced structurally, the
same "true by absence" pattern every research/brain module already uses:
this file imports nothing from engine/, never imports or references
approve_and_lock() or Contract.lock() (hypothesis_intake.py's OWN sole
`.lock()` call site is never reached from here — this module never calls
approve_and_lock at all), never imports research.experiments.runner or
references run_experiment(), never opens memory/state.json, and never
touches broker credentials. A successful attempt in this file can end in
exactly one place: a Contract at status="draft". Nothing beyond DRAFT.

Never run from run_cycle.sh, and never sharing a log with it, for the same
reason research/recorder.py already gives for its own cron independence: a
stuck or erroring overnight research run must never be able to delay or
abort a trading cycle, and a trading-cycle failure must never silently skip
a night's research.

Permission boundary: this module launches the Research AI exactly the way
research/brain/investigator.py's own `_default_runner` already does (the
default `runner=None` falls through to that same function) — same prompt
file, same narrowed `--add-dir research --add-dir routines`, same shared
`.claude/settings.json` deny list established in Slice J. Nothing here
constructs a different Claude Code invocation with different permissions,
and this slice does not modify `.claude/settings.json` at all.
"""

from __future__ import annotations

import sys
import json
import argparse
from pathlib import Path
from typing import NamedTuple, Optional

from .store import Store, TimeLike, now_ist, iso
from .contracts import Contract, REGISTRY_DIR, registry as _registry
from .experiments.comparison import _rule_fingerprint
from .brain import hypothesis_intake as hi
from .brain import investigator as inv
from .brain.digest import build_digest

LOG_DIR = Path(__file__).parent.parent / "logs"
RUN_LOG = Path(__file__).parent / "overnight_runs.jsonl"

MAX_PROPOSAL_ATTEMPTS_PER_RUN = 3
"""Small, explicit, and deliberately conservative — this slice does not
assume a production-tuned number is known yet (the same posture Slice O's
MAX_LOCKS_PER_PERIOD takes for the research budget). Each attempt is one
Research AI subprocess call; this bounds a single overnight run's total
cost and blast radius regardless of how many attempts end in NoProposal,
DuplicateProposal, or a rejection. Not a queue, not a scheduler — a flat
per-run cap, checked with a plain `range()` loop."""


class AttemptOutcome(NamedTuple):
    """One bounded attempt's result, already reduced to a JSON-serializable
    shape for the run log and the notification — never the raw AI output
    (that stays in research memory via the normal provenance/hypothesis-
    claim rows Slice I/N already write; this run log is an operational
    summary, not a second copy of research data)."""

    outcome: str  # "draft_created" | "no_proposal" | "duplicate" | "rejected" | "ai_error"
    hypothesis_id: Optional[str]
    contract_id: Optional[str]
    detail: str


class OvernightRunResult(NamedTuple):
    started: str
    finished: str
    as_of: str
    max_attempts: int
    attempts: list  # list[AttemptOutcome]
    drafts_created: list  # list[str] of hypothesis_ids, in attempt order
    error: Optional[str]  # set only if the WHOLE run failed outright


def _existing_duplicate(proposal: dict, *, registry_dir: Path) -> Optional[str]:
    """Would a Contract built from this NOT-YET-CREATED proposal share
    research.experiments.comparison._rule_fingerprint() with a Contract
    already in the registry? Returns the existing contract_id if so, else
    None.

    Reuses `_rule_fingerprint()` directly — the SAME function
    research/brain/similarity.py's global exact-duplicate detection (Slice
    L) already treats as the sole, authoritative definition of "duplicate"
    — rather than inventing a second, independent notion of it. A
    throwaway Contract is built from the proposal's fingerprint-relevant
    fields only (mirroring hypothesis_intake.create_draft()'s own
    entry_rule/exit_rule JSON-encoding exactly, so the fingerprint is
    computed identically to how it would be for the real, eventually-
    persisted Contract); id/title/hypothesis/etc. are placeholders, since
    _rule_fingerprint() never reads them. This throwaway object is never
    saved, locked, or registered anywhere.

    Fails OPEN (returns None, i.e. "not a duplicate") on any malformed
    proposal shape: this is a pre-flight optimization to avoid spending a
    create_draft() call on an obvious duplicate, not a validation boundary
    — hypothesis_intake.validate_proposal() (invoked downstream, inside
    submit_proposal(), for every proposal that reaches it) remains the
    real, authoritative gate for a malformed proposal, and produces a far
    more informative rejection than this function could.
    """
    try:
        candidate = Contract(
            id="__overnight_duplicate_preview__",
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


def run_overnight_cycle(
    store: Store,
    as_of: TimeLike,
    *,
    registry_dir: Path = REGISTRY_DIR,
    runner=None,
    max_attempts: int = MAX_PROPOSAL_ATTEMPTS_PER_RUN,
) -> OvernightRunResult:
    """The whole run, directly callable (no subprocess) — what main() below
    wraps for cron, and what every test in this slice calls directly with
    an injected `runner`, per the same "never exercise the real Claude
    binary in a unit test" mandate every prior Research AI slice has kept.

    Builds the digest EXACTLY ONCE for the entire run — before the first
    attempt, not inside the loop — and passes that identical snapshot to
    every attempt via investigate()'s new `digest=` parameter (Phase 2
    Slice P). This is deliberate, not an optimization detail: without it,
    an attempt that successfully creates a draft would change the
    registry, so a later attempt in the SAME run that rebuilt its own
    digest would see a subtly different research state than the first
    attempt did (a different `contract_registry`/`exact_duplicates`
    section) — every attempt in one run is meant to see the same "what did
    research memory know at the start of tonight's run" snapshot.

    Each attempt is fully isolated: hi.IntakeRejected, inv.
    InvestigatorError, and any other exception a single attempt raises are
    all caught here and recorded as that attempt's outcome, and the loop
    continues to the next bounded attempt — one bad attempt never aborts
    the run, the same "one feed failing never stops the others" posture
    research/recorder.py already takes toward its own sources. This
    function itself never raises for an ordinary attempt-level failure;
    only a problem in the loop's own bookkeeping (not expected) would
    propagate out, which is why main() below still wraps the call in its
    own try/except for the true "run failed outright" case.
    """
    started = now_ist()
    digest = build_digest(store, as_of, registry_dir=registry_dir)

    attempts: list = []
    drafts_created: list = []

    for _ in range(max_attempts):
        try:
            result = inv.investigate(
                store, as_of, runner=runner, registry_dir=registry_dir,
                digest=digest,
                duplicate_check=lambda p: _existing_duplicate(p, registry_dir=registry_dir),
            )
        except hi.IntakeRejected as e:
            attempts.append(AttemptOutcome(
                outcome="rejected", hypothesis_id=None, contract_id=None,
                detail=("; ".join(e.reasons))[:300]))
            continue
        except inv.InvestigatorError as e:
            attempts.append(AttemptOutcome(
                outcome="ai_error", hypothesis_id=None, contract_id=None,
                detail=str(e)[:300]))
            continue
        except Exception as e:  # an unexpected failure in ONE attempt must
            # never abort the whole run — recorded, not re-raised.
            attempts.append(AttemptOutcome(
                outcome="ai_error", hypothesis_id=None, contract_id=None,
                detail=f"{type(e).__name__}: {e}"[:300]))
            continue

        if isinstance(result, inv.NoProposal):
            attempts.append(AttemptOutcome(
                outcome="no_proposal", hypothesis_id=None, contract_id=None,
                detail=result.reason[:300]))
        elif isinstance(result, inv.DuplicateProposal):
            attempts.append(AttemptOutcome(
                outcome="duplicate", hypothesis_id=None, contract_id=None,
                detail=f"duplicates existing contract {result.existing_contract_id}"))
        else:  # inv.InvestigatorResult — a genuine new DRAFT, never beyond it
            attempts.append(AttemptOutcome(
                outcome="draft_created", hypothesis_id=result.hypothesis_id,
                contract_id=result.contract_id, detail=""))
            drafts_created.append(result.hypothesis_id)

    finished = now_ist()
    return OvernightRunResult(
        started=iso(started), finished=iso(finished), as_of=digest["as_of"],
        max_attempts=max_attempts, attempts=attempts,
        drafts_created=drafts_created, error=None,
    )


def _persist_run(result: OvernightRunResult, *, run_log: Path = RUN_LOG) -> None:
    """A record of what the overnight process itself did, so a quiet night
    can later be distinguished from a broken one — the same reason
    research/recorder.py's recorder_runs.jsonl exists, same JSONL-append
    shape, separate file.

    `run_log` defaults to the real repository log (RUN_LOG) but can be
    overridden — the same parameterization convention used everywhere
    else in this codebase for `registry_dir`/`db` — so tests can verify
    the JSONL content without writing into the real
    research/overnight_runs.jsonl."""
    row = result._asdict()
    # AttemptOutcome is itself a NamedTuple (a tuple subclass), so left
    # as-is it would serialize as a bare JSON array — technically
    # parseable but not self-describing for a human reading the log.
    # Expand each attempt to its own dict so every field is named.
    row["attempts"] = [a._asdict() for a in row["attempts"]]
    try:
        run_log.parent.mkdir(parents=True, exist_ok=True)
        with open(run_log, "a") as f:
            f.write(json.dumps(row, default=str) + "\n")
    except OSError:
        pass


def _notify(message: str) -> None:
    """Best-effort Telegram — identical pattern to research/recorder.py's
    own _notify(): guarded hard, because a process that fails BECAUSE it
    could not send a message about its own result is worse than one that
    stays quiet. A failed notification here can never corrupt research
    state: it is always called after every write for this run has already
    completed, never in the middle of one."""
    try:
        sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
        from telegram_notify import send_message  # type: ignore
        send_message(message)
    except Exception:
        pass


def _summarize_for_notification(result: OvernightRunResult, *, registry_dir: Path) -> str:
    """Concise, factual, and deliberately narrow in what it claims — no
    confidence language, no implied profitability, no suggestion that a
    DRAFT has been validated or is ready to trade. Only: what ran, how
    many attempts, how many drafts, which hypothesis_ids, and (if
    available) each new draft's own title/claim text exactly as the AI
    stated it — never Claude's or this module's own editorializing on top.
    """
    n_attempts = len(result.attempts)
    n_drafts = len(result.drafts_created)
    n_duplicate = sum(1 for a in result.attempts if a.outcome == "duplicate")
    n_no_proposal = sum(1 for a in result.attempts if a.outcome == "no_proposal")
    n_rejected = sum(1 for a in result.attempts if a.outcome in ("rejected", "ai_error"))

    lines = [
        f"🔬 Overnight research [{result.as_of}]: {n_attempts} attempt(s), "
        f"{n_drafts} DRAFT hypothesis(es) created for review, "
        f"{n_duplicate} duplicate(s) skipped, {n_no_proposal} no-proposal, "
        f"{n_rejected} rejected/errored.",
    ]
    if n_drafts:
        lines.append("New DRAFT(s) — proposals only, not validated, not locked, not tradeable:")
        for a in result.attempts:
            if a.outcome != "draft_created":
                continue
            claim = ""
            try:
                c = Contract.load(a.contract_id, registry_dir)
                claim = c.title or c.hypothesis
            except Exception:
                pass
            suffix = f' — "{claim}"' if claim else ""
            lines.append(f"  {a.hypothesis_id} ({a.contract_id}){suffix}")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Living Quant overnight Research AI cadence — PROPOSAL "
                     "ONLY. Creates DRAFT hypotheses for human review; never "
                     "approves, locks, or executes anything.")
    ap.add_argument("--db", default=None)
    ap.add_argument("--registry-dir", default=None)
    ap.add_argument("--run-log", default=None,
                    help="Override the JSONL run-log path (default: "
                         "research/overnight_runs.jsonl)")
    ap.add_argument("--max-attempts", type=int, default=MAX_PROPOSAL_ATTEMPTS_PER_RUN)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet-on-success", action="store_true",
                    help="Only print when something went wrong (for cron)")
    args = ap.parse_args()

    registry_dir = Path(args.registry_dir) if args.registry_dir else REGISTRY_DIR
    as_of = now_ist()

    try:
        store = Store.open(args.db) if args.db else Store.open()
    except Exception as e:
        print(f"FATAL: could not open the research store: {e}", file=sys.stderr)
        _notify(f"🔴 Overnight research: could not open the research store — "
                f"{type(e).__name__}: {e}")
        sys.exit(1)

    try:
        result = run_overnight_cycle(
            store, as_of, registry_dir=registry_dir, max_attempts=args.max_attempts)
    except Exception as e:
        store.close()
        print(f"FATAL: overnight run failed outright: {e}", file=sys.stderr)
        _notify(f"🔴 Overnight research: run failed outright — "
                f"{type(e).__name__}: {e}")
        sys.exit(1)

    store.close()
    run_log = Path(args.run_log) if args.run_log else RUN_LOG
    _persist_run(result, run_log=run_log)

    failed_attempts = [a for a in result.attempts if a.outcome in ("rejected", "ai_error")]

    if args.json:
        print(json.dumps(result._asdict(), indent=2, default=str))
    elif not (args.quiet_on_success and not failed_attempts):
        print(f"overnight research {result.started} .. {result.finished} "
              f"(as_of={result.as_of})")
        for a in result.attempts:
            print(f"  {a.outcome:<14} {a.hypothesis_id or '':<28} {a.detail}")
        print(f"  {len(result.drafts_created)} draft(s) created | "
              f"{len(failed_attempts)} attempt(s) failed/rejected")

    _notify(_summarize_for_notification(result, registry_dir=registry_dir))
    sys.exit(0)


if __name__ == "__main__":
    main()
