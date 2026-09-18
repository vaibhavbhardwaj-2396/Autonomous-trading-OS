"""
api/ai_status.py — read/write adapters for the AI provider configuration
and budget. INTELLIGENT + EFFICIENT + TRACEABLE (outcome 2), Part C4/G.

    get_ai_status()   -> control.ai_config.get_config() + control.
                          ai_budget.get_budget_state()/AIBudgetLimits,
                          combined into one dashboard-ready view.
    set_ai_provider()  -> control.ai_config.set_config() — the ONE write
                          this module performs. Never calls a provider,
                          never touches research state, never verifies
                          the provider actually works (see
                          control/ai_config.py's own docstring on why
                          that is deliberate and safe).

This is the second write-capable surface this API has ever had — the
first was /control/mode (Priority Phase 4). Same posture: an explicit,
narrow, named exception to the "every route is GET-only" mechanical
check in tests/test_deployment_readiness.py and tests/test_broker_truth.py,
not a loosening of it.
"""

from __future__ import annotations

from control import ai_budget, ai_config
from research.store import iso, now_ist


def get_ai_status() -> dict:
    cfg = ai_config.get_config()
    budget_state = ai_budget.get_budget_state()
    limits = ai_budget.AIBudgetLimits.from_env()
    from research.brain import llm  # local import: avoid api/ importing
    # research.brain at module load time for every route, only when asked
    effective_provider = llm.current_provider_name()
    effective_model = llm.current_model_name(effective_provider)
    return {
        "effective_provider": effective_provider,
        "effective_model": effective_model,
        "configured_provider": cfg.get("provider"),
        "configured_model": cfg.get("model"),
        "config_changed_at": cfg.get("changed_at"),
        "config_changed_by": cfg.get("actor"),
        "config_reason": cfg.get("reason"),
        "config_history": cfg.get("history") or [],
        "known_providers": list(llm.KNOWN_PROVIDERS),
        "budget": {
            "calls_today": budget_state.get("calls_today"),
            "expensive_calls_today": budget_state.get("expensive_calls_today"),
            "tokens_today": budget_state.get("tokens_today"),
            "max_tokens_per_cycle": limits.max_tokens_per_cycle,
            "max_tokens_per_day": limits.max_tokens_per_day,
            "max_expensive_calls_per_day": limits.max_expensive_calls_per_day,
            "last_call_at": budget_state.get("last_call_at"),
        },
        "as_of": iso(now_ist()),
    }


def set_ai_provider(*, provider: str, model, actor: str, reason: str) -> dict:
    """Raises control.ai_config.InvalidAIConfig for a bad provider/blank
    actor/blank reason — deliberately NOT caught here. api/app.py's route
    validates provider/actor/reason BEFORE calling this (the same
    "validate in the route, 400 on bad input" pattern /control/mode
    already uses), so this should not raise in practice via the API;
    left unconverted rather than silently mapped to the wrong HTTP status
    if that assumption is ever wrong."""
    return ai_config.set_config(provider=provider, model=model, actor=actor, reason=reason)
