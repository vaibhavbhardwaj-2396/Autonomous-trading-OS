"""Audited desired state for independently scheduled subsystems.

Cron remains the transport.  This file is the authoritative pause/resume
control: scheduled entry points always run, read this state, and make a
recorded no-op when paused.  Commenting cron lines is never a control action.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import json
from pathlib import Path

CONTROL_DIR = Path(__file__).resolve().parent
STATE_PATH = CONTROL_DIR / "component_state.json"
LOCK_PATH = CONTROL_DIR / ".component_state.lock"

COMPONENTS = ("DATA", "RESEARCH_AI", "EXPERIMENTS", "PAPER", "BROKER_SYNC",
              "VALIDATION", "NOTIFICATIONS", "WATCHDOG")
DESIRED_STATES = ("RUNNING", "PAUSED", "DISABLED")
RESUME_POLICIES = ("AUTOMATIC", "MANUAL")
_IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


class InvalidComponentState(ValueError):
    pass


def now_iso() -> str:
    return dt.datetime.now(_IST).isoformat(timespec="seconds")


def _default(component: str) -> dict:
    return {"component": component, "desired_state": "RUNNING", "reason": "default",
            "actor": "system", "changed_at": None, "resume_policy": "AUTOMATIC",
            "history": []}


def get_all(*, path: Path = STATE_PATH) -> dict:
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        raw = {}
    rows = raw.get("components", raw) if isinstance(raw, dict) else {}
    result = {}
    for component in COMPONENTS:
        row = dict(_default(component))
        if isinstance(rows.get(component), dict):
            row.update(rows[component])
        if row.get("desired_state") not in DESIRED_STATES:
            row = _default(component)
        result[component] = row
    return result


def get(component: str, *, path: Path = STATE_PATH) -> dict:
    if component not in COMPONENTS:
        raise InvalidComponentState(f"unknown component {component!r}")
    return get_all(path=path)[component]


def allowed(component: str, *, path: Path = STATE_PATH) -> bool:
    return get(component, path=path)["desired_state"] == "RUNNING"


def set_state(component: str, desired_state: str, *, reason: str, actor: str,
              resume_policy: str = "MANUAL", path: Path = STATE_PATH,
              lock_path: Path = LOCK_PATH) -> dict:
    if component not in COMPONENTS:
        raise InvalidComponentState(f"unknown component {component!r}")
    if desired_state not in DESIRED_STATES:
        raise InvalidComponentState(f"desired_state must be one of {DESIRED_STATES}")
    if resume_policy not in RESUME_POLICIES:
        raise InvalidComponentState(f"resume_policy must be one of {RESUME_POLICIES}")
    if not str(reason).strip() or not str(actor).strip():
        raise InvalidComponentState("reason and actor are required")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        rows = get_all(path=path)
        previous = rows[component]
        history = list(previous.get("history") or [])
        history.append({k: previous.get(k) for k in
                        ("desired_state", "reason", "actor", "changed_at", "resume_policy")})
        rows[component] = {"component": component, "desired_state": desired_state,
                           "reason": reason.strip(), "actor": actor.strip(),
                           "changed_at": now_iso(), "resume_policy": resume_policy,
                           "history": history[-50:]}
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"schema_version": 1, "components": rows}, indent=2))
        tmp.replace(path)
        return rows[component]
