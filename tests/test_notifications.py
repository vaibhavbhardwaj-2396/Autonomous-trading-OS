"""Notification policy, auditing and deterministic digest tests."""
from __future__ import annotations
import tempfile
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from control import notifications
from scripts import notification_service

passed = failed = 0
def check(name, condition):
    global passed, failed
    passed += bool(condition); failed += not bool(condition)
    print(("  ✓ " if condition else "  ✗ ") + name)

tmp = Path(tempfile.mkdtemp())
cfg, lock, audit = tmp/"config.json", tmp/"lock", tmp/"audit.jsonl"
state = notifications.set_config(enabled=True, categories={"SYSTEM": True, "PAPER": False},
    thresholds={"research_milestone_count": 10, "repeated_error_count": 2},
    actor="admin", reason="test", path=cfg, lock_path=lock)
check("configuration is audited and category-specific", state["changed_by"] == "admin" and not state["categories"]["PAPER"])
check("credentials never appear in notification configuration", "TOKEN" not in cfg.read_text().upper())
row = notifications.record_delivery(category="SYSTEM", message_type="admin_test", actor="admin",
    status="delivered", latency_ms=12, response={"ok": True, "message_id": 7}, path=audit)
check("delivery audit records safe Telegram response metadata", row["status"] == "delivered" and notifications.recent_audit(path=audit)[0]["telegram_response"]["message_id"] == 7)

old_send = notification_service.send_message
old_record = notifications.record_delivery
old_enabled = notifications.category_enabled
try:
    notification_service.send_message = lambda text: {"ok": True, "message_id": 8}
    notifications.category_enabled = lambda category: True
    notifications.record_delivery = lambda **kw: kw
    sent = notification_service.deliver("LIVING QUANT SYSTEM TEST", category="SYSTEM", message_type="admin_test", actor="admin")
    check("test delivery uses the labelled message and reports delivered", sent["status"] == "delivered" and sent["response"]["ok"])
finally:
    notification_service.send_message = old_send
    notifications.record_delivery = old_record
    notifications.category_enabled = old_enabled

snapshot = {
    "funnel": {"reported_experiments": 6, "evidence": 1,
               "strategy_versions": 0, "paper_trades": 0},
    "paper_readiness": {"ready": False, "blockers": ["No eligible strategy"]},
    "data": {"datasets": {"research_packet": 63}},
    "velocity": {"observations": {"24h": 612}, "hypotheses": {"24h": 0},
                 "evidence": {"24h": 0}},
    "capacity": {"state": "NORMAL"},
}
worker = {
    "last_heartbeat_at": "2026-10-01T12:30:07+05:30",
    "last_no_work_reason": "1 promotion attempt(s), none succeeded — skipped_budget",
    "last_queue_health": {"snapshot": {"draft_count": 99, "locked_runnable_count": 0},
                          "decision": {"blocking_reasons": ["draft backlog 99 reached high-water mark 3"]}},
}
runtime = {"components": [{"component": "RESEARCH_AI", "effective_state": "HEALTHY"}]}
digest = notification_service._daily_digest_text(
    snapshot, {"calls_today": 0, "tokens_today": 0},
    {"total_net_pnl": 0.0, "drawdown_pct": 0.0}, worker, runtime)
check("digest distinguishes rolling activity from cumulative totals",
      "Last 24h:" in digest and "Cumulative pipeline:" in digest
      and "Reported experiments 6" in digest)
check("zero AI usage includes the truthful worker/backpressure reason",
      "0 calls" in digest and "skipped_budget" in digest
      and "draft backlog 99 reached high-water mark 3" in digest)
check("digest reports the observed runtime heartbeat instead of hardcoding ACTIVE",
      "Research runtime: HEALTHY" in digest
      and "Research heartbeat: 2026-10-01T12:30:07+05:30" in digest)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
