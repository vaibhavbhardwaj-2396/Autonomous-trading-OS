"""
api/auth.py — a deliberately small bearer-token check.

No identity system, no sessions, no user table: this is a single-operator
read-only dashboard, so a single shared secret (the same posture
scripts/kite_callback_server.py and every other credential in this repo
already takes — see .env.example) is the appropriate amount of machinery,
not a placeholder for something bigger.

    Authorization: Bearer <DASHBOARD_API_TOKEN>

Fails CLOSED: if the server has no token configured, every protected
request is refused with 401 — there is no "auth disabled" mode. The
configured token is compared with hmac.compare_digest (constant-time) so a
network observer cannot learn it a character at a time from response
timing. The token itself is never written to a log line, an error message,
or a response body.
"""

from __future__ import annotations

import hmac
from functools import wraps

from flask import current_app, jsonify, request

from . import config


def _supplied_token() -> str:
    header = request.headers.get("Authorization", "")
    prefix = "Bearer "
    if not header.startswith(prefix):
        return ""
    return header[len(prefix):].strip()


def is_authorized() -> bool:
    configured = config.api_token()
    if not configured:
        return False
    supplied = _supplied_token()
    if not supplied:
        return False
    return hmac.compare_digest(supplied, configured)


def require_auth(view):
    """Route decorator. CORS preflight (OPTIONS) is let through unchecked —
    it carries no credentials and triggers no read of any kind; the actual
    GET that follows is what gets authenticated."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        if request.method == "OPTIONS":
            return view(*args, **kwargs)
        if not is_authorized():
            if not config.api_token():
                current_app.logger.warning(
                    "Rejected request to %s: DASHBOARD_API_TOKEN is not configured.",
                    request.path,
                )
            return jsonify({"error": "unauthorized",
                            "detail": "a valid Authorization: Bearer token is required"}), 401
        return view(*args, **kwargs)

    return wrapped
