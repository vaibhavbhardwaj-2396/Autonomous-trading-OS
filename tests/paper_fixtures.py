"""
tests/paper_fixtures.py — shared, deterministic fixtures for the AA
(paper/shadow engine) test suite. NOT itself a test module (no test_
prefix, so `python -m tests.test_X` never picks it up as one) — imported by
tests/test_paper_*.py the same way tests/test_api.py builds its own
temporary fixtures rather than touching real memory/research/strategies
directories.

Every fixture here is fully isolated: temp directories for the paper DB,
the eligibility log, and the strategies registry, and a fixed IST clock —
nothing in this module ever reads or writes memory/state.json,
research/market_memory.db, or strategies/registry/.
"""

from __future__ import annotations

import datetime as dt
import shutil
import tempfile
from pathlib import Path
from typing import Optional

import pandas as pd

from strategies.core import Signal, Strategy, create_strategy_version
from strategies import registry as sreg

from paper import algorithms as palgo
from paper import eligibility as pelig
from paper.clock import FixedClock
from paper.store import PaperStore

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
FIXED_NOW = dt.datetime(2026, 1, 5, 16, 0, 0, tzinfo=IST)


class BuyEveryTimeStrategy(Strategy):
    """The simplest possible deterministic Strategy: BUYs every symbol in
    its universe, every call, using only context.as_of — no randomness, no
    wall-clock read, no I/O. Used by every test in this suite that needs
    *a* Strategy without caring about its internal logic."""

    def generate_signal(self, context, universe):
        return [
            self._make_signal(symbol=sym, action="BUY", generated_at=context.as_of,
                              reasons=("fixture: always buy",))
            for sym in universe
        ]


class SellEveryTimeStrategy(Strategy):
    def generate_signal(self, context, universe):
        return [
            self._make_signal(symbol=sym, action="SELL", generated_at=context.as_of,
                              reasons=("fixture: always sell",))
            for sym in universe
        ]


class RaisesStrategy(Strategy):
    """Simulates a Strategy with a bug — generate_signal() always raises.
    Used to prove one broken Strategy cannot take down a whole paper
    cycle (AA spec section 20)."""

    def generate_signal(self, context, universe):
        raise RuntimeError("fixture: this strategy is broken on purpose")


class OutsideUniverseStrategy(Strategy):
    """Signals a symbol that is NOT in the universe it was handed —
    exercises the structural-validation reject path."""

    def generate_signal(self, context, universe):
        return [self._make_signal(symbol="NOT-IN-UNIVERSE", action="BUY",
                                  generated_at=context.as_of)]


def fixed_history_df(closes: list[float], start="2026-01-01") -> pd.DataFrame:
    """A tiny, fully deterministic daily-bar DataFrame in exactly the shape
    engine.market_data.get_history() returns (open/high/low/close/volume,
    indexed by date) — no network, no yfinance, ever, in this test suite."""
    idx = pd.date_range(start=start, periods=len(closes), freq="D")
    return pd.DataFrame({
        "open": closes, "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes], "close": closes,
        "volume": [10_000] * len(closes),
    }, index=idx)


class PaperTestEnv:
    """One fully-isolated environment: temp paper DB + temp eligibility dir
    + temp strategies registry + a fixed clock. `cleanup()` removes every
    temp path; use as a context manager to guarantee that even on a failed
    assertion."""

    def __init__(self, *, initial_capital: Optional[float] = None,
                max_lots_per_position: Optional[int] = None,
                max_concurrent_positions: Optional[int] = None,
                position_notional: Optional[float] = None):
        self.tmp = Path(tempfile.mkdtemp(prefix="paper_test_"))
        self.db_path = self.tmp / "paper_shadow_test.db"
        self.eligibility_dir = self.tmp / "eligibility"
        self.registry_dir = self.tmp / "strategies_registry"
        self.registry_dir.mkdir(parents=True, exist_ok=True)

        import os
        self._env_overrides = {"PAPER_DB_PATH": str(self.db_path)}
        if initial_capital is not None:
            self._env_overrides["PAPER_INITIAL_CAPITAL"] = str(initial_capital)
        if max_lots_per_position is not None:
            self._env_overrides["PAPER_MAX_LOTS_PER_POSITION"] = str(max_lots_per_position)
        if max_concurrent_positions is not None:
            self._env_overrides["PAPER_MAX_CONCURRENT_POSITIONS"] = str(max_concurrent_positions)
        if position_notional is not None:
            self._env_overrides["PAPER_POSITION_NOTIONAL"] = str(position_notional)
        self._prior_env = {}
        for k, v in self._env_overrides.items():
            self._prior_env[k] = os.environ.get(k)
            os.environ[k] = v

        self.clock = FixedClock(FIXED_NOW)
        self.store = PaperStore.open(self.db_path)

    def register_and_approve(self, *, strategy_id: str, algorithm_id: str,
                             strategy_cls: type, parameters: Optional[dict] = None,
                             actor: str = "test-fixture") -> str:
        palgo.register_algorithm(algorithm_id, strategy_cls)
        version = create_strategy_version(
            strategy_id=strategy_id, algorithm_id=algorithm_id,
            parameters=parameters or {}, implementation=strategy_cls,
        )
        sreg.save_version(version, directory=self.registry_dir)
        pelig.mark_paper_eligible(
            version_id=version.version_id, strategy_id=strategy_id, actor=actor,
            reason="test fixture", now=FIXED_NOW.isoformat(),
            directory=self.eligibility_dir, registry_dir=self.registry_dir,
        )
        return version.version_id

    def register_only(self, *, strategy_id: str, algorithm_id: str,
                      strategy_cls: type, parameters: Optional[dict] = None) -> str:
        """Registered in strategies/registry, but never marked paper-eligible
        — used to prove eligibility gating actually excludes it."""
        palgo.register_algorithm(algorithm_id, strategy_cls)
        version = create_strategy_version(
            strategy_id=strategy_id, algorithm_id=algorithm_id,
            parameters=parameters or {}, implementation=strategy_cls,
        )
        sreg.save_version(version, directory=self.registry_dir)
        return version.version_id

    def close(self) -> None:
        import os
        try:
            self.store.close()
        except Exception:
            pass
        for k, prior in self._prior_env.items():
            if prior is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prior
        shutil.rmtree(self.tmp, ignore_errors=True)

    def __enter__(self) -> "PaperTestEnv":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
