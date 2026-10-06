"""Deterministic Telegram test, daily digest, and audited category sender."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from control import ai_budget, notifications, watchdog
from research.brain.worker import worker_status
from research.brain import hypothesis_intake
from research.store import Store
from research.validation import build_snapshot
from scripts.telegram_notify import send_message
from paper import config as paper_config
from paper.portfolio import PaperPortfolio
from paper.store import PaperStore


def deliver(text: str, *, category: str, message_type: str, actor: str) -> dict:
    if category not in notifications.CATEGORIES:
        raise ValueError(f"unknown category {category}")
    if not notifications.category_enabled(category):
        return notifications.record_delivery(category=category, message_type=message_type,
            actor=actor, status="suppressed", latency_ms=0, error="category disabled")
    started = time.monotonic()
    try:
        response = send_message(text)
        return notifications.record_delivery(category=category, message_type=message_type,
            actor=actor, status="delivered", latency_ms=round((time.monotonic()-started)*1000),
            response=response)
    except Exception as exc:
        return notifications.record_delivery(category=category, message_type=message_type,
            actor=actor, status="failed", latency_ms=round((time.monotonic()-started)*1000),
            error=f"{type(exc).__name__}: {exc}")


def _daily_digest_text(snapshot: dict, budget: dict, paper_perf: dict,
                       worker: dict, runtime: dict) -> str:
    f = snapshot["funnel"]
    blockers = snapshot["paper_readiness"]["blockers"]
    datasets = snapshot["data"]["datasets"]
    velocity = snapshot.get("velocity") or {}
    runtime_rows = runtime.get("components") or []
    research_row = next((row for row in runtime_rows
                         if row.get("component") == "RESEARCH_AI"), {})
    research_state = research_row.get("effective_state") or "UNKNOWN"
    queue = (worker.get("last_queue_health") or {}).get("snapshot") or {}
    decision = (worker.get("last_queue_health") or {}).get("decision") or {}
    queue_reasons = decision.get("blocking_reasons") or []
    no_work = worker.get("last_no_work_reason") or "none"
    locks_used = hypothesis_intake.locks_in_period()
    lock_limit = hypothesis_intake.MAX_LOCKS_PER_PERIOD
    ai_note = ""
    if not budget.get("calls_today", 0):
        ai_note = f" · no call required by latest worker: {no_work}"
    return "\n".join([
        "LIVING QUANT — DAILY", "",
        f"System capacity: {snapshot['capacity']['state']}",
        f"Research runtime: {research_state}",
        f"Research heartbeat: {worker.get('last_heartbeat_at') or 'missing'}",
        f"Experiments: {'ACTIVE' if queue.get('locked_runnable_count', 0) else 'WAITING'}",
        f"Paper: {'ACTIVE' if snapshot['paper_readiness']['ready'] else 'WAITING'}", "",
        "Last 24h:",
        f"Observations +{(velocity.get('observations') or {}).get('24h', 0)}",
        f"Hypotheses +{(velocity.get('hypotheses') or {}).get('24h', 0)}",
        f"Evidence +{(velocity.get('evidence') or {}).get('24h', 0)}", "",
        "Cumulative pipeline:",
        f"Research packets {datasets.get('research_packet', 0):,}",
        f"Reported experiments {f['reported_experiments']:,}",
        f"Evidence {f['evidence']:,} · Strategies {f['strategy_versions']:,}",
        f"Paper trades {f['paper_trades']:,} · P&L ₹{paper_perf.get('total_net_pnl', 0):,.2f}",
        f"Drawdown {paper_perf.get('drawdown_pct') if paper_perf.get('drawdown_pct') is not None else '—'}%", "",
        f"AI: {budget.get('calls_today', 0)} calls · {budget.get('tokens_today', 0)} tokens · cost unknown{ai_note}",
        f"Queue: {queue.get('draft_count', 0)} drafts · {queue.get('locked_runnable_count', 0)} runnable",
        f"Research capacity: {locks_used}/{lock_limit} locks in rolling "
        f"{hypothesis_intake.LOCK_BUDGET_PERIOD_DAYS}d",
        f"Backpressure: {'; '.join(queue_reasons) if queue_reasons else 'none'}",
        f"Main finding: {blockers[0] if blockers else 'No material pipeline blocker detected'}",
        "Live Promotion: LOCKED",
    ])


def daily_digest(*, actor: str = "scheduler") -> dict:
    with Store.open() as store:
        snapshot = build_snapshot(store)
    budget = ai_budget.get_budget_state()
    paper_perf = {"total_net_pnl": 0.0, "drawdown_pct": None}
    if paper_config.db_path().is_file():
        paper_store = PaperStore.open_readonly()
        try:
            paper_perf = PaperPortfolio(paper_store).performance_summary()
        finally:
            paper_store.close()
    text = _daily_digest_text(snapshot, budget, paper_perf, worker_status(), watchdog.read_state())
    return deliver(text, category="DAILY_DIGEST", message_type="daily_research_digest", actor=actor)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="command", required=True)
    test = sub.add_parser("test"); test.add_argument("--actor", default="cli-admin")
    digest = sub.add_parser("digest"); digest.add_argument("--actor", default="scheduler")
    args = ap.parse_args(argv)
    result = deliver("LIVING QUANT SYSTEM TEST\nTelegram delivery path is operational.\nNo trading action was performed.",
                     category="SYSTEM", message_type="admin_test", actor=args.actor) if args.command == "test" else daily_digest(actor=args.actor)
    print(json.dumps(result, indent=2)); return 0 if result["status"] == "delivered" else 1


if __name__ == "__main__":
    raise SystemExit(main())
