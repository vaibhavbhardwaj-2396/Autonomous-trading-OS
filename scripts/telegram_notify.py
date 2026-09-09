"""Minimal Telegram notifier used for trade alerts, daily summaries, and the pre-market
login reminder link."""

import os
from pathlib import Path

import requests
from dotenv import load_dotenv

# Works whether invoked manually, from cron, or from systemd.
load_dotenv(Path(__file__).parent.parent / ".env")


def send_message(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set in environment")

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(url, data={"chat_id": chat_id, "text": text}, timeout=15)
    resp.raise_for_status()


if __name__ == "__main__":
    send_message("Trading agent: Telegram notifications are working.")
