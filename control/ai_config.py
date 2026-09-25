"""
control/ai_config.py — persisted AI provider/model configuration.
INTELLIGENT + EFFICIENT + TRACEABLE (outcome 2).

WHY THIS FILE EXISTS, SEPARATE FROM AN ENV VAR
---------------------------------------------------------------------------
research/brain/llm.py originally read RESEARCH_AI_PROVIDER straight from
os.environ. That works for a human editing the crontab, but not for a UI
control: the dashboard API (api/app.py, a long-running gunicorn process)
and the research worker (research/brain/worker.py, a fresh subprocess
started by cron every ~10 minutes) are TWO DIFFERENT PROCESSES. An
in-memory os.environ change made inside the API process is invisible to
the worker's next cron-launched subprocess, which gets a brand new
environment from the shell, not from the API. A UI "change provider"
action needs somewhere durable, on disk, that both processes read —
exactly the same reasoning control/runtime.py already documents for the
RUNNING/PAUSED/SAFE_MODE/STOPPED state.

Precedence, checked in research.brain.llm.current_provider_name():
  1. RESEARCH_AI_PROVIDER env var, if set — an explicit, ops-level
     override (e.g. a one-off manual run, or an emergency override that
     doesn't require going through the API at all).
  2. This file's persisted `provider`, if set — the UI-driven config.
  3. DEFAULT_PROVIDER ("anthropic_cli") — the safe, always-worked default.

SAFETY — this file can select a research reasoning provider. It cannot:
  - touch live trading, engine/, or memory/state.json (imports nothing
    from any of them, by the same "safety by absence" argument
    control/runtime.py and control/resources.py already make)
  - hold or expose an API key (OPENAI_API_KEY stays in the server
    environment only — see research/brain/llm.py; this file only ever
    stores which provider/model to use, never a credential)
  - change what a provider IS ALLOWED to do — provider output still goes
    through investigator.py's unmodified parse_ai_output() ->
    hypothesis_intake.validate_proposal() firewall no matter which
    provider produced it.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import json
from pathlib import Path
from typing import Optional

CONTROL_DIR = Path(__file__).resolve().parent
STATE_PATH = CONTROL_DIR / "ai_config.json"
LOCK_PATH = CONTROL_DIR / ".ai_config.lock"

_IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

KNOWN_PROVIDERS = ("anthropic_cli", "openai")
AI_ROLES = ("classification", "news_extraction", "observation_summarization",
            "hypothesis_generation", "experiment_design", "evidence_critique",
            "strategy_review", "daily_digest")
DEFAULT_ROLE_MAPPINGS = {
    "classification": "gpt-5.6-luna", "news_extraction": "gpt-5.6-luna",
    "observation_summarization": "gpt-5.6-luna", "hypothesis_generation": "gpt-5.6-terra",
    "experiment_design": "gpt-5.6-terra", "evidence_critique": "gpt-6-astra",
    "strategy_review": "gpt-6-astra", "daily_digest": "deterministic",
}


def _now_iso() -> str:
    return dt.datetime.now(_IST).isoformat(timespec="seconds")


class InvalidAIConfig(ValueError):
    pass


def _default_config() -> dict:
    return {"provider": None, "model": None, "changed_at": None, "actor": None,
            "reason": None, "history": [], "role_mappings": dict(DEFAULT_ROLE_MAPPINGS),
            "fallback": {"enabled": True, "model": "gpt-5.6-luna",
                         "optional_provider": "anthropic_cli"}}


def get_config(*, path: Path = STATE_PATH) -> dict:
    """The persisted config, or an "unset" default (provider=None) if no
    one has ever changed it — in which case
    research.brain.llm.current_provider_name() falls through to
    DEFAULT_PROVIDER. Fails open to the default on any read/parse error."""
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return _default_config()
    if not isinstance(data, dict):
        return _default_config()
    base = _default_config()
    base.update({k: v for k, v in data.items() if k not in ("role_mappings", "fallback")})
    base["role_mappings"].update(data.get("role_mappings") or {})
    base["fallback"].update(data.get("fallback") or {})
    return base


def _save(data: dict, *, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    tmp.replace(path)


MAX_HISTORY = 20


def set_config(
    *, provider: str, model: Optional[str] = None, actor: str, reason: str,
    role_mappings: Optional[dict] = None, fallback: Optional[dict] = None,
    path: Path = STATE_PATH, lock_path: Path = LOCK_PATH,
) -> dict:
    """Persist a new provider/model choice. Validated, flock-protected,
    atomic write — the same shape as control.runtime.set_state(). Does
    NOT verify the provider actually works (no network/subprocess call
    here) — that is exactly what makes switching safe to do from the UI
    without risking a hang; a broken provider fails at the next real call,
    recorded as an "error" artifact the same as any other provider
    failure, never here."""
    if provider not in KNOWN_PROVIDERS:
        raise InvalidAIConfig(f"unknown provider {provider!r} — expected one of {KNOWN_PROVIDERS}")
    if not actor or not actor.strip():
        raise InvalidAIConfig("actor is required")
    if not reason or not reason.strip():
        raise InvalidAIConfig("reason is required")
    mappings = {**get_config(path=path).get("role_mappings", {}), **(role_mappings or {})}
    if set(mappings) - set(AI_ROLES) or any(not isinstance(v, str) or not v.strip() for v in mappings.values()):
        raise InvalidAIConfig("role_mappings contains an unknown role or blank model")
    fallback_cfg = {**get_config(path=path).get("fallback", {}), **(fallback or {})}
    if not isinstance(fallback_cfg.get("enabled"), bool):
        raise InvalidAIConfig("fallback.enabled must be boolean")

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fh = open(lock_path, "w")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        current = get_config(path=path)
        history = list(current.get("history") or [])
        if current.get("provider") is not None:
            history.append({
                "provider": current.get("provider"), "model": current.get("model"),
                "changed_at": current.get("changed_at"), "actor": current.get("actor"),
                "reason": current.get("reason"),
            })
        history = history[-MAX_HISTORY:]
        new_state = {
            "provider": provider, "model": model, "changed_at": _now_iso(),
            "actor": actor, "reason": reason, "history": history,
            "role_mappings": mappings, "fallback": fallback_cfg,
        }
        _save(new_state, path=path)
        return new_state
    finally:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        lock_fh.close()
