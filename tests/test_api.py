"""
Phase 4 Slice Z — read-only API + dashboard tests.

Covers: authentication (401 without/with-wrong credentials, 200 with a
valid one), CORS (no header for an unconfigured origin, header only for a
configured one), the read-only guarantee (proven empirically — file hashes/
mtimes of every real underlying data source, captured before and after
hitting every single endpoint, must be identical), every endpoint's shape
against the real (currently empty) repository state, every endpoint's shape
against a representative NON-empty state (built in temporary fixtures,
never in the real memory/research/strategies directories), the
underlying-source-unavailable error path (503, no leaked path), and
security (no secret in any response body or log line, no wildcard CORS, no
exception detail in a 500).

Run with:  python -m tests.test_api
"""

import re
import sys
import json
import shutil
import logging
import hashlib
import tempfile
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import os  # noqa: E402

os.environ["DASHBOARD_API_TOKEN"] = "test-secret-token-do-not-leak"
os.environ["DASHBOARD_CORS_ORIGINS"] = "http://localhost:5173"

from api import app as api_app_mod          # noqa: E402
from api import auth as api_auth            # noqa: E402
from api import config as api_config        # noqa: E402
from api import data as api_data            # noqa: E402
from engine import guardrails as gr         # noqa: E402
from engine import journal as jr            # noqa: E402
from research.store import Store, IST       # noqa: E402
from research import memory as rm           # noqa: E402
from research.brain import hypothesis_intake as hi   # noqa: E402
from research.contracts import Contract, registry as _reg  # noqa: E402
from strategies import registry as sreg     # noqa: E402
from strategies.core import create_strategy_version  # noqa: E402

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

PROTECTED_GET_ROUTES = [
    "/account", "/positions", "/orders", "/trades", "/risk", "/regime",
    "/research/drafts", "/research/evidence", "/research/areas",
    "/strategies", "/backtests",
]

ALL_ROUTES = ["/health"] + PROTECTED_GET_ROUTES


# ---------------------------------------------------------------------------
# Fingerprinting the real, live data sources — used by the read-only proof
# and never modified by anything in this file.
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).parent.parent
REAL_STATE_FILE = PROJECT_ROOT / "memory" / "state.json"
REAL_TRADES_JSONL = PROJECT_ROOT / "memory" / "trades.jsonl"
REAL_REGIME_JSONL = PROJECT_ROOT / "memory" / "regime_log.jsonl"
REAL_MARKET_DB = PROJECT_ROOT / "research" / "market_memory.db"
REAL_STRATEGIES_REGISTRY = PROJECT_ROOT / "strategies" / "registry"
REAL_RESEARCH_REGISTRY = PROJECT_ROOT / "research" / "registry"


def _fingerprint_tree(path: Path) -> str:
    """A stable digest of a file's bytes, or of an existing directory's
    filenames + each file's bytes. A path that doesn't exist fingerprints as
    a fixed sentinel, so 'still doesn't exist' and 'now exists' are both
    detectable."""
    if not path.exists():
        return "MISSING"
    h = hashlib.sha256()
    if path.is_file():
        h.update(path.read_bytes())
        return h.hexdigest()
    for p in sorted(path.rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(path)).encode())
            h.update(p.read_bytes())
    return h.hexdigest()


def _fingerprint_all() -> dict:
    return {
        "state.json": _fingerprint_tree(REAL_STATE_FILE),
        "trades.jsonl": _fingerprint_tree(REAL_TRADES_JSONL),
        "regime_log.jsonl": _fingerprint_tree(REAL_REGIME_JSONL),
        "market_memory.db": _fingerprint_tree(REAL_MARKET_DB),
        "strategies/registry": _fingerprint_tree(REAL_STRATEGIES_REGISTRY),
        "research/registry": _fingerprint_tree(REAL_RESEARCH_REGISTRY),
    }


# ===========================================================================
print("\n--- A: /health is public ---")
# ===========================================================================

r = client.get("/health")
check("A: /health returns 200 with no Authorization header", r.status_code == 200)
body = r.get_json()
check("A: /health body reports app status, not broker/research health",
      body.get("status") == "ok" and "broker" not in body and "research" not in body,
      str(body))


# ===========================================================================
print("\n--- B: authentication ---")
# ===========================================================================

r = client.get("/account")
check("B: protected endpoint with no credentials -> 401", r.status_code == 401)
check("B: 401 body never echoes back what token WAS expected", TOKEN not in json.dumps(r.get_json()))

