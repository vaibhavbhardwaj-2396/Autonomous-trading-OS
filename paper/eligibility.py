"""
paper/eligibility.py — the explicit, auditable paper-eligibility boundary.

Slice AA's own instruction (see the AA spec, section 4) is explicit: AA must
NOT paper-trade every registered StrategyVersion automatically. A
StrategyVersion existing in strategies/registry/ means only "this exact
algorithm+parameters+source combination is DEFINED/REGISTERED" (see
strategies/registry.py's own module docstring) — it says nothing about
whether anyone has decided it should be shadow-traded.

This module is that missing decision, recorded as an append-only event log
(mirrors engine.journal's TRADES_JSONL / REGIME_JSONL "append-only,
newest-derived" convention, applied here to an approval decision instead of
a trade) rather than a single mutable "approved: true/false" flag file —
an event log means "who approved this, when, and why" and "was it ever
revoked, and when" are BOTH recoverable, which a boolean field would lose.

Explicitly NOT the eventual LIVE promotion gate (see the AA spec, section
4's own "do not yet implement the eventual LIVE promotion gate" — that is
a separate, later, and much higher-stakes decision).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from strategies import registry as sreg

from . import config as paper_config

APPROVE = "APPROVE"
REVOKE = "REVOKE"
VALID_EVENTS = (APPROVE, REVOKE)

LOG_FILENAME = "eligibility_log.jsonl"


class EligibilityError(ValueError):
    """Refusing to record an eligibility event — e.g. the given version_id
    does not exist in strategies/registry/ at all. Fail closed: an
    eligibility decision about a StrategyVersion nobody has actually
    registered is never recorded, silently or otherwise."""


def _resolve_dir(directory: Optional[Path] = None) -> Path:
    return directory if directory is not None else paper_config.eligibility_dir()


def _log_path_for_write(directory: Optional[Path] = None) -> Path:
    """Resolves the eligibility log path AND ensures its directory exists.
    Only ever called from the write side (_append_event, below) — a read
    must never create a directory as a side effect. See _read_events(),
    which api.paper_data.get_paper_strategies() calls on every dashboard
    poll: that path must work under a strictly read-only service account
    (no write permission on paper/ at all), the same posture the API
    already had for memory/ and research/ before Slice AA existed."""
    d = _resolve_dir(directory)
    d.mkdir(parents=True, exist_ok=True)
    return d / LOG_FILENAME


def _append_event(event: dict, directory: Optional[Path] = None) -> None:
    path = _log_path_for_write(directory)
    with open(path, "a") as f:
        f.write(json.dumps(event, sort_keys=True, default=str) + "\n")


def _read_events(directory: Optional[Path] = None) -> list[dict]:
    path = _resolve_dir(directory) / LOG_FILENAME
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue  # one malformed line must never hide the rest of the log
        if isinstance(row, dict):
            out.append(row)
    return out


def mark_paper_eligible(
    *, version_id: str, strategy_id: str, actor: str, reason: str, now: str,
    directory: Optional[Path] = None, registry_dir: Optional[Path] = None,
) -> dict:
    """Record an APPROVE event for `version_id`. Fails closed
    (EligibilityError) if `version_id` is not an actual, currently
    registered StrategyVersion in strategies/registry/, or if its
    strategy_id disagrees with the one given — this is what keeps the log
    auditable against reality rather than a list of arbitrary strings.

    Idempotent in effect: approving an already-eligible version again just
    appends another APPROVE event (a re-affirmation) — it is never an
    error, and is_paper_eligible() is unaffected either way.
    """
    reg_dir = registry_dir if registry_dir is not None else sreg.REGISTRY_DIR
    try:
        version = sreg.load_version(version_id, directory=reg_dir)
    except (FileNotFoundError, OSError) as e:
        raise EligibilityError(
            f"cannot mark {version_id!r} paper-eligible: no such StrategyVersion "
            f"in the registry ({reg_dir})") from e
    if version.strategy_id != strategy_id:
        raise EligibilityError(
            f"version_id {version_id!r} belongs to strategy_id "
            f"{version.strategy_id!r}, not {strategy_id!r} — refusing to record "
            f"an eligibility event with a mismatched strategy_id")
    if not actor or not actor.strip():
        raise EligibilityError("actor is required — an eligibility decision must be attributable")

    event = {
        "ts": now, "event": APPROVE, "version_id": version_id,
        "strategy_id": strategy_id, "algorithm_id": version.algorithm_id,
        "actor": actor.strip(), "reason": reason or "",
    }
    _append_event(event, directory)
    return event


def revoke_paper_eligibility(
    *, version_id: str, strategy_id: str, actor: str, reason: str, now: str,
    directory: Optional[Path] = None,
) -> dict:
    """Record a REVOKE event. Does not require the StrategyVersion to still
    exist in the registry (a version can be revoked even if its file was
    later removed) — it only requires that `version_id` has at least one
    prior APPROVE event on file, so a revoke can never be recorded for a
    version that was never actually made eligible."""
    if not is_paper_eligible(version_id, directory=directory):
        raise EligibilityError(
            f"cannot revoke {version_id!r}: it is not currently paper-eligible "
            f"(no APPROVE event on file, or already revoked)")
    if not actor or not actor.strip():
        raise EligibilityError("actor is required — an eligibility decision must be attributable")

    event = {
        "ts": now, "event": REVOKE, "version_id": version_id,
        "strategy_id": strategy_id, "actor": actor.strip(), "reason": reason or "",
    }
    _append_event(event, directory)
    return event


def _latest_status(directory: Optional[Path] = None) -> dict[str, dict]:
    """version_id -> its most recent event (by log order — this is an
    append-only log, so file order IS chronological order)."""
    latest: dict[str, dict] = {}
    for event in _read_events(directory):
        vid = event.get("version_id")
        if vid:
            latest[vid] = event
    return latest


def is_paper_eligible(version_id: str, directory: Optional[Path] = None) -> bool:
    latest = _latest_status(directory).get(version_id)
    return bool(latest) and latest.get("event") == APPROVE


def list_paper_eligible(directory: Optional[Path] = None) -> list[dict]:
    """Every version_id whose most recent event is APPROVE, with that
    event's own metadata (actor/reason/ts/algorithm_id) — the auditable
    "who approved this and why" record, not just a bare id list."""
    latest = _latest_status(directory)
    return sorted(
        (event for event in latest.values() if event.get("event") == APPROVE),
        key=lambda e: e["version_id"],
    )


def list_events(directory: Optional[Path] = None) -> list[dict]:
    """The full, raw audit trail — every APPROVE and REVOKE ever recorded,
    in chronological order, including for versions no longer eligible."""
    return _read_events(directory)


# ---------------------------------------------------------------------------
# CLI — manual approve/revoke, e.g.:
#   python -m paper.eligibility approve --version-id <id> --strategy-id momentum \
#       --by Vaibhav --reason "promising backtest, want shadow tracking"
#   python -m paper.eligibility revoke  --version-id <id> --strategy-id momentum \
#       --by Vaibhav --reason "backtest degraded"
#   python -m paper.eligibility list
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    from .clock import system_clock

    parser = argparse.ArgumentParser(description="Paper-eligibility registry (Slice AA)")
    sub = parser.add_subparsers(dest="command", required=True)

    a = sub.add_parser("approve", help="Mark a StrategyVersion paper-eligible")
    a.add_argument("--version-id", required=True)
    a.add_argument("--strategy-id", required=True)
    a.add_argument("--by", required=True, dest="actor")
    a.add_argument("--reason", default="")

    r = sub.add_parser("revoke", help="Revoke a StrategyVersion's paper eligibility")
    r.add_argument("--version-id", required=True)
    r.add_argument("--strategy-id", required=True)
    r.add_argument("--by", required=True, dest="actor")
    r.add_argument("--reason", default="")

    sub.add_parser("list", help="List currently paper-eligible StrategyVersions")

    args = parser.parse_args()
    now = system_clock().isoformat(timespec="seconds")

    if args.command == "approve":
        event = mark_paper_eligible(
            version_id=args.version_id, strategy_id=args.strategy_id,
            actor=args.actor, reason=args.reason, now=now)
        print(json.dumps(event, indent=2))
    elif args.command == "revoke":
        event = revoke_paper_eligibility(
            version_id=args.version_id, strategy_id=args.strategy_id,
            actor=args.actor, reason=args.reason, now=now)
        print(json.dumps(event, indent=2))
    else:
        print(json.dumps(list_paper_eligible(), indent=2))


if __name__ == "__main__":
    main()
