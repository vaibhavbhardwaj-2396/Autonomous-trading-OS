"""Durable, secret-free AI provider and capability routing configuration.

Research code asks for a capability. This module resolves each research role
to that capability and stores primary/fallback provider-model routes. It never
stores credentials: adapters read them from the worker environment.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import json
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

CONTROL_DIR = Path(__file__).resolve().parent
STATE_PATH = CONTROL_DIR / "ai_config.json"
LOCK_PATH = CONTROL_DIR / ".ai_config.lock"
_IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

KNOWN_PROVIDERS = ("openai", "anthropic", "openai_compatible", "local_openai")
CAPABILITIES = ("ECONOMY", "BALANCED", "REASONING", "STRONG_REASONING",
                "LONG_CONTEXT", "FAST", "STRUCTURED_EXTRACTION")
AI_ROLES = ("classification", "news_extraction", "entity_resolution",
            "observation_summarization", "hypothesis_generation",
            "experiment_design", "evidence_critique", "strategy_review",
            "research_synthesis", "daily_digest")
DEFAULT_ROLE_MAPPINGS = {
    "classification": "ECONOMY", "news_extraction": "ECONOMY",
    "entity_resolution": "ECONOMY", "observation_summarization": "BALANCED",
    "hypothesis_generation": "REASONING", "experiment_design": "REASONING",
    "evidence_critique": "STRONG_REASONING", "strategy_review": "STRONG_REASONING",
    "research_synthesis": "REASONING", "daily_digest": "DETERMINISTIC",
}

PROVIDER_DEFAULTS = {
    "openai": {"enabled": True, "auth_mode": "API_KEY",
               "endpoint": "https://api.openai.com/v1/responses",
               "supported_capabilities": list(CAPABILITIES), "models": []},
    "anthropic": {"enabled": True, "auth_mode": "API_KEY",
                  "endpoint": "https://api.anthropic.com/v1/messages",
                  "supported_capabilities": list(CAPABILITIES), "models": []},
    "openai_compatible": {"enabled": False, "auth_mode": "API_KEY", "endpoint": None,
                          "supported_capabilities": list(CAPABILITIES), "models": []},
    "local_openai": {"enabled": False, "auth_mode": "LOCAL_NO_AUTH", "endpoint": None,
                     "supported_capabilities": list(CAPABILITIES), "models": []},
}


class InvalidAIConfig(ValueError):
    pass


def _now_iso() -> str:
    return dt.datetime.now(_IST).isoformat(timespec="seconds")


def _empty_route() -> dict:
    return {"primary": None, "fallback": None}


def _default_config() -> dict:
    return {
        "schema_version": 2, "provider": None, "model": None,
        "changed_at": None, "actor": None, "reason": None, "history": [],
        "providers": {k: dict(v) for k, v in PROVIDER_DEFAULTS.items()},
        "capability_routes": {c: _empty_route() for c in CAPABILITIES},
        "role_mappings": dict(DEFAULT_ROLE_MAPPINGS),
        "fallback": {"enabled": True, "model": None, "optional_provider": None},
    }


def _normalize_route(value) -> dict:
    route = value if isinstance(value, dict) else {}
    return {"primary": route.get("primary"), "fallback": route.get("fallback")}


def _migrate(data: dict) -> dict:
    base = _default_config()
    for key in ("provider", "model", "changed_at", "actor", "reason", "history"):
        if key in data:
            base[key] = data[key]
    base["role_mappings"].update(data.get("role_mappings") or {})
    for pid, override in (data.get("providers") or {}).items():
        if pid in base["providers"] and isinstance(override, dict):
            base["providers"][pid].update(override)
    for capability, route in (data.get("capability_routes") or {}).items():
        if capability in CAPABILITIES:
            base["capability_routes"][capability] = _normalize_route(route)
    base["fallback"].update(data.get("fallback") or {})

    # Compatibility migration from the old global provider/model schema.
    provider, model = base.get("provider"), base.get("model")
    if provider in KNOWN_PROVIDERS and model:
        for capability in ("ECONOMY", "BALANCED", "REASONING", "STRONG_REASONING"):
            if not base["capability_routes"][capability]["primary"]:
                base["capability_routes"][capability]["primary"] = {
                    "provider": provider, "model": model}
    old_fb_provider = base["fallback"].get("optional_provider")
    old_fb_model = base["fallback"].get("model")
    if (base["fallback"].get("enabled") and old_fb_provider in KNOWN_PROVIDERS
            and old_fb_model and not base["capability_routes"]["REASONING"]["fallback"]):
        base["capability_routes"]["REASONING"]["fallback"] = {
            "provider": old_fb_provider, "model": old_fb_model}
    return base


def get_config(*, path: Path = STATE_PATH) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return _default_config()
    return _migrate(data) if isinstance(data, dict) else _default_config()


def _save(data: dict, *, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True, default=str))
    tmp.replace(path)


def _validate_endpoint(provider: str, endpoint) -> None:
    if endpoint in (None, ""):
        return
    if not isinstance(endpoint, str):
        raise InvalidAIConfig("provider endpoint must be a string or null")
    parsed = urlparse(endpoint)
    if provider in ("openai", "anthropic"):
        if endpoint != PROVIDER_DEFAULTS[provider]["endpoint"]:
            raise InvalidAIConfig(f"the official {provider} endpoint is immutable")
    elif provider == "local_openai":
        if parsed.scheme not in ("http", "https") or parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise InvalidAIConfig("local_openai endpoint must be an explicit loopback HTTP URL")
    elif parsed.scheme != "https" or not parsed.hostname:
        raise InvalidAIConfig("openai_compatible endpoint must be an absolute HTTPS URL")


def _validate_target(target, providers: dict, *, capability: Optional[str] = None,
                     allow_none: bool = True) -> None:
    if target is None and allow_none:
        return
    if not isinstance(target, dict):
        raise InvalidAIConfig("route target must be an object or null")
    provider, model = target.get("provider"), target.get("model")
    if provider not in KNOWN_PROVIDERS or provider not in providers:
        raise InvalidAIConfig(f"unknown provider {provider!r}")
    if not isinstance(model, str) or not model.strip():
        raise InvalidAIConfig("route model must be a non-empty string")
    provider_cfg = providers[provider]
    if not provider_cfg.get("enabled"):
        raise InvalidAIConfig(f"provider {provider!r} is disabled")
    supported = provider_cfg.get("supported_capabilities") or []
    if capability and capability not in supported:
        raise InvalidAIConfig(f"provider {provider!r} does not support {capability}")


def resolve_role(role: str, *, config: Optional[dict] = None) -> tuple[str, dict]:
    cfg = config or get_config()
    capability = (cfg.get("role_mappings") or {}).get(role)
    if capability == "DETERMINISTIC":
        return capability, _empty_route()
    if capability not in CAPABILITIES:
        raise InvalidAIConfig(f"role {role!r} has no valid capability mapping")
    return capability, _normalize_route((cfg.get("capability_routes") or {}).get(capability))


MAX_HISTORY = 50


def set_config(
    *, provider: Optional[str] = None, model: Optional[str] = None, actor: str,
    reason: str, role_mappings: Optional[dict] = None,
    capability_routes: Optional[dict] = None, providers: Optional[dict] = None,
    fallback: Optional[dict] = None, path: Path = STATE_PATH,
    lock_path: Path = LOCK_PATH,
) -> dict:
    if provider is not None and provider not in KNOWN_PROVIDERS:
        raise InvalidAIConfig(f"unknown provider {provider!r}")
    if not actor or not actor.strip() or not reason or not reason.strip():
        raise InvalidAIConfig("actor and reason are required")

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        next_state = get_config(path=path)
        provider_cfg = next_state["providers"]
        for pid, update in (providers or {}).items():
            if pid not in KNOWN_PROVIDERS or not isinstance(update, dict):
                raise InvalidAIConfig(f"invalid provider registry entry {pid!r}")
            allowed = {"enabled", "endpoint", "supported_capabilities", "models"}
            if set(update) - allowed:
                raise InvalidAIConfig(f"unsupported provider fields: {sorted(set(update)-allowed)}")
            if "enabled" in update and not isinstance(update["enabled"], bool):
                raise InvalidAIConfig("provider enabled must be boolean")
            if "endpoint" in update:
                _validate_endpoint(pid, update["endpoint"])
            if "supported_capabilities" in update:
                caps = update["supported_capabilities"]
                if not isinstance(caps, list) or any(c not in CAPABILITIES for c in caps):
                    raise InvalidAIConfig("provider supported_capabilities is invalid")
            if "models" in update:
                models = update["models"]
                if not isinstance(models, list) or any(not isinstance(m, str) or not m.strip() for m in models):
                    raise InvalidAIConfig("provider models must be non-empty strings")
            provider_cfg[pid].update(update)
        mappings = {**next_state["role_mappings"], **(role_mappings or {})}
        if set(mappings) - set(AI_ROLES):
            raise InvalidAIConfig("role_mappings contains an unknown role")
        if any(v not in CAPABILITIES + ("DETERMINISTIC",) for v in mappings.values()):
            raise InvalidAIConfig("role_mappings must name a capability")

        routes = {c: _normalize_route(r) for c, r in next_state["capability_routes"].items()}
        for capability, route in (capability_routes or {}).items():
            if capability not in CAPABILITIES:
                raise InvalidAIConfig(f"unknown capability {capability!r}")
            normalized = _normalize_route(route)
            _validate_target(normalized["primary"], provider_cfg, capability=capability)
            _validate_target(normalized["fallback"], provider_cfg, capability=capability)
            routes[capability] = normalized
        if provider is not None:
            if not model or not model.strip():
                raise InvalidAIConfig("model is required when provider is selected")
            routes["REASONING"]["primary"] = {"provider": provider, "model": model.strip()}

        now = _now_iso()
        history = list(next_state.get("history") or [])
        history.append({"changed_at": now, "actor": actor.strip(), "reason": reason.strip(),
                        "provider": provider, "model": model,
                        "change": "routing_configuration"})
        next_state.update({
            "provider": provider if provider is not None else next_state.get("provider"),
            "model": model if provider is not None else next_state.get("model"),
            "changed_at": now, "actor": actor.strip(), "reason": reason.strip(),
            "history": history[-MAX_HISTORY:], "providers": provider_cfg,
            "capability_routes": routes, "role_mappings": mappings,
        })
        if fallback:
            next_state["fallback"].update(fallback)
        _save(next_state, path=path)
        return next_state


def record_provider_result(provider: str, *, success: bool, error: Optional[str] = None,
                           latency_seconds: Optional[float] = None,
                           path: Path = STATE_PATH, lock_path: Path = LOCK_PATH) -> None:
    """Persist health only; never persists a prompt, response, or credential."""
    if provider not in KNOWN_PROVIDERS:
        return
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        cfg = get_config(path=path)
        row = cfg["providers"][provider]
        row["health"] = "HEALTHY" if success else "ERROR"
        row["last_successful_request"] = _now_iso() if success else row.get("last_successful_request")
        row["last_error"] = None if success else (error or "provider request failed")[:500]
        row["last_latency_seconds"] = latency_seconds
        _save(cfg, path=path)
