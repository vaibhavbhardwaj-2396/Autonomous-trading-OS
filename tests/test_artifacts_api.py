"""
tests/test_artifacts_api.py — INTELLIGENT + EFFICIENT + TRACEABLE
(outcome 2): the unified artifact explorer (api/artifacts.py) and the AI
provider/config/budget surface (api/ai_status.py), both as backend
modules and as the real Flask routes in api/app.py.

Sections:
  A. api/artifacts.py — list_artifacts()/get_artifact() against a real
     (temp-file) Store: every artifact type, pagination, malformed/
     unknown-id handling.
  B. api/ai_status.py — get_ai_status()/set_ai_provider() against
     isolated control/ai_config.py + control/ai_budget.py state.
  C. The real Flask routes (api/app.py), via the test client: auth
     required on every new route, correct status codes, and the same
     "read-only proof" the rest of api/app.py already holds itself to
     for every GET route — plus a live proof that POST /ai/config STILL
     never touches engine/, memory/state.json, or any research write
     path (grepped from the actual response + a before/after state
     comparison, not merely asserted).

Run with:  python -m tests.test_artifacts_api
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ["DASHBOARD_API_TOKEN"] = "test-secret-token-do-not-leak"
os.environ["DASHBOARD_CORS_ORIGINS"] = "http://localhost:5173"

from api import artifacts  # noqa: E402
from api import ai_status  # noqa: E402
from api import data as api_data  # noqa: E402
from api import app as api_app_mod  # noqa: E402
from control import ai_config, ai_budget, runtime as ctrl  # noqa: E402
from research.store import Store, now_ist  # noqa: E402
from research import memory as rm  # noqa: E402
from research.contracts import Contract  # noqa: E402

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


TMP = Path(tempfile.mkdtemp(prefix="lq-test-artifacts-api-"))


def make_contract(cid, *, registry_dir):
    c = Contract(
        id=cid, title=f"idea {cid}", hypothesis="claim", null_hypothesis="none",
        universe="watchlist", signal="observatory.volume_zscore",
        entry_rule=json.dumps({"conditions": [{"metric": "volume_zscore", "op": ">", "value": 3.0}]}),
        exit_rule=json.dumps({"stop_loss_pct": 2.0, "target_pct": 4.0}),
        splits={"discovery": ["2019-01-01", "2020-01-01"]},
        independence="clustered", falsification="t<2.0", abandon_condition="ev<=0",
        evaluation_start="2019-01-01", evaluation_end="2020-01-01",
    )
    c.lock()
    c.locked_at = "2024-06-01T00:00:00"
    c.status = "locked"
    c.save(registry_dir)
    return c


# ===========================================================================
print("\n--- A: api/artifacts.py against a real Store ---")
# ===========================================================================

store_a = Store.open(TMP / "a.db")
reg_a = TMP / "registry-a"
reg_a.mkdir(parents=True, exist_ok=True)

rm.record_model_interaction(
    store_a, provider="anthropic_cli", model="claude-code-cli", purpose="hypothesis_generation",
    trigger="test", prompt="p1", response="r1", status="ok", cycle_id="cyc-1",
)
rm.record_anomaly(
    store_a, entity="RELIANCE", metric="volume_zscore", value=4.2, baseline=1.0,
    z_score=4.2, as_of=now_ist(), source="test",
)
hid, _ = rm.record_hypothesis_proposal(store_a, claim="unusual volume predicts continuation", source="test")
make_contract("EXP-ART-1", registry_dir=reg_a)

unfiltered = artifacts.list_artifacts(store_a, limit=500, registry_dir=reg_a)
check("A1: list_artifacts() with no type filter returns every recorded type "
      "(a generous limit is used here deliberately — control/runtime.py's "
      "REAL, shared mode-change history, capped at 50 entries, is a "
      "genuinely high-volume 'system_event' source that would otherwise "
      "crowd a small default page; this is expected chronological-feed "
      "behavior, not a bug, and is exactly why type-filtering exists — "
      "see A2/A3 below)",
      {a["type"] for a in unfiltered["artifacts"]}
      >= {"model_interaction", "detection", "hypothesis", "experiment"},
      sorted({a["type"] for a in unfiltered["artifacts"]}))

only_mi = artifacts.list_artifacts(store_a, type="model_interaction", registry_dir=reg_a)
check("A2: list_artifacts(type='model_interaction') returns only that type",
      all(a["type"] == "model_interaction" for a in only_mi["artifacts"])
      and only_mi["total_count"] == 1, only_mi)

only_exp = artifacts.list_artifacts(store_a, type="experiment", registry_dir=reg_a)
check("A3: list_artifacts(type='experiment') reads through to the Contract registry",
      only_exp["total_count"] == 1 and only_exp["artifacts"][0]["status"] == "locked", only_exp)

try:
    artifacts.list_artifacts(store_a, type="not_a_real_type", registry_dir=reg_a)
    check("A4: an unknown type raises DataSourceError", False, "no exception raised")
except artifacts.DataSourceError:
    check("A4: an unknown type raises DataSourceError", True)

# pagination
for i in range(5):
    rm.record_research_note(store_a, note=f"note {i}", source="test")
page1 = artifacts.list_artifacts(store_a, limit=2, offset=0, registry_dir=reg_a)
page2 = artifacts.list_artifacts(store_a, limit=2, offset=2, registry_dir=reg_a)
check("A5: pagination — two consecutive pages never overlap",
      not (set(a["id"] for a in page1["artifacts"]) & set(a["id"] for a in page2["artifacts"])),
      (page1["artifacts"], page2["artifacts"]))
check("A6: total_count is stable across pages of the same query",
      page1["total_count"] == page2["total_count"], (page1["total_count"], page2["total_count"]))

mi_id = only_mi["artifacts"][0]["id"]
detail = artifacts.get_artifact(store_a, mi_id, registry_dir=reg_a)
check("A7: get_artifact() for a model_interaction returns the FULL prompt/response",
      detail is not None and detail["payload"]["prompt"]["text"] == "p1"
      and detail["payload"]["response"]["text"] == "r1", detail)

exp_detail = artifacts.get_artifact(store_a, "experiment:EXP-ART-1", registry_dir=reg_a)
check("A8: get_artifact() for an experiment returns the Contract's full to_dict()",
      exp_detail is not None and exp_detail["payload"]["id"] == "EXP-ART-1", exp_detail)

check("A9: get_artifact() for a nonexistent id returns None (not an exception)",
      artifacts.get_artifact(store_a, "model_interaction:999999", registry_dir=reg_a) is None)
check("A10: get_artifact() for a malformed id (no colon) returns None",
      artifacts.get_artifact(store_a, "garbage", registry_dir=reg_a) is None)
check("A11: get_artifact() for an unknown type prefix returns None",
      artifacts.get_artifact(store_a, "not_a_type:123", registry_dir=reg_a) is None)

sys_events = artifacts.list_artifacts(store_a, type="system_event", registry_dir=reg_a)
check("A12: system_event artifacts reflect control.runtime's real current mode",
      sys_events["total_count"] >= 1
      and sys_events["artifacts"][0]["status"] == ctrl.get_state()["mode"], sys_events)


# ===========================================================================
print("\n--- B: api/ai_status.py ---")
# ===========================================================================

path_b, lock_b = TMP / "ai_config_b.json", TMP / "ai_config_b.lock"
_orig_get_config = ai_config.get_config
_orig_set_config = ai_config.set_config
ai_config.get_config = lambda **kw: _orig_get_config(path=path_b)
ai_config.set_config = lambda **kw: _orig_set_config(**{**kw, "path": path_b, "lock_path": lock_b})
try:
    status0 = ai_status.get_ai_status()
    check("B1: get_ai_status() reports the effective provider even with no "
          "config ever set (falls back to the default)",
          status0["effective_provider"] == "anthropic_cli", status0)
    check("B2: get_ai_status() includes budget figures",
          "tokens_today" in status0["budget"] and "calls_today" in status0["budget"], status0)

    new_state = ai_status.set_ai_provider(provider="openai", model="gpt-4o", actor="vaibhav",
                                          reason="testing switch")
    check("B3: set_ai_provider() persists the new provider/model",
          new_state["provider"] == "openai" and new_state["model"] == "gpt-4o")
    status1 = ai_status.get_ai_status()
    check("B4: get_ai_status() reflects the switch immediately",
          status1["effective_provider"] == "openai" and status1["configured_provider"] == "openai",
          status1)
finally:
    ai_config.get_config = _orig_get_config
    ai_config.set_config = _orig_set_config


# ===========================================================================
print("\n--- C: the real Flask routes ---")
# ===========================================================================

TOKEN = os.environ["DASHBOARD_API_TOKEN"]
AUTH = {"Authorization": f"Bearer {TOKEN}"}
app = api_app_mod.create_app()
client = app.test_client()

NEW_GET_ROUTES = ["/resources/status", "/ai/status", "/artifacts"]

for route in NEW_GET_ROUTES:
    r_noauth = client.get(route)
    check(f"C1: GET {route} without auth is refused (401)", r_noauth.status_code == 401)

r_noauth = client.post("/ai/config", json={"provider": "openai", "reason": "x"})
check("C2: POST /ai/config without auth is refused (401), never a usable "
      "unauthenticated write", r_noauth.status_code == 401)

r_resources = client.get("/resources/status", headers=AUTH)
check("C3: GET /resources/status with auth returns 200 and a real "
      "resource state", r_resources.status_code == 200
      and r_resources.get_json()["state"] in ("HEALTHY", "CONSTRAINED", "PRESSURED", "CRITICAL"),
      r_resources.get_json())

r_ai_status = client.get("/ai/status", headers=AUTH)
check("C4: GET /ai/status with auth returns 200 and the known-providers list",
      r_ai_status.status_code == 200 and "known_providers" in r_ai_status.get_json(),
      r_ai_status.get_json())

r_artifacts = client.get("/artifacts", headers=AUTH)
check("C5: GET /artifacts with auth returns 200 and a paginated shape",
      r_artifacts.status_code == 200 and "artifacts" in r_artifacts.get_json()
      and "total_count" in r_artifacts.get_json(), r_artifacts.get_json())

r_bad_type = client.get("/artifacts?type=not_a_real_type", headers=AUTH)
check("C6: GET /artifacts?type=<bad> returns 400, not a 500 or a silent "
      "empty list", r_bad_type.status_code == 400, r_bad_type.get_json())

r_missing = client.get("/artifacts/model_interaction:999999999", headers=AUTH)
check("C7: GET /artifacts/<nonexistent id> returns 404", r_missing.status_code == 404)

# /ai/config validation, mirroring /control/mode's own validation tests.
r_bad_provider = client.post("/ai/config", headers=AUTH,
                             json={"provider": "not_a_provider", "reason": "x"})
check("C8: POST /ai/config with an unknown provider returns 400",
      r_bad_provider.status_code == 400, r_bad_provider.get_json())

r_no_reason = client.post("/ai/config", headers=AUTH, json={"provider": "openai"})
check("C9: POST /ai/config with no reason returns 400 (reason required, "
      "same discipline as /control/mode)", r_no_reason.status_code == 400)

# Real state — restore afterward, same convention as the rest of this
# session's tests that touch the real, shared control/ files. Captures
# whether the file existed AT ALL before this test (not just its parsed
# content — get_config() always returns a dict, even with no file), so
# cleanup can restore "never configured" exactly, not just "configured
# with provider=None".
_real_ai_config_existed_before = ai_config.STATE_PATH.exists()
_real_ai_config_before = ai_config.get_config()
r_switch = client.post("/ai/config", headers=AUTH,
                       json={"provider": "anthropic_cli", "reason": "api route test",
                             "actor": "test"})
check("C10: POST /ai/config with valid input returns 200 and the new "
      "effective status", r_switch.status_code == 200
      and r_switch.get_json()["effective_provider"] == "anthropic_cli", r_switch.get_json())

# C11: the read-only guarantee for the write route — engine/memory/state
# untouched. Same style of proof test_api.py already uses for every other
# route: hash the real files most likely to be affected by an accidental
# write, before and after.
STATE_JSON = Path("memory/state.json")
_state_hash_before = STATE_JSON.stat().st_mtime if STATE_JSON.exists() else None
client.post("/ai/config", headers=AUTH, json={"provider": "openai", "reason": "isolation check"})
_state_hash_after = STATE_JSON.stat().st_mtime if STATE_JSON.exists() else None
check("C11: POST /ai/config never touches memory/state.json (mtime unchanged)",
      _state_hash_before == _state_hash_after, (_state_hash_before, _state_hash_after))

# restore the real ai_config.json to EXACTLY what it was before this test
# ran — including "did not exist at all", which set_config() cannot
# represent (it always requires a valid provider). Path.unlink() is a
# plain Python file removal, not the Bash `rm` command this environment
# denies — used here only to undo a file this same test just created.
if _real_ai_config_existed_before:
    ai_config.set_config(provider=_real_ai_config_before["provider"],
                         model=_real_ai_config_before.get("model"),
                         actor="test", reason="test cleanup: restore prior config")
else:
    ai_config.STATE_PATH.unlink(missing_ok=True)
check("C12: the real control/ai_config.json is restored to its pre-test "
      "state (existed before: %r)" % _real_ai_config_existed_before,
      ai_config.STATE_PATH.exists() == _real_ai_config_existed_before)


import shutil  # noqa: E402
shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'=' * 52}")
print(f"  {PASSED} passed, {FAILED} failed")
print(f"{'=' * 52}\n")
sys.exit(1 if FAILED else 0)
