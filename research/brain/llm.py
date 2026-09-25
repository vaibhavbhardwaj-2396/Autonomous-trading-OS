"""
research/brain/llm.py — the LLM provider abstraction.
INTELLIGENT + EFFICIENT + TRACEABLE (outcome 2).

THE ACTUAL SEAM THIS BUILDS ON
---------------------------------------------------------------------------
research/brain/investigator.py::investigate() already takes an injectable
`runner: Callable[[str], str]` — prompt in, raw text out — and its default
is `_default_runner`, the existing Claude Code CLI subprocess call,
UNCHANGED. That is already a complete provider abstraction seam; nothing
about investigate(), hypothesis_intake, digest, or anything downstream of
build_prompt() needed to change. This module's only job is to build the
Callable[[str], str] that research/brain/worker.py hands to investigate(),
selecting a provider and wrapping every call with budget-checking and
artifact-recording — all invisible to investigate() itself.

    research/brain/worker.py::main()
              |
              v
    build_configured_runner(store=..., cycle_id=..., purpose=..., trigger=...)
              |                                    (constructs, does not call)
              v
    run_worker_cycle(..., runner=<that closure>)
              |
              v
    investigate(..., runner=<that closure>)         UNCHANGED
              |
              v
    <closure>(prompt)                               THIS is where:
              |                                        1. budget checked
              |                                        2. provider selected
              |                                        3. latency timed
              |                                        4. artifact recorded
              v
    raw text, exactly as investigate() has always expected

WHY THIS DOES NOT TOUCH run_cycle.sh's LIVE AGENT
---------------------------------------------------------------------------
The live trading agent (run_cycle.sh, docs/ENGINE_DEPLOY.md) invokes the
Claude Code CLI as a full agentic process with file/tool access to make
trading judgments — a fundamentally different thing from this module's
"send a bounded prompt, parse a JSON object back" structured call. Nothing
in this file is imported by run_cycle.sh or anything engine/ touches; the
existing engine<->research isolation (tests/test_kernel_isolation.py)
covers this module the same as every other file in research/brain/.

PROVIDERS
---------------------------------------------------------------------------
  anthropic_cli (default) — calls investigator._default_runner exactly as
      before. No token counts (the CLI's plain-text mode does not report
      them); latency and the prompt/response themselves are still
      recorded.
  openai — one real HTTP call to OpenAI's chat completions endpoint via
      the standard library only (urllib) — no new dependency for a single
      POST request. Reports real input/output token counts from the
      API's own `usage` field.

Selected via RESEARCH_AI_PROVIDER (env), defaulting to "anthropic_cli" —
an unconfigured deployment behaves exactly as it always has.

See docs/AI_PROVIDERS.md for the full design note and
tests/test_llm_providers.py for the test suite (provider selection,
budget deferral, artifact recording, malformed responses, provider
failure — all against injected/mocked calls, never a real network
request or a real claude subprocess).
"""

from __future__ import annotations

import json
import os
import shutil
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional, Protocol

from . import investigator as inv
from .. import memory as rm
from control import ai_budget
from control import ai_config

PROVIDER_ANTHROPIC_CLI = "anthropic_cli"
PROVIDER_OPENAI = "openai"
KNOWN_PROVIDERS = (PROVIDER_ANTHROPIC_CLI, PROVIDER_OPENAI)

ENV_PROVIDER = "RESEARCH_AI_PROVIDER"
ENV_OPENAI_MODEL = "RESEARCH_AI_OPENAI_MODEL"
ENV_OPENAI_API_KEY = "OPENAI_API_KEY"
DEFAULT_PROVIDER = PROVIDER_ANTHROPIC_CLI
DEFAULT_OPENAI_MODEL = "gpt-5.6-terra"
ANTHROPIC_CLI_MODEL_LABEL = "claude-code-cli"

OPENAI_ENDPOINT = "https://api.openai.com/v1/responses"
OPENAI_TIMEOUT_SECONDS = 120
OPENAI_SYSTEM_PREAMBLE = (
    "You are the Living Quant Research AI. Respond with a single JSON "
    "object only, exactly matching the instructions in the prompt. No "
    "prose, no markdown code fences, no explanation outside the object."
)


