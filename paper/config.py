"""
paper/config.py — environment-driven configuration for the paper engine.

Same convention api/config.py already uses: every value is read from the
process environment at call time (never cached at import time, so tests can
set/unset env vars per-case), nothing sensitive is hardcoded, and every
default is documented here rather than buried in call sites.

These are SIMULATION constraints for the paper account only — they are not,
and must never become, a second copy of engine/guardrails.py's live risk
limits. See docs/PAPER_TRADING.md's "Paper vs live risk limits" section.
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent

# ---------------------------------------------------------------------------
# Paper capital. Deliberately NOT derived from memory/state.json's
# allocated_capital — that would visually and semantically couple a
# simulation number to the live mandate. A safe, clearly-synthetic default
# is used instead and must be set explicitly in production via env var if a
# different figure is wanted.
# ---------------------------------------------------------------------------

ENV_INITIAL_CAPITAL = "PAPER_INITIAL_CAPITAL"
DEFAULT_INITIAL_CAPITAL = 100_000.0
"""₹1,00,000 — a round, obviously-synthetic research figure. Chosen so it
can never be mistaken for CLAUDE.md's live examples (₹10,000 allocated
capital) or any real account balance. Documented in docs/PAPER_TRADING.md."""

# Per-lot paper position size — mirrors research/experiments/
# strategy_backtest.py's STRATEGY_BACKTEST_POSITION_NOTIONAL convention (a
# fixed notional per opened lot, so results are comparable across symbols),
# but kept independent so a change to one can never silently move the other.
ENV_POSITION_NOTIONAL = "PAPER_POSITION_NOTIONAL"
DEFAULT_POSITION_NOTIONAL = 10_000.0

# ---------------------------------------------------------------------------
# Simulation-only risk limits (see module docstring) — these stop a
# misbehaving Strategy from producing a nonsensical paper book, nothing more.
# ---------------------------------------------------------------------------

ENV_MAX_CONCURRENT_POSITIONS = "PAPER_MAX_CONCURRENT_POSITIONS"
DEFAULT_MAX_CONCURRENT_POSITIONS = 10

ENV_MAX_LOTS_PER_POSITION = "PAPER_MAX_LOTS_PER_POSITION"
DEFAULT_MAX_LOTS_PER_POSITION = 5
"""Caps how many times a single (strategy_version_id, symbol) may be
pyramided via consecutive BUY signals before further BUYs are rejected —
see paper/portfolio.py."""

ENV_MIN_CASH_BUFFER = "PAPER_MIN_CASH_BUFFER"
DEFAULT_MIN_CASH_BUFFER = 0.0
"""A BUY that would take paper cash below this is rejected (sized down is
NOT attempted in v1 — see docs/PAPER_TRADING.md's known limitations)."""

# ---------------------------------------------------------------------------
# Storage location. Deliberately NOT named state.json or anything that could
# be confused with memory/state.json (LIVE) or research/market_memory.db
# (RESEARCH) — see paper/__init__.py and docs/PAPER_TRADING.md's "LIVE /
# PAPER / RESEARCH" section.
# ---------------------------------------------------------------------------

ENV_DB_PATH = "PAPER_DB_PATH"
DEFAULT_DB_PATH = PROJECT_ROOT / "paper" / "paper_shadow.db"

ENV_ELIGIBILITY_DIR = "PAPER_ELIGIBILITY_DIR"
DEFAULT_ELIGIBILITY_DIR = PROJECT_ROOT / "paper" / "eligibility"

# Universe passed to research.experiments.strategy_backtest-style resolution
# — reused here only as a string convention, not an import (paper/context.py
# resolves it independently; see that module for why).
ENV_UNIVERSE = "PAPER_UNIVERSE"
DEFAULT_UNIVERSE = "watchlist"

# History lookback (in days) handed to a Strategy's context.history() call
# when the runner does not know a Strategy's own required_lookback — see
# paper/context.py.
ENV_DEFAULT_LOOKBACK_DAYS = "PAPER_DEFAULT_LOOKBACK_DAYS"
DEFAULT_LOOKBACK_DAYS = 260


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def initial_capital() -> float:
    return _float_env(ENV_INITIAL_CAPITAL, DEFAULT_INITIAL_CAPITAL)


def position_notional() -> float:
    return _float_env(ENV_POSITION_NOTIONAL, DEFAULT_POSITION_NOTIONAL)


def max_concurrent_positions() -> int:
    return _int_env(ENV_MAX_CONCURRENT_POSITIONS, DEFAULT_MAX_CONCURRENT_POSITIONS)


def max_lots_per_position() -> int:
    return _int_env(ENV_MAX_LOTS_PER_POSITION, DEFAULT_MAX_LOTS_PER_POSITION)


def min_cash_buffer() -> float:
    return _float_env(ENV_MIN_CASH_BUFFER, DEFAULT_MIN_CASH_BUFFER)


def db_path() -> Path:
    raw = os.environ.get(ENV_DB_PATH, "").strip()
    return Path(raw) if raw else DEFAULT_DB_PATH


def eligibility_dir() -> Path:
    raw = os.environ.get(ENV_ELIGIBILITY_DIR, "").strip()
    return Path(raw) if raw else DEFAULT_ELIGIBILITY_DIR


def universe() -> str:
    return os.environ.get(ENV_UNIVERSE, "").strip() or DEFAULT_UNIVERSE


def default_lookback_days() -> int:
    return _int_env(ENV_DEFAULT_LOOKBACK_DAYS, DEFAULT_LOOKBACK_DAYS)
