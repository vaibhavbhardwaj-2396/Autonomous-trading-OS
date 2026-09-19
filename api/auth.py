"""
api/auth.py — a deliberately small bearer-token check.

No identity database is required for this single-operator system. It has two
explicit roles: an observer token for read routes and a distinct administrator
token for the two audited configuration writes. The frontend never embeds or
persists the administrator credential beyond the current browser session.

    Authorization: Bearer <DASHBOARD_API_TOKEN>
    Authorization: Bearer <DASHBOARD_ADMIN_TOKEN>  # admin routes only

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
    configured = [t for t in (config.api_token(), config.admin_token()) if t]
    if not configured:
        return False
    supplied = _supplied_token()
    if not supplied:
        return False
    return any(hmac.compare_digest(supplied, candidate) for candidate in configured)


def is_admin_authorized() -> bool:
    configured = config.admin_token()
    supplied = _supplied_token()
    return bool(configured and supplied and hmac.compare_digest(supplied, configured))


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


def require_admin(view):
    """Require the distinct admin token; the embedded observer token is refused."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if request.method == "OPTIONS":
            return view(*args, **kwargs)
        if not is_admin_authorized():
            if not config.admin_token():
                current_app.logger.warning(
                    "Rejected admin request to %s: DASHBOARD_ADMIN_TOKEN is not configured.",
                    request.path,
                )
            return jsonify({"error": "admin_unauthorized",
                            "detail": "a valid administrator token is required"}), 403
        return view(*args, **kwargs)
    return wrapped
