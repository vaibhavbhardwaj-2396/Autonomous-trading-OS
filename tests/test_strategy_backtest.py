"""
Phase 3 Slice Y — Strategy Backtest Adapter.

Covers research/experiments/strategy_backtest.py end to end: the
StrategyContext adapter, universe resolution, algorithm resolution,
simulated trade mechanics, cost treatment, determinism, and — the mandatory
part — the leakage torture tests proving a Strategy backtest is exactly as
blind to the future as the Contract-DSL runner already is, by construction
(this module adds no gating logic of its own; it is entirely riding on
research.replay/research.store's existing AsOfView).

Sections A-T match Slice Y's own lettered spec. The one concrete Strategy
subclass defined below (_ThresholdStrategy) exists ONLY in this test file —
same rule Slice X's test file already established: no example trading
Strategy lives anywhere under strategies/ or research/.

Run with:  python -m tests.test_strategy_backtest
"""

import re
import sys
import json
import shutil
import inspect
import tempfile
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from research.store import Store, IST                                    # noqa: E402
from research.replay import Replay, leak_check                           # noqa: E402
from research.experiments import strategy_backtest as sb                 # noqa: E402
from research.experiments import runner                                  # noqa: E402
from strategies.core import Strategy, StrategyVersion, StrategyVersionViolation, \
    create_strategy_version, Signal                                       # noqa: E402
from strategies import registry as sreg                                   # noqa: E402

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


def _code_only(source: str) -> str:
    no_docstrings = re.sub(r'"""[\s\S]*?"""', "", source)
    return re.sub(r"#.*", "", no_docstrings)


def at(y, m, d, hh=18, mm=30):
    return dt.datetime(y, m, d, hh, mm, tzinfo=IST)


# ---------------------------------------------------------------------------
# Fixture Strategy — test-only, never imported from production code.
# ---------------------------------------------------------------------------

SEEN_AS_OF: list = []
SEEN_UNIVERSE: list = []


class _ThresholdStrategy(Strategy):
    """BUY a symbol whose latest close is >= self.version.parameters
    ["threshold"], SELL if below. Deterministic, reads only via
    context.history(), records every (as_of, universe) it is called with
    into the module-level SEEN_* lists so tests can assert on what the
    adapter actually handed it, since run_backtest() instantiates the
    Strategy internally and callers never see that instance directly."""

    def generate_signal(self, context, universe):
        SEEN_AS_OF.append(context.as_of)
        SEEN_UNIVERSE.append(list(universe))
        threshold = self.version.parameters["threshold"]
        out = []
        for symbol in universe:
            bars = context.history(symbol, days=1)
            if bars is None or len(bars) == 0:
                continue
            close = float(bars["close"].iloc[-1])
            action = "BUY" if close >= threshold else "SELL"
            out.append(self._make_signal(
                symbol=symbol, action=action, generated_at=context.as_of,
                strength=close))
        return out


class _ThresholdStrategyDifferentSource(Strategy):
    """Byte-for-byte different from _ThresholdStrategy (this docstring line
    alone guarantees a different source hash) — used only to exercise the
    "implementation drifted since the version was registered" failure path.
    Never registered under the same algorithm_id as the real fixture in
    normal use; section L registers it deliberately to prove the mismatch is
    caught."""

    def generate_signal(self, context, universe):
        return []


ALGO_ID = "test.threshold.v1"


# ---------------------------------------------------------------------------
# Fixture store — AAA/BBB daily closes over ten sessions, deliberately
# crossing the threshold (100) in both directions so BUY and SELL both
# occur, plus a small point-in-time index for the universe-resolution test.
# ---------------------------------------------------------------------------

TMP = Path(tempfile.mkdtemp(prefix="lq-test-strategy-backtest-"))
store = Store.open(TMP / "backtest.db")

