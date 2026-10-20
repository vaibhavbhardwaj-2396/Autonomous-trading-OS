"""Isolated tests for capability routing, fallback, redaction and traces."""
from __future__ import annotations
import datetime as dt
import json, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from control import ai_budget, ai_config
from research import memory as rm
from research.brain import llm
from research.store import Store

PASSED = FAILED = 0
def check(name, condition, detail=""):
    global PASSED, FAILED
    if condition: PASSED += 1; print(f"  ✓ {name}")
    else: FAILED += 1; print(f"  ✗ {name}\n      {detail}")

tmp = Path(tempfile.mkdtemp(prefix="lq-ai-gateway-"))
cfg_path, lock_path = tmp / "ai.json", tmp / "ai.lock"
print("\n--- capability configuration ---")
fresh = ai_config.get_config(path=cfg_path)
check("new config has no implicit provider", fresh["provider"] is None)
check("roles map to capabilities, not models", fresh["role_mappings"]["hypothesis_generation"] == "REASONING")
configured = ai_config.set_config(provider="openai", model="reason-model", actor="test",
    reason="isolated routing proof", capability_routes={"REASONING": {
        "primary": {"provider": "openai", "model": "reason-model"},
        "fallback": {"provider": "anthropic", "model": "fallback-model"}}},
    path=cfg_path, lock_path=lock_path)
check("capability route persists cross-provider fallback", configured["capability_routes"]["REASONING"]["fallback"]["provider"] == "anthropic")
check("persisted config contains no credential values", "secret-value" not in json.dumps(configured).lower())
try:
    ai_config.set_config(provider="openai", model="m", actor="test", reason="reject key exfiltration",
        providers={"openai":{"endpoint":"https://attacker.invalid/v1/responses"}},
        path=cfg_path, lock_path=lock_path)
    official_endpoint_rejected = False
except ai_config.InvalidAIConfig:
    official_endpoint_rejected = True
check("official provider endpoint cannot be redirected with its API key", official_endpoint_rejected)
try:
    ai_config.set_config(provider="openai", model="m", actor="test", reason="reject SSRF",
        providers={"local_openai":{"enabled":True,"endpoint":"http://169.254.169.254/latest"}},
        path=cfg_path, lock_path=lock_path)
    local_ssrf_rejected = False
except ai_config.InvalidAIConfig:
    local_ssrf_rejected = True
check("no-auth local provider is restricted to loopback", local_ssrf_rejected)

print("\n--- provider registry ---")
catalog = llm.provider_catalog()
check("official and compatible adapters are registered", {"openai", "anthropic", "openai_compatible", "local_openai"} == {r["provider"] for r in catalog})
check("catalog exposes status but no credential", all("credential_status" in r for r in catalog) and "secret-value" not in json.dumps(catalog).lower())
orig_get_config_for_catalog = llm.ai_config.get_config
orig_anthropic_key_for_catalog = llm.os.environ.get(llm.ENV_ANTHROPIC_API_KEY)
stale_cfg = ai_config.get_config(path=cfg_path)
stale_cfg["providers"]["anthropic"].update({
    "health": "HEALTHY",
    "last_successful_request": (dt.datetime.now().astimezone() - dt.timedelta(days=2)).isoformat(),
})
try:
    llm.ai_config.get_config = lambda: stale_cfg
    llm.os.environ[llm.ENV_ANTHROPIC_API_KEY] = "secret-value"
    stale_row = next(r for r in llm.provider_catalog() if r["provider"] == "anthropic")
    check("a configured provider with a success older than 24h reports STALE, not HEALTHY",
          stale_row["configured"] is True and stale_row["health"] == "STALE", stale_row)
finally:
    llm.ai_config.get_config = orig_get_config_for_catalog
    if orig_anthropic_key_for_catalog is None:
        llm.os.environ.pop(llm.ENV_ANTHROPIC_API_KEY, None)
    else:
        llm.os.environ[llm.ENV_ANTHROPIC_API_KEY] = orig_anthropic_key_for_catalog

print("\n--- Anthropic workspace routing ---")
orig_read_json = llm._read_json
orig_key = llm.os.environ.get(llm.ENV_ANTHROPIC_API_KEY)
orig_workspace = llm.os.environ.get(llm.ENV_ANTHROPIC_WORKSPACE_ID)
captured_headers = {}
def capture_anthropic_request(request):
    captured_headers.update({k.lower(): v for k, v in request.header_items()})
    return {"content": [{"type": "text", "text": "{}"}], "usage": {}}
