"""Component desired-state and deterministic watchdog reconciliation."""

from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from control import components, watchdog  # noqa: E402

PASSED = FAILED = 0
def check(name, condition, detail=""):
    global PASSED, FAILED
    if condition: PASSED += 1; print(f"  ✓ {name}")
    else: FAILED += 1; print(f"  ✗ {name}\n      {detail}")

tmp = Path(tempfile.mkdtemp(prefix="lq-runtime-reconcile-"))
state = tmp / "components.json"; lock = tmp / "components.lock"

row = components.set_state("RESEARCH_AI", "PAUSED", reason="AI_BUDGET_EXHAUSTED",
                           actor="test-admin", resume_policy="MANUAL",
                           path=state, lock_path=lock)
check("component pause persists actor, reason and manual resume policy",
      row["desired_state"] == "PAUSED" and row["reason"] == "AI_BUDGET_EXHAUSTED"
      and row["resume_policy"] == "MANUAL")
check("component pause is enforced by allowed()", not components.allowed("RESEARCH_AI", path=state))

now = dt.datetime(2026, 9, 26, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30)))
fresh = now.isoformat()
desired = components.get_all(path=state)
for name in components.COMPONENTS:
    desired[name] = {**desired[name], "desired_state":"RUNNING"}
heartbeats = {name:{"last_attempt":fresh,"last_success":fresh} for name in components.COMPONENTS}
cron = "\n".join(spec["pattern"] for spec in watchdog.SPECS.values())
ok = watchdog.reconcile(crontab_text=cron, heartbeats=heartbeats, desired=desired, now=now)
check("aligned scheduler, heartbeat and dependencies resolve HEALTHY",
      all(r["effective_state"] == "HEALTHY" for r in ok["components"]), ok)

drift = watchdog.reconcile(crontab_text=cron.replace("research.brain.worker", "missing", 2),
                           heartbeats=heartbeats, desired=desired, now=now)
research = next(r for r in drift["components"] if r["component"] == "RESEARCH_AI")
check("desired RUNNING plus missing schedule becomes CONFIGURATION_DRIFT",
      research["effective_state"] == "CONFIGURATION_DRIFT", research)

blocked = watchdog.reconcile(crontab_text=cron, heartbeats=heartbeats, desired=desired, now=now,
    dependencies={"RESEARCH_AI":{"state":"NOT_CONFIGURED","reason":"OPENAI_API_KEY missing"}})
research = next(r for r in blocked["components"] if r["component"] == "RESEARCH_AI")
check("missing external dependency becomes BLOCKED, not HEALTHY or ERROR",
      research["effective_state"] == "BLOCKED" and "OPENAI" in research["reason"], research)

stale_hb = dict(heartbeats)
stale_hb["DATA"] = {"last_attempt":(now-dt.timedelta(days=2)).isoformat(), "last_success":None}
stale = watchdog.reconcile(crontab_text=cron, heartbeats=stale_hb, desired=desired, now=now)
data = next(r for r in stale["components"] if r["component"] == "DATA")
check("overdue scheduled heartbeat becomes STALE", data["effective_state"] == "STALE", data)

print(f"\n{PASSED} passed, {FAILED} failed")
raise SystemExit(1 if FAILED else 0)