r = client.get("/account", headers={"Authorization": "Bearer wrong-token"})
check("B: protected endpoint with an invalid token -> 401", r.status_code == 401)

r = client.get("/account", headers={"Authorization": "not-even-bearer-shaped"})
check("B: a malformed Authorization header -> 401", r.status_code == 401)

r = client.get("/account", headers=AUTH)
check("B: protected endpoint with the valid token -> 200", r.status_code == 200)

for route in PROTECTED_GET_ROUTES:
    r_no = client.get(route)
    r_ok = client.get(route, headers=AUTH)
    check(f"B: {route} enforces auth (401 without, 200 with)",
          r_no.status_code == 401 and r_ok.status_code == 200,
          f"no-auth={r_no.status_code} with-auth={r_ok.status_code}")


# ===========================================================================
print("\n--- C: CORS is explicit, never a wildcard ---")
# ===========================================================================

r = client.get("/health", headers={"Origin": "http://localhost:5173"})
check("C: a configured origin gets Access-Control-Allow-Origin echoed back",
      r.headers.get("Access-Control-Allow-Origin") == "http://localhost:5173")

r = client.get("/health", headers={"Origin": "https://evil.example.com"})
check("C: an UNconfigured origin gets no CORS header at all",
      "Access-Control-Allow-Origin" not in r.headers, dict(r.headers))

r = client.get("/health")
check("C: no Origin header at all -> no CORS header either", "Access-Control-Allow-Origin" not in r.headers)

check("C: DASHBOARD_CORS_ORIGINS is never '*' by default in this test's config",
      "*" not in api_config.cors_origins())


# ===========================================================================
print("\n--- D: read-only — nothing this API does mutates anything ---")
# ===========================================================================

before = _fingerprint_all()
for route in ALL_ROUTES:
    client.get(route, headers=AUTH)
    client.get(route)  # unauthenticated attempts must not mutate anything either
    client.get(route, headers={"Authorization": "Bearer wrong"})
after = _fingerprint_all()

check("D: memory/state.json is byte-identical after hitting every endpoint",
      before["state.json"] == after["state.json"])
check("D: memory/trades.jsonl is byte-identical (no order/trade was created)",
      before["trades.jsonl"] == after["trades.jsonl"])
check("D: memory/regime_log.jsonl is byte-identical",
      before["regime_log.jsonl"] == after["regime_log.jsonl"])
check("D: research/market_memory.db is byte-identical (no Contract/draft/evidence written)",
      before["market_memory.db"] == after["market_memory.db"])
check("D: strategies/registry/ is byte-identical (no StrategyVersion saved)",
      before["strategies/registry"] == after["strategies/registry"])
check("D: research/registry/ is byte-identical (no Contract locked/approved)",
      before["research/registry"] == after["research/registry"])


# ===========================================================================
print("\n--- E: every endpoint against the real (currently empty) repo state ---")
# ===========================================================================

r = client.get("/account", headers=AUTH)
acct = r.get_json()
check("E: /account has the documented fields",
      {"cash", "portfolio_value", "total_value", "pnl_today", "as_of"} <= acct.keys(), str(acct))

r = client.get("/positions", headers=AUTH)
pos = r.get_json()
check("E: /positions returns an empty list cleanly on real (empty) state",
      pos["positions"] == [] and pos["count"] == 0, str(pos))

r = client.get("/orders", headers=AUTH)
check("E: /orders returns an empty list cleanly", r.get_json()["orders"] == [])

r = client.get("/trades", headers=AUTH)
check("E: /trades returns an empty list cleanly", r.get_json()["trades"] == [])

r = client.get("/risk", headers=AUTH)
risk = r.get_json()
check("E: /risk exposes guardrail state without inventing a new risk model",
      {"drawdown_level", "risk_per_trade_pct", "max_total_open_risk", "tier"} <= risk.keys(),
      str(risk))

r = client.get("/regime", headers=AUTH)
regime = r.get_json()
check("E: /regime reports unavailable rather than guessing, when nothing has been logged",
      regime["available"] is False, str(regime))

r = client.get("/research/drafts", headers=AUTH)
check("E: /research/drafts returns the shape build_backlog() defines",
      set(r.get_json().keys()) >= {"drafts", "shown_count", "total_count", "truncated", "as_of"})

r = client.get("/research/evidence", headers=AUTH)
check("E: /research/evidence returns the digest's evidence-section shape",
      set(r.get_json().keys()) >= {"hypotheses", "shown_count", "total_count", "truncated"})

