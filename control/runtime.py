"""
control/runtime.py — the global runtime control layer (Priority Phase 4).

THE GAP THIS CLOSES
--------------------
Research, paper/shadow, and live trading are three DELIBERATELY isolated
subsystems (tests/test_kernel_isolation.py, tests/test_research_engine_
boundary.py) — for good reason: a bug in one must never be able to reach
another. Live trading already has a real, tested pause primitive
(`engine.journal.set_pause`, checked by `engine.guardrails`). Research and
paper had NONE — the only way to stop them was to SSH into the VPS and
hand-edit crontab, twice, in one week. This module is the fix: one small,
neutral, file-based state machine that research/, paper/, and the API can
all depend on safely, without any of them depending on EACH OTHER.

WHY THIS MODULE IMPORTS NOTHING FROM engine/, research/, OR paper/
--------------------------------------------------------------------
If this module imported `engine.journal` (to also flip the live pause flag
when entering SAFE_MODE), then research.brain.worker importing THIS module
would transitively make `engine` importable from inside a research
process — exactly the coupling test_kernel_isolation.py exists to prevent,
even though that test only scans direct imports and would not literally
catch it. The isolation boundary is a promise about what CAN be reached
from a research process, not just what a text scanner happens to notice.
So this module stays a pure, dependency-free file-backed state machine —
nothing here can ever touch live capital, a broker, or memory/state.json,
structurally, by absence.

The one place SAFE_MODE/STOPPED actually reach into live trading is
`api/runtime_bridge.py` — a SEPARATE module, deliberately not importable from
research/ or paper/, that orchestrates BOTH this module AND the existing
`engine.journal.set_pause()`. See that module's own docstring for the
one-way-ratchet safety argument (this layer can request "pause live," it
can never request "resume live" — only a human, through the existing
Telegram command, does that).

THE STATE MACHINE
------------------
    RUNNING    — normal. Every subsystem operates under its own EXISTING
                 limits (WorkerLimits, the research budget, paper's own
                 caps, engine's own guardrails) — this layer adds no
                 additional restriction.
    PAUSED     — research (discovery/experiments/promotion/substrate
                 creation) and paper/shadow cycles do not start. Cheap,
                 valuable data capture (the recorder) keeps running, so no
                 market history is lost while investigating a cost issue.
                 Live trading is UNCHANGED — it has its own separate pause.
    SAFE_MODE  — research, paper/shadow, AND data ingestion all keep
                 running (this is the "keep learning, never trade" mode).
                 Live execution is switched off via the EXISTING, already-
                 enforced kill switch (api/runtime_bridge.py bridges this).
    STOPPED    — the most conservative state: nothing autonomous runs.
                 Research, paper, and data ingestion all stop; live
                 execution is also switched off.

None of this is a NEW enforcement mechanism for live trading — it is a
NEW front door onto the mechanism that already existed
(`engine.journal.set_pause` / `engine.guardrails`'s existing check of
`trading_paused`), reached only through api/runtime_bridge.py, never from here.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Optional

CONTROL_DIR = Path(__file__).resolve().parent
STATE_PATH = CONTROL_DIR / "runtime_mode.json"
LOCK_PATH = CONTROL_DIR / ".runtime.lock"

RUNTIME_MODES = ("RUNNING", "PAUSED", "SAFE_MODE", "STOPPED")
DEFAULT_MODE = "RUNNING"
"""Deliberately RUNNING, not a restrictive default. This module is new
plumbing added to an ALREADY-OPERATING system — defaulting its own absence
to something restrictive would silently change existing live-trading
behaviour the moment this file is deployed, which is a change nobody asked
for here. If you want the system to start in SAFE_MODE/STOPPED, set it
explicitly (see set_state() / the CLI at the bottom of this file) — the
absence of a control file must never itself be a behavioural change."""

MAX_HISTORY = 50
"""Bounded, same convention as every other per-file cap in this codebase
(DEFAULT_MAX_REASSESSMENT_EVENTS_PER_CYCLE, MAX_LOCKS_PER_PERIOD, ...) — how
many past transitions this file keeps, so a state file that has been
toggled thousands of times over months never grows without bound."""

_IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def _now_iso() -> str:
    """A local, independent IST timestamp — deliberately NOT imported from
    research.store.now_ist()/iso(), for the same zero-coupling reason the
    module docstring gives for imports in general. A few duplicated lines
    here are the correct trade against a structural dependency edge."""
    return dt.datetime.now(_IST).isoformat(timespec="seconds")


class InvalidRuntimeMode(ValueError):
    """Raised for an unrecognized mode, or a set_state() call missing a
    reason/actor — the same "no anonymous, unexplained consequential
    action" posture research.brain.opportunity.retire() already enforces."""


