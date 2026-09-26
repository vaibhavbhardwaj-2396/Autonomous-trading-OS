"""Honest cross-subsystem status for the overview and incident runbook."""

from __future__ import annotations

import datetime as dt

from control import runtime, watchdog
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
    reconciled = watchdog.read_state()
    effective = {r.get("component"): r for r in reconciled.get("components", [])}
    worker = op["research_worker"]
    worker_age = _age_hours(worker.get("last_heartbeat_at"))
    recorder_age = _age_hours((op["recorder"].get("last_run") or {}).get("finished_at") or
                              (op["recorder"].get("last_run") or {}).get("ts"))
    ai_connected = next((p.get("configured") for p in ai["provider_catalog"]
                         if p["provider"] == ai["effective_provider"]), False)
    system_state = "HEALTHY" if reconciled.get("healthy") and control["mode"] == "RUNNING" else (
        control["mode"] if control["mode"] != "RUNNING" else "DEGRADED")
    research_state = (effective.get("RESEARCH_AI") or {}).get("effective_state") or (
        "ACTIVE" if worker_age is not None and worker_age < 1 else "STALE")
    indicators = [
        {"key":"system", "label":"SYSTEM", "state":system_state, "detail":f"{len(reconciled.get('alerts', []))} watchdog alerts", "target":"system"},
        {"key":"research", "label":"RESEARCH", "state":research_state, "detail":(effective.get("RESEARCH_AI") or {}).get("reason"), "target":"research"},
        {"key":"ai", "label":"AI", "state":f"{ai['effective_provider'].upper()} / {'CONNECTED' if ai_connected else 'NOT CONFIGURED'}", "target":"control"},
        {"key":"data", "label":"DATA", "state":(effective.get("DATA") or {}).get("effective_state") or op["recorder"]["state"], "detail":(effective.get("DATA") or {}).get("reason"), "target":"research"},
        {"key":"broker", "label":"BROKER", "state":"SEE ACCOUNT", "target":"trading"},
        {"key":"paper", "label":"PAPER", "state":"WAITING" if not campaign["paper_readiness"]["ready"] else ((effective.get("PAPER") or {}).get("effective_state") or "READY"), "detail":"No eligible StrategyVersion" if not campaign["paper_readiness"]["ready"] else (effective.get("PAPER") or {}).get("reason"), "target":"paper"},
        {"key":"telegram", "label":"TELEGRAM", "state":"HEALTHY" if telegram["last_successful_message"] else ("CONFIGURED / UNPROVEN" if telegram["configured"] else "NOT CONFIGURED"), "target":"control"},
    ]
    queue = ((worker.get("last_queue_health") or {}).get("backlog_count") or
             worker.get("last_opportunities_considered") or 0)
    active = []
    if recorder_age is not None and recorder_age < 24: active.append("Collecting deterministic market and news observations")
    if worker_age is not None and worker_age < 1: active.append(f"Research worker processing a queue of {queue}")
    else: active.append("Research AI worker is not scheduled; deterministic ingestion continues")
    if not campaign["paper_readiness"]["ready"]: active.append("Paper engine waiting for an explicitly eligible StrategyVersion")
    matrix = [{
        "module": row["component"],
        "expected_state": row["desired_state"],
        "actual_state": row["effective_state"],
        "scheduler_state": row["scheduler_state"],
        "heartbeat_state": row["heartbeat_state"],
        "dependency_state": row["dependency_state"],
        "last_successful_run": row.get("last_success"),
        "last_attempt": row.get("last_attempt"),
        "next_expected_run": row.get("next_expected_run"),
        "last_error": row.get("reason") if row["effective_state"] in
                      ("CONFIGURATION_DRIFT", "STALE", "DEGRADED", "BLOCKED") else None,
        "queue_depth": queue if row["component"] in ("RESEARCH_AI", "EXPERIMENTS") else None,
        "dependencies": [row["dependency_state"]],
    } for row in reconciled.get("components", [])]
    matrix.append({"module":"AI_GATEWAY", "expected_state":"provider-neutral",
                   "actual_state":"CONNECTED" if ai_connected else "OPENAI_PROVIDER_NOT_CONFIGURED",
                   "scheduler_state":"N/A", "heartbeat_state":"N/A",
                   "dependency_state":"READY" if ai_connected else "NOT_CONFIGURED",
                   "last_successful_run":ai["budget"].get("last_call_at"),
                   "last_attempt":ai["budget"].get("last_call_at"), "next_expected_run":None,
                   "last_error":None if ai_connected else "server-side developer API credential absent",
                   "queue_depth":queue, "dependencies":["OPENAI_API_KEY"]})
    return {"as_of": now_ist().isoformat(), "indicators": indicators, "active_work": active,
            "runtime_matrix": matrix, "funnel": campaign["funnel"], "conversion": campaign["conversion"],
            "velocity": campaign.get("velocity", {}), "backlog": campaign.get("backlog", {}),
            "blockers": campaign["paper_readiness"]["blockers"], "watchdog_alerts": reconciled.get("alerts", []),
            "phase_11":"LOCKED"}