r = client.get("/research/areas", headers=AUTH)
check("E: /research/areas returns an empty list cleanly", r.get_json()["areas"] == [])

r = client.get("/strategies", headers=AUTH)
check("E: /strategies returns an empty list cleanly on the real registry",
      r.get_json()["strategies"] == [])

r = client.get("/backtests", headers=AUTH)
check("E: /backtests returns an empty list cleanly", r.get_json()["backtests"] == [])

r = client.get("/account?limit=abc", headers=AUTH)
r2 = client.get("/orders?limit=abc", headers=AUTH)
check("E: a malformed limit query param -> 400, not a 500",
      r2.status_code == 400, str(r2.get_json()))


# ===========================================================================
print("\n--- F: representative NON-empty state (temp fixtures, real code) ---")
# ===========================================================================

TMP = Path(tempfile.mkdtemp(prefix="lq-test-api-"))


def at(y, m, d, hh=18, mm=30):
    return dt.datetime(y, m, d, hh, mm, tzinfo=IST)


# -- account / positions / risk: a populated memory/state.json equivalent --

# broker_snapshot.synced_at is computed fresh at test time so this fixture
# exercises the "fresh successful sync" path of api/broker_truth.py (a hard-
# coded old timestamp would now be classified "stale" and total_value withheld
# — that behaviour is covered explicitly by F2 below).
_fresh_sync = (dt.datetime.now(IST) - dt.timedelta(hours=1)).isoformat(timespec="seconds")
_base_state = {
    "allocated_capital": 20000.0, "capital": 21500.0, "peak_capital": 22000.0,
    "cash_available": 15000.0, "realized_pnl_alltime": 1500.0, "total_costs_alltime": 80.0,
    "consecutive_losing_days": 0, "trading_paused": False, "pause_reason": None,
    "awaiting_human_ack": False, "drawdown_level": "NORMAL",
    "day": {"date": "2026-09-09", "starting_capital": 21000.0, "realized_pnl": 500.0,
            "trades_taken": 1, "process_grade": None},
    "week": {"start_date": "2026-09-08", "starting_capital": 20500.0},
    "open_positions": [{
        "symbol": "INFY", "side": "BUY", "quantity": 10, "entry": 1500.0,
        "stop": 1450.0, "target": 1650.0, "open_risk": 500.0, "thesis": "test fixture",
        "opened": "2026-09-09T09:20:00+05:30",
    }],
    "overlap_approved_symbols": [],
    "broker_snapshot": {"total_account_value": 500000.0, "free_cash": 15000.0,
                        "unmanaged_symbols": ["RELIANCE"], "synced_at": _fresh_sync},
    "last_updated": _fresh_sync,
}
fake_state_path = TMP / "state.json"
fake_state_path.write_text(json.dumps(_base_state))

_orig_state_file = gr.STATE_FILE
gr.STATE_FILE = fake_state_path
try:
    acct2 = api_data.get_account()
    pos2 = api_data.get_positions()
    risk2 = api_data.get_risk()
finally:
    gr.STATE_FILE = _orig_state_file

check("F: /account (populated, fresh broker sync) reports the fixture's real numbers",
      acct2["cash"] == 15000.0 and acct2["portfolio_value"] == 21500.0
      and acct2["total_value"] == 500000.0 and acct2["pnl_today"] == 500.0, str(acct2))
check("F: /account (fresh sync) is labelled INDmoney / INDstocks and marked fresh",
      acct2["broker"]["label"] == "INDmoney / INDstocks"
      and acct2["account_value_status"] == "fresh"
      and acct2["book_value_reconciled"] is True, str(acct2))

# F2: a stale broker_snapshot (old synced_at) + a Kite-era `capital` that
# equals the whole account total must NOT be presented as current INDmoney.
_stale_state = json.loads(json.dumps(_base_state))
_stale_state["broker_snapshot"]["synced_at"] = "2026-06-01T09:00:00+05:30"
_stale_state["last_updated"] = "2026-06-01T09:00:00+05:30"
_stale_state["capital"] = 500000.0           # Kite-era poisoning: capital == account total
_stale_state["broker_snapshot"]["total_account_value"] = 500000.0
_stale_path = TMP / "state_stale.json"
_stale_path.write_text(json.dumps(_stale_state))
gr.STATE_FILE = _stale_path
try:
    acct_stale = api_data.get_account()
finally:
    gr.STATE_FILE = _orig_state_file