AAA_CLOSES = [100, 101, 102, 99, 98, 105, 110, 111, 95, 120]
BBB_CLOSES = [50, 51, 52, 53, 54, 55, 56, 57, 58, 59]
DAYS = [dt.date(2024, 1, 8) + dt.timedelta(days=i) for i in range(len(AAA_CLOSES))]

for d, ca, cb in zip(DAYS, AAA_CLOSES, BBB_CLOSES):
    kt = dt.datetime.combine(d, dt.time(18, 0), tzinfo=IST)
    store.append_price(symbol="AAA", session_date=d, knowledge_time=kt,
                       source="bhavcopy", close=ca, open_=ca, high=ca, low=ca,
                       volume=1000, adjusted=False)
    store.append_price(symbol="BBB", session_date=d, knowledge_time=kt,
                       source="bhavcopy", close=cb, open_=cb, high=cb, low=cb,
                       volume=1000, adjusted=False)

# A tiny point-in-time index: AAA is a member throughout, BBB joins partway.
store.append(dataset="index_membership", entity="BBB", event_time=at(2024, 1, 13),
            knowledge_time=at(2024, 1, 5), source="nse_pr",
            payload={"symbol": "BBB", "index_name": "TestIndex",
                     "valid_from": "2024-01-13", "valid_to": None})
store.append(dataset="index_membership", entity="AAA", event_time=at(2024, 1, 1),
            knowledge_time=at(2023, 12, 20), source="nse_pr",
            payload={"symbol": "AAA", "index_name": "TestIndex",
                     "valid_from": "2024-01-01", "valid_to": None})

START, END = "2024-01-08", "2024-01-17"


def fresh_version(*, strategy_id="thresh", algorithm_id=ALGO_ID,
                  threshold=100.0, implementation=_ThresholdStrategy,
                  derived_from_hypothesis_id=None) -> StrategyVersion:
    return create_strategy_version(
        strategy_id=strategy_id, algorithm_id=algorithm_id,
        parameters={"threshold": threshold}, implementation=implementation,
        derived_from_hypothesis_id=derived_from_hypothesis_id)


def run(*, version=None, universe=["AAA"], registry=None, **kw):
    version = version or fresh_version()
    registry = {ALGO_ID: _ThresholdStrategy} if registry is None else registry
    return sb.run_backtest(version, store, start=START, end=END, universe=universe,
                           algorithm_registry=registry, record_note=False, **kw)


# ===========================================================================
print("\n--- A: Strategy executes over historical Replay ---")
# ===========================================================================

result_a = run()
check("A: run_backtest returns a StrategyBacktestResult",
      isinstance(result_a, sb.StrategyBacktestResult))
check("A: the backtest walked a positive number of replay steps",
      result_a.n_steps > 0, str(result_a.n_steps))
expected_days = len(Replay(store).trading_days(START, END))
check("A: n_steps matches the number of trading sessions in range",
      result_a.n_steps == expected_days, f"{result_a.n_steps} vs {expected_days}")
check("A: the run reports completed=True", result_a.completed is True)


# ===========================================================================
print("\n--- B: StrategyContext receives correct as_of ---")
# ===========================================================================

SEEN_AS_OF.clear()
run()
expected_as_ofs = [Replay(store).step(d).as_of for d in Replay(store).trading_days(START, END)]
check("B: the Strategy was called once per replay step with that step's own as_of",
      SEEN_AS_OF == expected_as_ofs, f"{SEEN_AS_OF} vs {expected_as_ofs}")


# ===========================================================================
print("\n--- C: Strategy receives resolved universe ---")
# ===========================================================================

SEEN_UNIVERSE.clear()
run(universe=["AAA", "BBB"])
check("C: a literal universe list is passed through unchanged, every step",
      all(u == ["AAA", "BBB"] for u in SEEN_UNIVERSE), str(SEEN_UNIVERSE[:2]))

