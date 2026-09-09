"""
Quick health check. Run this any time to see whether the agent is ready to operate:

    cd /root/trading-agent && venv/bin/python scripts/status.py

Checks credentials are present, whether a Kite session exists for today, and — if it does —
whether the broker actually answers and what the account balance is.
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).parent.parent
load_dotenv(PROJECT_ROOT / ".env")
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from kite_auth import has_valid_token_today, get_kite_client, get_login_url  # noqa: E402

OK = "\033[92m✓\033[0m"
NO = "\033[91m✗\033[0m"


def check_env():
    print("\nCredentials")
    required = [
        "KITE_API_KEY",
        "KITE_API_SECRET",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
    ]
    all_present = True
    for key in required:
        value = os.environ.get(key)
        if value:
            # Show only enough to confirm it's the right value, never the whole secret.
            masked = value[:4] + "…" + value[-2:] if len(value) > 8 else "set"
            print(f"  {OK} {key:<22} {masked}")
        else:
            print(f"  {NO} {key:<22} MISSING")
            all_present = False
    return all_present


def check_session():
    print("\nKite session")
    if has_valid_token_today():
        print(f"  {OK} Valid session for today")
        return True
    print(f"  {NO} No session for today — log in via:")
    try:
        print(f"      {get_login_url()}")
    except Exception as e:
        print(f"      (could not build login URL: {e})")
    return False


def check_broker():
    print("\nBroker connectivity")
    try:
        kite = get_kite_client()
        profile = kite.profile()
        margins = kite.margins(segment="equity")
        available = margins.get("available", {}).get("live_balance")
        print(f"  {OK} Connected as {profile.get('user_name')} ({profile.get('user_id')})")
        print(f"  {OK} Equity balance: ₹{available}")
        return True
    except Exception as e:
        print(f"  {NO} {type(e).__name__}: {e}")
        return False


def main():
    print("=" * 52)
    print("  Trading Agent — status")
    print("=" * 52)

    env_ok = check_env()
    session_ok = check_session() if env_ok else False
    broker_ok = check_broker() if session_ok else False

    print("\n" + "-" * 52)
    if env_ok and session_ok and broker_ok:
        print(f"  {OK} READY — credentials, session and broker all good")
    elif env_ok and not session_ok:
        print("  ⚠  Waiting on today's Zerodha login (expected before market open)")
    else:
        print("  ⚠  Not ready — see failures above")
    print("-" * 52 + "\n")


if __name__ == "__main__":
    main()