check("F2: a stale/Kite-era state -> total_value is None (₹5L is NOT shown as current)",
      acct_stale["total_value"] is None and acct_stale["broker_free_cash"] is None, str(acct_stale))
check("F2: a stale/Kite-era state -> account_value_status 'stale', book_value_reconciled False",
      acct_stale["account_value_status"] == "stale"
      and acct_stale["book_value_reconciled"] is False, str(acct_stale))
check("F2: expected_book_value is allocated + realised P&L (₹21,500), not the stale ₹5L capital",
      acct_stale["expected_book_value"] == 21500.0
      and acct_stale["portfolio_value"] == 500000.0
      and len(acct_stale["account_warnings"]) >= 1, str(acct_stale))
check("F2: allocated_capital is still the mandate (₹20,000), never the account total",
      acct_stale["allocated_capital"] == 20000.0, str(acct_stale))

# F3: a FRESH sync whose account total collapsed to cash-only (holdings priced
# at 0 — the observed first-INDmoney-sync case: ₹32.31 with 26 holdings).
_cashonly = json.loads(json.dumps(_base_state))
_cashonly["broker_snapshot"]["total_account_value"] = 32.31
_cashonly["broker_snapshot"]["free_cash"] = 32.31
_cashonly["broker_snapshot"]["unmanaged_symbols"] = [f"SYM{i}" for i in range(26)]
_cashonly["cash_available"] = 32.31
_cashonly["open_positions"] = []
_cashonly_path = TMP / "state_cashonly.json"
_cashonly_path.write_text(json.dumps(_cashonly))
gr.STATE_FILE = _cashonly_path
try:
    acct_co = api_data.get_account()
finally:
    gr.STATE_FILE = _orig_state_file

check("F3: cash-only total on a fresh sync -> total_value None (dashboard: 'Unavailable', not ₹32.31)",
      acct_co["total_value"] is None, str(acct_co))
check("F3: account_value_status is 'incomplete' (sync fresh, total not verifiable)",
      acct_co["account_value_status"] == "incomplete", str(acct_co))
check("F3: broker_free_cash IS still shown (₹32.31 — a directly-confirmed field)",
      acct_co["broker_free_cash"] == 32.31, str(acct_co))
check("F3: unmanaged_holdings exposes count 26 but value None (can't verify)",
      acct_co["unmanaged_holdings"]["count"] == 26
      and acct_co["unmanaged_holdings"]["value"] is None, str(acct_co["unmanaged_holdings"]))
check("F3: agent_spendable_cash and allocated_capital are separate agent fields, untouched",
      acct_co["agent_spendable_cash"] == 32.31 and acct_co["allocated_capital"] == 20000.0,
      str(acct_co))
check("F3: a warning explains the total could not be verified",
      any("could not be verified" in w for w in acct_co["account_warnings"]),
      str(acct_co["account_warnings"]))

check("F: /positions (populated) returns the one open position with the right fields",
      len(pos2["positions"]) == 1 and pos2["positions"][0]["symbol"] == "INFY"
      and pos2["positions"][0]["side"] == "BUY" and pos2["positions"][0]["quantity"] == 10,
      str(pos2))
check("F: /positions (populated) never invents current_price/unrealized_pnl",
      "current_price" not in pos2["positions"][0] and "unrealized_pnl" not in pos2["positions"][0])
check("F: /risk (populated) reflects the fixture's drawdown/tier state",
      risk2["capital"] == 21500.0 and risk2["drawdown_level"] in ("NORMAL", "AMBER", "ORANGE", "RED"),
      str(risk2))

# -- orders / trades / regime: populated JSONL logs --

fake_trades = TMP / "trades.jsonl"
fake_trades.write_text("\n".join(json.dumps(r) for r in [
    {"ts": "2026-09-09T09:20:00+05:30", "kind": "ENTRY", "symbol": "INFY", "side": "BUY",
     "quantity": 10, "entry": 1500.0, "stop": 1450.0, "target": 1650.0, "risk_amount": 500.0,
     "regime": "TRENDING_UP", "playbook": "momentum_long", "thesis": "test", "order_id": "OID1"},
    {"ts": "2026-09-08T15:00:00+05:30", "kind": "EXIT", "symbol": "TCS", "exit_price": 3600.0,
     "pnl": 800.0, "r_multiple": 1.6, "regime": "TRENDING_UP", "reason": "target", "holding_days": 3},
    {"ts": "2026-09-08T09:20:00+05:30", "kind": "REJECTED", "symbol": "WIPRO",
     "reasons": ["reward:risk below minimum"], "regime": "RANGE_BOUND"},
    "not even json",
]) + "\n")