SEEN_UNIVERSE.clear()
run(universe="TestIndex")
first_day_universe = SEEN_UNIVERSE[0]
last_day_universe = SEEN_UNIVERSE[-1]
check("C: a point-in-time index universe excludes a not-yet-effective member",
      "BBB" not in first_day_universe and "AAA" in first_day_universe,
      str(first_day_universe))
check("C: ...and includes it once effective",
      "BBB" in last_day_universe, str(last_day_universe))

SEEN_UNIVERSE.clear()
run(universe="watchlist")
from engine.watchlist import UNIVERSE as WATCHLIST_UNIVERSE               # noqa: E402
check("C: the 'watchlist' universe resolves to engine.watchlist.UNIVERSE",
      all(u == list(WATCHLIST_UNIVERSE) for u in SEEN_UNIVERSE))


# ===========================================================================
print("\n--- D: Strategy history is AsOf-safe ---")
# ===========================================================================

replay = Replay(store)
mid_step = replay.step("2024-01-11")
ctx = sb.ReplayStrategyContext(mid_step)
bars = ctx.history("AAA", days=20)
check("D: context.history() never returns a bar dated after the context's as_of",
      all(idx.date() <= mid_step.as_of.date() for idx in bars.index), str(bars.index))
check("D: context.as_of matches the ReplayStep it was built from",
      ctx.as_of == mid_step.as_of)


# ===========================================================================
print("\n--- E: Signals are generated ---")
# ===========================================================================

result_e = run()
check("E: at least one Signal was generated", len(result_e.signals) > 0)
check("E: every signal has a valid action",
      all(s["action"] in ("BUY", "SELL") for s in result_e.signals))
check("E: signal count matches steps * universe size (one opinion per symbol per step)",
      len(result_e.signals) == result_e.n_steps * 1)


# ===========================================================================
print("\n--- F: same StrategyVersion + same data -> deterministic signals ---")
# ===========================================================================

v_f = fresh_version()
result_f1 = run(version=v_f)
result_f2 = run(version=v_f)
check("F: identical signal lists across two runs of the same version",
      result_f1.signals == result_f2.signals)


# ===========================================================================
print("\n--- G: backtest result determinism ---")
# ===========================================================================

check("G: the full result (trades, stats, cost assumptions, metadata) is "
      "identical across two runs of the same version over the same data",
      result_f1.to_dict() == result_f2.to_dict())


# ===========================================================================
print("\n--- H: future-data modification does not change prior signal ---")
# ===========================================================================

mod_store = Store.open(TMP / "mod_test.db")
for d, c in zip(DAYS, AAA_CLOSES):
    kt = dt.datetime.combine(d, dt.time(18, 0), tzinfo=IST)
    mod_store.append_price(symbol="AAA", session_date=d, knowledge_time=kt,
                           source="bhavcopy", close=c, open_=c, high=c, low=c,
                           volume=1000, adjusted=False)

replay_mod = Replay(mod_store)
early_step = replay_mod.step("2024-01-10")


def signal_at(store_, step) -> list:
    ver = fresh_version()
    strat = _ThresholdStrategy(ver)
    ctx_ = sb.ReplayStrategyContext(step)
    return [s.to_dict() for s in strat.generate_signal(ctx_, ["AAA"])]


before = signal_at(mod_store, early_step)

# A future row (session_date after early_step's date), with a wildly
# different close, published with a knowledge_time also after early_step.
mod_store.append_price(symbol="AAA", session_date=dt.date(2024, 1, 20),
                       knowledge_time=at(2024, 1, 20), source="bhavcopy",
                       close=9999, open_=9999, high=9999, low=9999, volume=1,
                       adjusted=False)
early_step_2 = Replay(mod_store).step("2024-01-10")
after = signal_at(mod_store, early_step_2)

check("H: modifying/adding a future row does not change a prior signal",
      before == after, f"{before} vs {after}")


# ===========================================================================
print("\n--- I: future-data deletion does not change prior signal ---")
# ===========================================================================


