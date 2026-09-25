"""Notification control-plane adapter; never exposes Telegram credentials."""

from __future__ import annotations

import os
from collections import Counter

from control import notifications
from scripts import notification_service


def get_status() -> dict:
    rows = notifications.recent_audit()
    delivered = [r for r in rows if r.get("status") == "delivered"]
    failed = [r for r in rows if r.get("status") == "failed"]
    today = notifications.now_iso()[:10]
    return {
        "configured": bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")),
        "preferences": notifications.get_config(),
        "last_successful_message": delivered[-1] if delivered else None,
        "last_failed_message": failed[-1] if failed else None,
        "last_heartbeat": next((r for r in reversed(rows) if r.get("message_type") in ("daily_research_digest", "system_heartbeat")), None),
        "messages_sent_today": sum(1 for r in delivered if str(r.get("timestamp", "")).startswith(today)),
        "delivery_status_counts": dict(Counter(r.get("status") for r in rows)),
        "recent": rows[-25:],
    }


def send_test(*, actor: str) -> dict:
    return notification_service.deliver(
        "LIVING QUANT SYSTEM TEST\nTelegram delivery path is operational.\nNo trading action was performed.",
        category="SYSTEM", message_type="admin_test", actor=actor)
