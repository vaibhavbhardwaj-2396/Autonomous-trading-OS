"""
api/app.py — the dashboard and administrator HTTP API.
All reads use the observer bearer token. Five narrowly scoped writes use
the separate administrator token and cannot place or modify an order.

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
    GET /research/worker-status      |
    GET /research/data-quality       |
    GET /operations/status           |
    GET /validation/status           |
    GET /strategies                  |
    GET /backtests                  /

    GET /paper/account               \\
    GET /paper/positions              |  Slice AA — paper/shadow engine
    GET /paper/orders                 |  reads (also protected). See
    GET /paper/trades                 |  api/paper_data.py. Distinct from
    GET /paper/strategies             |  the live routes above on purpose —
    GET /paper/performance           /   never merged into /account etc.

    GET  /control/status            \\  Priority Phase 4 — the global
    GET  /admin/status               |  verifies administrator credentials
    POST /control/mode                /  RUNNING/PAUSED/SAFE_MODE/STOPPED
                                         control layer. POST changes ONLY
                                         the mode (+ a required reason/
                                         actor) — see api/runtime_bridge.py.
                                         It can never place an order,
                                         never touches capital, and can
                                         only ever ADD a live-trading
                                         pause, never remove one.

    GET  /resources/status           —  outcome 1 — the Resource Governor's
                                         current CPU/RAM/disk snapshot and
                                         HEALTHY/CONSTRAINED/PRESSURED/
                                         CRITICAL classification.

    GET  /ai/status                  \\  outcome 2 — the AI provider
    POST /ai/config                   /  abstraction. POST selects which
                                         provider/model the NEXT research
                                         AI call uses (+ required actor/
                                         reason) — see control/ai_config.py.
                                         Never calls a provider, never
                                         touches research state or risk
                                         gates.

    GET  /artifacts                  \\  outcome 2 — the unified,
    GET  /artifacts/<id>               /  read-only artifact explorer (model
                                         interactions, detections,
                                         hypotheses, experiments, evidence,
                                         opportunity events, system events)
                                         — see api/artifacts.py.

tests/test_deployment_readiness.py and tests/test_broker_truth.py carry a
narrow, explicit, named exception list for these write routes; every
other route on this app is still mechanically checked to be GET-only.

Every GET route is a thin function: parse query params -> call one
api.data (or api.paper_data / api.artifacts / api.ai_status) function ->
jsonify the result. No business logic, no import of engine.execute /
engine.guardrails.validate_order|save_state / engine.broker* /
research.brain.hypothesis_intake.approve_and_lock / research.experiments.
runner.run_experiment / research.experiments.strategy_backtest.run_backtest /
strategies.registry.save_version / paper.runner.run_paper_cycle anywhere in
this file — grep for "import" to re-verify. /control/mode and /ai/config
are deliberate exceptions to "no write" (not to any of the imports
above, which remain absent even from those routes) — see
api/runtime_bridge.py and control/ai_config.py respectively.

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
from . import runtime_bridge  # noqa: E402 — Priority Phase 4: global control layer
from . import artifacts  # noqa: E402 — outcome 2: the unified artifact explorer
from . import ai_status  # noqa: E402 — outcome 2: AI provider config + budget
from . import operations  # noqa: E402 — bounded, read-only subsystem health
from . import validation  # noqa: E402 — Phase 10.5 read-only campaign status
from . import notifications as notification_api  # noqa: E402
from . import runtime_status  # noqa: E402
from . import system_status, activity  # noqa: E402
from control import components as component_control  # noqa: E402
from control import notifications as notification_config  # noqa: E402
from control import runtime as ctrl  # noqa: E402
from control import resources as rg  # noqa: E402 — outcome 1: the Resource Governor
from control import capacity as capacity_planner  # noqa: E402 — Phase 10.5 allocation plan

APP_NAME = "living-quant-api"
APP_VERSION = "1.4.0"


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
            resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
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

    @app.route("/research/worker-status", methods=["GET"])
    @auth.require_auth
    def research_worker_status():
        return jsonify(data.get_research_worker_status())

    @app.route("/research/data-quality", methods=["GET"])
    @auth.require_auth
    def research_data_quality():
        return jsonify(data.get_data_quality())

    @app.route("/operations/status", methods=["GET"])
    @auth.require_auth
    def operations_status():
        return jsonify(operations.get_operations_status())

    @app.route("/runtime/status", methods=["GET"])
    @auth.require_auth
    def runtime_status_route():
        return jsonify(runtime_status.get_runtime_status(data.get_default_store()))

    @app.route("/system/status", methods=["GET"])
    @auth.require_auth
    def system_status_route():
        return jsonify(system_status.get_status())

    @app.route("/activity", methods=["GET"])
    @auth.require_auth
    def activity_route():
        limit, err = _int_query_param("limit", 50)
        if err: return err
        offset, err = _int_query_param("offset", 0)
        if err: return err
        return jsonify(activity.get_activity(data.get_default_store(),
            limit=max(0, min(limit, 200)), offset=max(0, offset), kind=request.args.get("kind")))

    @app.route("/validation/status", methods=["GET"])
    @auth.require_auth
    def validation_status():
        limit, err = _int_query_param("history_limit", 30)
        if err:
            return err
        return jsonify(validation.get_validation_status(
            data.get_default_store(), history_limit=max(0, min(limit, 365))))

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

    # -- Global runtime control (Priority Phase 4) --------------------------
    # The ONE write-capable surface on this API. See api/runtime_bridge.py
    # for the full safety argument — this can only ever ADD a live-trading
    # pause (via the pre-existing engine.journal.set_pause), never remove
    # one; it can never place, size, or modify an order.

    @app.route("/control/status", methods=["GET"])
    @auth.require_auth
    def control_status():
        return jsonify(runtime_bridge.get_full_status())

    @app.route("/admin/status", methods=["GET"])
    @auth.require_admin
    def admin_status():
        return jsonify({"authorized": True, "role": "administrator"})

    @app.route("/notifications/status", methods=["GET"])
    @auth.require_auth
    def notifications_status():
        return jsonify(notification_api.get_status())

    @app.route("/notifications/test", methods=["POST"])
    @auth.require_admin
    def notifications_test():
        body = request.get_json(silent=True) or {}
        result = notification_api.send_test(actor=body.get("actor") or "dashboard-admin")
        return jsonify(result), (200 if result.get("status") == "delivered" else 503)

    @app.route("/notifications/config", methods=["POST"])
    @auth.require_admin
    def notifications_config():
        body = request.get_json(silent=True) or {}
        reason = body.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            return jsonify({"error": "bad_request", "detail": "reason is required"}), 400
        try:
            notification_config.set_config(
                enabled=body.get("enabled") if isinstance(body.get("enabled"), bool) else True,
                categories=body.get("categories") if isinstance(body.get("categories"), dict) else {},
                thresholds=body.get("thresholds") if isinstance(body.get("thresholds"), dict) else {},
                actor=body.get("actor") or "dashboard-admin", reason=reason)
        except ValueError as exc:
            return jsonify({"error": "bad_request", "detail": str(exc)}), 400
        return jsonify(notification_api.get_status())

    @app.route("/control/mode", methods=["POST"])
    @auth.require_admin
    def control_mode():
        body = request.get_json(silent=True) or {}
        mode = body.get("mode")
        reason = body.get("reason")
        actor = body.get("actor") or "dashboard"
        if not isinstance(mode, str) or mode not in ctrl.RUNTIME_MODES:
            return jsonify({"error": "bad_request",
                           "detail": f"'mode' must be one of {list(ctrl.RUNTIME_MODES)}"}), 400
        if not isinstance(reason, str) or not reason.strip():
            return jsonify({"error": "bad_request",
                           "detail": "'reason' is required and must be non-empty"}), 400
        if not isinstance(actor, str) or not actor.strip():
            return jsonify({"error": "bad_request",
                           "detail": "'actor' must be a non-empty string when given"}), 400
        runtime_bridge.apply_mode_change(mode, reason=reason, actor=actor)
        return jsonify(runtime_bridge.get_full_status())

    @app.route("/control/component", methods=["POST"])
    @auth.require_admin
    def control_component():
        body = request.get_json(silent=True) or {}
        try:
            result = component_control.set_state(
                body.get("component"), body.get("desired_state"),
                reason=body.get("reason") or "", actor=body.get("actor") or "dashboard-admin",
                resume_policy=body.get("resume_policy") or "MANUAL")
        except component_control.InvalidComponentState as exc:
            return jsonify({"error":"bad_request", "detail":str(exc)}), 400
        return jsonify(result)

    # -- Resource Governor (outcome 1) --------------------------------------

    @app.route("/resources/status", methods=["GET"])
    @auth.require_auth
    def resources_status():
        measured = rg.get_resource_state()
        result = dict(measured)
        result["capacity_plan"] = capacity_planner.plan(measured)
        return jsonify(result)

    # -- AI provider/model configuration + budget (outcome 2) --------------
    # POST /ai/config is the SECOND write-capable route this API has ever
    # had (the first was /control/mode). It only ever selects which
    # provider/model the NEXT research AI call should use — see
    # control/ai_config.py's own docstring for why that is safe: it never
    # calls a provider, never verifies one works, and cannot touch live
    # trading, risk gates, or research state by construction (imports
    # nothing from engine/ or research/brain/hypothesis_intake).

    @app.route("/ai/status", methods=["GET"])
    @auth.require_auth
    def ai_status_route():
        return jsonify(ai_status.get_ai_status())

    @app.route("/ai/config", methods=["POST"])
    @auth.require_admin
    def ai_config_route():
        from control import ai_config as ai_config_mod
        body = request.get_json(silent=True) or {}
        provider = body.get("provider")
        model = body.get("model")
        actor = body.get("actor") or "dashboard"
        reason = body.get("reason")
        if not isinstance(provider, str) or provider not in ai_config_mod.KNOWN_PROVIDERS:
            return jsonify({"error": "bad_request",
                           "detail": f"'provider' must be one of {list(ai_config_mod.KNOWN_PROVIDERS)}"}), 400
        if model is not None and not isinstance(model, str):
            return jsonify({"error": "bad_request", "detail": "'model' must be a string or null"}), 400
        if not isinstance(reason, str) or not reason.strip():
            return jsonify({"error": "bad_request",
                           "detail": "'reason' is required and must be non-empty"}), 400
        if not isinstance(actor, str) or not actor.strip():
            return jsonify({"error": "bad_request",
                           "detail": "'actor' must be a non-empty string when given"}), 400
        ai_status.set_ai_provider(provider=provider, model=model, actor=actor, reason=reason,
                                  role_mappings=body.get("role_mappings"),
                                  fallback=body.get("fallback"))
        return jsonify(ai_status.get_ai_status())

    # -- Artifacts (outcome 2) — the unified, read-only artifact explorer --

    @app.route("/artifacts", methods=["GET"])
    @auth.require_auth
    def artifacts_list():
        artifact_type = request.args.get("type")
        if artifact_type and artifact_type not in artifacts.ALL_ARTIFACT_TYPES:
            return jsonify({"error": "bad_request",
                           "detail": f"'type' must be one of {list(artifacts.ALL_ARTIFACT_TYPES)}"}), 400
        limit, err = _int_query_param("limit", 50)
        if err:
            return err
        offset, err = _int_query_param("offset", 0)
        if err:
            return err
        sort = request.args.get("sort") or "timestamp"
        order = request.args.get("order") or "desc"
        if sort not in ("timestamp", "type", "status", "source", "summary"):
            return jsonify({"error": "bad_request", "detail": "invalid artifact sort field"}), 400
        if order not in ("asc", "desc"):
            return jsonify({"error": "bad_request", "detail": "artifact order must be asc or desc"}), 400
        return jsonify(artifacts.list_artifacts(
            data.get_default_store(), type=artifact_type, limit=max(0, min(limit, 200)),
            offset=max(0, offset), query=request.args.get("q"),
            status=request.args.get("status"), source=request.args.get("source"),
            sort=sort, order=order))

    @app.route("/artifacts/<path:artifact_id>", methods=["GET"])
    @auth.require_auth
    def artifact_detail(artifact_id: str):
        result = artifacts.get_artifact(data.get_default_store(), artifact_id)
        if result is None:
            return jsonify({"error": "not_found", "detail": f"no artifact {artifact_id!r}"}), 404
        return jsonify(result)

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