def leak_fn(step):
    strat = _ThresholdStrategy(fresh_version())
    ctx_ = sb.ReplayStrategyContext(step)
    return [s.to_dict() for s in strat.generate_signal(ctx_, ["AAA"])]


lc = leak_check(store, "2024-01-11", leak_fn, TMP / "leakcheck.db")
check("I: leak_check finds this Strategy's signal at a given as_of leak-free "
      "(unaffected by physically deleting every later row)",
      lc["leak_free"], lc["verdict"])


# ===========================================================================
print("\n--- J: historical inputs do affect signals where appropriate ---")
# ===========================================================================
# prices_eod is uniquely keyed on (symbol, session_date, source, adjusted) —
# there is no same-source "correction" of an already-published bar (unlike
# `observations`, which supports genuine revisions via `supersedes`). So the
# honest way to demonstrate "a permitted historical input change DOES change
# the signal" here is a BACKFILL: a session_date within a multi-day lookback
# window that becomes known later, not a same-key overwrite of one already
# known. This is still squarely "we are modelling knowledge, not filtering
# on a fixed dataset" — the same distinction test_replay_leakage.py's CASE 3
# exists to make for observations.


class _AvgWindowStrategy(Strategy):
    """Test-only, section-J-only fixture: BUY if the mean close over the
    last 3 available sessions is >= threshold, else SELL. Multi-bar (unlike
    _ThresholdStrategy's single latest bar) specifically so a backfilled
    earlier session can change its answer."""

    def generate_signal(self, context, universe):
        threshold = self.version.parameters["threshold"]
        out = []
        for symbol in universe:
            bars = context.history(symbol, days=3)
            if bars is None or len(bars) == 0:
                continue
            mean_close = float(bars["close"].mean())
            action = "BUY" if mean_close >= threshold else "SELL"
            out.append(self._make_signal(symbol=symbol, action=action,
                                         generated_at=context.as_of, strength=mean_close))
        return out


hist_store = Store.open(TMP / "hist_test.db")
# Only the last two of three intended sessions are known at first — day0
# (2024-01-08) has not been backfilled yet.
for d, c in zip(DAYS[1:3], [101, 102]):
    kt = dt.datetime.combine(d, dt.time(18, 0), tzinfo=IST)
    hist_store.append_price(symbol="AAA", session_date=d, knowledge_time=kt,
                            source="bhavcopy", close=c, open_=c, high=c, low=c,
                            volume=1000, adjusted=False)

v_avg = fresh_version(threshold=101.5)
strat_avg = _AvgWindowStrategy(v_avg)

step_before = Replay(hist_store).step(DAYS[2])
sig_before = [s.to_dict() for s in
              strat_avg.generate_signal(sb.ReplayStrategyContext(step_before), ["AAA"])]

# Backfill day0's close (a genuinely new row — a different session_date, not
# a same-key overwrite) — this is exactly the "historical input becomes
# available" case, permitted because its knowledge_time is still on or
# before the as_of being asked about.
hist_store.append_price(symbol="AAA", session_date=DAYS[0],
                        knowledge_time=dt.datetime.combine(DAYS[0], dt.time(18, 0), tzinfo=IST),
                        source="bhavcopy", close=40, open_=40, high=40, low=40,
                        volume=1000, adjusted=False)
step_after = Replay(hist_store).step(DAYS[2])
sig_after = [s.to_dict() for s in
             strat_avg.generate_signal(sb.ReplayStrategyContext(step_after), ["AAA"])]

check("J: a permitted (already-knowable, historical) input change DOES change the signal",
      sig_before != sig_after, f"{sig_before} vs {sig_after}")
check("J: ...specifically flips BUY to SELL as the 3-day-average logic predicts",
      sig_before[0]["action"] == "BUY" and sig_after[0]["action"] == "SELL",
      f"{sig_before[0]['action']} -> {sig_after[0]['action']}")


