"""Deterministic Telegram test, daily digest, and audited category sender."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from control import ai_budget, notifications
from research.store import Store
from research.validation import build_snapshot
from scripts.telegram_notify import send_message


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


def daily_digest(*, actor: str = "scheduler") -> dict:
    with Store.open() as store:
        snapshot = build_snapshot(store)
    f = snapshot["funnel"]
    budget = ai_budget.get_budget_state()
    blockers = snapshot["paper_readiness"]["blockers"]
    text = "\n".join([
        "LIVING QUANT — DAILY", "",
        f"Observations: {f['observations']:,}",
        f"News: {snapshot['data']['datasets'].get('news_arrival', 0):,}",
        f"Hypotheses: {f['hypotheses']:,}", f"Experiments: {f['reported_experiments']:,}",
        f"Accepted Evidence: {f['evidence']:,}", f"Strategies: {f['strategy_versions']:,}",
        f"Paper Trades: {f['paper_trades']:,}", "",
        f"Capacity: {snapshot['capacity']['state']}",
        f"AI Calls Today: {budget.get('calls_today', 0)}",
        f"Current Bottleneck: {blockers[0] if blockers else 'None detected'}",
        "Live Promotion: LOCKED",
    ])
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
