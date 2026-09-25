"""Capacity-aware work planner for autonomous research operations.

This module translates the low-level resource governor into a stable five-level
operating vocabulary and an explicit allocation for four work classes.  It is
pure planning: it never starts a process, changes runtime mode, or touches live
trading.  Callers remain responsible for executing only the work they own.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional

from . import resources

CAPACITY_STATES = ("IDLE", "LOW_LOAD", "NORMAL", "HIGH_LOAD", "CRITICAL")
WORK_CLASSES = ("critical", "continuous", "heavy", "background")


@dataclass(frozen=True)
class WorkAllocation:
    work_class: str
    enabled: bool
    concurrency: int
    cpu_share: float
    reason: str


def normalize(resource_state: dict) -> str:
    """Map measured infrastructure state to the Phase 10.5 vocabulary.

    HEALTHY is split using measured utilisation so an idle VPS can spend spare
    capacity on backfills without treating all healthy periods identically.
    Legacy states remain unchanged in ``control.resources`` for compatibility.
    """
    legacy = resource_state.get("state")
    if legacy == "CRITICAL":
        return "CRITICAL"
    if legacy in ("PRESSURED", "CONSTRAINED"):
        return "HIGH_LOAD"
    snap = resource_state.get("snapshot") or {}
    load = snap.get("load_ratio")
    memory_used = None
    if snap.get("mem_available_ratio") is not None:
        memory_used = 1.0 - float(snap["mem_available_ratio"])
    if load is not None and load <= 0.10 and (memory_used is None or memory_used <= 0.35):
        return "IDLE"
    if load is not None and load <= 0.40 and (memory_used is None or memory_used <= 0.60):
        return "LOW_LOAD"
    return "NORMAL"


_POLICY = {
    "IDLE": {
        "critical": (1.0, "reserved first"),
        "continuous": (0.30, "observation and health loops remain active"),
        "heavy": (0.45, "spare capacity available for experiments/backtests"),
        "background": (0.25, "backfills and maintenance admitted"),
    },
    "LOW_LOAD": {
        "critical": (1.0, "reserved first"),
        "continuous": (0.40, "observation and health loops remain active"),
        "heavy": (0.45, "bounded heavy work admitted"),
        "background": (0.15, "background work throttled"),
    },
    "NORMAL": {
        "critical": (1.0, "reserved first"),
        "continuous": (0.55, "continuous work has priority"),
        "heavy": (0.35, "one bounded heavy lane admitted"),
        "background": (0.10, "background work strongly throttled"),
    },
    "HIGH_LOAD": {
        "critical": (1.0, "reserved first"),
        "continuous": (0.75, "only essential continuous work admitted"),
        "heavy": (0.0, "heavy work deferred under load"),
        "background": (0.0, "background work paused under load"),
    },
    "CRITICAL": {
        "critical": (1.0, "health, telemetry and safety remain available"),
        "continuous": (0.0, "non-critical work paused"),
        "heavy": (0.0, "heavy work paused"),
        "background": (0.0, "background work paused"),
    },
}


def plan(resource_state: Optional[dict] = None) -> dict:
    """Return an explainable capacity plan based on ratios, not VPS size."""
    raw = resource_state if resource_state is not None else resources.get_resource_state()
    state = normalize(raw)
    cpus = int((raw.get("snapshot") or {}).get("cpu_count") or 1)
    allocations = []
    for work_class in WORK_CLASSES:
        share, reason = _POLICY[state][work_class]
        # A one-vCPU host still gets at most one lane. A disabled class always
        # receives zero; enabled non-critical classes never manufacture more
        # than their proportional share of the measured CPU capacity.
        if share <= 0:
            concurrency = 0
        elif work_class == "critical":
            concurrency = 1
        else:
            concurrency = max(1, min(cpus, round(cpus * share)))
        allocations.append(WorkAllocation(
            work_class=work_class,
            enabled=concurrency > 0,
            concurrency=concurrency,
            cpu_share=share,
            reason=reason,
        ))
    return {
        "state": state,
        "legacy_resource_state": raw.get("state"),
        "measured_at": raw.get("measured_at"),
        "reasons": list(raw.get("reasons") or []),
        "unmeasured": list(raw.get("unmeasured") or []),
        "allocations": [asdict(a) for a in allocations],
        "snapshot": raw.get("snapshot") or {},
    }


def permits(work_class: str, resource_state: Optional[dict] = None) -> bool:
    if work_class not in WORK_CLASSES:
        raise ValueError(f"unknown work class {work_class!r}; expected one of {WORK_CLASSES}")
    allocation = next(a for a in plan(resource_state)["allocations"]
                      if a["work_class"] == work_class)
    return bool(allocation["enabled"])
