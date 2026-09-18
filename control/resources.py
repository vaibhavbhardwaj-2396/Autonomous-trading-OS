"""
control/resources.py — the Resource Governor's measurement core.

STABLE + CONTROLLED (outcome 1): "Living Quant should know how much
capacity it has and regulate its own workload accordingly." This module is
the "know how much capacity it has" half. The "regulate" half is the small
set of predicates at the bottom (`allowed_concurrency`, `should_throttle`,
`expensive_work_allowed`) that worker.py/recorder.py/paper/runner.py call
the same way they already call control.runtime's predicates.

CAPACITY-DRIVEN, NOT HARDWARE-SPECIFIC
---------------------------------------------------------------------------
Every threshold here is a RATIO (fraction of total capacity), never an
absolute number. "RAM available < 15% of total" behaves correctly on
today's 1 vCPU / 950MB VPS AND on a future 4 vCPU / 8GB upgrade without a
code change — that is an explicit, load-bearing requirement, not a style
preference: hard-coding "defer if available RAM < 150MB" would silently
stop mattering the moment the VPS is resized, in either direction.

WHY THIS LIVES IN control/, NOT engine/ OR research/
---------------------------------------------------------------------------
Same reasoning as control/runtime.py's own docstring: this is a
cross-subsystem concern (worker, recorder, and paper/runner all need to
ask "can I afford to do this?"), and control/ is already the one place
that's a neutral, dependency-free sibling of all three — importing nothing
from engine/, research/, or paper/, so adding a resource check here adds
no edge to the engine<->research isolation graph
(tests/test_kernel_isolation.py) or to the research<->paper isolation
tests either. This module imports ONLY the Python standard library.

ORGANISM STATE vs INFRASTRUCTURE STATE — two independent axes
---------------------------------------------------------------------------
control/runtime.py answers "is a human allowed this to run right now"
(RUNNING/PAUSED/SAFE_MODE/STOPPED) — a deliberate, human-driven decision.
This module answers "can the machine physically afford it right now"
(HEALTHY/CONSTRAINED/PRESSURED/CRITICAL) — a continuously-measured,
machine-driven fact. They compose, they don't replace each other: a
RUNNING organism on a CRITICAL machine still doesn't get to run expensive
work; a PAUSED organism on a HEALTHY machine still doesn't run, because
that pause was a deliberate human choice this module has no opinion on.

THE LADDER: THROTTLE -> DEFER -> DRAIN -> PAUSE, never a hard kill
---------------------------------------------------------------------------
Nothing in this module ever sends a signal to a process or forcibly stops
one. It only answers questions ("how many experiments may I attempt this
heartbeat", "is this worth starting at all"); the caller (worker.py today)
already has natural, cheap throttle points — WorkerLimits' existing
max_discovery_attempts/max_experiments/max_promotions/
max_substrate_creations caps — so PRESSURED can be expressed as "run the
same cycle, but ask for fewer things" without any new stop/kill mechanism.
Only CRITICAL asks a caller to skip the heartbeat entirely, and even that
is the same clean, already-established "skip and record why" shape
control/runtime.py's STOPPED path uses — never a process kill.
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import NamedTuple, Optional

RESOURCE_STATES = ("HEALTHY", "CONSTRAINED", "PRESSURED", "CRITICAL")

_IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def _now_iso() -> str:
    return dt.datetime.now(_IST).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Measurement — stdlib only. Every field is Optional because a metric that
# genuinely cannot be read (e.g. /proc/meminfo on a non-Linux dev machine)
# must degrade to "unknown", never to a fabricated number — the same
# "do not fabricate values; where a metric cannot be measured, explicitly
# say so" discipline the rest of this project already holds itself to.
# ---------------------------------------------------------------------------

class ResourceSnapshot(NamedTuple):
    measured_at: str
    cpu_count: Optional[int]
    load_1m: Optional[float]
    load_ratio: Optional[float]          # load_1m / cpu_count — >1.0 means oversubscribed
    mem_total_bytes: Optional[int]
    mem_available_bytes: Optional[int]
    mem_available_ratio: Optional[float]  # available / total
    disk_total_bytes: Optional[int]
    disk_free_bytes: Optional[int]
    disk_free_ratio: Optional[float]      # free / total
    read_errors: tuple                    # e.g. ("meminfo: ...",) — for diagnostics only


def _read_meminfo() -> tuple:
    """(total_bytes, available_bytes) from /proc/meminfo, or (None, None)
    with an error string if unavailable (any non-Linux OS, or a locked-down
    container without /proc). MemAvailable (not MemFree) is what's used —
    it already accounts for reclaimable buff/cache, the same number `free
    -h`'s "available" column reports, and the one that actually answers
    "how much could a new process use without swapping.\""""
    path = Path("/proc/meminfo")
    if not path.is_file():
        return None, None, "meminfo: /proc/meminfo not available (non-Linux host?)"
    try:
        total = available = None
        for line in path.read_text().splitlines():
            if line.startswith("MemTotal:"):
                total = int(line.split()[1]) * 1024
            elif line.startswith("MemAvailable:"):
                available = int(line.split()[1]) * 1024
            if total is not None and available is not None:
                break
        if total is None or available is None:
            return None, None, "meminfo: MemTotal/MemAvailable not found"
        return total, available, None
    except (OSError, ValueError) as e:
        return None, None, f"meminfo: {type(e).__name__}: {e}"


