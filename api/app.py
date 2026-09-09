"""
api/app.py — Phase 4 Slice Z: the read-only HTTP API.

    GET /health                     — public, app liveness only

    GET /account                    \\
    GET /positions                   |
    GET /orders                      |  all protected (Authorization: Bearer
    GET /trades                      |  <DASHBOARD_API_TOKEN> required —
    GET /risk                        |  see api/auth.py)
    GET /regime                      |
    GET /research/drafts             |
    GET /research/evidence           |
    GET /research/areas              |
    GET /strategies                  |
    GET /backtests                  /

    GET /paper/account               \\
    GET /paper/positions              |  Slice AA — paper/shadow engine
    GET /paper/orders                 |  reads (also protected). See
    GET /paper/trades                 |  api/paper_data.py. Distinct from
    GET /paper/strategies             |  the live routes above on purpose —
    GET /paper/performance           /   never merged into /account etc.

Every route is a thin function: parse query params -> call one api.data (or
api.paper_data) function -> jsonify the result. No business logic, no
write, no import of engine.execute / engine.guardrails.validate_order|
save_state / engine.broker* / research.brain.hypothesis_intake.
approve_and_lock / research.experiments.runner.run_experiment /
research.experiments.strategy_backtest.run_backtest /
strategies.registry.save_version / paper.runner.run_paper_cycle anywhere in
this file — grep for "import" to re-verify.

Run locally:  python -m api.app          (see docs/API.md for env setup)
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from flask import Flask, Response, jsonify, request  # noqa: E402
from werkzeug.exceptions import HTTPException  # noqa: E402

from . import auth, config, data  # noqa: E402
from . import paper_data  # noqa: E402 — Slice AA: read-only paper/shadow endpoints

APP_NAME = "living-quant-api"
APP_VERSION = "1.0.0"


def _int_query_param(name: str, default: Optional[int]) -> tuple[Optional[int], Optional[Response]]:
    """Parse an optional integer query param. Returns (value, error_response) —
    error_response is None on success, or a ready-to-return 400 Response on
    a malformed value. `?<name>=` (empty) or an absent param means "use the
    default"; `?<name>=0` is a legitimate, distinct value (show none) and is
    never treated as falsy/absent — the same convention every bounded
    research.brain.* function in this repository already follows."""
    raw = request.args.get(name)
    if raw is None or raw == "":
        return default, None
    try:
        return int(raw), None
    except ValueError:
        return None, (jsonify({"error": "bad_request",
                               "detail": f"'{name}' must be an integer"}), 400)


def create_app() -> Flask:
    app = Flask(APP_NAME)

    # -- CORS --------------------------------------------------------------
    # Explicit allow-list only (api.config.cors_origins(), from
    # DASHBOARD_CORS_ORIGINS) — never a wildcard. An origin not on the list
    # simply gets no Access-Control-Allow-Origin header, which every browser
    # treats as "blocked". See docs/API.md for local/production values.

    @app.after_request
    def add_cors_headers(resp):
        origin = request.headers.get("Origin")
        if origin and origin in config.cors_origins():
            resp.headers["Access-Control-Allow-Origin"] = origin
            resp.headers["Vary"] = "Origin"
            resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
            resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
        return resp

    # -- Error handling — never leak an internal exception to the client ---

    @app.errorhandler(data.DataSourceError)
    def _data_unavailable(e: data.DataSourceError):
        app.logger.warning("data source unavailable: %s", e, exc_info=e.__cause__ or e)
        return jsonify({"error": "service_unavailable", "detail": str(e)}), 503

    @app.errorhandler(paper_data.PaperDataSourceError)
    def _paper_data_unavailable(e: paper_data.PaperDataSourceError):
        app.logger.warning("paper data source unavailable: %s", e, exc_info=e.__cause__ or e)
        return jsonify({"error": "service_unavailable", "detail": str(e)}), 503

    @app.errorhandler(404)
    def _not_found(e):
        return jsonify({"error": "not_found"}), 404

    @app.errorhandler(405)
    def _method_not_allowed(e):
        return jsonify({"error": "method_not_allowed"}), 405

    @app.errorhandler(HTTPException)
    def _http_error(e: HTTPException):
        # Any other werkzeug/Flask routing-level exception (400 bad request
        # from a malformed request line, etc.) — reuse its own status code
        # and name, never fall through to the generic 500 below.
        return jsonify({"error": e.name.lower().replace(" ", "_")}), e.code

    @app.errorhandler(Exception)
    def _internal_error(e):
        # Only a genuinely unexpected exception reaches here: HTTPException
        # (404/405/400/...) is handled above and never falls through to
        # this generic 500. The real exception is logged server-side only —
        # the client gets a generic message, no stack trace, no file path,
        # no exception class name that could hint at internals.
        app.logger.exception("unhandled error serving %s", request.path)
        return jsonify({"error": "internal_error"}), 500

    # -- /health — public, app liveness only --------------------------------
    # Deliberately does not check the broker or the research factory: this
    # slice's own instructions are explicit that /health must not "pretend
    # the broker or research factory is healthy unless that can be checked
    # safely and deterministically" — and checking either safely would mean
    # a live broker call or opening the research store, neither of which
    # belongs in an uptime probe that a monitor may hit every few seconds.

    @app.route("/health", methods=["GET"])
    def health():
        return jsonify({"status": "ok", "service": APP_NAME, "version": APP_VERSION})

    # -- Trading (account / positions / orders / trades / risk) -----------

    @app.route("/account", methods=["GET"])
    @auth.require_auth
    def account():
        return jsonify(data.get_account())

    @app.route("/positions", methods=["GET"])
    @auth.require_auth
    def positions():
        return jsonify(data.get_positions())

    @app.route("/orders", methods=["GET"])
    @auth.require_auth
    def orders():
        limit, err = _int_query_param("limit", 100)
        if err:
            return err
        return jsonify(data.get_orders(limit=limit))

    @app.route("/trades", methods=["GET"])
    @auth.require_auth
    def trades():
        limit, err = _int_query_param("limit", 100)
        if err:
            return err
        return jsonify(data.get_trades(limit=limit))

    @app.route("/risk", methods=["GET"])
    @auth.require_auth
    def risk():
        return jsonify(data.get_risk())

    @app.route("/regime", methods=["GET"])
    @auth.require_auth
    def regime():
        return jsonify(data.get_regime())

    # -- Research -----------------------------------------------------------

    @app.route("/research/drafts", methods=["GET"])
    @auth.require_auth
    def research_drafts():
        limit, err = _int_query_param("limit", data.draft_backlog.DEFAULT_BACKLOG_LIMIT)
        if err:
            return err
        return jsonify(data.get_research_drafts(data.get_default_store(), limit=limit))

    @app.route("/research/evidence", methods=["GET"])
    @auth.require_auth
    def research_evidence():
        limit, err = _int_query_param("limit", data.digest_mod.DEFAULT_EVIDENCE_LIMIT)
        if err:
            return err
        return jsonify(data.get_research_evidence(data.get_default_store(), limit=limit))

    @app.route("/research/areas", methods=["GET"])
    @auth.require_auth
    def research_areas_route():
        return jsonify(data.get_research_areas(data.get_default_store()))

    # -- Strategies / backtests ---------------------------------------------

    @app.route("/strategies", methods=["GET"])
    @auth.require_auth
    def strategies():
        limit, err = _int_query_param("limit", None)
        if err:
            return err
        return jsonify(data.get_strategies(limit=limit))

    @app.route("/backtests", methods=["GET"])
    @auth.require_auth
    def backtests():
        limit, err = _int_query_param("limit", 50)
        if err:
            return err
        return jsonify(data.get_backtests(data.get_default_store(), limit=limit))

    # -- Paper / shadow (Slice AA) -------------------------------------------
    # Every route below is GET-only and reads exclusively through
    # api/paper_data.py -> paper/store.py. None of them can trigger a paper
    # trading cycle, open/close a paper position, or touch anything live —
    # see api/paper_data.py's own module docstring. Response shapes are
    # deliberately parallel to the live /account, /positions, /orders,
    # /trades routes above, with "paper"/"PAPER" made unmistakable in every
    # payload (never silently mixed into the live endpoints).

    @app.route("/paper/account", methods=["GET"])
    @auth.require_auth
    def paper_account():
        return jsonify(paper_data.get_paper_account())

    @app.route("/paper/positions", methods=["GET"])
    @auth.require_auth
    def paper_positions():
        return jsonify(paper_data.get_paper_positions())

    @app.route("/paper/orders", methods=["GET"])
    @auth.require_auth
    def paper_orders():
        limit, err = _int_query_param("limit", 100)
        if err:
            return err
        return jsonify(paper_data.get_paper_orders(limit=limit))

    @app.route("/paper/trades", methods=["GET"])
    @auth.require_auth
    def paper_trades():
        limit, err = _int_query_param("limit", 100)
        if err:
            return err
        return jsonify(paper_data.get_paper_trades(limit=limit))

    @app.route("/paper/strategies", methods=["GET"])
    @auth.require_auth
    def paper_strategies():
        return jsonify(paper_data.get_paper_strategies())

    @app.route("/paper/performance", methods=["GET"])
    @auth.require_auth
    def paper_performance():
        return jsonify(paper_data.get_paper_performance())

    return app


def main() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    app = create_app()
    if not config.api_token():
        app.logger.warning(
            "DASHBOARD_API_TOKEN is not set — every protected endpoint will return 401 "
            "until it is configured. See docs/API.md.")
    app.run(host=config.bind_host(), port=config.bind_port())


if __name__ == "__main__":
    main()
