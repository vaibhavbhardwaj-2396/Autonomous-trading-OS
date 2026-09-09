"""
tests/test_paper_api.py — the /paper/* dashboard API routes (Slice AA):
authentication, GET-only, correct response shapes against both an empty and
a populated paper store, the unavailable-store 503 path, and — critically —
that these routes never alter live state and the live routes never surface
paper data. Deliberately a NEW file, not an edit to tests/test_api.py (see
the AA spec section 27's protected-files caution and paper/__init__.py's
own "don't blur LIVE/PAPER" boundary) — test_api.py is left untouched.

Run with:  python -m tests.test_paper_api
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ["DASHBOARD_API_TOKEN"] = "paper-test-secret-token"
os.environ["DASHBOARD_CORS_ORIGINS"] = "http://localhost:5173"

import tempfile  # noqa: E402

os.environ["PAPER_DB_PATH"] = tempfile.mktemp(suffix="-paper-api-test.db")

from api import app as api_app_mod          # noqa: E402
from api import paper_data                  # noqa: E402
from tests.paper_fixtures import (          # noqa: E402
    PaperTestEnv, BuyEveryTimeStrategy, fixed_history_df,
)
from paper.runner import run_paper_cycle    # noqa: E402

PASSED, FAILED = 0, 0


def check(name, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  ✓ {name}")
    else:
        FAILED += 1
        print(f"  ✗ {name}")
        if detail:
            print(f"      {detail}")


TOKEN = os.environ["DASHBOARD_API_TOKEN"]
AUTH = {"Authorization": f"Bearer {TOKEN}"}

app = api_app_mod.create_app()
client = app.test_client()

PAPER_ROUTES = [
    "/paper/account", "/paper/positions", "/paper/orders", "/paper/trades",
    "/paper/strategies", "/paper/performance",
]


# ---------------------------------------------------------------------------
print("\n--- authentication: every /paper/* route is protected ---")
# ---------------------------------------------------------------------------

for route in PAPER_ROUTES:
    r = client.get(route)
    check(f"GET {route} with no Authorization header -> 401", r.status_code == 401)

for route in PAPER_ROUTES:
    r = client.get(route, headers={"Authorization": "Bearer wrong-token"})
    check(f"GET {route} with a WRONG token -> 401", r.status_code == 401)

for route in PAPER_ROUTES:
    r = client.get(route, headers=AUTH)
    check(f"GET {route} with a VALID token -> 200", r.status_code == 200)


# ---------------------------------------------------------------------------
print("\n--- GET-only: no write verb is accepted on any /paper/* route ---")
# ---------------------------------------------------------------------------

for route in PAPER_ROUTES:
    for method, client_fn in (("POST", client.post), ("PUT", client.put),
                              ("DELETE", client.delete), ("PATCH", client.patch)):
        r = client_fn(route, headers=AUTH)
        check(f"{method} {route} -> 405 (method not allowed) — no write path exists",
              r.status_code == 405)


# ---------------------------------------------------------------------------
print("\n--- shape against an EMPTY paper store ---")
# ---------------------------------------------------------------------------

r = client.get("/paper/account", headers=AUTH)
body = r.get_json()
check("empty /paper/account has the documented fields and a PAPER label",
      "initial_capital" in body and "cash" in body and "PAPER" in body.get("label", ""))

r = client.get("/paper/positions", headers=AUTH)
body = r.get_json()
check("empty /paper/positions -> positions: [], count: 0",
      body["positions"] == [] and body["count"] == 0)

r = client.get("/paper/orders", headers=AUTH)
check("empty /paper/orders -> orders: []", r.get_json()["orders"] == [])

r = client.get("/paper/trades", headers=AUTH)
check("empty /paper/trades -> trades: []", r.get_json()["trades"] == [])

r = client.get("/paper/strategies", headers=AUTH)
check("empty /paper/strategies -> strategies: []", r.get_json()["strategies"] == [])

r = client.get("/paper/performance", headers=AUTH)
body = r.get_json()
check("empty /paper/performance -> n_trades: 0, win_rate: null",
      body["n_trades"] == 0 and body["win_rate"] is None)


# ---------------------------------------------------------------------------
print("\n--- shape against a POPULATED paper store (built via a real paper cycle) ---")
# ---------------------------------------------------------------------------

with PaperTestEnv(position_notional=10_000.0) as env:
    env.register_and_approve(
        strategy_id="api_test_strategy", algorithm_id=f"api_test_{hashlib.sha256(b'x').hexdigest()[:8]}",
        strategy_cls=BuyEveryTimeStrategy)
    df = fixed_history_df([100.0, 101.0, 102.0])
    run_paper_cycle(
        cycle_id="api-test-cycle", clock=env.clock, history_provider=lambda s, d: df,
        universe=["INFY"], registry_dir=env.registry_dir,
        eligibility_dir=env.eligibility_dir, store=env.store, notify=False,
    )

    paper_data.reset_default_paper_store_for_testing()
    prior_db_path = os.environ["PAPER_DB_PATH"]
    os.environ["PAPER_DB_PATH"] = str(env.db_path)
    try:
        r = client.get("/paper/positions", headers=AUTH)
        body = r.get_json()
        check("populated /paper/positions returns the open position with symbol/qty/avg entry",
              body["count"] == 1 and body["positions"][0]["symbol"] == "INFY"
              and body["positions"][0]["quantity"] > 0)

        r = client.get("/paper/orders", headers=AUTH)
        body = r.get_json()
        check("populated /paper/orders returns the FILLED order",
              body["shown_count"] == 1 and body["orders"][0]["status"] == "FILLED")

        # /paper/strategies reads strategies.registry.REGISTRY_DIR / paper.config.
        # eligibility_dir() by default — the SAME "no per-request override" design
        # api/data.get_strategies() already has (see tests/test_api.py's own "F:
        # /strategies (populated)" check, which calls api_data.get_strategies(
        # registry_dir=...) directly rather than through the HTTP route for exactly
        # this reason). Mirrored here at the function level, not the route level.
        strategies_body = paper_data.get_paper_strategies(
            eligibility_dir=env.eligibility_dir, registry_dir=env.registry_dir)
        check("populated get_paper_strategies() shows the approved StrategyVersion "
              "with its approval metadata", strategies_body["count"] == 1
              and strategies_body["strategies"][0]["approved_by"] == "test-fixture")
    finally:
        os.environ["PAPER_DB_PATH"] = prior_db_path
        paper_data.reset_default_paper_store_for_testing()


# ---------------------------------------------------------------------------
print("\n--- unavailable paper store -> 503, no leaked path ---")
# ---------------------------------------------------------------------------

paper_data.reset_default_paper_store_for_testing()
prior_db_path = os.environ.get("PAPER_DB_PATH")
# A nonexistent path is no longer a failure case: PaperStore.open_readonly()
# treats it as "no paper cycle has run yet" and api.paper_data returns a
# zero-state default (see that module's docstrings) — that is the whole
# point of Slice AA's read-only-service-account fix. To exercise a
# genuinely BROKEN store under open_readonly(), the target path must exist
# as a real file (so Path.is_file() is True and open_readonly() does not
# take the "not yet initialized" branch) but contain garbage that is not a
# valid SQLite database — sqlite3.connect(..., mode=ro) succeeds at
# connect time regardless, but the first real query against it raises
# sqlite3.DatabaseError, which api.paper_data's own try/except still turns
# into a safe 503.
garbage_db_path = tempfile.mktemp(suffix="-broken-paper.db")
with open(garbage_db_path, "w") as f:
    f.write("this is not a sqlite database\n")
os.environ["PAPER_DB_PATH"] = garbage_db_path
try:
    r = client.get("/paper/account", headers=AUTH)
    check("a corrupt paper DB file -> 503, not a 500 or a stack trace",
          r.status_code == 503)
    body = r.get_json()
    check("the 503 body never leaks the underlying filesystem path",
          garbage_db_path not in str(body))
finally:
    if os.path.exists(garbage_db_path):
        os.remove(garbage_db_path)
    if prior_db_path is not None:
        os.environ["PAPER_DB_PATH"] = prior_db_path
    else:
        os.environ.pop("PAPER_DB_PATH", None)
    paper_data.reset_default_paper_store_for_testing()


# ---------------------------------------------------------------------------
print("\n--- LIVE and PAPER data are never mixed ---")
# ---------------------------------------------------------------------------

r_live = client.get("/positions", headers=AUTH)
r_paper = client.get("/paper/positions", headers=AUTH)
check("GET /positions (live) and GET /paper/positions are two distinct routes/payloads, "
      "never merged into one response",
      r_live.get_json() != r_paper.get_json())

r_live_acct = client.get("/account", headers=AUTH)
check("GET /account (live) response carries no 'label: PAPER' marker — live and paper "
      "payload shapes are visibly distinct, not just differently routed",
      "PAPER" not in str(r_live_acct.get_json()))


print(f"\n{'=' * 52}")
print(f"  {PASSED} passed, {FAILED} failed")
print(f"{'=' * 52}\n")
sys.exit(1 if FAILED else 0)
