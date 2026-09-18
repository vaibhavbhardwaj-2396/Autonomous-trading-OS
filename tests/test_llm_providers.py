"""
tests/test_llm_providers.py — INTELLIGENT + EFFICIENT + TRACEABLE
(outcome 2): the LLM provider abstraction (research/brain/llm.py) and its
AI budget (control/ai_budget.py).

Every test that would otherwise touch a real network endpoint or spawn a
real `claude` subprocess uses an injected/monkeypatched stand-in instead
(see Part L's own instruction: "create a properly isolated mock/provider
test — do not fake a production success"). Nothing here ever calls
OpenAI's real API or the real Claude Code CLI binary.

Sections:
  A. control/ai_budget.py — state, reset, budget_allows(), env overrides
  B. research/brain/llm.py — provider selection, budget deferral, the
     successful call path (both providers, mocked), error handling,
     artifact recording
  C. _openai_call() request/response construction, isolated from the
     closure, with urllib.request.urlopen mocked
  D. artifact bounding (research.memory.record_model_interaction) via a
     real (temp-file) Store — proves truncation without touching
     research/store.py

Run with:  python -m tests.test_llm_providers
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from control import ai_budget  # noqa: E402
from research.brain import llm  # noqa: E402
from research.brain import investigator as inv  # noqa: E402
from research import memory as rm  # noqa: E402
from research.store import Store, now_ist  # noqa: E402

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


TMP = Path(tempfile.mkdtemp(prefix="lq-test-llm-"))


def fresh_store(name) -> Store:
    return Store.open(TMP / f"{name}.db")


def isolated_budget(name):
    return TMP / f"budget-{name}.json", TMP / f"budget-{name}.lock"


# ===========================================================================
print("\n--- A: control/ai_budget.py ---")
# ===========================================================================

path_a, lock_a = isolated_budget("a")
s0 = ai_budget.get_budget_state(path=path_a)
check("A1: a fresh state (no file yet) has zero calls/tokens today",
      s0["calls_today"] == 0 and s0["tokens_today"] == 0
      and s0["expensive_calls_today"] == 0)
check("A2: a fresh state carries today's IST date", s0["date"] == ai_budget._today_str())

s1 = ai_budget.record_usage(tokens=500, path=path_a, lock_path=lock_a)
check("A3: record_usage() increments calls_today", s1["calls_today"] == 1)
check("A4: record_usage() increments tokens_today by the given amount",
      s1["tokens_today"] == 500)
check("A5: record_usage(expensive=True, default) increments expensive_calls_today too",
      s1["expensive_calls_today"] == 1)

s2 = ai_budget.record_usage(tokens=300, expensive=False, path=path_a, lock_path=lock_a)
check("A6: a second call accumulates tokens_today (500+300=800)", s2["tokens_today"] == 800)
check("A7: calls_today counts every call (2)", s2["calls_today"] == 2)
check("A8: expensive=False does not increment expensive_calls_today (stays 1)",
      s2["expensive_calls_today"] == 1)

# A9: a stale date resets to fresh zeros on next read.
stale = dict(s2)
stale["date"] = "2000-01-01"
path_a.write_text(json.dumps(stale))
s3 = ai_budget.get_budget_state(path=path_a)
check("A9: a stale (yesterday-or-older) date auto-resets to zero, never "
      "carries over a prior day's usage", s3["calls_today"] == 0
      and s3["tokens_today"] == 0, s3)

# A10-A13: budget_allows() logic against a hand-built state, isolated from
# any file at all.
limits = ai_budget.AIBudgetLimits(
    max_tokens_per_cycle=1000, max_tokens_per_day=5000, max_expensive_calls_per_day=3)
healthy_state = {"tokens_today": 0, "expensive_calls_today": 0}
allowed, reason = ai_budget.budget_allows(estimated_tokens=100, limits=limits, state=healthy_state)
check("A10: a small estimate against an empty budget is allowed",
      allowed is True and reason is None)

allowed, reason = ai_budget.budget_allows(estimated_tokens=1500, limits=limits, state=healthy_state)
check("A11: an estimate exceeding max_tokens_per_cycle is denied, "
      "with a reason naming the per-cycle limit",
      allowed is False and "max_tokens_per_cycle" in reason, reason)

near_daily_limit = {"tokens_today": 4900, "expensive_calls_today": 0}
allowed, reason = ai_budget.budget_allows(estimated_tokens=200, limits=limits, state=near_daily_limit)
check("A12: an estimate that would push today's total over max_tokens_per_day "
      "is denied", allowed is False and "max_tokens_per_day" in reason, reason)

exhausted_calls = {"tokens_today": 0, "expensive_calls_today": 3}
allowed, reason = ai_budget.budget_allows(estimated_tokens=10, limits=limits, state=exhausted_calls)
check("A13: hitting max_expensive_calls_per_day is denied, with a reason "
      "naming that limit", allowed is False and "max_expensive_calls_per_day" in reason, reason)

# A14: env overrides, same posture as WorkerLimits/ResourceThresholds.
_orig_env = os.environ.get("AI_BUDGET_MAX_TOKENS_PER_DAY")
try:
    os.environ["AI_BUDGET_MAX_TOKENS_PER_DAY"] = "12345"
    t = ai_budget.AIBudgetLimits.from_env()
    check("A14: a valid env override is applied", t.max_tokens_per_day == 12345)
    os.environ["AI_BUDGET_MAX_TOKENS_PER_DAY"] = "not-a-number"
    t2 = ai_budget.AIBudgetLimits.from_env()
    check("A15: a garbage env value falls back to the default, never crashes",
          t2.max_tokens_per_day == ai_budget.DEFAULT_LIMITS.max_tokens_per_day)
finally:
    if _orig_env is None:
        os.environ.pop("AI_BUDGET_MAX_TOKENS_PER_DAY", None)
    else:
        os.environ["AI_BUDGET_MAX_TOKENS_PER_DAY"] = _orig_env


# ===========================================================================
print("\n--- B: research/brain/llm.py — provider selection & call path ---")
# ===========================================================================

_orig_provider_env = os.environ.get(llm.ENV_PROVIDER)
try:
    os.environ.pop(llm.ENV_PROVIDER, None)
    check("B1: current_provider_name() defaults to anthropic_cli when unset",
          llm.current_provider_name() == llm.PROVIDER_ANTHROPIC_CLI)
    os.environ[llm.ENV_PROVIDER] = "OpenAI"
    check("B2: current_provider_name() respects RESEARCH_AI_PROVIDER "
          "(case-insensitive)", llm.current_provider_name() == llm.PROVIDER_OPENAI)
finally:
    if _orig_provider_env is None:
        os.environ.pop(llm.ENV_PROVIDER, None)
    else:
        os.environ[llm.ENV_PROVIDER] = _orig_provider_env

store_b = fresh_store("b")

_orig_budget_allows = ai_budget.budget_allows
_orig_record_usage = ai_budget.record_usage

# B3: budget exhausted — the closure must never call any provider.
provider_called = {"n": 0}
_orig_default_runner = inv._default_runner
inv._default_runner = lambda p: (provider_called.__setitem__("n", provider_called["n"] + 1), "SHOULD NOT RUN")[1]
ai_budget.budget_allows = lambda **kw: (False, "test-forced deferral")
try:
    runner_b3 = llm.build_configured_runner(
        store=store_b, cycle_id="cyc-b3", purpose="test", trigger="test",
        provider=llm.PROVIDER_ANTHROPIC_CLI)
    out_b3 = runner_b3("a prompt")
    parsed_b3 = json.loads(out_b3)
    check("B3: when the budget denies the call, the runner returns the "
          "existing no_proposal escape hatch, not an exception",
          parsed_b3.get("no_proposal") is True, parsed_b3)
    check("B3b: the deferral reason names the budget", "AI budget" in parsed_b3.get("reason", ""))
    check("B3c: no provider function was ever called", provider_called["n"] == 0)
    rows_b3 = rm.query_research_log(store_b, rm.DATASET_MODEL_INTERACTION, entity=rm.MARKET_ENTITY)
    check("B3d: a 'deferred' model-interaction artifact was still recorded",
          len(rows_b3) == 1 and rows_b3[0]["payload"]["status"] == "deferred", rows_b3)
finally:
    inv._default_runner = _orig_default_runner
    ai_budget.budget_allows = _orig_budget_allows

# B4: budget OK, anthropic_cli provider, mocked CLI call.
ai_budget.budget_allows = lambda **kw: (True, None)
usage_recorded = {}
ai_budget.record_usage = lambda **kw: usage_recorded.update(kw) or ai_budget.get_budget_state()
inv._default_runner = lambda p: '{"no_proposal": true, "reason": "nothing interesting"}'
try:
    runner_b4 = llm.build_configured_runner(
        store=store_b, cycle_id="cyc-b4", purpose="hypothesis_generation",
        trigger="test", provider=llm.PROVIDER_ANTHROPIC_CLI)
    out_b4 = runner_b4("digest prompt here")
    check("B4: with budget OK, the CLI provider's (mocked) output is "
          "returned unchanged", "nothing interesting" in out_b4)
    check("B4b: record_usage was called with tokens=0 (the CLI cannot "
          "report token counts — never fabricated)", usage_recorded.get("tokens") == 0)
    rows_b4 = [r for r in rm.query_research_log(store_b, rm.DATASET_MODEL_INTERACTION,
                                                entity=rm.MARKET_ENTITY) if r["payload"]["cycle_id"] == "cyc-b4"]
    check("B4c: an 'ok' artifact was recorded with the CLI model label",
          len(rows_b4) == 1 and rows_b4[0]["payload"]["status"] == "ok"
          and rows_b4[0]["payload"]["model"] == llm.ANTHROPIC_CLI_MODEL_LABEL, rows_b4)
    check("B4d: the artifact's input/output tokens are honestly None, not "
          "fabricated zeros", rows_b4[0]["payload"]["input_tokens"] is None)
    check("B4e: the artifact's prompt/response are stored in full for a "
          "short call", rows_b4[0]["payload"]["prompt"]["text"] == "digest prompt here"
          and "nothing interesting" in rows_b4[0]["payload"]["response"]["text"])
finally:
    inv._default_runner = _orig_default_runner

# B5: budget OK, openai provider, mocked _openai_call.
_orig_openai_call = llm._openai_call
llm._openai_call = lambda prompt, *, model: (
    '{"no_proposal": true, "reason": "openai says no"}',
    {"input_tokens": 120, "output_tokens": 40})
try:
    runner_b5 = llm.build_configured_runner(
        store=store_b, cycle_id="cyc-b5", purpose="hypothesis_generation",
        trigger="test", provider=llm.PROVIDER_OPENAI)
    out_b5 = runner_b5("another prompt")
    check("B5: with budget OK, the OpenAI (mocked) provider's output is "
          "returned unchanged", "openai says no" in out_b5)
    check("B5b: record_usage was called with the REAL reported token sum "
          "(120+40=160)", usage_recorded.get("tokens") == 160)
    rows_b5 = [r for r in rm.query_research_log(store_b, rm.DATASET_MODEL_INTERACTION,
                                                entity=rm.MARKET_ENTITY) if r["payload"]["cycle_id"] == "cyc-b5"]
    check("B5c: the artifact records provider=openai and real token counts",
          len(rows_b5) == 1 and rows_b5[0]["payload"]["provider"] == "openai"
          and rows_b5[0]["payload"]["input_tokens"] == 120
          and rows_b5[0]["payload"]["output_tokens"] == 40
          and rows_b5[0]["payload"]["total_tokens"] == 160, rows_b5)
finally:
    llm._openai_call = _orig_openai_call
    ai_budget.record_usage = _orig_record_usage
    ai_budget.budget_allows = _orig_budget_allows

# B6: a ProviderError from the OpenAI call becomes an InvestigatorError,
# never a bare ProviderError leaking to investigate().
ai_budget.budget_allows = lambda **kw: (True, None)
ai_budget.record_usage = lambda **kw: None
llm._openai_call = lambda prompt, *, model: (_ for _ in ()).throw(
    llm.ProviderError("simulated network failure"))
try:
    runner_b6 = llm.build_configured_runner(
        store=store_b, cycle_id="cyc-b6", purpose="test", trigger="test",
        provider=llm.PROVIDER_OPENAI)
    raised_type = None
    try:
        runner_b6("prompt")
    except Exception as e:  # noqa: BLE001
        raised_type = type(e)
    check("B6: a ProviderError from the provider call surfaces as "
          "InvestigatorError, not ProviderError, to any caller",
          raised_type is inv.InvestigatorError, raised_type)
    rows_b6 = [r for r in rm.query_research_log(store_b, rm.DATASET_MODEL_INTERACTION,
                                                entity=rm.MARKET_ENTITY) if r["payload"]["cycle_id"] == "cyc-b6"]
    check("B6b: an 'error' artifact was recorded with the failure reason",
          len(rows_b6) == 1 and rows_b6[0]["payload"]["status"] == "error"
          and "simulated network failure" in (rows_b6[0]["payload"]["error"] or ""), rows_b6)
finally:
    llm._openai_call = _orig_openai_call

# B7: an InvestigatorError from the CLI runner still propagates as
# InvestigatorError (unchanged type) and is recorded as an error artifact.
inv._default_runner = lambda p: (_ for _ in ()).throw(
    inv.InvestigatorError("simulated timeout", stage="timeout"))
try:
    runner_b7 = llm.build_configured_runner(
        store=store_b, cycle_id="cyc-b7", purpose="test", trigger="test",
        provider=llm.PROVIDER_ANTHROPIC_CLI)
    raised_stage = None
    try:
        runner_b7("prompt")
    except inv.InvestigatorError as e:
        raised_stage = e.stage
    check("B7: an InvestigatorError from the CLI runner propagates "
          "unchanged (same stage)", raised_stage == "timeout", raised_stage)
    rows_b7 = [r for r in rm.query_research_log(store_b, rm.DATASET_MODEL_INTERACTION,
                                                entity=rm.MARKET_ENTITY) if r["payload"]["cycle_id"] == "cyc-b7"]
    check("B7b: an 'error' artifact was recorded for the CLI timeout too",
          len(rows_b7) == 1 and rows_b7[0]["payload"]["status"] == "error", rows_b7)
finally:
    inv._default_runner = _orig_default_runner
    ai_budget.budget_allows = _orig_budget_allows
    ai_budget.record_usage = _orig_record_usage

# B8: an unknown provider name fails closed with a clear message.
ai_budget.budget_allows = lambda **kw: (True, None)
try:
    runner_b8 = llm.build_configured_runner(
        store=store_b, cycle_id="cyc-b8", purpose="test", trigger="test",
        provider="not_a_real_provider")
    raised = None
    try:
        runner_b8("prompt")
    except inv.InvestigatorError as e:
        raised = str(e)
    check("B8: an unknown provider name raises InvestigatorError naming "
          "the bad value", raised is not None and "not_a_real_provider" in raised, raised)
finally:
    ai_budget.budget_allows = _orig_budget_allows


# ===========================================================================
print("\n--- C: _openai_call() request/response construction ---")
# ===========================================================================

class _FakeHTTPResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


_orig_urlopen = llm.urllib.request.urlopen
_orig_api_key_env = os.environ.get(llm.ENV_OPENAI_API_KEY)

captured_request = {}


def _fake_urlopen(req, timeout=None):
    captured_request["url"] = req.full_url
    captured_request["headers"] = dict(req.headers)
    captured_request["body"] = json.loads(req.data.decode("utf-8"))
    captured_request["timeout"] = timeout
    return _FakeHTTPResponse(json.dumps({
        "choices": [{"message": {"content": '{"no_proposal": true, "reason": "ok"}'}}],
        "usage": {"prompt_tokens": 77, "completion_tokens": 33},
    }).encode("utf-8"))


try:
    os.environ[llm.ENV_OPENAI_API_KEY] = "sk-test-not-a-real-key"
    llm.urllib.request.urlopen = _fake_urlopen
    text, usage = llm._openai_call("hello world", model="gpt-4o-mini")
    check("C1: _openai_call() sends the correct endpoint URL",
          captured_request["url"] == llm.OPENAI_ENDPOINT, captured_request.get("url"))
    check("C2: the Authorization header carries the API key as a Bearer token",
          captured_request["headers"].get("Authorization") == "Bearer sk-test-not-a-real-key",
          captured_request["headers"])
    check("C3: the request body carries the given model and the prompt "
          "as the user message",
          captured_request["body"]["model"] == "gpt-4o-mini"
          and captured_request["body"]["messages"][-1]["content"] == "hello world",
          captured_request["body"])
    check("C4: the parsed response returns the model's content text",
          text == '{"no_proposal": true, "reason": "ok"}', text)
    check("C5: the parsed response returns the REAL reported token counts",
          usage == {"input_tokens": 77, "output_tokens": 33}, usage)
finally:
    llm.urllib.request.urlopen = _orig_urlopen
    if _orig_api_key_env is None:
        os.environ.pop(llm.ENV_OPENAI_API_KEY, None)
    else:
        os.environ[llm.ENV_OPENAI_API_KEY] = _orig_api_key_env

# C6: missing API key fails closed WITHOUT ever attempting the network call.
_orig_api_key_env2 = os.environ.get(llm.ENV_OPENAI_API_KEY)
network_attempted = {"n": 0}


def _boom_if_called(req, timeout=None):
    network_attempted["n"] += 1
    raise AssertionError("should never reach the network with no API key")


try:
    os.environ.pop(llm.ENV_OPENAI_API_KEY, None)
    llm.urllib.request.urlopen = _boom_if_called
    raised = None
    try:
        llm._openai_call("prompt", model="gpt-4o-mini")
    except llm.ProviderError as e:
        raised = str(e)
    check("C6: a missing OPENAI_API_KEY raises ProviderError with a clear "
          "message, naming the env var", raised is not None and llm.ENV_OPENAI_API_KEY in raised, raised)
    check("C6b: the network was never actually attempted", network_attempted["n"] == 0)
finally:
    llm.urllib.request.urlopen = _orig_urlopen
    if _orig_api_key_env2 is None:
        os.environ.pop(llm.ENV_OPENAI_API_KEY, None)
    else:
        os.environ[llm.ENV_OPENAI_API_KEY] = _orig_api_key_env2

# C7: a malformed response shape (missing choices) raises ProviderError.
try:
    os.environ[llm.ENV_OPENAI_API_KEY] = "sk-test"
    llm.urllib.request.urlopen = lambda req, timeout=None: _FakeHTTPResponse(
        json.dumps({"unexpected": "shape"}).encode("utf-8"))
    raised = None
    try:
        llm._openai_call("prompt", model="gpt-4o-mini")
    except llm.ProviderError as e:
        raised = str(e)
    check("C7: a malformed OpenAI response shape raises ProviderError, "
          "not a bare KeyError", raised is not None, raised)
finally:
    llm.urllib.request.urlopen = _orig_urlopen
    if _orig_api_key_env is None:
        os.environ.pop(llm.ENV_OPENAI_API_KEY, None)
    else:
        os.environ[llm.ENV_OPENAI_API_KEY] = _orig_api_key_env

# C8: a non-2xx HTTP response raises ProviderError with the status code.
class _FakeHTTPError(urllib.error.HTTPError):
    def __init__(self, code, body):
        super().__init__("url", code, "msg", {}, None)
        self._body = body

    def read(self):
        return self._body


def _raise_http_error(req, timeout=None):
    raise _FakeHTTPError(401, b'{"error": "invalid api key"}')


try:
    os.environ[llm.ENV_OPENAI_API_KEY] = "sk-test"
    llm.urllib.request.urlopen = _raise_http_error
    raised = None
    try:
        llm._openai_call("prompt", model="gpt-4o-mini")
    except llm.ProviderError as e:
        raised = str(e)
    check("C8: a non-2xx HTTP response raises ProviderError naming the "
          "status code", raised is not None and "401" in raised, raised)
finally:
    llm.urllib.request.urlopen = _orig_urlopen
    if _orig_api_key_env is None:
        os.environ.pop(llm.ENV_OPENAI_API_KEY, None)
    else:
        os.environ[llm.ENV_OPENAI_API_KEY] = _orig_api_key_env


# ===========================================================================
print("\n--- D: artifact bounding (research.memory.record_model_interaction) ---")
# ===========================================================================

store_d = fresh_store("d")
huge_prompt = "x" * (rm.MODEL_INTERACTION_TEXT_MAX + 500)
rm.record_model_interaction(
    store_d, provider="anthropic_cli", model="claude-code-cli", purpose="test",
    trigger="test", prompt=huge_prompt, response="short response", status="ok",
    cycle_id="cyc-d1",
)
rows_d = rm.query_research_log(store_d, rm.DATASET_MODEL_INTERACTION, entity=rm.MARKET_ENTITY)
check("D1: a prompt longer than MODEL_INTERACTION_TEXT_MAX is truncated in storage",
      len(rows_d[0]["payload"]["prompt"]["text"]) == rm.MODEL_INTERACTION_TEXT_MAX, rows_d)
check("D2: the ORIGINAL length is still recorded, even though the text was truncated",
      rows_d[0]["payload"]["prompt"]["length"] == len(huge_prompt), rows_d[0]["payload"]["prompt"])
check("D3: the truncated flag is set", rows_d[0]["payload"]["prompt"]["truncated"] is True)
check("D4: a short response is stored in full, with truncated=False",
      rows_d[0]["payload"]["response"]["text"] == "short response"
      and rows_d[0]["payload"]["response"]["truncated"] is False)
check("D5: no secret-shaped field (api key) exists anywhere in the payload",
      "api_key" not in json.dumps(rows_d[0]["payload"]).lower()
      and "sk-test" not in json.dumps(rows_d[0]["payload"]))


# ===========================================================================
print("\n--- E: control/ai_config.py — persisted provider/model config ---")
# ===========================================================================

from control import ai_config  # noqa: E402

path_e = TMP / "ai_config_e.json"
lock_e = TMP / "ai_config_e.lock"

check("E1: an unset config (no file yet) has provider=None",
      ai_config.get_config(path=path_e)["provider"] is None)

s_e1 = ai_config.set_config(provider="openai", model="gpt-4o", actor="vaibhav",
                            reason="testing", path=path_e, lock_path=lock_e)
check("E2: set_config() persists provider/model/actor/reason",
      s_e1["provider"] == "openai" and s_e1["model"] == "gpt-4o"
      and s_e1["actor"] == "vaibhav" and s_e1["reason"] == "testing")
check("E3: a fresh read sees the same persisted config",
      ai_config.get_config(path=path_e)["provider"] == "openai")

try:
    ai_config.set_config(provider="not_a_provider", model=None, actor="x", reason="y",
                          path=path_e, lock_path=lock_e)
    check("E4: an unknown provider is rejected", False, "no exception raised")
except ai_config.InvalidAIConfig:
    check("E4: an unknown provider is rejected", True)

try:
    ai_config.set_config(provider="openai", actor="", reason="y", path=path_e, lock_path=lock_e)
    check("E5: a blank actor is rejected", False, "no exception raised")
except ai_config.InvalidAIConfig:
    check("E5: a blank actor is rejected", True)

s_e2 = ai_config.set_config(provider="anthropic_cli", model=None, actor="vaibhav",
                            reason="switching back", path=path_e, lock_path=lock_e)
check("E6: switching providers records the PREVIOUS choice in history",
      len(s_e2["history"]) == 1 and s_e2["history"][0]["provider"] == "openai", s_e2)

# E7-E9: precedence in research.brain.llm.current_provider_name()/
# current_model_name() — env var > persisted config > built-in default.
_orig_env_provider = os.environ.get(llm.ENV_PROVIDER)
_orig_get_config = ai_config.get_config
try:
    os.environ.pop(llm.ENV_PROVIDER, None)
    ai_config.get_config = lambda **kw: {"provider": None, "model": None}
    check("E7: with no env var and no persisted config, current_provider_name() "
          "falls back to DEFAULT_PROVIDER", llm.current_provider_name() == llm.DEFAULT_PROVIDER)

    ai_config.get_config = lambda **kw: {"provider": "openai", "model": "gpt-4.1"}
    check("E8: with a persisted config and no env override, "
          "current_provider_name() uses the persisted choice",
          llm.current_provider_name() == "openai")
    check("E8b: current_model_name() uses the persisted model for openai",
          llm.current_model_name("openai") == "gpt-4.1")

    os.environ[llm.ENV_PROVIDER] = "anthropic_cli"
    check("E9: an explicit env var always wins over the persisted config",
          llm.current_provider_name() == "anthropic_cli")
finally:
    ai_config.get_config = _orig_get_config
    if _orig_env_provider is None:
        os.environ.pop(llm.ENV_PROVIDER, None)
    else:
        os.environ[llm.ENV_PROVIDER] = _orig_env_provider


import shutil  # noqa: E402
shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'=' * 52}")
print(f"  {PASSED} passed, {FAILED} failed")
print(f"{'=' * 52}\n")
sys.exit(1 if FAILED else 0)
