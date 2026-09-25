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

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