def _default_state() -> dict:
    return {"mode": DEFAULT_MODE, "reason": "", "actor": "system",
            "changed_at": _now_iso(), "history": []}


def get_state(*, path: Path = STATE_PATH) -> dict:
    """The current control state — mode, who/why it was last changed, and
    a bounded history. Never raises: a missing or corrupt state file reads
    back as the default (RUNNING) rather than blocking every caller on a
    file-system hiccup — the same fail-open-to-"nothing new" posture
    research/.worker_state.json's own _load_state() already takes, except
    here "nothing new" for research/paper callers means "assume RUNNING",
    which is the least surprising thing to assume when this file simply
    doesn't exist yet.

    `path`, like `state_path` on research.brain.worker.run_worker_cycle(),
    exists so tests can point at an isolated file instead of the one real,
    deliberately-global control file — every PRODUCTION call site (worker.
    main(), recorder.main(), paper.runner.run_paper_cycle(),
    api/runtime_bridge.py) uses the default, on purpose: there is only
    supposed to be ONE real answer to "is the organism paused" for the
    whole system, unlike research's own per-registry bookmarks."""
    try:
        raw = path.read_text()
    except OSError:
        return _default_state()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return _default_state()
    if not isinstance(data, dict) or data.get("mode") not in RUNTIME_MODES:
        return _default_state()
    data.setdefault("history", [])
    return data