def _read_loadavg() -> tuple:
    try:
        load_1m = os.getloadavg()[0]
    except (OSError, AttributeError) as e:
        return None, f"loadavg: {type(e).__name__}: {e}"
    return load_1m, None


def _read_disk(path: Path) -> tuple:
    try:
        usage = shutil.disk_usage(path)
        return usage.total, usage.free, None
    except OSError as e:
        return None, None, f"disk: {type(e).__name__}: {e}"


def measure(*, disk_path: Optional[Path] = None) -> ResourceSnapshot:
    """One honest, best-effort snapshot of what this machine can currently
    afford. Never raises — a measurement failure degrades individual
    fields to None (and is recorded in `read_errors`), it never crashes
    the caller. `disk_path` defaults to this repo's own root, since that's
    the filesystem research/paper/logs actually grow on."""
    errors = []

    cpu_count = os.cpu_count()

    load_1m, load_err = _read_loadavg()
    if load_err:
        errors.append(load_err)
    load_ratio = (load_1m / cpu_count) if (load_1m is not None and cpu_count) else None

    mem_total, mem_available, mem_err = _read_meminfo()
    if mem_err:
        errors.append(mem_err)
    mem_ratio = (mem_available / mem_total) if (mem_total and mem_available is not None) else None

    disk_target = disk_path or Path(__file__).resolve().parent.parent
    disk_total, disk_free, disk_err = _read_disk(disk_target)
    if disk_err:
        errors.append(disk_err)
    disk_ratio = (disk_free / disk_total) if (disk_total and disk_free is not None) else None

    return ResourceSnapshot(
        measured_at=_now_iso(),
        cpu_count=cpu_count,
        load_1m=load_1m,
        load_ratio=load_ratio,
        mem_total_bytes=mem_total,
        mem_available_bytes=mem_available,
        mem_available_ratio=mem_ratio,
        disk_total_bytes=disk_total,
        disk_free_bytes=disk_free,
        disk_free_ratio=disk_ratio,
        read_errors=tuple(errors),
    )


