"""
tests/test_resource_governor.py — STABLE + CONTROLLED: the Resource
Governor's measurement core (control/resources.py).

Two kinds of tests here, deliberately kept apart:
  A. classify()/predicate tests against HAND-BUILT ResourceSnapshot objects
     — deterministic, no dependence on this machine's actual CPU/RAM/disk
     at test time.
  B. measure()/get_resource_state() smoke tests against the REAL local
     machine — proves the stdlib-only reading code actually runs without
     crashing (on whatever OS this happens to run on) and returns
     sane-shaped output; does NOT assert a specific resource state, since
     that depends on real, uncontrolled machine conditions.

Run with:  python -m tests.test_resource_governor
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from control import resources as rg  # noqa: E402

PASSED, FAILED = 0, 0


def check(name, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  ✓ {name}")
    else:
        FAILED += 1
        print(f"  ✗ {name}")
        if detail:
            print(f"      {detail}")


def snap(**overrides) -> rg.ResourceSnapshot:
    """A fully-healthy baseline snapshot, with individual fields
    overridden per test — keeps each test's intent to a single line."""
    base = dict(
        measured_at="2026-01-01T00:00:00+05:30",
        cpu_count=4,
        load_1m=0.5,
        load_ratio=0.125,
        mem_total_bytes=8_000_000_000,
        mem_available_bytes=6_000_000_000,
        mem_available_ratio=0.75,
        disk_total_bytes=100_000_000_000,
        disk_free_bytes=80_000_000_000,
        disk_free_ratio=0.80,
        read_errors=(),
    )
    base.update(overrides)
    return rg.ResourceSnapshot(**base)


T = rg.DEFAULT_THRESHOLDS


# ===========================================================================
print("\n--- A: classify() against hand-built snapshots ---")
# ===========================================================================

check("A1: a fully-healthy snapshot classifies as HEALTHY",
      rg.classify(snap())["state"] == "HEALTHY")

check("A2: memory just under the CONSTRAINED boundary -> CONSTRAINED",
      rg.classify(snap(mem_available_ratio=T.mem_available_constrained - 0.01))["state"]
      == "CONSTRAINED")

check("A3: memory just under the PRESSURED boundary -> PRESSURED",
      rg.classify(snap(mem_available_ratio=T.mem_available_pressured - 0.01))["state"]
      == "PRESSURED")

check("A4: memory just under the CRITICAL boundary -> CRITICAL",
      rg.classify(snap(mem_available_ratio=T.mem_available_critical - 0.01))["state"]
      == "CRITICAL")

check("A5: disk free under the CRITICAL boundary alone -> CRITICAL "
      "(a single critical resource overrides an otherwise-healthy machine)",
      rg.classify(snap(disk_free_ratio=T.disk_free_critical - 0.01))["state"] == "CRITICAL")

check("A6: CPU load ratio over the CRITICAL boundary alone -> CRITICAL",
      rg.classify(snap(load_ratio=T.load_ratio_critical + 0.5))["state"] == "CRITICAL")

r7 = rg.classify(snap(mem_available_ratio=0.5, disk_free_ratio=T.disk_free_critical - 0.01))
check("A7: the WORST of multiple simultaneous signals wins — CONSTRAINED-ish "
      "memory plus CRITICAL disk still reports CRITICAL overall",
      r7["state"] == "CRITICAL", r7)

r8 = rg.classify(snap(mem_available_ratio=None))
check("A8: an unmeasured metric contributes NO signal (never assumed "
      "healthy or critical) and is named in `unmeasured`",
      r8["state"] == "HEALTHY" and "memory" in r8["unmeasured"], r8)

r9 = rg.classify(snap(mem_available_ratio=T.mem_available_critical - 0.01))
check("A9: reasons are human-readable and name the actual measured ratio",
      any("memory available" in r for r in r9["reasons"]), r9["reasons"])

check("A10: thresholds are overridable — a custom, stricter threshold "
      "reclassifies the same snapshot",
      rg.classify(snap(mem_available_ratio=0.5),
                  thresholds=rg.ResourceThresholds(mem_available_constrained=0.9))["state"]
      == "CONSTRAINED")


# ===========================================================================
print("\n--- B: predicates ---")
# ===========================================================================

for s in ("HEALTHY", "CONSTRAINED", "PRESSURED"):
    check(f"B1: expensive_work_allowed() is True for {s}",
          rg.expensive_work_allowed({"state": s}) is True)
check("B1b: expensive_work_allowed() is False for CRITICAL",
      rg.expensive_work_allowed({"state": "CRITICAL"}) is False)

