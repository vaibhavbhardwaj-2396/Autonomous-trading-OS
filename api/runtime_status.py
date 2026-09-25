"""Honest cross-subsystem status for the overview and incident runbook."""

from __future__ import annotations

import datetime as dt

from control import runtime
from research.store import now_ist

from . import ai_status, notifications, operations, validation


def _age_hours(value) -> float | None:
    if not value: return None
    try:
        stamp = dt.datetime.fromisoformat(str(value))
        if stamp.tzinfo is None: stamp = stamp.replace(tzinfo=now_ist().tzinfo)
        return round((now_ist() - stamp).total_seconds() / 3600, 2)
    except (ValueError, TypeError): return None


def get_runtime_status(store) -> dict:
    op = operations.get_operations_status()
    campaign = validation.get_validation_status(store, history_limit=2)["current"]
    ai = ai_status.get_ai_status()
    telegram = notifications.get_status()
    control = runtime.get_state()
    worker = op["research_worker"]
    worker_age = _age_hours(worker.get("last_heartbeat_at"))
    recorder_age = _age_hours((op["recorder"].get("last_run") or {}).get("finished_at") or
                              (op["recorder"].get("last_run") or {}).get("ts"))
    ai_connected = next((p.get("configured") for p in ai["provider_catalog"]
                         if p["provider"] == ai["effective_provider"]), False)
    indicators = [
        {"key":"system", "label":"SYSTEM", "state":"HEALTHY" if control["mode"] == "RUNNING" else control["mode"], "target":"control"},
        {"key":"research", "label":"RESEARCH", "state":"ACTIVE" if worker_age is not None and worker_age < 1 else "PAUSED / STALE", "target":"research"},
        {"key":"ai", "label":"AI", "state":f"{ai['effective_provider'].upper()} / {'CONNECTED' if ai_connected else 'NOT CONFIGURED'}", "target":"control"},
        {"key":"data", "label":"DATA", "state":"HEALTHY" if op["recorder"]["state"] == "HEALTHY" and (recorder_age or 999) < 24 else op["recorder"]["state"], "target":"research"},
        {"key":"broker", "label":"BROKER", "state":"SEE ACCOUNT", "target":"trading"},
        {"key":"paper", "label":"PAPER", "state":"WAITING FOR STRATEGY" if not campaign["paper_readiness"]["ready"] else "READY", "target":"paper"},
        {"key":"telegram", "label":"TELEGRAM", "state":"HEALTHY" if telegram["last_successful_message"] else ("CONFIGURED / UNPROVEN" if telegram["configured"] else "NOT CONFIGURED"), "target":"control"},
    ]
    queue = ((worker.get("last_queue_health") or {}).get("backlog_count") or
             worker.get("last_opportunities_considered") or 0)
    active = []
    if recorder_age is not None and recorder_age < 24: active.append("Collecting deterministic market and news observations")
    if worker_age is not None and worker_age < 1: active.append(f"Research worker processing a queue of {queue}")
    else: active.append("Research AI worker is not scheduled; deterministic ingestion continues")
    if not campaign["paper_readiness"]["ready"]: active.append("Paper engine waiting for an explicitly eligible StrategyVersion")
    matrix = [
        {"module":"market recorder / news ingestion", "expected_state":"scheduled", "actual_state":op["recorder"]["state"], "last_successful_run":(op["recorder"].get("last_run") or {}).get("finished_at"), "last_attempt":(op["recorder"].get("last_run") or {}).get("finished_at"), "heartbeat":recorder_age, "last_error":op["recorder"].get("failed_sources"), "queue_depth":None, "dependencies":["public market/news sources"]},
        {"module":"research allocator / discovery / experiments", "expected_state":"10-minute heartbeat", "actual_state":"ACTIVE" if worker_age is not None and worker_age < 1 else "STALE", "last_successful_run":worker.get("last_heartbeat_at"), "last_attempt":worker.get("last_heartbeat_at"), "heartbeat":worker_age, "last_error":worker.get("last_errors"), "queue_depth":queue, "dependencies":["AI Gateway", "research store"]},
        {"module":"AI Gateway", "expected_state":"provider-neutral", "actual_state":"CONNECTED" if ai_connected else "OPENAI_PROVIDER_NOT_CONFIGURED", "last_successful_run":ai["budget"].get("last_call_at"), "last_attempt":ai["budget"].get("last_call_at"), "heartbeat":None, "last_error":None, "queue_depth":queue, "dependencies":["server-side developer API credential"]},
        {"module":"paper engine", "expected_state":"daily when eligible", "actual_state":op["paper"]["state"], "last_successful_run":(op["paper"].get("last_cycle") or {}).get("completed_at"), "last_attempt":(op["paper"].get("last_cycle") or {}).get("completed_at"), "heartbeat":None, "last_error":None, "queue_depth":campaign["funnel"]["paper_eligible"], "dependencies":["eligible StrategyVersion"]},
        {"module":"Telegram notifier", "expected_state":"event + daily digest", "actual_state":"HEALTHY" if telegram["last_successful_message"] else "UNPROVEN", "last_successful_run":(telegram["last_successful_message"] or {}).get("timestamp"), "last_attempt":((telegram["recent"] or [{}])[-1]).get("timestamp"), "heartbeat":None, "last_error":telegram["last_failed_message"], "queue_depth":0, "dependencies":["Telegram API"]},
    ]
    return {"as_of": now_ist().isoformat(), "indicators": indicators, "active_work": active,
            "runtime_matrix": matrix, "funnel": campaign["funnel"], "conversion": campaign["conversion"],
            "blockers": campaign["paper_readiness"]["blockers"], "phase_11":"LOCKED"}
