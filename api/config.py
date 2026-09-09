"""
api/config.py — environment-driven configuration. No secret is ever
hardcoded here; every sensitive value is read from the process environment
(loaded from .env via python-dotenv, the same convention the rest of this
repository already uses — see .env.example) at call time, never cached at
import time, so tests can set/unset environment variables per-case without
reloading the module.
"""

from __future__ import annotations

import os

# The bearer token protected endpoints require. Unset/empty means "no token
# configured" — every protected request is refused (fail closed), never
# treated as "auth disabled".
ENV_API_TOKEN = "DASHBOARD_API_TOKEN"

# Comma-separated list of origins allowed to make cross-origin requests
# (e.g. "http://localhost:5173,https://bhardwajvaibhav.com"). Empty/unset
# means no origin is allowed — CORS headers are simply not sent, which is
# the safe default a browser will treat as "blocked", never "unrestricted".
ENV_CORS_ORIGINS = "DASHBOARD_CORS_ORIGINS"

# Host/port the API binds to when run directly (python -m api.app). Defaults
# match the rest of this repo's convention (scripts/kite_callback_server.py)
# of binding to localhost only and letting a reverse proxy (Caddy) terminate
# HTTPS and expose it publicly — see docs/API.md.
ENV_HOST = "DASHBOARD_API_HOST"
ENV_PORT = "DASHBOARD_API_PORT"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787


def api_token() -> str | None:
    """The configured bearer token, or None if none is set. Never logged,
    never included in any response."""
    token = os.environ.get(ENV_API_TOKEN, "").strip()
    return token or None


def cors_origins() -> set[str]:
    raw = os.environ.get(ENV_CORS_ORIGINS, "")
    return {o.strip() for o in raw.split(",") if o.strip()}


def bind_host() -> str:
    return os.environ.get(ENV_HOST, DEFAULT_HOST)


def bind_port() -> int:
    try:
        return int(os.environ.get(ENV_PORT, DEFAULT_PORT))
    except ValueError:
        return DEFAULT_PORT
