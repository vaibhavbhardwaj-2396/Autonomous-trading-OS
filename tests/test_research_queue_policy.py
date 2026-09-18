"""Focused, side-effect-free coverage for research discovery backpressure."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from research.brain.queue_policy import ResearchQueuePolicy, ResearchQueueSnapshot

PASSED = FAILED = 0


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


policy = ResearchQueuePolicy(draft_high_watermark=3, runnable_experiment_high_watermark=2)

empty = policy.decide(ResearchQueueSnapshot(draft_count=0, locked_runnable_count=0))
check("empty downstream queues allow discovery", empty.discovery_allowed, empty)

draft_full = policy.decide(ResearchQueueSnapshot(draft_count=3, locked_runnable_count=0))
check("draft watermark blocks discovery at equality", not draft_full.discovery_allowed, draft_full)
check("draft watermark explains the block", "draft backlog" in draft_full.blocking_reasons[0], draft_full)

run_full = policy.decide(ResearchQueueSnapshot(draft_count=0, locked_runnable_count=2))
check("runnable-experiment watermark blocks discovery at equality", not run_full.discovery_allowed, run_full)
check("experiment watermark explains the block", "runnable experiment backlog" in run_full.blocking_reasons[0], run_full)

both = policy.decide(ResearchQueueSnapshot(draft_count=4, locked_runnable_count=3))
check("all saturated downstream queues are reported", len(both.blocking_reasons) == 2, both)

try:
    ResearchQueuePolicy(draft_high_watermark=0)
    rejected = False
except ValueError:
    rejected = True
check("zero watermark is rejected rather than silently blocking all discovery", rejected)

print(f"\n{PASSED} passed, {FAILED} failed")
raise SystemExit(1 if FAILED else 0)
