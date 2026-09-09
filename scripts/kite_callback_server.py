"""
Tiny HTTP server that receives Zerodha's login redirect and completes the token exchange
automatically — so the daily human step is just "tap the link, log in," nothing to
copy/paste.

Runs persistently on the VPS behind Caddy (see docs/VPS_DEPLOY.md for the HTTPS setup —
Kite Connect requires an https redirect URL, so this must sit behind a reverse proxy with a
valid cert, not be hit directly over http).

Register https://<your-caddy-domain>/callback as the redirect URL in your Kite Connect app
settings at developers.kite.trade.
"""

from flask import Flask, request

from kite_auth import exchange_request_token

app = Flask(__name__)


@app.route("/callback")
def callback():
    status = request.args.get("status")
    request_token = request.args.get("request_token")

    if status != "success" or not request_token:
        return "Login did not complete. Close this tab and tap the link again.", 400

    try:
        exchange_request_token(request_token)
    except Exception as e:
        return f"Login succeeded but token exchange failed: {e}", 500

    return "Logged in — the agent has a session for today. You can close this tab."


@app.route("/healthz")
def healthz():
    return "ok"


if __name__ == "__main__":
    # Bind to localhost only — Caddy proxies to this from the public HTTPS port.
    app.run(host="127.0.0.1", port=5000)