@dataclass(frozen=True)
class ModelResponse:
    text: str
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None


class ModelProvider(Protocol):
    """Provider-neutral boundary used by every research reasoning call."""

    name: str
    auth_mode: str

    def invoke(self, prompt: str, *, model: str) -> ModelResponse: ...


class ProviderError(RuntimeError):
    """A provider-level failure — network, auth, malformed API response.
    Always converted to an investigator.InvestigatorError before
    escaping build_configured_runner()'s closure, so investigate() never
    needs a second except clause for "the provider abstraction failed" vs.
    "the CLI subprocess failed" — both boundary failures look identical
    from investigate()'s point of view, which is the whole point of the
    abstraction."""

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


def current_provider_name() -> str:
    """Precedence: RESEARCH_AI_PROVIDER env var (an explicit, ops-level
    override) > control/ai_config.py's persisted, UI-settable choice >
    DEFAULT_PROVIDER. Read fresh every call — nothing here is cached at
    import — so a provider switch via the AI configuration API takes
    effect on the very next heartbeat (the worker is a fresh subprocess
    per cron tick, so there is no running process to notify — it simply
    reads the current file next time it runs), the same "no caching, read
    at call time" posture investigator.resolve_claude_binary() already
    documents. See control/ai_config.py's own docstring for why a plain
    env var alone cannot do this (the API and the worker are different
    processes)."""
    env_override = (os.environ.get(ENV_PROVIDER) or "").strip().lower()
    if env_override:
        return env_override
    persisted = ai_config.get_config().get("provider")
    return persisted or DEFAULT_PROVIDER


def current_model_name(provider_name: str, purpose: Optional[str] = None) -> str:
    """The model to use for `provider_name`, honoring the same precedence
    as current_provider_name(): env var > persisted config > built-in
    default. Only meaningful for openai today (the CLI provider's "model"
    is always ANTHROPIC_CLI_MODEL_LABEL, a label, not a real selection)."""
    if provider_name != PROVIDER_OPENAI:
        return ANTHROPIC_CLI_MODEL_LABEL
    env_override = (os.environ.get(ENV_OPENAI_MODEL) or "").strip()
    if env_override:
        return env_override
    persisted = ai_config.get_config()
    mapped = (persisted.get("role_mappings") or {}).get(purpose or "")
    if mapped and mapped != "deterministic":
        return mapped
    if persisted.get("provider") == PROVIDER_OPENAI and persisted.get("model"):
        return persisted["model"]
    return DEFAULT_OPENAI_MODEL


def _estimate_tokens(text: str) -> int:
    """A rough, honest, ~4-chars/token estimate used ONLY for the
    before-the-call budget check — never reported as a measured value in
    any artifact. See research.memory.record_model_interaction, which
    only ever stores provider-REPORTED counts (or None when a provider,
    like the CLI, cannot report them)."""
    return max(1, len(text) // 4)


def _anthropic_cli_call(prompt: str) -> tuple:
    """The existing, unmodified Claude Code CLI subprocess call. Returns
    (raw_text, usage) with usage always None."""
    raw = inv._default_runner(prompt)
    return raw, None


def _openai_call(prompt: str, *, model: str) -> tuple:
    """One HTTP POST to OpenAI's chat completions endpoint. Raises
    ProviderError for any failure (missing key, network, non-2xx,
    unparseable body, unexpected shape) — never a bare exception type a
    caller would need to know this module's internals to catch."""
    api_key = (os.environ.get(ENV_OPENAI_API_KEY) or "").strip()
    if not api_key:
        raise ProviderError(
            f"{ENV_OPENAI_API_KEY} is not set on the server — cannot call "
            f"OpenAI. Set it in the server environment only (never in "
            f"frontend code, browser storage, logs, or committed config).")

    body = json.dumps({"model": model, "store": False,
        "instructions": OPENAI_SYSTEM_PREAMBLE, "input": prompt,
        "reasoning": {"effort": "low"}, "max_output_tokens": 4000}).encode("utf-8")
    req = urllib.request.Request(
        OPENAI_ENDPOINT, data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=OPENAI_TIMEOUT_SECONDS) as resp:
            parsed = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:500]
        retryable = e.code in (408, 409, 429) or e.code >= 500
        raise ProviderError(
            f"OpenAI API returned HTTP {e.code}: {detail}", retryable=retryable
        ) from e
    except urllib.error.URLError as e:
        raise ProviderError(f"could not reach the OpenAI API: {e}", retryable=True) from e
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ProviderError(f"OpenAI API returned an unparseable response body: {e}") from e

    try:
        text = parsed.get("output_text")
        if not text:
            text = next(part["text"] for item in parsed.get("output", [])
                        if item.get("type") == "message" for part in item.get("content", [])
                        if part.get("type") == "output_text")
    except (KeyError, IndexError, TypeError, StopIteration) as e:
        raise ProviderError(
            f"OpenAI API response did not have the expected shape "
            f"(Responses output_text): {e}") from e

    usage = parsed.get("usage") or {}
    return text, {"input_tokens": usage.get("input_tokens"),
                 "output_tokens": usage.get("output_tokens")}