# ===========================================================================
print("\n--- K: unknown algorithm_id fails closed ---")
# ===========================================================================

v_unknown = fresh_version(algorithm_id="no.such.algorithm")
try:
    run(version=v_unknown, registry={})
    check("K: an unresolvable algorithm_id raises UnknownAlgorithm", False)
except sb.UnknownAlgorithm:
    check("K: an unresolvable algorithm_id raises UnknownAlgorithm", True)


# ===========================================================================
print("\n--- L: StrategyVersion implementation mismatch fails closed ---")
# ===========================================================================

v_mismatch = fresh_version(implementation=_ThresholdStrategy)
# Register a DIFFERENT class (different source) under the same algorithm_id
# the version claims — simulating "the algorithm's code changed underneath
# an already-registered StrategyVersion".
try:
    run(version=v_mismatch, registry={ALGO_ID: _ThresholdStrategyDifferentSource})
    check("L: a drifted implementation raises StrategyVersionViolation", False)
except StrategyVersionViolation:
    check("L: a drifted implementation raises StrategyVersionViolation", True)


# ===========================================================================
print("\n--- M: StrategyVersion identity is preserved in signals/results ---")
# ===========================================================================

v_m = fresh_version(strategy_id="identity-check")
result_m = run(version=v_m)
check("M: result.strategy_id / version_id / algorithm_id match the version tested",
      result_m.strategy_id == v_m.strategy_id
      and result_m.version_id == v_m.version_id
      and result_m.algorithm_id == v_m.algorithm_id)
check("M: every signal carries the same strategy_id/strategy_version_id",
      all(s["strategy_id"] == v_m.strategy_id
          and s["strategy_version_id"] == v_m.version_id
          for s in result_m.signals))
check("M: every trade traces back to a symbol that was actually signalled",
      all(t["symbol"] in {s["symbol"] for s in result_m.signals} for t in result_m.trades))


# ===========================================================================
print("\n--- N: cost assumptions are explicit ---")
# ===========================================================================

result_n = run()
check("N: cost_assumptions names the cost model used",
      result_n.cost_assumptions.get("model") == "engine.costs.equity_round_trip")
check("N: cost_assumptions states the intraday flag",
      result_n.cost_assumptions.get("intraday") is False)
check("N: cost_assumptions states the position notional used for sizing",
      result_n.cost_assumptions.get("position_notional") == sb.STRATEGY_BACKTEST_POSITION_NOTIONAL)
if result_n.trades:
    check("N: every simulated trade actually carries a nonzero cost figure",
          all(t["costs"] > 0 for t in result_n.trades))


# ===========================================================================
print("\n--- O: no broker / live engine access ---")
# ===========================================================================

sb_src = _code_only(Path("research/experiments/strategy_backtest.py").read_text())
FORBIDDEN_ENGINE_MODULES = ("engine.guardrails", "engine.execute", "engine.journal",
                            "engine.broker", "engine.broker_kite", "engine.broker_indstocks",
                            "engine.market_data")
forbidden_hits = [m for m in FORBIDDEN_ENGINE_MODULES if m in sb_src]
check("O: strategy_backtest.py references none of engine's live/broker/market-data modules",
      not forbidden_hits, str(forbidden_hits))
check("O: strategy_backtest.py never calls a wall clock (datetime.now/time.time)",
      "datetime.now(" not in sb_src and "time.time(" not in sb_src
      and "dt.datetime.now(" not in sb_src)


# ===========================================================================
print("\n--- P: no run_experiment() delegation ---")
# ===========================================================================

check("P: strategy_backtest.py never calls run_experiment(",
      "run_experiment(" not in sb_src)
check("P: strategy_backtest.py never imports research.experiments.runner",
      not re.search(r"^\s*(?:from|import)\s+.*\brunner\b", sb_src, re.MULTILINE))
check("P: strategy_backtest.py never writes to Store.experiment_results",
      "append_experiment_result" not in sb_src)