def _save_state(data: dict, *, path: Path) -> None:
    """Atomic write — tmp file + os.replace, the exact convention
    research.brain.worker._save_state()/research.recorder._persist_run()
    already use, so a crash mid-write can never leave a half-written,
    unparseable control file behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    tmp.replace(path)


def set_state(mode: str, *, reason: str, actor: str,
             path: Path = STATE_PATH, lock_path: Path = LOCK_PATH) -> dict:
    """Change the global mode. `reason` and `actor` are both REQUIRED,
    non-blank — every consequential control action here is named and
    explained, never anonymous (the same discipline
    research.brain.opportunity.retire()/freeze() already established for
    a very similar reason: a human debugging "why did research stop"
    six weeks from now must never find a blank).

    Locked with a POSIX flock (control/.runtime.lock) around the whole
    read-modify-write, so two callers changing the mode at the same instant
    (two browser tabs, a script and the API) can never produce a lost
    update — the same concurrency-safety posture
    research.brain.worker.worker_lock() already uses for the exact same
    reason, just for a write instead of a whole cycle.

    `path`/`lock_path` exist for test isolation — see get_state()'s own
    docstring for why every production call site uses the defaults.
    """
    if mode not in RUNTIME_MODES:
        raise InvalidRuntimeMode(
            f"mode must be one of {RUNTIME_MODES}, got {mode!r}")
    if not reason or not reason.strip():
        raise InvalidRuntimeMode("set_state() requires a non-empty reason")
    if not actor or not actor.strip():
        raise InvalidRuntimeMode("set_state() requires a non-empty actor")

    import fcntl
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fh = open(lock_path, "w")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        current = get_state(path=path)
        previous_mode = current.get("mode", DEFAULT_MODE)
        now = _now_iso()
        history = list(current.get("history") or [])
        history.append({"mode": previous_mode, "reason": current.get("reason", ""),
                        "actor": current.get("actor", ""), "changed_at": current.get("changed_at", "")})
        history = history[-MAX_HISTORY:]
        new_state = {"mode": mode, "reason": reason.strip(), "actor": actor.strip(),
                    "changed_at": now, "previous_mode": previous_mode, "history": history}
        _save_state(new_state, path=path)
        return new_state
    finally:
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        lock_fh.close()


# ---------------------------------------------------------------------------
# Named, explainable predicates — every caller (research.brain.worker,
# research.recorder, paper.runner, the future UI) asks ONE of these, never
# reads "mode" and re-derives the rule itself. One truth table, one place.
# `state`, if given, avoids a redundant file read inside a hot loop; if
# omitted, each predicate reads the current state fresh (correct, cheap —
# one small JSON file).
# ---------------------------------------------------------------------------

def _mode(state: Optional[dict]) -> str:
    return (state or get_state()).get("mode", DEFAULT_MODE)


def research_allowed(state: Optional[dict] = None) -> bool:
    """Discovery, experiments, autonomous promotion, substrate creation —
    the WORKER's own expensive/LLM-costly path. False under PAUSED and
    STOPPED; true under RUNNING and SAFE_MODE (research keeps learning even
    when live trading is switched off)."""
    return _mode(state) in ("RUNNING", "SAFE_MODE")


def paper_allowed(state: Optional[dict] = None) -> bool:
    """Paper/shadow cycles — same truth table as research_allowed(): an
    autonomous background cycle, paused/stopped together with research,
    kept running under SAFE_MODE (paper decisions are not live capital)."""
    return _mode(state) in ("RUNNING", "SAFE_MODE")


def data_ingestion_allowed(state: Optional[dict] = None) -> bool:
    """The recorder — cheap, not LLM-costly, and valuable to keep running
    even while paused (so no market history is lost while investigating a
    cost issue). Only STOPPED, the most conservative state, turns this
    off too."""
    return _mode(state) != "STOPPED"


def live_execution_recommended(state: Optional[dict] = None) -> bool:
    """This layer's OWN opinion on whether live execution should be
    allowed — False under SAFE_MODE and STOPPED. This is advisory from
    here: the actual, only enforcement point is the EXISTING
    engine.guardrails check of memory/state.json's trading_paused flag,
    reached only via api/runtime_bridge.py — see that module. Exposed here purely
    for read-only observability (a future UI showing "why is live
    disabled" without needing to import engine)."""
    return _mode(state) not in ("SAFE_MODE", "STOPPED")


def autonomous_activity_allowed(state: Optional[dict] = None) -> bool:
    """True unless STOPPED — "is ANY autonomous activity permitted at
    all," for a caller that just wants the single most conservative
    answer without naming a specific subsystem."""
    return _mode(state) != "STOPPED"


# ---------------------------------------------------------------------------
# CLI — usable from a VPS terminal today, before any UI exists.
# ---------------------------------------------------------------------------

def main(argv: Optional[list] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description="Global runtime control (RUNNING/PAUSED/SAFE_MODE/STOPPED) "
                    "for research, paper/shadow, and (via api/runtime_bridge.py only) "
                    "live trading.")
    ap.add_argument("--json", action="store_true")
    sub = ap.add_subparsers(dest="command")

    sub.add_parser("status", help="show the current mode (default if no command given)")

    setp = sub.add_parser("set", help="change the global mode")
    setp.add_argument("mode", choices=RUNTIME_MODES)
    setp.add_argument("--reason", required=True)
    setp.add_argument("--actor", default=os.environ.get("USER") or "cli")

    args = ap.parse_args(argv)

    if args.command == "set":
        try:
            state = set_state(args.mode, reason=args.reason, actor=args.actor)
        except InvalidRuntimeMode as e:
            print(f"error: {e}")
            return 1
    else:
        state = get_state()

    if args.json:
        print(json.dumps(state, indent=2, default=str))
    else:
        print(f"mode           : {state['mode']}")
        print(f"reason         : {state.get('reason') or '(none)'}")
        print(f"actor          : {state.get('actor') or '(none)'}")
        print(f"changed_at     : {state.get('changed_at')}")
        print(f"research_allowed        : {research_allowed(state)}")
        print(f"paper_allowed            : {paper_allowed(state)}")
        print(f"data_ingestion_allowed   : {data_ingestion_allowed(state)}")
        print(f"live_execution_recommended: {live_execution_recommended(state)}")
        print("NOTE: live execution is only actually gated via api/runtime_bridge.py's "
              "bridge to the existing engine.journal.set_pause() — this module "
              "never touches engine/ or memory/state.json.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