fake_regime = TMP / "regime_log.jsonl"
fake_regime.write_text(json.dumps({
    "ts": "2026-09-09T09:00:00+05:30", "kind": "REGIME", "regime": "TRENDING_UP",
    "playbook": "momentum_long", "confidence": "high", "evidence": {"adx": 28.4},
    "shadow": {"available": False},
}) + "\n")

_orig_trades, _orig_regime = jr.TRADES_JSONL, jr.REGIME_JSONL
jr.TRADES_JSONL = fake_trades
jr.REGIME_JSONL = fake_regime
try:
    orders2 = api_data.get_orders()
    trades2 = api_data.get_trades()
    regime2 = api_data.get_regime()
finally:
    jr.TRADES_JSONL, jr.REGIME_JSONL = _orig_trades, _orig_regime

check("F: /orders (populated) includes the ENTRY and the REJECTED, not the EXIT",
      {o["kind"] for o in orders2["orders"]} == {"ENTRY", "REJECTED"}
      and orders2["total_count"] == 2, str(orders2))
check("F: /orders (populated) tolerates a malformed JSONL line without crashing",
      orders2["total_count"] == 2)
check("F: /trades (populated) includes exactly the one EXIT, with its pnl/r_multiple",
      trades2["total_count"] == 1 and trades2["trades"][0]["symbol"] == "TCS"
      and trades2["trades"][0]["pnl"] == 800.0, str(trades2))
check("F: /regime (populated) reports the logged classification, not a live one",
      regime2["available"] is True and regime2["regime"] == "TRENDING_UP"
      and regime2["confidence"] == "high", str(regime2))

# -- strategies / backtests: temp registry + a real Store with a backtest note --

strat_reg_dir = TMP / "strat_registry"
version = create_strategy_version(
    strategy_id="fixture-strategy", algorithm_id="test.fixture.v1",
    parameters={"threshold": 100.0}, implementation="def run(): pass",
    derived_from_hypothesis_id="HYP-FIXTURE-1")
sreg.save_version(version, directory=strat_reg_dir)

strat3 = api_data.get_strategies(registry_dir=strat_reg_dir)
check("F: /strategies (populated) lists the saved StrategyVersion with its provenance",
      len(strat3["strategies"]) == 1
      and strat3["strategies"][0]["version_id"] == version.version_id
      and strat3["strategies"][0]["derived_from_hypothesis_id"] == "HYP-FIXTURE-1",
      str(strat3))

fixture_store = Store.open(TMP / "fixture.db")
rm.record_research_note(
    fixture_store, note="Strategy backtest completed: fixture-strategy — 3 trade(s).",
    source="research.experiments.strategy_backtest",
    extra={"strategy_id": "fixture-strategy", "version_id": version.version_id,
           "algorithm_id": "test.fixture.v1", "n_trades": 3})
rm.record_research_note(fixture_store, note="unrelated note", source="something.else")

bt3 = api_data.get_backtests(fixture_store)
check("F: /backtests (populated) surfaces the recorded completion note, and only that source",
      bt3["total_count"] == 1 and bt3["backtests"][0]["strategy_id"] == "fixture-strategy"
      and bt3["backtests"][0]["n_trades"] == 3, str(bt3))

# -- research drafts/evidence/areas: a real DRAFT contract in a temp registry --

research_reg_dir = TMP / "research_registry"
draft_proposal = {
    "title": "Fixture hypothesis for Slice Z's API test",
    "hypothesis": "High-volume anomalies precede a short-term drift, for test purposes.",
    "null_hypothesis": "Volume anomalies have no relationship to subsequent returns.",
    "universe": "watchlist",
    "signal": "observatory.volume_zscore",
    "entry_rule": {"conditions": [{"metric": "volume_zscore", "op": ">", "value": 3.0}]},
    "exit_rule": {"stop_loss_pct": 2.0, "target_pct": 4.0, "max_hold_days": 10},
    "splits": {
        "discovery": ["2019-01-01", "2022-12-31"],
        "validation": ["2023-01-01", "2024-12-31"],
    },
    "independence": "one entry per symbol per rolling 10-day window; overlapping signals dropped",
    "falsification": "expectancy_r <= 0 on the validation split, or t_stat < 2.0",
    "abandon_condition": "if discovery-split expectancy_r <= 0, abandon without touching validation",
    "evaluation_start": "2019-01-01",
    "evaluation_end": "2022-12-31",
    "source": "test_fixture",
}
result = hi.intake(fixture_store, draft_proposal, registry_dir=research_reg_dir)