class AnthropicCLIProvider:
    name = PROVIDER_ANTHROPIC_CLI
    auth_mode = "local_cli_session"

    def invoke(self, prompt: str, *, model: str) -> ModelResponse:
        text, usage = _anthropic_cli_call(prompt)
        return ModelResponse(text=text)


class OpenAIProvider:
    name = PROVIDER_OPENAI
    auth_mode = "environment_api_key"

    def invoke(self, prompt: str, *, model: str) -> ModelResponse:
        text, usage = _openai_call(prompt, model=model)
        usage = usage or {}
        return ModelResponse(text=text, input_tokens=usage.get("input_tokens"),
                             output_tokens=usage.get("output_tokens"))


PROVIDER_REGISTRY: dict[str, ModelProvider] = {
    PROVIDER_ANTHROPIC_CLI: AnthropicCLIProvider(),
    PROVIDER_OPENAI: OpenAIProvider(),
}


def get_provider(name: str) -> ModelProvider:
    try:
        return PROVIDER_REGISTRY[name]
    except KeyError as exc:
        raise ProviderError(
            f"unknown {ENV_PROVIDER}={name!r} — expected one of {tuple(PROVIDER_REGISTRY)}") from exc


def provider_catalog() -> list[dict]:
    """Non-secret provider metadata suitable for the admin UI."""
    configured = {
        PROVIDER_OPENAI: bool((os.environ.get(ENV_OPENAI_API_KEY) or "").strip()),
        PROVIDER_ANTHROPIC_CLI: bool(shutil.which("claude")),
    }
    return [{"provider": name, "auth_mode": provider.auth_mode,
             "configured": configured[name],
             "status": "CONFIGURED_UNVERIFIED" if configured[name] else "NOT_CONFIGURED"}
            for name, provider in PROVIDER_REGISTRY.items()]


