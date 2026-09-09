"""
Run by cron shortly before market open each trading day (e.g. 8:30am IST). If there's no
valid Kite session yet for today, sends the login link via Telegram so a ~30-second tap can
complete it before the pre-market routine needs to trade.
"""

from kite_auth import has_valid_token_today, get_login_url
from telegram_notify import send_message


def main():
    if has_valid_token_today():
        return  # already logged in today

    url = get_login_url()
    send_message(
        "Good morning! The agent needs today's Zerodha login before it can trade.\n"
        f"Tap to log in: {url}\n"
        "Takes about 30 seconds, nothing else needed after that."
    )


if __name__ == "__main__":
    main()