drafts3 = api_data.get_research_drafts(fixture_store, registry_dir=research_reg_dir)
check("F: /research/drafts (populated) surfaces the DRAFT contract just created",
      drafts3["total_count"] == 1 and drafts3["drafts"][0]["contract_id"] == result.contract.id,
      str(drafts3))
check("F: /research/drafts (populated) never shows a locked/non-draft contract "
      "as pending — none was created here, so this is still an empty exclusion set",
      all(d.get("status", "draft") == "draft" for d in drafts3["drafts"]), str(drafts3))

areas3 = api_data.get_research_areas(fixture_store)
check("F: /research/areas (real Store, no tags written) returns a clean list",
      isinstance(areas3["areas"], list))

fixture_store.close()
shutil.rmtree(TMP, ignore_errors=True)


# ===========================================================================
print("\n--- G: underlying source unavailable -> 503, nothing leaked ---")
# ===========================================================================

_orig_state_file = gr.STATE_FILE
gr.STATE_FILE = PROJECT_ROOT / "memory" / "definitely-does-not-exist-state.json"
try:
    r = client.get("/account", headers=AUTH)
    check("G: a missing state file -> 503, not a 500 crash", r.status_code == 503, str(r.get_json()))
    body_text = json.dumps(r.get_json())
    check("G: the 503 body never contains the real filesystem path",
          str(PROJECT_ROOT) not in body_text and "definitely-does-not-exist" not in body_text,
          body_text)
    check("G: the 503 body never names the Python exception class",
          "RuntimeError" not in body_text and "FileNotFoundError" not in body_text, body_text)
finally:
    gr.STATE_FILE = _orig_state_file

r = client.get("/account", headers=AUTH)
check("G: after restoring the real path, /account works normally again", r.status_code == 200)


# ===========================================================================
print("\n--- H: security ---")
# ===========================================================================

log_records = []


class _CaptureHandler(logging.Handler):
    def emit(self, record):
        log_records.append(self.format(record))


handler = _CaptureHandler()
app.logger.addHandler(handler)
app.logger.setLevel(logging.DEBUG)

client.get("/account")  # no credentials -> logs a warning
client.get("/account", headers={"Authorization": "Bearer totally-wrong-value"})
client.get("/account", headers=AUTH)

app.logger.removeHandler(handler)

check("H: the real bearer token never appears in any log line", not any(TOKEN in m for m in log_records))
check("H: the wrong token that was tried never appears in any log line either",
      not any("totally-wrong-value" in m for m in log_records))

secret_markers = ["KITE_API_SECRET", "INDSTOCKS_TOTP_SECRET", "INDSTOCKS_MPIN",
                  "TELEGRAM_BOT_TOKEN", TOKEN]
for route in PROTECTED_GET_ROUTES:
    r = client.get(route, headers=AUTH)
    body_text = r.get_data(as_text=True)
    leaked = [m for m in secret_markers if m in body_text]
    check(f"H: {route}'s response body contains no known secret marker", not leaked, str(leaked))

# Force an unexpected exception deep in a data function and confirm the
# client gets a generic 500 with no exception text.
_orig_get_account = api_data.get_account


def _boom():
    raise ValueError("a very specific internal detail: /some/secret/path")


api_data.get_account = _boom
try:
    r = client.get("/account", headers=AUTH)
    check("H: an unexpected exception -> 500, not a crash", r.status_code == 500)
    body_text = json.dumps(r.get_json())
    check("H: a 500 body never contains the original exception's message",
          "a very specific internal detail" not in body_text and "/some/secret/path" not in body_text,
          body_text)
finally:
    api_data.get_account = _orig_get_account

r = client.get("/account", headers=AUTH)
check("H: after restoring, /account works normally again", r.status_code == 200)


# ===========================================================================
print("\n--- I: no write endpoints exist ---")
# ===========================================================================

for method in ("post", "put", "patch", "delete"):
    for route in ALL_ROUTES:
        r = getattr(client, method)(route, headers=AUTH)
        check(f"I: {method.upper()} {route} is not a usable write endpoint (405/404, never 200)",
              r.status_code in (404, 405), f"{r.status_code}")


print(f"\n{'=' * 52}")
print(f"  {PASSED} passed, {FAILED} failed")
print(f"{'=' * 52}\n")
sys.exit(1 if FAILED else 0)
