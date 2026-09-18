"""
api/runtime_bridge.py — the ONE bridge between the new global control
layer (control/runtime.py, a top-level package — not this file) and the
EXISTING, already-tested live-trading kill switch
(engine.journal.set_pause / engine.guardrails' own check of
trading_paused).

Deliberately lives here, not in control/runtime.py, research/, or paper/:
this is the ONLY module in the whole system permitted to import BOTH
control.runtime AND engine.journal. api/ is already the established
integration layer that spans engine + research + paper — api/data.py has
imported engine.guardrails/engine.journal/research.* together since
Slice Z. research.brain.worker and paper.runner import ONLY
control.runtime (never engine.*, never this module) — see those modules'
own isolation sections for why that boundary matters and stays intact.

ONE-WAY RATCHET — the whole safety argument in one sentence: this module
can PAUSE live trading automatically (when the global mode enters
SAFE_MODE or STOPPED), but it can NEVER resume it automatically. Resuming
live trading stays exactly what it always was — a deliberate human action
via the EXISTING Telegram `resume` command (scripts/telegram_inbox.py) —
so leaving SAFE_MODE from the global control layer never silently re-arms
live trading, even if a human had ALSO separately, deliberately paused it
for their own reason. RUNNING and PAUSED never touch live trading's own
pause state at all, in either direction — this module only ever ADDS a
pause, never removes one.

This module never calls engine.execute, never imports engine.guardrails,
never touches a broker, and never places or modifies an order — the ONLY
engine surface it reaches is the pre-existing pause flag every other
human-facing control (Telegram) already uses.
"""

from __future__ import annotations

from control import runtime as ctrl
from engine import journal as jr

SAFE_MODE_PAUSE_TAG = "[global control]"


def apply_mode_change(mode: str, *, reason: str, actor: str) -> dict:
    """Change the global mode and, ONLY if the new mode requires it,
    engage the EXISTING live-trading pause (see module docstring for why
    this is a one-way ratchet — RUNNING/PAUSED never touch live trading's
    pause state; only SAFE_MODE/STOPPED ever set it, and only to True).
    Raises control.runtime.InvalidRuntimeMode for a bad mode/reason/actor,
    exactly as control.runtime.set_state() does — this function adds no
    new validation of its own, it composes the existing one.
    """
    new_state = ctrl.set_state(mode, reason=reason, actor=actor)
    if mode in ("SAFE_MODE", "STOPPED"):
        jr.set_pause(True, reason=f"{SAFE_MODE_PAUSE_TAG} mode={mode}: {reason}")
    return new_state


def get_full_status() -> dict:
    """Read-only combined view for the dashboard: the control layer's own
    state, plus a read-only peek at whether live trading is currently
    paused and why. Never writes anything. A failure to read live state
    degrades to `None` fields rather than raising — observability must
    never be able to crash the caller."""
    state = ctrl.get_state()
    live_paused: object = None
    live_pause_reason: object = None
    try:
        live_state = jr.load_state()
        live_paused = bool(live_state.get("trading_paused"))
        live_pause_reason = live_state.get("pause_reason")
    except Exception:  # noqa: BLE001
        pass
    return {
        "mode": state["mode"],
        "reason": state.get("reason"),
        "actor": state.get("actor"),
        "changed_at": state.get("changed_at"),
        "research_allowed": ctrl.research_allowed(state),
        "paper_allowed": ctrl.paper_allowed(state),
        "data_ingestion_allowed": ctrl.data_ingestion_allowed(state),
        "live_execution_recommended": ctrl.live_execution_recommended(state),
        "live_trading_paused": live_paused,
        "live_trading_pause_reason": live_pause_reason,
        "live_paused_by_global_control": bool(live_pause_reason)
                                        and SAFE_MODE_PAUSE_TAG in str(live_pause_reason),
        "history": state.get("history", []),
    }