try:
    llm.os.environ[llm.ENV_ANTHROPIC_API_KEY] = "secret-value"
    llm.os.environ[llm.ENV_ANTHROPIC_WORKSPACE_ID] = "workspace-value"
    llm._read_json = capture_anthropic_request
    llm.AnthropicProvider().invoke("bounded test", model="model")
    check("organization-level Anthropic keys send the configured workspace header",
          captured_headers.get("anthropic-workspace-id") == "workspace-value")
finally:
    llm._read_json = orig_read_json
    if orig_key is None: llm.os.environ.pop(llm.ENV_ANTHROPIC_API_KEY, None)
    else: llm.os.environ[llm.ENV_ANTHROPIC_API_KEY] = orig_key
    if orig_workspace is None: llm.os.environ.pop(llm.ENV_ANTHROPIC_WORKSPACE_ID, None)
    else: llm.os.environ[llm.ENV_ANTHROPIC_WORKSPACE_ID] = orig_workspace

print("\n--- gateway trace and fallback ---")
store = Store.open(tmp / "research.db")
orig_resolve, orig_budget = ai_config.resolve_role, ai_budget.budget_allows
orig_usage, orig_health = ai_budget.record_usage, ai_config.record_provider_result
orig_registry = dict(llm.PROVIDER_REGISTRY)
class Fake:
    auth_mode = "API_KEY"
    def __init__(self, name, fail=False, retryable=False): self.name, self.fail, self.retryable = name, fail, retryable
    def invoke(self, prompt, *, model):
        if self.fail: raise llm.ProviderError(f"{self.name} operational failure", retryable=self.retryable)
        return llm.ModelResponse('{"no_proposal":true,"reason":"bounded proof"}', 11, 4)
ai_config.resolve_role = lambda role, config=None: ("REASONING", {"primary":{"provider":"openai","model":"primary"}, "fallback":{"provider":"anthropic","model":"fallback"}})
ai_budget.budget_allows = lambda **kw: (True, None)
usage = {}
ai_budget.record_usage = lambda **kw: usage.update(kw)
ai_config.record_provider_result = lambda *a, **kw: None
llm.PROVIDER_REGISTRY["openai"], llm.PROVIDER_REGISTRY["anthropic"] = Fake("openai", True, True), Fake("anthropic")
try:
    output = llm.build_configured_runner(store=store, cycle_id="cycle-1", purpose="hypothesis_generation", trigger="test")("compact packet")
    rows = rm.query_research_log(store, rm.DATASET_MODEL_INTERACTION, limit=None)
    check("retryable failure uses cross-provider fallback", "bounded proof" in output and len(rows) == 2)
    check("attempts record capability and fallback reason", rows[0]["payload"]["capability"] == "REASONING" and rows[1]["payload"]["fallback_used"] is True and "operational failure" in rows[1]["payload"]["fallback_reason"])
    check("reported token use is recorded", usage.get("tokens") == 15)
    llm.PROVIDER_REGISTRY["openai"] = Fake("openai", True, False)
    before = len(rows)
    try: llm.build_configured_runner(store=store, cycle_id="cycle-2", purpose="hypothesis_generation", trigger="test")("packet")
    except Exception: pass
    after = rm.query_research_log(store, rm.DATASET_MODEL_INTERACTION, limit=None)
    check("non-retryable failure never falls back", len(after) == before + 1)
    ai_config.resolve_role = lambda role, config=None: ("REASONING", {"primary":None,"fallback":None})
    deferred = llm.build_configured_runner(store=store, cycle_id="cycle-3", purpose="hypothesis_generation", trigger="test")("packet")
    check("missing route reports generic state", "NO_PROVIDER_CONFIGURED" in deferred)
    ai_budget.budget_allows = lambda **kw: (False, "daily call limit reached")
    proof = llm.test_provider("openai", "test")
    check("provider proof is bounded by the shared AI budget", proof.get("blocked") is True and "budget" in proof["error"].lower())
finally:
    ai_config.resolve_role, ai_budget.budget_allows = orig_resolve, orig_budget
    ai_budget.record_usage, ai_config.record_provider_result = orig_usage, orig_health
    llm.PROVIDER_REGISTRY.clear(); llm.PROVIDER_REGISTRY.update(orig_registry); store.close()
print(f"\n{PASSED} passed, {FAILED} failed")
if FAILED: raise SystemExit(1)
