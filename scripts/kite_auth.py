"""
Kite Connect authentication helper.

Zerodha access tokens expire daily (~6am IST) and there is no way around a fresh login each
trading day. This module makes that login as low-friction as possible:

  1. get_login_url()        -> the Kite login URL, sent to Vaibhav via Telegram each morning.
  2. exchange_request_token -> called by kite_callback_server.py once Kite redirects back
                                with a request_token; completes the exchange automatically.
  3. load_access_token / has_valid_token_today / get_kite_client -> used by every other
     script before doing anything that touches the account.

So the human step really is just: tap the Telegram link, log in on Zerodha's page, done.
Everything after that (token exchange, storage) is automatic.
"""

import os
import json
import datetime
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from kiteconnect import KiteConnect

PROJECT_ROOT = Path(__file__).parent.parent

# Load credentials from the project's .env regardless of how this is invoked (manual shell,
# cron, or systemd). systemd/cron supply the environment themselves; load_dotenv does not
# override anything already set, so both paths work.
load_dotenv(PROJECT_ROOT / ".env")

TOKEN_FILE = PROJECT_ROOT / "memory" / ".kite_session.json"


def _api_key() -> str:
    key = os.environ.get("KITE_API_KEY")
    if not key:
        raise RuntimeError("KITE_API_KEY not set in environment")
    return key


def _api_secret() -> str:
    secret = os.environ.get("KITE_API_SECRET")
    if not secret:
        raise RuntimeError("KITE_API_SECRET not set in environment")
    return secret


def get_login_url() -> str:
    kite = KiteConnect(api_key=_api_key())
    return kite.login_url()


def exchange_request_token(request_token: str) -> str:
    """Call once Kite redirects to the callback server with a request_token."""
    kite = KiteConnect(api_key=_api_key())
    data = kite.generate_session(request_token, api_secret=_api_secret())
    access_token = data["access_token"]
    _save_token(access_token)
    return access_token


def _save_token(access_token: str) -> None:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps({
        "access_token": access_token,
        "date": datetime.date.today().isoformat(),
    }))
    # Session file contains a live credential — keep it out of git.
    os.chmod(TOKEN_FILE, 0o600)


def load_access_token() -> Optional[str]:
    """Returns today's access token if one exists and is still fresh, else None."""
    if not TOKEN_FILE.exists():
        return None
    try:
        data = json.loads(TOKEN_FILE.read_text())
    except json.JSONDecodeError:
        return None
    if data.get("date") != datetime.date.today().isoformat():
        return None
    return data.get("access_token")


def has_valid_token_today() -> bool:
    return load_access_token() is not None


def get_kite_client() -> KiteConnect:
    """For use by research/execution scripts once a session exists for today."""
    token = load_access_token()
    if not token:
        raise RuntimeError(
            "No valid Kite session for today. The pre-market login reminder should have "
            "gone out via Telegram — complete that login first, then re-run."
        )
    kite = KiteConnect(api_key=_api_key())
    kite.set_access_token(token)
    return kite


if __name__ == "__main__":
    # Manual helper: print the login URL directly, useful when testing from the VPS shell.
    print(get_login_url())