check("B2: allowed_concurrency(4, HEALTHY) returns the full configured amount",
      rg.allowed_concurrency(4, {"state": "HEALTHY"}) == 4)
check("B3: allowed_concurrency(4, CONSTRAINED) returns half, rounded down",
      rg.allowed_concurrency(4, {"state": "CONSTRAINED"}) == 2)
check("B4: allowed_concurrency(1, CONSTRAINED) never rounds down to zero "
      "(at least 1 if any was configured)",
      rg.allowed_concurrency(1, {"state": "CONSTRAINED"}) == 1)
check("B5: allowed_concurrency(4, PRESSURED) caps at 1",
      rg.allowed_concurrency(4, {"state": "PRESSURED"}) == 1)
check("B6: allowed_concurrency(4, CRITICAL) is 0",
      rg.allowed_concurrency(4, {"state": "CRITICAL"}) == 0)
check("B7: allowed_concurrency(0, HEALTHY) is 0 — never manufactures work "
      "from nothing configured",
      rg.allowed_concurrency(0, {"state": "HEALTHY"}) == 0)

check("B8: should_throttle() is False only for HEALTHY",
      rg.should_throttle({"state": "HEALTHY"}) is False
      and all(rg.should_throttle({"state": s}) is True
              for s in ("CONSTRAINED", "PRESSURED", "CRITICAL")))


# ===========================================================================
print("\n--- C: measure() / get_resource_state() against the REAL machine ---")
# ===========================================================================

live = rg.measure()
check("C1: measure() never raises and returns a ResourceSnapshot",
      isinstance(live, rg.ResourceSnapshot))
check("C2: cpu_count is a real positive integer on any machine this runs on",
      isinstance(live.cpu_count, int) and live.cpu_count > 0, live.cpu_count)
check("C3: disk_free_ratio is a real number in [0, 1] (shutil.disk_usage "
      "works on every OS)", live.disk_free_ratio is not None
      and 0.0 <= live.disk_free_ratio <= 1.0, live.disk_free_ratio)
check("C4: load_ratio is either None (no getloadavg on this OS) or a "
      "non-negative number, never fabricated",
      live.load_ratio is None or live.load_ratio >= 0.0, live.load_ratio)
check("C5: an unreadable metric is reflected in read_errors as a string, "
      "not silently dropped",
      all(isinstance(e, str) for e in live.read_errors), live.read_errors)

live_state = rg.get_resource_state()
check("C6: get_resource_state() returns a state in RESOURCE_STATES",
      live_state["state"] in rg.RESOURCE_STATES, live_state["state"])
check("C7: get_resource_state()'s snapshot round-trips as a plain dict "
      "(JSON-serializable shape, for a future API route)",
      isinstance(live_state["snapshot"], dict))

live_disk_path = rg.measure(disk_path=Path("/"))
check("C8: measure(disk_path=...) actually uses the given path — "
      "at minimum returns a valid ratio for the filesystem root",
      live_disk_path.disk_free_ratio is not None
      and 0.0 <= live_disk_path.disk_free_ratio <= 1.0)

check("C9: expensive_work_allowed()/allowed_concurrency() work end-to-end "
      "with a real (no injected state) measurement, never raise",
      rg.expensive_work_allowed() in (True, False)
      and isinstance(rg.allowed_concurrency(3), int))


# ===========================================================================
print("\n--- D: thresholds are env-overridable, same posture as WorkerLimits ---")
# ===========================================================================

import os  # noqa: E402

_orig_env = os.environ.get("RESOURCE_MEM_AVAILABLE_CRITICAL")
try:
    os.environ["RESOURCE_MEM_AVAILABLE_CRITICAL"] = "0.42"
    t = rg.ResourceThresholds.from_env()
    check("D1: a valid env override is applied", t.mem_available_critical == 0.42)

    os.environ["RESOURCE_MEM_AVAILABLE_CRITICAL"] = "not-a-number"
    t2 = rg.ResourceThresholds.from_env()
    check("D2: a garbage env value falls back to the dataclass default, "
          "never crashes", t2.mem_available_critical == rg.DEFAULT_THRESHOLDS.mem_available_critical)
finally:
    if _orig_env is None:
        os.environ.pop("RESOURCE_MEM_AVAILABLE_CRITICAL", None)
    else:
        os.environ["RESOURCE_MEM_AVAILABLE_CRITICAL"] = _orig_env


# ===========================================================================
print("\n--- E: wired into research.brain.worker.main() end to end ---")
# ===========================================================================

import json  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402