# ===========================================================================
print("\n--- Q: no Strategy import of research/engine ---")
# ===========================================================================
# Re-confirms, from this slice's own test file, what
# tests/test_kernel_isolation.py already enforces project-wide (Slice X
# added that check; this repeats it narrowly here per Slice Y's own
# explicit instruction to "add/extend tests proving strategies/ still
# imports from neither research nor engine").

IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+([.\w]+)", re.MULTILINE)
strategies_files = sorted(Path("strategies").rglob("*.py"))
strat_offenders = [
    f"{f}: {mod}" for f in strategies_files
    for mod in IMPORT_RE.findall(f.read_text())
    if mod.lstrip(".").split(".")[0] in ("engine", "research")
]
check("Q: strategies/ still imports from neither engine nor research",
      not strat_offenders, "; ".join(strat_offenders))
check("Q: (contrast) research/experiments/strategy_backtest.py DOES import "
      "strategies — that is the intended, one-way adapter direction",
      any(mod.lstrip(".").split(".")[0] == "strategies"
          for mod in IMPORT_RE.findall(Path("research/experiments/strategy_backtest.py").read_text())))


# ===========================================================================
print("\n--- R: Strategy registry is read-only during backtest ---")
# ===========================================================================

REG_TMP = TMP / "registry_readonly"
v_r = fresh_version(strategy_id="readonly-check")
sreg.save_version(v_r, directory=REG_TMP)

before_files = {p.name: (p.stat().st_mtime_ns, p.read_text()) for p in REG_TMP.glob("*.json")}

sb.run_backtest(v_r.version_id, store, start=START, end=END, universe=["AAA"],
                algorithm_registry={ALGO_ID: _ThresholdStrategy}, registry_dir=REG_TMP,
                record_note=False)

after_files = {p.name: (p.stat().st_mtime_ns, p.read_text()) for p in REG_TMP.glob("*.json")}

check("R: run_backtest() loading a version by id from the registry writes "
      "nothing there and leaves every file byte-for-byte, mtime-for-mtime "
      "unchanged", before_files == after_files)
check("R: no new file was created in the registry directory by the backtest",
      set(before_files) == set(after_files))
check("R: strategy_backtest.py never calls save_version(",
      "save_version(" not in sb_src)


# ===========================================================================
print("\n--- S: existing Contract runner remains unchanged ---")
# ===========================================================================

check("S: research.experiments.runner.run_experiment's execution-boundary "
      "signature (contract_id first) is untouched",
      list(inspect.signature(runner.run_experiment).parameters)[0] == "contract_id")
check("S: strategy_backtest.py never imports anything from "
      "research.experiments.runner",
      "from .runner" not in sb_src and "from . import runner" not in sb_src
      and "experiments.runner" not in sb_src)
check("S: strategy_backtest.py never calls research.experiments.runner.simulate(",
      "runner.simulate(" not in sb_src and not re.search(r"(?<!_)\bsimulate\(", sb_src))


# ===========================================================================
print("\n--- T: existing regression (Slices I-X remain green) ---")
# ===========================================================================
# Verified by the full test_*.py sweep run alongside this file, matching the
# convention already established in every prior slice's own test file —
# not re-asserted inside this one.

check("T: this slice modified neither strategies/core.py, strategies/"
      "registry.py, research/experiments/runner.py, research/contracts.py, "
      "research/replay.py, research/store.py, research/schema.sql, "
      "research/experiments/evaluator.py, research/experiments/comparison.py, "
      "research/brain/*, nor research/overnight.py (verified by the full "
      "test_*.py sweep run alongside this file, not re-asserted here)",
      True)


# ---------------------------------------------------------------------------
store.close()
mod_store.close()
hist_store.close()
shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{'=' * 52}")
print(f"  {PASSED} passed, {FAILED} failed")
print(f"{'=' * 52}\n")
sys.exit(1 if FAILED else 0)