# ---------------------------------------------------------------------------
# Thresholds — ratios, env-overridable, same "small, explicit, clearly
# named, overridable" posture as research/brain/worker.py's WorkerLimits.
# Each pair is (CONSTRAINED boundary, PRESSURED boundary, CRITICAL
# boundary); a metric below the CRITICAL boundary means "this single
# resource alone is enough to call the whole machine CRITICAL."
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ResourceThresholds:
    mem_available_constrained: float = 0.35
    mem_available_pressured: float = 0.20
    mem_available_critical: float = 0.10

    disk_free_constrained: float = 0.30
    disk_free_pressured: float = 0.15
    disk_free_critical: float = 0.07

    load_ratio_constrained: float = 0.85
    load_ratio_pressured: float = 1.25
    load_ratio_critical: float = 2.00

    ENV = {
        "mem_available_constrained": "RESOURCE_MEM_AVAILABLE_CONSTRAINED",
        "mem_available_pressured": "RESOURCE_MEM_AVAILABLE_PRESSURED",
        "mem_available_critical": "RESOURCE_MEM_AVAILABLE_CRITICAL",
        "disk_free_constrained": "RESOURCE_DISK_FREE_CONSTRAINED",
        "disk_free_pressured": "RESOURCE_DISK_FREE_PRESSURED",
        "disk_free_critical": "RESOURCE_DISK_FREE_CRITICAL",
        "load_ratio_constrained": "RESOURCE_LOAD_RATIO_CONSTRAINED",
        "load_ratio_pressured": "RESOURCE_LOAD_RATIO_PRESSURED",
        "load_ratio_critical": "RESOURCE_LOAD_RATIO_CRITICAL",
    }

    @classmethod
    def from_env(cls) -> "ResourceThresholds":
        base = cls()
        values: dict = {}
        for field_name, env_name in cls.ENV.items():
            raw = (os.environ.get(env_name) or "").strip()
            if not raw:
                values[field_name] = getattr(base, field_name)
                continue
            try:
                values[field_name] = float(raw)
            except ValueError:
                values[field_name] = getattr(base, field_name)
        return cls(**values)


DEFAULT_THRESHOLDS = ResourceThresholds()


def classify(
    snapshot: ResourceSnapshot, *, thresholds: ResourceThresholds = DEFAULT_THRESHOLDS,
) -> dict:
    """The worst (most severe) signal among CPU/RAM/disk wins — a single
    critical resource is enough to call the whole machine CRITICAL, per
    "Resource Protection Must Override Research." A metric that could not
    be measured contributes no signal (never assumed healthy OR critical)
    — see `unmeasured` in the returned dict, which callers/UI should
    surface honestly rather than silently treating as HEALTHY."""
    severity = {"HEALTHY": 0, "CONSTRAINED": 1, "PRESSURED": 2, "CRITICAL": 3}
    worst = "HEALTHY"
    reasons: list = []
    unmeasured: list = []

    def _bump(candidate: str, reason: str) -> None:
        nonlocal worst
        if severity[candidate] > severity[worst]:
            worst = candidate
        if candidate != "HEALTHY":
            reasons.append(reason)

    if snapshot.mem_available_ratio is None:
        unmeasured.append("memory")
    else:
        r = snapshot.mem_available_ratio
        if r < thresholds.mem_available_critical:
            _bump("CRITICAL", f"memory available {r:.0%} < critical threshold "
                              f"{thresholds.mem_available_critical:.0%}")
        elif r < thresholds.mem_available_pressured:
            _bump("PRESSURED", f"memory available {r:.0%} < pressured threshold "
                               f"{thresholds.mem_available_pressured:.0%}")
        elif r < thresholds.mem_available_constrained:
            _bump("CONSTRAINED", f"memory available {r:.0%} < constrained threshold "
                                 f"{thresholds.mem_available_constrained:.0%}")

    if snapshot.disk_free_ratio is None:
        unmeasured.append("disk")
    else:
        r = snapshot.disk_free_ratio
        if r < thresholds.disk_free_critical:
            _bump("CRITICAL", f"disk free {r:.0%} < critical threshold "
                              f"{thresholds.disk_free_critical:.0%}")
        elif r < thresholds.disk_free_pressured:
            _bump("PRESSURED", f"disk free {r:.0%} < pressured threshold "
                               f"{thresholds.disk_free_pressured:.0%}")
        elif r < thresholds.disk_free_constrained:
            _bump("CONSTRAINED", f"disk free {r:.0%} < constrained threshold "
                                 f"{thresholds.disk_free_constrained:.0%}")

    if snapshot.load_ratio is None:
        unmeasured.append("cpu_load")
    else:
        r = snapshot.load_ratio
        if r > thresholds.load_ratio_critical:
            _bump("CRITICAL", f"load ratio {r:.2f} > critical threshold "
                              f"{thresholds.load_ratio_critical:.2f}")
        elif r > thresholds.load_ratio_pressured:
            _bump("PRESSURED", f"load ratio {r:.2f} > pressured threshold "
                               f"{thresholds.load_ratio_pressured:.2f}")
        elif r > thresholds.load_ratio_constrained:
            _bump("CONSTRAINED", f"load ratio {r:.2f} > constrained threshold "
                                 f"{thresholds.load_ratio_constrained:.2f}")

    return {
        "state": worst,
        "reasons": reasons,
        "unmeasured": unmeasured,
        "measured_at": snapshot.measured_at,
        "snapshot": asdict(snapshot) if hasattr(snapshot, "__dataclass_fields__")
                    else snapshot._asdict(),
    }


