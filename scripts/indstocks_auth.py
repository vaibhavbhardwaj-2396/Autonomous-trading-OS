"""
INDstocks session management.

INDstocks tokens expire every 24 hours and are minted from the web UI — there's no OAuth
redirect to automate, which actually simplifies the infrastructure: no callback server, no
Caddy, no subdomain. The daily human step becomes "paste today's token", and the Telegram
control channel makes that a five-second job from a phone.

    python scripts/indstocks_auth.py --status
    python scripts/indstocks_auth.py --set <token>
"""

from __future__ import annotations

import os
import sys
import json
import time
import argparse
import datetime as dt
from pathlib import Path

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).parent.parent
load_dotenv(PROJECT_ROOT / ".env")
sys.path.insert(0, str(PROJECT_ROOT))

TOKEN_FILE = PROJECT_ROOT / "memory" / ".indstocks_session.json"
# SEBI-mandated: INDstocks resets all tokens at 7AM IST daily, regardless of when they
# were issued — NOT a rolling 24h from generation. A token minted at 11pm is dead at 7am,
# ~8 hours later, not 24. This TTL is a safety ceiling for the "is this stale" check, not
# the real expiry rule — --auto (below) is what actually keeps a live token available by
# re-minting after every 7am reset, rather than trusting a duration calculation.
TOKEN_TTL_SECONDS = 24 * 3600
GENERATE_URL = "https://api.indstocks.com/generate/token"

TOKEN_INSTRUCTIONS = (
    "Log in at indstocks.com → Access Tokens → generate today's token, then send it "
    "here as:\n\n`token <paste it>`"
)


def save_token(token: str) -> dict:
    token = token.strip()
    if len(token) < 20:
        raise ValueError("That doesn't look like a token (too short)")
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    blob = {"access_token": token, "saved_at": time.time(),
            "saved_at_iso": dt.datetime.now().isoformat(timespec="seconds")}
    TOKEN_FILE.write_text(json.dumps(blob))
    TOKEN_FILE.chmod(0o600)  # it is a live credential
    return blob


def token_status() -> dict:
    if not TOKEN_FILE.exists():
        return {"valid": False, "reason": "no token stored"}
    try:
        blob = json.loads(TOKEN_FILE.read_text())
    except json.JSONDecodeError:
        return {"valid": False, "reason": "token file unreadable"}

    age = time.time() - blob.get("saved_at", 0)
    if age > TOKEN_TTL_SECONDS:
        return {"valid": False, "reason": f"expired ({age / 3600:.1f}h old, max 24h)",
                "saved_at": blob.get("saved_at_iso")}
    return {
        "valid": True,
        "age_hours": round(age / 3600, 1),
        "expires_in_hours": round((TOKEN_TTL_SECONDS - age) / 3600, 1),
        "saved_at": blob.get("saved_at_iso"),
    }


def has_valid_token_today() -> bool:
    """Name matches the Kite helper so run_cycle.sh stays broker-agnostic."""
    return token_status().get("valid", False)


def generate_token_via_totp() -> dict:
    """Mint a fresh token with no human involved, per api-docs.indstocks.com/Users/:
        POST /generate/token   header x-api-key=<client_id>   body {mpin, totp}
    Requires INDSTOCKS_CLIENT_ID, INDSTOCKS_TOTP_SECRET, INDSTOCKS_MPIN in .env — all as
    sensitive as a trading password, so this only ever reads them from the environment,
    never a CLI argument (which would land in shell history and `ps`).
    """
    import pyotp  # imported lazily so --set/--status don't need it installed

    client_id = os.environ.get("INDSTOCKS_CLIENT_ID")
    secret = os.environ.get("INDSTOCKS_TOTP_SECRET")
    mpin = os.environ.get("INDSTOCKS_MPIN")
    missing = [n for n, v in (("INDSTOCKS_CLIENT_ID", client_id),
                              ("INDSTOCKS_TOTP_SECRET", secret),
                              ("INDSTOCKS_MPIN", mpin)) if not v]
    if missing:
        raise RuntimeError(f"--auto needs {', '.join(missing)} set in .env")

    code = pyotp.TOTP(secret).now()
    r = requests.post(GENERATE_URL,
                      headers={"x-api-key": client_id, "Content-Type": "application/json"},
                      json={"mpin": mpin, "totp": code}, timeout=20)
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {json.dumps(body)[:300]}")

    # The docs describe {"status": "success", "data": {"token": ...}}, but the real
    # response observed on 2026-09-07 is flat: {"token": ..., "expires_in": ...}, no
    # "status" key at all. Accept either shape rather than trust the docs over reality.
    token = body.get("token") or (body.get("data") or {}).get("token")
    if not token:
        raise RuntimeError(f"No token in response: {json.dumps(body)[:300]}")
    return save_token(token)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", metavar="TOKEN", help="store today's access token")
    ap.add_argument("--auto", action="store_true",
                    help="mint today's token via TOTP, no human involved (needs .env setup)")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--verify", action="store_true",
                    help="store/check, then actually call the broker to confirm it works")
    args = ap.parse_args()

    if args.set:
        try:
            save_token(args.set)
        except ValueError as e:
            print(f"Rejected: {e}", file=sys.stderr)
            return 2
        print("Token stored.")
    elif args.auto:
        try:
            generate_token_via_totp()
        except Exception as e:
            print(f"Auto-login failed: {type(e).__name__}: {e}", file=sys.stderr)
            return 2
        print("Token minted via TOTP and stored.")

    status = token_status()
    print(json.dumps(status, indent=2))

    if args.verify:
        from engine.broker_indstocks import INDstocksBroker
        b = INDstocksBroker()
        try:
            funds = b.funds()
            print(f"✓ Broker reachable — available funds ₹{funds:,.2f}")
        except Exception as e:
            print(f"✗ Broker call failed: {type(e).__name__}: {e}", file=sys.stderr)
            return 1

    return 0 if status.get("valid") else 1


if __name__ == "__main__":
    sys.exit(main())