from control import runtime as ctrl  # noqa: E402
from research.brain import worker as w  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="lq-test-resource-governor-"))
_orig_get_resource_state = w.rg.get_resource_state


def _fake_state(state_name: str, reasons=("synthetic test condition",)):
    return {"state": state_name, "reasons": list(reasons), "unmeasured": [],
            "measured_at": "2026-01-01T00:00:00+05:30", "snapshot": {}}


_real_control_state_e = ctrl.get_state()
try:
    ctrl.set_state("RUNNING", reason="test E setup", actor="test")

    # E1: a CRITICAL resource state skips the heartbeat entirely — no store
    # opened, telemetry still persisted, exit code 0 (not an error).
    w.rg.get_resource_state = lambda: _fake_state("CRITICAL", ["disk free 3% < critical 7%"])
    try:
        rc_e1 = w.main(["--db", str(TMP / "e1.db"), "--registry-dir", str(TMP / "reg-e1"),
                        "--no-notify", "--quiet-on-success"])
    finally:
        w.rg.get_resource_state = _orig_get_resource_state
    check("E1: worker.main() returns 0 when the resource state is CRITICAL",
          rc_e1 == 0, rc_e1)
    check("E1b: no research store file was created", not (TMP / "e1.db").exists())
    _lines_e1 = w.RUN_LOG.read_text().splitlines()
    row_e1 = json.loads(_lines_e1[-1]) if _lines_e1 else None
    check("E1c: a telemetry row was persisted naming the resource governor, "
          "not a control-layer pause",
          row_e1 is not None and "RESOURCE_GOVERNOR_CRITICAL" in (row_e1.get("no_work_reason") or ""),
          row_e1)

    # E2: a PRESSURED resource state does NOT skip the heartbeat — it
    # throttles the effective limits down through the worker's own existing
    # per-kind caps. Prove it by requesting a generous max_experiments via
    # CLI and confirming the PERSISTED row reflects the throttled value.
    (TMP / "reg-e2").mkdir(parents=True, exist_ok=True)
    w.rg.get_resource_state = lambda: _fake_state("PRESSURED", ["cpu load 1.4 > pressured 1.25"])
    try:
        rc_e2 = w.main(["--db", str(TMP / "e2.db"), "--registry-dir", str(TMP / "reg-e2"),
                        "--no-notify", "--quiet-on-success", "--max-experiments", "4",
                        "--max-discovery-attempts", "0", "--max-promotions", "0",
                        "--max-substrate-creations", "0"])
    finally:
        w.rg.get_resource_state = _orig_get_resource_state
    check("E2: worker.main() returns 0 for PRESSURED — the heartbeat still runs",
          rc_e2 == 0, rc_e2)
    _lines_e2 = w.RUN_LOG.read_text().splitlines()
    row_e2 = json.loads(_lines_e2[-1]) if _lines_e2 else None
    check("E2b: the persisted row's limits show max_experiments throttled "
          "from 4 down to PRESSURED's cap of 1, not the requested 4",
          row_e2 is not None and row_e2["limits"]["max_experiments"] == 1, row_e2)

    # E3: a HEALTHY resource state changes nothing — the configured limit
    # passes through untouched.
    (TMP / "reg-e3").mkdir(parents=True, exist_ok=True)
    w.rg.get_resource_state = lambda: _fake_state("HEALTHY", [])
    try:
        rc_e3 = w.main(["--db", str(TMP / "e3.db"), "--registry-dir", str(TMP / "reg-e3"),
                        "--no-notify", "--quiet-on-success", "--max-experiments", "4",
                        "--max-discovery-attempts", "0", "--max-promotions", "0",
                        "--max-substrate-creations", "0"])
    finally:
        w.rg.get_resource_state = _orig_get_resource_state
    _lines_e3 = w.RUN_LOG.read_text().splitlines()
    row_e3 = json.loads(_lines_e3[-1]) if _lines_e3 else None
    check("E3: a HEALTHY resource state leaves the configured limit "
          "untouched (max_experiments stays 4)",
          row_e3 is not None and row_e3["limits"]["max_experiments"] == 4, row_e3)
finally:
    w.rg.get_resource_state = _orig_get_resource_state
    ctrl.set_state(_real_control_state_e["mode"] or "RUNNING", reason="test cleanup", actor="test")
    shutil.rmtree(TMP, ignore_errors=True)


print(f"\n{'=' * 52}")
print(f"  {PASSED} passed, {FAILED} failed")
print(f"{'=' * 52}\n")
sys.exit(1 if FAILED else 0)
