"""
control/ai_budget.py — the second governor: "can the AI budget afford
this," alongside control/resources.py's "can the infrastructure afford
this." INTELLIGENT + EFFICIENT + TRACEABLE (outcome 2), built the same way
outcome 1 built control/resources.py: a small, dependency-free, testable
module in the neutral control/ package, imported by whichever research
code needs to ask the question — never the other way around.

WHY A SEPARATE MODULE FROM control/resources.py
---------------------------------------------------------------------------
Two independently-exhaustible resources, not one: a machine can be
HEALTHY (plenty of CPU/RAM/disk) while the AI budget is exhausted for the
day, and a machine can be under real resource PRESSURE while the AI
budget still has room. Collapsing them into one state would hide exactly
the distinction the UI needs to explain "why didn't it call the AI" —
"resource pressure" and "budget exhausted" are different, actionable
answers.

WHAT THIS DOES NOT DO
---------------------------------------------------------------------------
It does not cap calls PER CYCLE — that is already
research.brain.worker.WorkerLimits.max_discovery_attempts's job (a
discovery attempt is the only thing that can trigger an LLM call, and
that cap already exists and is already tested). Duplicating it here would
be two sources of truth for the same limit. This module owns the
CROSS-CYCLE, DAILY budget (tokens/day, expensive-calls/day) and a single
per-call size ceiling (max_tokens_per_cycle, guarding against one
pathological digest, not against too many cycles) — the part nothing
else in the codebase tracks.

THE STATE IS DELIBERATELY GLOBAL AND SINGULAR
---------------------------------------------------------------------------
Same reasoning as control/runtime.py's own STATE_PATH: one VPS, one
research AI, one daily budget. `path`/`lock_path` overrides exist purely
for test isolation; every production call site uses the defaults.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

CONTROL_DIR = Path(__file__).resolve().parent
STATE_PATH = CONTROL_DIR / "ai_budget_state.json"
LOCK_PATH = CONTROL_DIR / ".ai_budget.lock"

_IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def _now() -> dt.datetime:
    return dt.datetime.now(_IST)


def _today_str() -> str:
    return _now().date().isoformat()


def _now_iso() -> str:
    return _now().isoformat(timespec="seconds")


def _default_state() -> dict:
    return {"date": _today_str(), "calls_today": 0, "expensive_calls_today": 0,
            "tokens_today": 0, "last_call_at": None, "last_reset_at": _now_iso()}


def get_budget_state(*, path: Path = STATE_PATH) -> dict:
    """Today's usage — auto-resets to a fresh, zeroed state the first time
    this is read on a new IST calendar day. Fails open to a fresh state on
    any read/parse error, same posture as control.runtime.get_state()."""
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return _default_state()
    if not isinstance(data, dict) or data.get("date") != _today_str():
        return _default_state()
    return data


def _save_state(data: dict, *, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    tmp.replace(path)


def record_usage(
    *, tokens: int = 0, expensive: bool = True,
    path: Path = STATE_PATH, lock_path: Path = LOCK_PATH,
) -> dict:
    """Atomically add one call's usage to today's running total. `tokens`
    should be the call's total_tokens (input+output) if known, else 0 —
    never fabricated; a provider that can't report tokens (the Claude CLI
    runner) simply contributes 0 to the token count while still
    incrementing calls_today, which is honest about what is and isn't
    measured (see research/brain/llm.py)."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fh = open(lock_path, "w")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        state = get_budget_state(path=path)
        state["calls_today"] = int(state.get("calls_today") or 0) + 1
        if expensive:
            state["expensive_calls_today"] = int(state.get("expensive_calls_today") or 0) + 1
        state["tokens_today"] = int(state.get("tokens_today") or 0) + max(0, tokens)
        state["last_call_at"] = _now_iso()
        _save_state(state, path=path)
        return state
    finally:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        lock_fh.close()


@dataclass(frozen=True)
class AIBudgetLimits:
    max_tokens_per_cycle: int = 30_000
    max_tokens_per_day: int = 300_000
    max_expensive_calls_per_day: int = 48  # ~1 every 30 min across a market day

    ENV = {
        "max_tokens_per_cycle": "AI_BUDGET_MAX_TOKENS_PER_CYCLE",
        "max_tokens_per_day": "AI_BUDGET_MAX_TOKENS_PER_DAY",
        "max_expensive_calls_per_day": "AI_BUDGET_MAX_EXPENSIVE_CALLS_PER_DAY",
    }

    @classmethod
    def from_env(cls) -> "AIBudgetLimits":
        base = cls()
        values: dict = {}
        for field_name, env_name in cls.ENV.items():
            raw = (os.environ.get(env_name) or "").strip()
            if not raw:
                values[field_name] = getattr(base, field_name)
                continue
            try:
                values[field_name] = int(raw)
            except ValueError:
                values[field_name] = getattr(base, field_name)
        return cls(**values)


DEFAULT_LIMITS = AIBudgetLimits()


def budget_allows(
    *, estimated_tokens: int = 0, limits: AIBudgetLimits = DEFAULT_LIMITS,
    state: Optional[dict] = None,
) -> tuple:
    """(allowed: bool, reason: Optional[str]) — reason is None iff allowed
    is True. Checked BEFORE a call is made, using an ESTIMATE (a caller
    that can't estimate tokens in advance, e.g. by len(prompt)//4, may
    pass 0 and rely solely on the daily/per-call-count checks)."""
    state = state if state is not None else get_budget_state()
    if estimated_tokens > limits.max_tokens_per_cycle:
        return False, (f"estimated {estimated_tokens} tokens exceeds "
                       f"max_tokens_per_cycle ({limits.max_tokens_per_cycle})")
    tokens_today = int(state.get("tokens_today") or 0)
    if tokens_today + estimated_tokens > limits.max_tokens_per_day:
        return False, (f"today's usage ({tokens_today} tokens) plus this call "
                       f"({estimated_tokens} tokens) would exceed "
                       f"max_tokens_per_day ({limits.max_tokens_per_day})")
    expensive_today = int(state.get("expensive_calls_today") or 0)
    if expensive_today >= limits.max_expensive_calls_per_day:
        return False, (f"already made {expensive_today} expensive AI call(s) "
                       f"today, at the max_expensive_calls_per_day limit "
                       f"({limits.max_expensive_calls_per_day})")
    return True, None


def main(argv: Optional[list] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Living Quant AI budget — today's usage")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    state = get_budget_state()
    limits = AIBudgetLimits.from_env()
    if args.json:
        print(json.dumps({"state": state, "limits": limits.__dict__}, indent=2, default=str))
        return 0
    print(f"AI budget — {state['date']}")
    print(f"  calls today          : {state['calls_today']}")
    print(f"  expensive calls today: {state['expensive_calls_today']} / "
          f"{limits.max_expensive_calls_per_day}")
    print(f"  tokens today         : {state['tokens_today']} / {limits.max_tokens_per_day}")
    print(f"  last call at         : {state.get('last_call_at')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
