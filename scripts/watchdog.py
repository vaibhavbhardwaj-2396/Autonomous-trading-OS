"""Deterministic production watchdog; no broker calls and no AI calls."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
from pathlib import Path
from typing import Optional

from control import watchdog
from paper import config as paper_config
from paper.store import PaperStore
from research.brain.worker import worker_status
from research.recorder import RUN_LOG as RECORDER_LOG
from research.store import now_ist
from research.validation import LEDGER_PATH
from scripts.notification_service import deliver

ROOT = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", override=False)
except (ImportError, OSError):
    pass


def _last_json(path: Path) -> dict:
    try:
        for line in reversed(path.read_text(errors="replace").splitlines()):
            try:
                row = json.loads(line)
                if isinstance(row, dict):
                    return row
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    return {}


def _mtime(path: Path):
    try:
        return dt.datetime.fromtimestamp(path.stat().st_mtime, tz=now_ist().tzinfo).isoformat()
    except OSError:
        return None


def collect_heartbeats() -> dict:
    recorder = _last_json(RECORDER_LOG)
    worker = worker_status()
    validation = _last_json(LEDGER_PATH)
    notify = _last_json(ROOT / "control" / "notification_audit.jsonl")
    paper_attempt = paper_success = None
    if paper_config.db_path().is_file():
        store = PaperStore.open_readonly()
        try:
            cycles = store.list_cycles(limit=1)
            if cycles:
                paper_attempt = cycles[0].get("started_at")
                if cycles[0].get("status") == "COMPLETED":
                    paper_success = cycles[0].get("completed_at")
        finally:
            store.close()
    recorder_time = recorder.get("finished_at") or recorder.get("ts")
    worker_time = worker.get("last_heartbeat_at")
    broker_time = _mtime(ROOT / "memory" / ".indstocks_session.json")
    return {
        "DATA": {"last_attempt": recorder_time, "last_success": recorder_time,
                 "last_error": (recorder.get("errors") or None)},
        "RESEARCH_AI": {"last_attempt": worker_time, "last_success": worker_time,
                        "last_error": (worker.get("last_errors") or None)},
        "EXPERIMENTS": {"last_attempt": worker_time, "last_success": worker_time,
                        "last_error": (worker.get("last_errors") or None)},
        "PAPER": {"last_attempt": paper_attempt, "last_success": paper_success},
        "BROKER_SYNC": {"last_attempt": broker_time, "last_success": broker_time},
        "VALIDATION": {"last_attempt": validation.get("captured_at"),
                       "last_success": validation.get("captured_at")},
        "NOTIFICATIONS": {"last_attempt": notify.get("timestamp"),
                          "last_success": notify.get("timestamp") if notify.get("status") == "delivered" else None,
                          "last_error": notify.get("error") if notify.get("status") == "failed" else None},
        "WATCHDOG": {"last_attempt": now_ist().isoformat(), "last_success": now_ist().isoformat()},
    }


def dependencies() -> dict:
    openai_ready = bool((os.environ.get("OPENAI_API_KEY") or "").strip())
    return {
        "RESEARCH_AI": {"state": "READY" if openai_ready else "NOT_CONFIGURED",
                        "reason": None if openai_ready else "OPENAI_API_KEY is not configured"},
        "PAPER": {"state": "READY", "reason": None},
    }


def run(*, crontab_text: Optional[str] = None, state_path: Path = watchdog.STATE_PATH,
        notify: bool = True) -> dict:
    if crontab_text is None:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10)
        crontab_text = result.stdout if result.returncode == 0 else ""
    previous = watchdog.read_state(path=state_path)
    state = watchdog.reconcile(crontab_text=crontab_text, heartbeats=collect_heartbeats(),
                               dependencies=dependencies())
    watchdog.write_state(state, path=state_path)
    old_codes = {a.get("code") for a in previous.get("alerts", [])}
    new_alerts = [a for a in state["alerts"] if a["code"] not in old_codes]
    if notify and new_alerts:
        lines = ["LIVING QUANT WATCHDOG"] + [f"{a['code']}: {a['reason']}" for a in new_alerts]
        deliver("\n".join(lines), category="ERROR", message_type="watchdog_transition",
                actor="watchdog")
    return state


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-notify", action="store_true")
    args = ap.parse_args(argv)
    state = run(notify=not args.no_notify)
    print(json.dumps(state, indent=2))
    # Findings are persisted/alerted state, not a watchdog process failure.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
