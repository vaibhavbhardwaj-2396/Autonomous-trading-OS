"""Minimal Telegram notifier used for trade alerts, daily summaries, and the pre-market
login reminder link."""

import os
from pathlib import Path

import requests
from dotenv import load_dotenv

# Works whether invoked manually, from cron, or from systemd.
# CLI/cron jobs normally run as root and may load the project's private env.
# The dashboard API intentionally runs as an unprivileged user and cannot read
# that file. Importing this module must still be safe there; systemd supplies a
# separate Telegram-only EnvironmentFile for the authenticated admin action.
try:
    load_dotenv(Path(__file__).parent.parent / ".env")
except OSError:
    pass


def send_message(text: str) -> dict:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set in environment")

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(url, data={"chat_id": chat_id, "text": text}, timeout=15)
    resp.raise_for_status()
    payload = resp.json()
    result = payload.get("result") or {}
    return {"ok": bool(payload.get("ok")), "message_id": result.get("message_id"),
            "date": result.get("date"), "chat_id_confirmed": bool((result.get("chat") or {}).get("id"))}


if __name__ == "__main__":
    send_message("Trading agent: Telegram notifications are working.")
