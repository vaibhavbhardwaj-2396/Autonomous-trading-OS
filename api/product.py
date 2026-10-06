"""One read-only product view over the autonomous research operating loop."""

from __future__ import annotations

import datetime as dt

from paper import config as paper_config
from paper.eligibility import list_paper_eligible
from paper.store import PaperStore
from research import contracts
from research import memory as rm
from research.brain import draft_backlog, hypothesis_intake
from research.store import iso, now_ist
from strategies import registry as strategy_registry

from . import ai_status, artifacts, data, operations, validation


def _latest(rows: list[dict]) -> dict | None:
    return rows[-1] if rows else None


def get_product_status(store) -> dict:
    now = now_ist()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    counts = store.observation_counts_since(today)
    op = operations.get_operations_status()
    worker = op["research_worker"]
    experiment = data.get_experiment_telemetry(store)
    campaign = validation.get_validation_status(store, history_limit=2)["current"]
    ai = ai_status.get_ai_status()
    contracts_rows = contracts.registry()
    status_counts: dict[str, int] = {}
    for contract in contracts_rows:
        status_counts[contract.status] = status_counts.get(contract.status, 0) + 1
    locks_used = hypothesis_intake.locks_in_period()
    lock_limit = hypothesis_intake.MAX_LOCKS_PER_PERIOD
    confirmation_limit = hypothesis_intake.MAX_CONFIRMATION_LOCKS_PER_PERIOD
    versions = strategy_registry.list_versions()
    eligible = list_paper_eligible()

    paper_trades = []
    paper_perf = {}
    paper_last_cycle = None
    if paper_config.db_path().is_file():
        paper_store = PaperStore.open_readonly()
        try:
            paper_trades = paper_store.list_trades(limit=None)
            cycles = paper_store.list_cycles(limit=1)
            paper_last_cycle = cycles[0] if cycles else None
            from paper.portfolio import PaperPortfolio
            paper_perf = PaperPortfolio(paper_store).performance_summary()
        finally:
            paper_store.close()

    evidence_rows = rm.query_research_log(store, rm.DATASET_EVIDENCE, limit=None)
    today_evidence = [r for r in evidence_rows if r.get("event_ts", 0) >= int(today.timestamp())]
    evidence_counts: dict[str, int] = {}
    for row in today_evidence:
        verdict = (row.get("payload") or {}).get("verdict") or "UNKNOWN"
        evidence_counts[verdict] = evidence_counts.get(verdict, 0) + 1

    drafts = draft_backlog.build_backlog(store, limit=12).get("drafts") or []
    current = []
    for c in sorted(contracts_rows, key=lambda x: x.locked_at or "", reverse=True):
        if c.status in ("running", "locked"):
            current.append({"title": c.title or c.hypothesis, "id": c.id,
                            "status": c.status.upper(), "why": c.hypothesis,
                            "artifact_id": f"experiment:{c.id}"})
    for row in drafts[:max(0, 8 - len(current))]:
        current.append({"title": row.get("claim"), "id": row.get("contract_id"),
                        "status": "AWAITING REVIEW", "why": row.get("claim"),
                        "artifact_id": f"experiment:{row.get('contract_id')}"})

    model_rows = rm.query_research_log(store, rm.DATASET_MODEL_INTERACTION, limit=None)
    packet_rows = rm.query_research_log(store, rm.DATASET_RESEARCH_PACKET, limit=None)
    anomaly_rows = rm.query_research_log(store, rm.DATASET_ANOMALY, limit=None)
    news_rows = rm.query_research_log(store, "news_arrival", limit=None)
    review_rows = rm.query_research_log(store, rm.DATASET_DRAFT_REVIEW, limit=None)

    def node(identifier, label, status, work, last, outputs, target, capability="DETERMINISTIC"):
        return {"id": identifier, "label": label, "status": status, "current_work": work,
                "last_run": (last or {}).get("event_time") if last else None,
                "outputs": outputs, "queue": 0, "capability": capability,
                "provider": ai.get("effective_provider") if capability != "DETERMINISTIC" else None,
                "model": ai.get("effective_model") if capability != "DETERMINISTIC" else None,
                "cost": None, "target": target}

    worker_fresh = bool(worker.get("last_heartbeat_at"))
    organization = [
        node("director", "Research Director", "ACTIVE" if worker_fresh else "STALE",
             worker.get("last_no_work_reason") or "Allocating the bounded research queue",
             None, worker.get("actions_succeeded", 0), "research"),
        node("market", "Market Intelligence", "ACTIVE" if anomaly_rows else "WAITING",
             "Monitoring price, volume, breadth and configured news sources",
             _latest(anomaly_rows), len(anomaly_rows), "research"),
        node("quant", "Quant Research", experiment.get("state") or "WAITING",
             experiment.get("current_contract") or "No experiment currently executing",
             None, campaign["funnel"].get("reported_experiments", 0), "research"),
        node("events", "Event Intelligence", "ACTIVE" if news_rows else "WAITING",
             "Structuring configured event sources" if news_rows else "No significant configured events",
             _latest(news_rows), len(news_rows), "research", "ECONOMY"),
        node("review", "Independent Review", "ACTIVE" if review_rows else "WAITING",
             "Deterministic leakage, sample, robustness and fragility review",
             _latest(review_rows), len(review_rows), "research", "STRONG_REASONING"),
        node("factory", "Strategy Factory", "ACTIVE" if versions else "WAITING",
             "Promoting only ROBUST evidence through deterministic gates",
             None, len(versions), "strategies"),
    ]

    timeline = artifacts.list_artifacts(store, limit=30)["artifacts"]
    today_paper_trades = [t for t in paper_trades if str(t.get("closed_at") or "").startswith(today.date().isoformat())]
    today_versions = [v for v in versions if str(getattr(v, "created_at", "")).startswith(today.date().isoformat())]
    provider_configured = any(p.get("configured") for p in ai["provider_catalog"])
    blockers = list(campaign["paper_readiness"]["blockers"])
    if not provider_configured:
        blockers.insert(0, "NO_PROVIDER_CONFIGURED — deterministic observation, experiments and paper remain independent.")
    return {
        "as_of": iso(now), "phase_11": {"state": "LOCKED",
            "requirements": ["validated paper evidence", "operational history",
                             "risk verification", "explicit human approval"]},
        "today": {"packets": counts.get(rm.DATASET_RESEARCH_PACKET, 0),
                  "hypotheses": counts.get(rm.DATASET_HYPOTHESIS, 0),
                  "experiments": counts.get(rm.DATASET_VERDICT, 0),
                  "rejected": evidence_counts.get("WEAK", 0) + evidence_counts.get("REDUNDANT", 0),
                  "inconclusive": evidence_counts.get("INCONCLUSIVE", 0),
                  "promising": evidence_counts.get("PROMISING", 0),
                  "robust": counts.get("robust_result", 0),
                  "strategies": len(today_versions), "paper_trades": len(today_paper_trades)},
        "organization": organization, "current_research": current,
        "timeline": timeline, "blockers": blockers,
        "research_velocity": campaign.get("velocity") or {},
        "research_governance": {
            "lock_budget": f"{lock_limit} locks / rolling {hypothesis_intake.LOCK_BUDGET_PERIOD_DAYS} days",
            "locks_used": locks_used, "locks_remaining": max(0, lock_limit - locks_used),
            "confirmation_budget": (
                f"{confirmation_limit - lock_limit} reserved confirmation locks / rolling "
                f"{hypothesis_intake.LOCK_BUDGET_PERIOD_DAYS} days"),
            "confirmation_locks_remaining": max(0, confirmation_limit - locks_used),
            "lock_budget_purpose": "capacity-calibrated operational throttle, not multiple-testing correction",
            "multiple_testing": "Bonferroni family-wise correction over explicit research families",
            "contract_status": status_counts},
        "ai": {"provider": ai.get("effective_provider"), "model": ai.get("effective_model"),
               "calls_today": ai["budget"].get("calls_today"),
               "tokens_today": ai["budget"].get("tokens_today"), "cost_today": None,
               "state": "READY" if provider_configured else "NO_PROVIDER_CONFIGURED",
               "recent_traces": len(model_rows)},
        "strategies": {"registered": len(versions), "paper_eligible": len(eligible)},
        "paper": {"performance": paper_perf, "last_cycle": paper_last_cycle,
                  "trade_count": len(paper_trades)},
    }
