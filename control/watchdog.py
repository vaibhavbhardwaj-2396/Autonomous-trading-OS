"""Deterministic runtime reconciliation and fault classification."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Optional

from . import components

STATE_PATH = Path(__file__).resolve().parent / "watchdog_state.json"
_IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

SPECS = {
    "DATA": {"pattern": "research.recorder", "heartbeat_hours": 24},
    "RESEARCH_AI": {"pattern": "research.brain.worker", "heartbeat_hours": 1},
    "EXPERIMENTS": {"pattern": "research.brain.worker", "heartbeat_hours": 1},
    "PAPER": {"pattern": "paper.runner run", "heartbeat_hours": 36},
    "BROKER_SYNC": {"pattern": "refresh_indstocks_session.sh", "heartbeat_hours": 30},
    "VALIDATION": {"pattern": "research.validation", "heartbeat_hours": 36},
    "NOTIFICATIONS": {"pattern": "scripts.notification_service digest", "heartbeat_hours": 30},
    "WATCHDOG": {"pattern": "scripts.watchdog", "heartbeat_hours": 1},
}


def _parse(value) -> Optional[dt.datetime]:
    if not value:
        return None
    try:
        stamp = dt.datetime.fromisoformat(str(value))
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=_IST)
    except (TypeError, ValueError):
        return None


def reconcile(*, crontab_text: str, heartbeats: dict, dependencies: Optional[dict] = None,
              desired: Optional[dict] = None, now: Optional[dt.datetime] = None) -> dict:
    now = now or dt.datetime.now(_IST)
    dependencies = dependencies or {}
    desired = desired or components.get_all()
    active_lines = [line.strip() for line in crontab_text.splitlines()
                    if line.strip() and not line.lstrip().startswith("#")]
    rows, alerts = [], []
    for name, spec in SPECS.items():
        wanted = desired[name]
        scheduled = any(spec["pattern"] in line for line in active_lines)
        heartbeat = heartbeats.get(name) or {}
        last_attempt = _parse(heartbeat.get("last_attempt"))
        last_success = _parse(heartbeat.get("last_success"))
        age = ((now - last_attempt).total_seconds() / 3600) if last_attempt else None
        heartbeat_state = "FRESH" if age is not None and age <= spec["heartbeat_hours"] else "STALE"
        dependency = dependencies.get(name) or {"state": "READY", "reason": None}
        dependency_state = dependency.get("state", "READY")
        if wanted["desired_state"] != "RUNNING":
            effective = wanted["desired_state"]
            reason = wanted.get("reason") or "operator control"
        elif not scheduled:
            effective, reason = "CONFIGURATION_DRIFT", "desired RUNNING but scheduler entry is absent"
        elif dependency_state not in ("READY", "HEALTHY"):
            effective, reason = "BLOCKED", dependency.get("reason") or "dependency unavailable"
        elif heartbeat_state == "STALE":
            effective, reason = "STALE", "scheduled heartbeat is missing or overdue"
        elif heartbeat.get("last_error"):
            effective, reason = "DEGRADED", str(heartbeat["last_error"])
        else:
            effective, reason = "HEALTHY", "scheduler, heartbeat and dependencies agree"
        row = {"component": name, "desired_state": wanted["desired_state"],
               "scheduler_state": "SCHEDULED" if scheduled else "UNSCHEDULED",
               "heartbeat_state": heartbeat_state, "dependency_state": dependency_state,
               "effective_state": effective, "reason": reason,
               "last_success": last_success.isoformat() if last_success else None,
               "last_attempt": last_attempt.isoformat() if last_attempt else None,
               "next_expected_run": heartbeat.get("next_expected_run"),
               "resume_policy": wanted.get("resume_policy")}
        rows.append(row)
        if effective in ("CONFIGURATION_DRIFT", "STALE", "DEGRADED"):
            alerts.append({"code": f"{name}_{effective}", "component": name,
                           "severity": "ERROR" if effective == "CONFIGURATION_DRIFT" else "WARNING",
                           "reason": reason})
    return {"schema_version": 1, "captured_at": now.isoformat(), "components": rows,
            "alerts": alerts, "healthy": not any(a["severity"] == "ERROR" for a in alerts)}


def read_state(*, path: Path = STATE_PATH) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"captured_at": None, "components": [], "alerts": [], "healthy": False}


def write_state(state: dict, *, path: Path = STATE_PATH) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str))
    tmp.replace(path)
