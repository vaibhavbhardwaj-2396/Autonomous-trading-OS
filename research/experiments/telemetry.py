"""Read-only experiment execution telemetry for API and operator UI."""

from __future__ import annotations

import datetime as dt
import json
import os
import statistics
from pathlib import Path
from typing import Optional

from control import components
from .. import memory as rm
from ..contracts import REGISTRY_DIR, registry
from ..store import Store, now_ist
from .supervisor import EXECUTION_LOG


def _events(path: Path, limit: int = 1000) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(errors="replace").splitlines()[-limit:]:
        try:
            row = json.loads(line)
            if isinstance(row, dict): out.append(row)
        except json.JSONDecodeError:
            continue
    return out


def _alive(pid) -> bool:
    if not isinstance(pid, int): return False
    try:
        os.kill(pid, 0); return True
    except (ProcessLookupError, PermissionError):
        return False


def _parse(value):
    try: return dt.datetime.fromisoformat(str(value))
    except (TypeError, ValueError): return None


def _seconds_between(first: Optional[dict], second: Optional[dict]) -> Optional[float]:
    start = _parse(first.get("timestamp")) if first else None
    end = _parse(second.get("timestamp")) if second else None
    return round(max(0.0, (end - start).total_seconds()), 3) if start and end else None


def _lifecycle(events: list[dict], terminal: Optional[dict]) -> Optional[dict]:
    if terminal is None:
        return None
    contract_id = terminal.get("contract_id")
    cutoff = events.index(terminal)
    prior = [event for event in events[:cutoff]
             if event.get("contract_id") == contract_id]
    queued = next((event for event in reversed(prior)
                   if event.get("state") == "QUEUED"), None)
    running = next((event for event in reversed(prior)
                    if event.get("state") == "RUNNING"), None)
    return {**terminal,
            "queue_wait_seconds": _seconds_between(queued, running),
            "execution_seconds": _seconds_between(running, terminal)}


def get_status(store: Store, *, execution_log: Path = EXECUTION_LOG,
               registry_dir: Path = REGISTRY_DIR) -> dict:
    events = _events(execution_log)
    latest_by_contract = {}
    for event in events:
        latest_by_contract[event.get("contract_id")] = event
    running = next((e for e in reversed(events)
                    if e.get("state") in ("RUNNING", "CANCEL_REQUESTED")
                    and latest_by_contract.get(e.get("contract_id")) is e
                    and _alive(e.get("pid"))), None)
    completed = _lifecycle(events, next(
        (e for e in reversed(events) if e.get("state") == "COMPLETED"), None))
    timeout = _lifecycle(events, next(
        (e for e in reversed(events) if e.get("state") == "ABANDONED"
         and e.get("failure_reason") == "TIMEOUT"), None))
    cutoff = now_ist() - dt.timedelta(hours=24)
    timeouts_24h = sum(1 for e in events if e.get("state") == "ABANDONED"
                       and e.get("failure_reason") == "TIMEOUT"
                       and (_parse(e.get("timestamp")) or cutoff - dt.timedelta(1)) >= cutoff)

    notes = [r for r in rm.query_research_log(store, rm.DATASET_NOTE, limit=None)
             if r.get("source") == "research.experiments.runner"
             and isinstance(r.get("payload", {}).get("execution_profile"), dict)]
    profiles = [r["payload"]["execution_profile"] for r in notes]
    latest_profile = profiles[-1] if profiles else None
    runtimes = [float(p.get("wall_seconds")) for p in profiles
                if p.get("wall_seconds") is not None]
    contracts = registry(registry_dir)
    counts = {state: sum(c.status == state for c in contracts)
              for state in ("draft", "locked", "running", "reported", "abandoned")}
    started = _parse(running.get("timestamp")) if running else None
    elapsed = max(0.0, (now_ist() - started).total_seconds()) if started else None
    current_queued = next((e for e in reversed(events)
                           if running and e.get("contract_id") == running.get("contract_id")
                           and e.get("state") == "QUEUED"), None)
    state = components.get("EXPERIMENTS")
    return {
        "state": state["desired_state"], "reason": state.get("reason"),
        "queue_depth": counts["locked"] + counts["draft"], "status_counts": counts,
        "current_contract": running.get("contract_id") if running else None,
        "current_pid": running.get("pid") if running else None,
        "process_state": "RUNNING" if running else "IDLE",
        "current_stage": "cancelling" if running and running.get("state") == "CANCEL_REQUESTED"
                         else ("simulation" if running else None),
        "elapsed_seconds": round(elapsed, 3) if elapsed is not None else None,
        "current_deadline_seconds": running.get("deadline_seconds") if running else None,
        "current_queue_wait_seconds": _seconds_between(current_queued, running),
        "last_event_at": events[-1].get("timestamp") if events else None,
        "last_completion": completed,
        "last_timeout": timeout,
        "timeouts_24h": timeouts_24h,
        "median_runtime_seconds": round(statistics.median(runtimes), 3) if runtimes else None,
        "completed_profile_count": len(profiles),
        "latest_profile": latest_profile,
        "as_of": now_ist().isoformat(timespec="seconds"),
    }
