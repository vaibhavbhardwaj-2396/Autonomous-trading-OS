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
check("component lock remains writable by the shared production group",
      (lock.stat().st_mode & 0o777) == 0o660)
original_fchmod = components.os.fchmod
def denied_fchmod(_fd, _mode):
    raise PermissionError("simulated non-owner")
components.os.fchmod = denied_fchmod
try:
    with components._shared_lock(lock):
        non_owner_group_write_works = True
except PermissionError:
    non_owner_group_write_works = False
finally:
    components.os.fchmod = original_fchmod
check("group writer accepts an already group-writable root-owned lock",
      non_owner_group_write_works)
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

overnight = now.replace(hour=1, minute=5)
old_worker = (overnight-dt.timedelta(hours=2)).isoformat()
overnight_hb = dict(heartbeats)
overnight_hb["RESEARCH_AI"] = {"last_attempt":old_worker,"last_success":old_worker}
overnight_hb["EXPERIMENTS"] = {"last_attempt":old_worker,"last_success":old_worker}
overnight_state = watchdog.reconcile(crontab_text=cron, heartbeats=overnight_hb,
                                     desired=desired, now=overnight)
overnight_workers = [r for r in overnight_state["components"]
                     if r["component"] in ("RESEARCH_AI", "EXPERIMENTS")]
check("overnight worker gap is NOT_DUE rather than a false stale alert",
      all(r["effective_state"] == "HEALTHY" and r["heartbeat_state"] == "NOT_DUE"
          and r["next_expected_run"].endswith("06:15:00+05:30")
          for r in overnight_workers)
      and not any(a["component"] in ("RESEARCH_AI", "EXPERIMENTS")
                  for a in overnight_state["alerts"]), overnight_state)

startup_grace = overnight.replace(hour=6, minute=5)
grace_state = watchdog.reconcile(crontab_text=cron, heartbeats=overnight_hb,
                                 desired=desired, now=startup_grace)
grace_workers = [r for r in grace_state["components"]
                 if r["component"] in ("RESEARCH_AI", "EXPERIMENTS")]
check("06:00 worker receives bounded startup grace before freshness is enforced",
      all(r["effective_state"] == "HEALTHY" and r["heartbeat_state"] == "NOT_DUE"
          for r in grace_workers), grace_state)

startup_overdue = overnight.replace(hour=6, minute=16)
overdue_state = watchdog.reconcile(crontab_text=cron, heartbeats=overnight_hb,
                                   desired=desired, now=startup_overdue)
overdue_workers = [r for r in overdue_state["components"]
                   if r["component"] in ("RESEARCH_AI", "EXPERIMENTS")]
check("missing worker after startup grace becomes STALE",
      all(r["effective_state"] == "STALE" and r["heartbeat_state"] == "STALE"
          for r in overdue_workers), overdue_state)

print(f"\n{PASSED} passed, {FAILED} failed")
raise SystemExit(1 if FAILED else 0)
