"""Read-only system/scheduler/watchdog control-plane view."""

from __future__ import annotations

from control import components, watchdog
from research.store import iso, now_ist

from . import operations


def get_status() -> dict:
    return {"as_of": iso(now_ist()), "desired_components": components.get_all(),
            "watchdog": watchdog.read_state(), "operations": operations.get_operations_status(),
            "phase_11": "LOCKED"}