def build_configured_runner(
    *, store, cycle_id: str, purpose: str, trigger: str,
    provider: Optional[str] = None,
) -> Callable[[str], str]:
    """Builds the Callable[[str], str] research/brain/worker.py hands to
    run_worker_cycle(runner=...). Constructing this is inert — nothing
    here touches the budget, a provider, or the store until the returned
    closure is actually CALLED, which only happens if investigate()
    genuinely decides to invoke its runner (i.e. a DISCOVER action was
    selected this heartbeat). Every existing test that passes its own
    `runner=` directly to run_worker_cycle()/investigate() bypasses this
    function entirely and is completely unaffected by it.

    `provider`, if given, overrides RESEARCH_AI_PROVIDER for this one
    runner (used by tests and by a future "try the other provider for
    this one call" API action) — production call sites never pass it,
    relying on the env var / DEFAULT_PROVIDER instead.
    """
    provider_name = provider or current_provider_name()

    def runner(prompt: str) -> str:
        estimated = _estimate_tokens(prompt)
        allowed, deny_reason = ai_budget.budget_allows(estimated_tokens=estimated)
        if not allowed:
            rm.record_model_interaction(
                store, provider=provider_name, model="(not called)", purpose=purpose,
                trigger=trigger, prompt=prompt, response=None, status="deferred",
                cycle_id=cycle_id, error=deny_reason,
            )
            # The SAME escape hatch investigate() already treats as a
            # normal, valid outcome — routines/research_investigate.md's
            # own {"no_proposal": true, "reason": ...} contract. Zero
            # changes needed downstream for "the AI budget said defer."
            return json.dumps({"no_proposal": True, "reason": f"AI budget: {deny_reason}"})

        t0 = time.monotonic()
        model_name = current_model_name(provider_name, purpose)
        actual_model = model_name
        fallback_used = False
        try:
            response = get_provider(provider_name).invoke(prompt, model=model_name)
            text = response.text
            usage = {"input_tokens": response.input_tokens,
                     "output_tokens": response.output_tokens}
        except inv.InvestigatorError as e:
            latency = round(time.monotonic() - t0, 3)
            rm.record_model_interaction(
                store, provider=provider_name, model=model_name, purpose=purpose,
                trigger=trigger, prompt=prompt, response=None, status="error",
                cycle_id=cycle_id, latency_seconds=latency,
                error=f"{e.stage}: {e}" if getattr(e, "stage", None) else str(e),
            )
            raise
        except ProviderError as e:
            latency = round(time.monotonic() - t0, 3)
            fallback = ai_config.get_config().get("fallback") or {}
            fallback_model = (fallback.get("model") or "").strip()
            may_fallback = (
                e.retryable and fallback.get("enabled") is True
                and provider_name == PROVIDER_OPENAI and fallback_model
                and fallback_model != model_name
            )
            rm.record_model_interaction(
                store, provider=provider_name, model=model_name, purpose=purpose,
                trigger=trigger, prompt=prompt, response=None, status="error",
                cycle_id=cycle_id, latency_seconds=latency, error=str(e),
                extra={"prompt_version": "research-json-v1", "workflow": purpose,
                       "retryable": e.retryable, "fallback_attempted": may_fallback,
                       "fallback_model": fallback_model if may_fallback else None},
            )
            if not may_fallback:
                raise inv.InvestigatorError(str(e), stage="invocation_error") from e
            try:
                t0 = time.monotonic()
                response = get_provider(provider_name).invoke(prompt, model=fallback_model)
                text = response.text
                usage = {"input_tokens": response.input_tokens,
                         "output_tokens": response.output_tokens}
                actual_model = fallback_model
                fallback_used = True
            except ProviderError as fallback_error:
                fallback_latency = round(time.monotonic() - t0, 3)
                rm.record_model_interaction(
                    store, provider=provider_name, model=fallback_model, purpose=purpose,
                    trigger=trigger, prompt=prompt, response=None, status="error",
                    cycle_id=cycle_id, latency_seconds=fallback_latency,
                    error=str(fallback_error),
                    extra={"prompt_version": "research-json-v1", "workflow": purpose,
                           "retryable": fallback_error.retryable,
                           "fallback_attempted": True, "fallback_from": model_name},
                )
                raise inv.InvestigatorError(
                    f"primary model failed ({e}); fallback failed ({fallback_error})",
                    stage="invocation_error",
                ) from fallback_error

        latency = round(time.monotonic() - t0, 3)
        input_tokens = (usage or {}).get("input_tokens")
        output_tokens = (usage or {}).get("output_tokens")
        total_tokens = (input_tokens or 0) + (output_tokens or 0) if usage else 0
        ai_budget.record_usage(tokens=total_tokens, expensive=True)
        rm.record_model_interaction(
            store, provider=provider_name, model=actual_model, purpose=purpose,
            trigger=trigger, prompt=prompt, response=text, status="ok",
            cycle_id=cycle_id, input_tokens=input_tokens, output_tokens=output_tokens,
            latency_seconds=latency,
            extra={"prompt_version": "research-json-v1", "workflow": purpose,
                   "fallback_used": fallback_used,
                   "fallback_from": model_name if fallback_used else None},
        )
        return text

    return runner
