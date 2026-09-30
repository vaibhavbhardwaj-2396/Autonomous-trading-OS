"""Provider-neutral AI gateway for bounded research reasoning.

The workflow requests a research role; ``control.ai_config`` maps that role
to a capability and the capability to a primary/fallback provider-model pair.
Fallback is attempted only after a retryable operational provider failure.
Every attempt is persisted as a model-interaction artifact.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Protocol

from control import ai_budget, ai_config
from . import investigator as inv
from .. import memory as rm

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

if load_dotenv is not None:
    # Root-run workers retain the root-only trading environment.  The API is
    # deliberately denied that file and receives only AI credentials through
    # deploy/ai.env.  Keep the loads independent: PermissionError on the root
    # file must not prevent the least-privilege file from being read.
    for _env_file in (
        Path(__file__).resolve().parents[2] / ".env",
        Path(__file__).resolve().parents[2] / "deploy" / "ai.env",
    ):
        try:
            load_dotenv(_env_file, override=False)
        except OSError:
            continue

PROVIDER_OPENAI = "openai"
PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_OPENAI_COMPATIBLE = "openai_compatible"
PROVIDER_LOCAL_OPENAI = "local_openai"
KNOWN_PROVIDERS = ai_config.KNOWN_PROVIDERS

ENV_PROVIDER = "RESEARCH_AI_PROVIDER"  # legacy emergency override
ENV_OPENAI_MODEL = "RESEARCH_AI_OPENAI_MODEL"
ENV_OPENAI_API_KEY = "OPENAI_API_KEY"
ENV_ANTHROPIC_API_KEY = "ANTHROPIC_API_KEY"
ENV_COMPATIBLE_API_KEY = "OPENAI_COMPATIBLE_API_KEY"
ENV_COMPATIBLE_BASE_URL = "OPENAI_COMPATIBLE_BASE_URL"
ENV_LOCAL_BASE_URL = "LOCAL_AI_BASE_URL"
DEFAULT_PROVIDER = None
DEFAULT_OPENAI_MODEL = None

REQUEST_TIMEOUT_SECONDS = 120
SYSTEM_PREAMBLE = (
    "You are the Living Quant Research AI. Return one JSON object exactly "
    "matching the prompt schema. Do not include prose or markdown fences."
)


@dataclass(frozen=True)
class ModelResponse:
    text: str
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None


class ModelProvider(Protocol):
    name: str
    auth_mode: str
    def invoke(self, prompt: str, *, model: str) -> ModelResponse: ...


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


def _read_json(req: urllib.request.Request) -> dict:
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        retryable = exc.code in (408, 409, 429) or exc.code >= 500
        raise ProviderError(f"provider returned HTTP {exc.code}: {detail}",
                            retryable=retryable) from exc
    except urllib.error.URLError as exc:
        raise ProviderError(f"provider unavailable: {exc}", retryable=True) from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ProviderError(f"provider returned invalid JSON: {exc}") from exc


def _output_text(parsed: dict) -> str:
    text = parsed.get("output_text")
    if text:
        return text
    for item in parsed.get("output", []):
        for part in item.get("content", []):
            if part.get("type") == "output_text" and part.get("text"):
                return part["text"]
    raise ProviderError("provider response contained no output text")


class OpenAIProvider:
    name, auth_mode = PROVIDER_OPENAI, "API_KEY"
    def invoke(self, prompt: str, *, model: str) -> ModelResponse:
        key = (os.environ.get(ENV_OPENAI_API_KEY) or "").strip()
        if not key:
            raise ProviderError(f"{ENV_OPENAI_API_KEY} is not configured")
        endpoint = ai_config.get_config()["providers"][self.name]["endpoint"]
        body = json.dumps({"model": model, "store": False,
                           "instructions": SYSTEM_PREAMBLE, "input": prompt,
                           "max_output_tokens": 4000}).encode()
        parsed = _read_json(urllib.request.Request(
            endpoint, data=body, method="POST",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}))
        usage = parsed.get("usage") or {}
        return ModelResponse(_output_text(parsed), usage.get("input_tokens"),
                             usage.get("output_tokens"))


class AnthropicProvider:
    name, auth_mode = PROVIDER_ANTHROPIC, "API_KEY"
    def invoke(self, prompt: str, *, model: str) -> ModelResponse:
        key = (os.environ.get(ENV_ANTHROPIC_API_KEY) or "").strip()
        if not key:
            raise ProviderError(f"{ENV_ANTHROPIC_API_KEY} is not configured")
        endpoint = ai_config.get_config()["providers"][self.name]["endpoint"]
        body = json.dumps({"model": model, "max_tokens": 4000,
                           "system": SYSTEM_PREAMBLE,
                           "messages": [{"role": "user", "content": prompt}]}).encode()
        parsed = _read_json(urllib.request.Request(
            endpoint, data=body, method="POST", headers={
                "x-api-key": key, "anthropic-version": "2023-06-01",
                "Content-Type": "application/json"}))
        try:
            text = "".join(x.get("text", "") for x in parsed["content"] if x.get("type") == "text")
            if not text:
                raise KeyError("empty content")
        except (KeyError, TypeError) as exc:
            raise ProviderError(f"Anthropic response contained no text: {exc}") from exc
        usage = parsed.get("usage") or {}
        return ModelResponse(text, usage.get("input_tokens"), usage.get("output_tokens"))


def _compatible_endpoint(provider_name: str) -> tuple[str, Optional[str]]:
    cfg = ai_config.get_config()["providers"][provider_name]
    if provider_name == PROVIDER_LOCAL_OPENAI:
        endpoint = (os.environ.get(ENV_LOCAL_BASE_URL) or cfg.get("endpoint") or "").strip()
        parsed = urllib.parse.urlparse(endpoint)
        if parsed.scheme not in ("http", "https") or parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ProviderError("LOCAL_AI_BASE_URL must be an explicit loopback HTTP endpoint")
        return endpoint.rstrip("/") + "/chat/completions", None
    endpoint = (os.environ.get(ENV_COMPATIBLE_BASE_URL) or cfg.get("endpoint") or "").strip()
    key = (os.environ.get(ENV_COMPATIBLE_API_KEY) or "").strip()
    if not endpoint or not key:
        raise ProviderError("OpenAI-compatible endpoint and API key are not configured")
    return endpoint.rstrip("/") + "/chat/completions", key


class OpenAICompatibleProvider:
    auth_mode = "API_KEY"
    def __init__(self, name: str):
        self.name = name
        if name == PROVIDER_LOCAL_OPENAI:
            self.auth_mode = "LOCAL_NO_AUTH"
    def invoke(self, prompt: str, *, model: str) -> ModelResponse:
        endpoint, key = _compatible_endpoint(self.name)
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        body = json.dumps({"model": model, "temperature": 0,
                           "messages": [{"role": "system", "content": SYSTEM_PREAMBLE},
                                        {"role": "user", "content": prompt}],
                           "max_tokens": 4000}).encode()
        parsed = _read_json(urllib.request.Request(endpoint, data=body, method="POST", headers=headers))
        try:
            text = parsed["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"compatible response contained no message: {exc}") from exc
        usage = parsed.get("usage") or {}
        return ModelResponse(text, usage.get("prompt_tokens"), usage.get("completion_tokens"))


PROVIDER_REGISTRY: dict[str, ModelProvider] = {
    PROVIDER_OPENAI: OpenAIProvider(), PROVIDER_ANTHROPIC: AnthropicProvider(),
    PROVIDER_OPENAI_COMPATIBLE: OpenAICompatibleProvider(PROVIDER_OPENAI_COMPATIBLE),
    PROVIDER_LOCAL_OPENAI: OpenAICompatibleProvider(PROVIDER_LOCAL_OPENAI),
}


def get_provider(name: str) -> ModelProvider:
    try:
        return PROVIDER_REGISTRY[name]
    except KeyError as exc:
        raise ProviderError(f"unknown provider {name!r}") from exc


def _credential_status(name: str, cfg: dict) -> tuple[bool, str]:
    if not cfg.get("enabled"):
        return False, "DISABLED"
    if name == PROVIDER_OPENAI:
        ok = bool((os.environ.get(ENV_OPENAI_API_KEY) or "").strip())
    elif name == PROVIDER_ANTHROPIC:
        ok = bool((os.environ.get(ENV_ANTHROPIC_API_KEY) or "").strip())
    elif name == PROVIDER_OPENAI_COMPATIBLE:
        ok = bool((os.environ.get(ENV_COMPATIBLE_API_KEY) or "").strip() and
                  (os.environ.get(ENV_COMPATIBLE_BASE_URL) or cfg.get("endpoint")))
    else:
        try:
            _compatible_endpoint(name)
            ok = True
        except ProviderError:
            ok = False
    return ok, "CONFIGURED" if ok else "NOT_CONFIGURED"


def provider_catalog() -> list[dict]:
    """Secret-free persisted registry plus current credential presence."""
    cfg = ai_config.get_config()
    out = []
    for name in KNOWN_PROVIDERS:
        row = dict(cfg["providers"][name])
        configured, status = _credential_status(name, row)
        row.update({"provider": name, "configured": configured,
                    "credential_status": status,
                    "health": row.get("health") or ("UNVERIFIED" if configured else status),
                    "rate_limit_state": row.get("rate_limit_state") or "UNKNOWN",
                    "usage": row.get("usage") or {}, "cost": row.get("cost")})
        out.append(row)
    return out


def current_provider_name(purpose: str = "hypothesis_generation") -> Optional[str]:
    override = (os.environ.get(ENV_PROVIDER) or "").strip().lower()
    if override:
        return override
    _, route = ai_config.resolve_role(purpose)
    return (route.get("primary") or {}).get("provider")


def current_model_name(provider_name: Optional[str], purpose: Optional[str] = None) -> Optional[str]:
    if provider_name == PROVIDER_OPENAI and (os.environ.get(ENV_OPENAI_MODEL) or "").strip():
        return os.environ[ENV_OPENAI_MODEL].strip()
    _, route = ai_config.resolve_role(purpose or "hypothesis_generation")
    for target in (route.get("primary"), route.get("fallback")):
        if target and target.get("provider") == provider_name:
            return target.get("model")
    return None


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _route_for(purpose: str, explicit_provider: Optional[str]) -> tuple[str, dict, Optional[dict]]:
    capability, route = ai_config.resolve_role(purpose)
    primary = route.get("primary")
    if explicit_provider:
        model = current_model_name(explicit_provider, purpose)
        primary = {"provider": explicit_provider, "model": model}
    if not primary or not primary.get("provider") or not primary.get("model"):
        raise ProviderError(f"NO_PROVIDER_CONFIGURED for capability {capability}")
    return capability, primary, route.get("fallback")


def build_configured_runner(*, store, cycle_id: str, purpose: str, trigger: str,
                            provider: Optional[str] = None) -> Callable[[str], str]:
    def runner(prompt: str) -> str:
        try:
            capability, primary, fallback = _route_for(purpose, provider)
        except ProviderError as exc:
            rm.record_model_interaction(store, provider="none", model="none", purpose=purpose,
                trigger=trigger, prompt=prompt, response=None, status="deferred",
                cycle_id=cycle_id, error=str(exc), extra={"capability": None,
                "prompt_version": "research-json-v2", "fallback_used": False})
            return json.dumps({"no_proposal": True, "reason": str(exc)})
        allowed, deny_reason = ai_budget.budget_allows(estimated_tokens=_estimate_tokens(prompt))
        if not allowed:
            rm.record_model_interaction(store, provider=primary["provider"], model=primary["model"],
                purpose=purpose, trigger=trigger, prompt=prompt, response=None,
                status="deferred", cycle_id=cycle_id, error=deny_reason,
                extra={"capability": capability, "prompt_version": "research-json-v2"})
            return json.dumps({"no_proposal": True, "reason": f"AI budget: {deny_reason}"})

        targets = [(primary, False)]
        if fallback and fallback != primary:
            targets.append((fallback, True))
        first_error = None
        for index, (target, is_fallback) in enumerate(targets):
            started = time.monotonic()
            try:
                response = get_provider(target["provider"]).invoke(prompt, model=target["model"])
            except ProviderError as exc:
                latency = round(time.monotonic() - started, 3)
                ai_config.record_provider_result(target["provider"], success=False,
                                                 error=str(exc), latency_seconds=latency)
                may_continue = index == 0 and exc.retryable and len(targets) > 1
                rm.record_model_interaction(store, provider=target["provider"], model=target["model"],
                    purpose=purpose, trigger=trigger, prompt=prompt, response=None, status="error",
                    cycle_id=cycle_id, latency_seconds=latency, error=str(exc),
                    extra={"role": purpose, "capability": capability,
                           "prompt_version": "research-json-v2", "retryable": exc.retryable,
                           "fallback_attempted": may_continue,
                           "fallback_reason": str(exc) if may_continue else None})
                first_error = first_error or exc
                if may_continue:
                    continue
                raise inv.InvestigatorError(str(exc), stage="invocation_error") from exc
            latency = round(time.monotonic() - started, 3)
            ai_config.record_provider_result(target["provider"], success=True,
                                             latency_seconds=latency)
            total = (response.input_tokens or 0) + (response.output_tokens or 0)
            ai_budget.record_usage(tokens=total, expensive=capability in
                                   ("REASONING", "STRONG_REASONING"))
            rm.record_model_interaction(store, provider=target["provider"], model=target["model"],
                purpose=purpose, trigger=trigger, prompt=prompt, response=response.text,
                status="ok", cycle_id=cycle_id, input_tokens=response.input_tokens,
                output_tokens=response.output_tokens, latency_seconds=latency,
                extra={"role": purpose, "capability": capability,
                       "prompt_version": "research-json-v2", "fallback_used": is_fallback,
                       "fallback_reason": str(first_error) if is_fallback else None,
                       "cost": None, "validation": "pending_downstream_schema_validation"})
            return response.text
        raise inv.InvestigatorError("no provider route succeeded", stage="invocation_error")
    return runner


def test_provider(provider: str, model: str) -> dict:
    """Minimal bounded provider proof; never exposes prompt or credential."""
    allowed, reason = ai_budget.budget_allows(estimated_tokens=64)
    if not allowed:
        return {"success": False, "blocked": True, "provider": provider,
                "model": model, "error": f"AI budget: {reason}", "cost": None}
    started = time.monotonic()
    try:
        response = get_provider(provider).invoke(
            '{"task":"return exactly {\\"ok\\":true}"}', model=model)
        latency = round(time.monotonic() - started, 3)
        parsed = json.loads(response.text)
        if parsed != {"ok": True}:
            raise ProviderError("provider test returned an unexpected structured response")
        ai_config.record_provider_result(provider, success=True, latency_seconds=latency)
        ai_budget.record_usage(tokens=(response.input_tokens or 0) +
                               (response.output_tokens or 0), expensive=False)
        return {"success": True, "provider": provider, "model": model,
                "latency_seconds": latency, "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens, "cost": None}
    except (ProviderError, json.JSONDecodeError) as exc:
        latency = round(time.monotonic() - started, 3)
        ai_config.record_provider_result(provider, success=False, error=str(exc),
                                         latency_seconds=latency)
        return {"success": False, "provider": provider, "model": model,
                "latency_seconds": latency, "error": str(exc), "cost": None}