def get_resource_state(
    *, disk_path: Optional[Path] = None, thresholds: ResourceThresholds = DEFAULT_THRESHOLDS,
) -> dict:
    """The one call most callers need: measure, then classify. Kept as a
    single function (mirroring control.runtime.get_state()'s shape) so a
    caller — or a future API route — doesn't need to know this is two
    steps internally."""
    return classify(measure(disk_path=disk_path), thresholds=thresholds)


# ---------------------------------------------------------------------------
# Predicates — the "regulate" half. Same style as control.runtime's
# research_allowed()/paper_allowed(): pure functions over a state dict,
# each independently testable with a hand-built dict, no I/O of their own.
# ---------------------------------------------------------------------------

def expensive_work_allowed(state: Optional[dict] = None) -> bool:
    """False only at CRITICAL — the one state that asks a caller to skip
    its heartbeat entirely rather than throttle through it."""
    state = state if state is not None else get_resource_state()
    return state["state"] != "CRITICAL"


def allowed_concurrency(configured_max: int, state: Optional[dict] = None) -> int:
    """How much of a caller's OWN configured budget (e.g.
    WorkerLimits.max_experiments) it may actually use this heartbeat.
    HEALTHY: the full configured amount — this governor should never make
    a healthy machine do LESS than its own configuration asks for.
    CONSTRAINED: at most half (rounded down, minimum 1 if any was
    configured at all). PRESSURED: at most 1. CRITICAL: 0 (callers should
    also be checking expensive_work_allowed() and skipping entirely, but
    this stays consistent even if only this one predicate is consulted)."""
    state = state if state is not None else get_resource_state()
    if configured_max <= 0:
        return 0
    s = state["state"]
    if s == "HEALTHY":
        return configured_max
    if s == "CONSTRAINED":
        return max(1, configured_max // 2)
    if s == "PRESSURED":
        return min(1, configured_max)
    return 0  # CRITICAL


def should_throttle(state: Optional[dict] = None) -> bool:
    """True for CONSTRAINED or worse — a coarse "is any reduction
    warranted at all" check for a caller that doesn't need the finer
    allowed_concurrency() gradation."""
    state = state if state is not None else get_resource_state()
    return state["state"] != "HEALTHY"


# ---------------------------------------------------------------------------
# CLI — python -m control.resources [--json]
# ---------------------------------------------------------------------------

def main(argv: Optional[list] = None) -> int:
    import argparse
    import json

    ap = argparse.ArgumentParser(description="Living Quant Resource Governor — one snapshot")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    state = get_resource_state()
    if args.json:
        print(json.dumps(state, indent=2, default=str))
        return 0

    snap = state["snapshot"]

    def _pct(x):
        return f"{x:.0%}" if x is not None else "unknown"

    print(f"Resource state: {state['state']}   ({state['measured_at']})")
    print(f"  CPU load (1m): {snap['load_1m']} across {snap['cpu_count']} cpu(s) "
          f"-> ratio {snap['load_ratio']}")
    print(f"  Memory available: {_pct(snap['mem_available_ratio'])}")
    print(f"  Disk free: {_pct(snap['disk_free_ratio'])}")
    if state["reasons"]:
        print("  Reasons:")
        for r in state["reasons"]:
            print(f"    - {r}")
    if state["unmeasured"]:
        print(f"  Could not measure: {', '.join(state['unmeasured'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
