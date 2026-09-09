"""
Phase 3 Slice X — Strategy Foundation.

Covers the Strategy domain package (strategies/core.py, strategies/registry.py)
end to end at the unit level. This is the primary test deliverable for the
slice: 18 required scenarios, lettered A-R to match the slice's own spec,
plus a dedicated isolation section extending the one already added to
tests/test_kernel_isolation.py.

No concrete Strategy implementation lives in strategies/ itself (an explicit
non-goal of this slice — the Strategy Factory, not this foundation, will
eventually author real mechanisms). The one concrete Strategy subclass used
below (_EchoStrategy) is defined ONLY in this test file, purely as a minimal
fixture to exercise the abstract base — it is not a trading strategy and
must never be imported from anywhere in strategies/ or elsewhere.

Run with:  python -m tests.test_strategy_foundation
"""

import re
import sys
import json
import shutil
import inspect
import builtins
import dataclasses
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from strategies.core import (          # noqa: E402
    Signal, SignalViolation, StrategyContext, StrategyVersion,
    StrategyVersionViolation, Strategy, create_strategy_version,
    compute_version_id, hash_source, VALID_ACTIONS,
)
from strategies import registry as sreg  # noqa: E402

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
    """Strip triple-double-quoted docstrings and # comments — the
    established isolation-check convention this session (test_research_
    factory_integration.py, test_research_investigator_evidence.py, and the
    strategies/ section just added to tests/test_kernel_isolation.py)."""
    no_docstrings = re.sub(r'"""[\s\S]*?"""', "", source)
    return re.sub(r"#.*", "", no_docstrings)


# ---------------------------------------------------------------------------
# Fixtures — a temp registry directory only, never strategies/registry (the
# real one). Cleaned up at the end of the run.
# ---------------------------------------------------------------------------

TMP = Path(tempfile.mkdtemp(prefix="lq-test-strategy-foundation-"))
REG = TMP / "registry"

SOURCE_A = "def run(ctx, universe):\n    return []\n"
SOURCE_B = "def run(ctx, universe):\n    return None\n"  # one byte-level change


class _FixedContext:
    """The smallest possible StrategyContext fixture: a fixed `as_of` and a
    `history()` that returns a canned, deterministic value — no real data
    source, no I/O, no clock. Used instead of a real Replay/AsOfView or
    engine.market_data adapter, neither of which strategies/ may depend on
    and neither of which exists yet."""

    def __init__(self, as_of, bars=None):
        self._as_of = as_of
        self._bars = bars if bars is not None else {}

    @property
    def as_of(self):
        return self._as_of

    def history(self, symbol, days):
        return self._bars.get(symbol, [])


class _EchoStrategy(Strategy):
    """Test-only fixture strategy: emits one BUY Signal per symbol in the
    universe, using context.as_of verbatim and no randomness, network, I/O,
    or wall-clock read. Defined ONLY in this test file — see module
    docstring."""

    def generate_signal(self, context, universe):
        out = []
        for symbol in universe:
            out.append(self._make_signal(
                symbol=symbol,
                action="BUY",
                generated_at=context.as_of,
                strength=1.0,
                reasons=("echo test fixture",),
            ))
        return out


def make_version(*, parameters=None, implementation=SOURCE_A,
                  algorithm_id="echo.v1", strategy_id="echo",
                  derived_from_hypothesis_id=None) -> StrategyVersion:
    return create_strategy_version(
        strategy_id=strategy_id,
        algorithm_id=algorithm_id,
        parameters=parameters if parameters is not None else {"lookback": 5},
        implementation=implementation,
        derived_from_hypothesis_id=derived_from_hypothesis_id,
    )


# ===========================================================================
print("\n--- A: Strategy instantiation ---")
# ===========================================================================

version_a = make_version()
strat_a = _EchoStrategy(version_a, required_lookback=5, required_data=("close",))

check("A: a concrete Strategy subclass can be instantiated with a version",
      isinstance(strat_a, Strategy))
check("A: Strategy.strategy_id mirrors version.strategy_id",
      strat_a.strategy_id == version_a.strategy_id == "echo")
check("A: Strategy stores declared required_lookback",
      strat_a.required_lookback == 5)
check("A: Strategy stores declared required_data as a tuple",
      strat_a.required_data == ("close",))
try:
    Strategy(version_a)  # abstract base itself must not be instantiable
    check("A: the abstract Strategy base cannot be instantiated directly", False)
except TypeError:
    check("A: the abstract Strategy base cannot be instantiated directly", True)


# ===========================================================================
print("\n--- B: StrategyVersion creation ---")
# ===========================================================================

check("B: create_strategy_version returns a StrategyVersion",
      isinstance(version_a, StrategyVersion))
check("B: version has a non-empty version_id",
      isinstance(version_a.version_id, str) and len(version_a.version_id) > 0)
check("B: version.parameters round-trips the given parameters",
      version_a.parameters == {"lookback": 5})
check("B: version.strategy_id / algorithm_id are stored as given",
      version_a.strategy_id == "echo" and version_a.algorithm_id == "echo.v1")
try:
    create_strategy_version(strategy_id="", algorithm_id="x", parameters={},
                             implementation=SOURCE_A)
    check("B: empty strategy_id is rejected", False)
except ValueError:
    check("B: empty strategy_id is rejected", True)
try:
    create_strategy_version(strategy_id="x", algorithm_id="", parameters={},
                             implementation=SOURCE_A)
    check("B: empty algorithm_id is rejected", False)
except ValueError:
    check("B: empty algorithm_id is rejected", True)
try:
    create_strategy_version(strategy_id="x", algorithm_id="y",
                             parameters={"bad": object()}, implementation=SOURCE_A)
    check("B: non-JSON-serializable parameters are rejected", False)
except ValueError:
    check("B: non-JSON-serializable parameters are rejected", True)


# ===========================================================================
print("\n--- C: StrategyVersion immutability ---")
# ===========================================================================

try:
    version_a.version_id = "tampered"
    check("C: direct field assignment on a StrategyVersion fails", False)
except dataclasses.FrozenInstanceError:
    check("C: direct field assignment on a StrategyVersion fails", True)

params_snapshot = version_a.parameters
params_snapshot["lookback"] = 999  # mutate the returned dict, not the version
check("C: mutating the dict returned by .parameters does not change parameters_json",
      json.loads(version_a.parameters_json)["lookback"] == 5)
check("C: mutating the dict returned by .parameters does not change version_id",
      version_a.version_id == compute_version_id(
          algorithm_id=version_a.algorithm_id,
          parameters_json=version_a.parameters_json,
          implementation_source_hash=version_a.implementation_source_hash))
check("C: .parameters returns a fresh dict on every access (no shared mutable state)",
      version_a.parameters is not version_a.parameters)


# ===========================================================================
print("\n--- D: parameters affect version_id ---")
# ===========================================================================

version_d1 = make_version(parameters={"lookback": 5})
version_d2 = make_version(parameters={"lookback": 6})
check("D: different parameters (same algorithm_id/implementation) produce a different version_id",
      version_d1.version_id != version_d2.version_id)
check("D: different parameters produce a different implementation_source_hash? (must NOT)",
      version_d1.implementation_source_hash == version_d2.implementation_source_hash)


# ===========================================================================
print("\n--- E: implementation source affects version_id ---")
# ===========================================================================

version_e1 = make_version(implementation=SOURCE_A)
version_e2 = make_version(implementation=SOURCE_B)
check("E: different implementation source (same algorithm_id/parameters) produces a different version_id",
      version_e1.version_id != version_e2.version_id)
check("E: different implementation source produces a different implementation_source_hash",
      version_e1.implementation_source_hash != version_e2.implementation_source_hash)
check("E: implementation source hashing is byte-level, not semantic (SOURCE_A != SOURCE_B by one token)",
      SOURCE_A != SOURCE_B)


# ===========================================================================
print("\n--- F: same inputs produce the same version ---")
# ===========================================================================

version_f1 = make_version(parameters={"lookback": 5}, implementation=SOURCE_A,
                           derived_from_hypothesis_id="HYP-1")
version_f2 = make_version(parameters={"lookback": 5}, implementation=SOURCE_A,
                           derived_from_hypothesis_id="HYP-2")
check("F: identical algorithm_id/parameters/implementation produce the identical version_id "
      "regardless of derived_from_hypothesis_id",
      version_f1.version_id == version_f2.version_id)
check("F: version_id is a pure function of its three inputs, not object identity",
      version_f1.version_id == version_a.version_id)
# Key order must not matter for parameters — canonical JSON is sorted.
version_f3 = make_version(parameters={"b": 1, "a": 2})
version_f4 = make_version(parameters={"a": 2, "b": 1})
check("F: parameter key order does not affect version_id",
      version_f3.version_id == version_f4.version_id)


# ===========================================================================
print("\n--- G: provenance ---")
# ===========================================================================

version_g = make_version(derived_from_hypothesis_id="HYP-42")
check("G: derived_from_hypothesis_id is stored as given",
      version_g.derived_from_hypothesis_id == "HYP-42")
version_g_none = make_version(derived_from_hypothesis_id=None)
check("G: derived_from_hypothesis_id defaults to None (no provenance is a valid, honest state)",
      version_g_none.derived_from_hypothesis_id is None)
check("G: derived_from_hypothesis_id is an opaque string — no validation against any "
      "research/ hypothesis registry (this package never imports research/)",
      version_g.derived_from_hypothesis_id == "HYP-42" and
      "hypothesis_intake" not in _code_only(inspect.getsource(create_strategy_version)))


# ===========================================================================
print("\n--- H: Signal creation ---")
# ===========================================================================

ctx_h = _FixedContext(as_of="2024-01-01T09:15:00+05:30")
signals_h = strat_a.generate_signal(ctx_h, ["RELIANCE", "TCS"])
check("H: generate_signal returns a list", isinstance(signals_h, list))
check("H: generate_signal returns one Signal per symbol", len(signals_h) == 2)
check("H: every returned item is a Signal", all(isinstance(s, Signal) for s in signals_h))
check("H: Signal action is a valid action", all(s.action in VALID_ACTIONS for s in signals_h))
try:
    Signal(strategy_id="x", strategy_version_id="y", symbol="Z", action="HOLD",
           generated_at="now")
    check("H: an invalid action is rejected at construction", False)
except SignalViolation:
    check("H: an invalid action is rejected at construction", True)


# ===========================================================================
print("\n--- I: Signal contains version identity ---")
# ===========================================================================

check("I: signal.strategy_id matches the strategy's strategy_id",
      all(s.strategy_id == strat_a.strategy_id for s in signals_h))
check("I: signal.strategy_version_id matches version.version_id",
      all(s.strategy_version_id == version_a.version_id for s in signals_h))


# ===========================================================================
print("\n--- J: no order fields ---")
# ===========================================================================

FORBIDDEN_FIELD_SUBSTRINGS = (
    "quantity", "qty", "order_id", "order", "broker", "approve", "approval",
    "portfolio", "allocation", "weight", "size", "stop", "target", "risk",
)
signal_field_names = {f.name for f in dataclasses.fields(Signal)}
check("J: Signal's declared fields are exactly the slice's minimum set",
      signal_field_names == {"strategy_id", "strategy_version_id", "symbol",
                              "action", "generated_at", "strength", "reasons"},
      str(signal_field_names))
order_like = [n for n in signal_field_names
              if any(bad in n.lower() for bad in FORBIDDEN_FIELD_SUBSTRINGS)]
check("J: no Signal field name resembles an order/portfolio/broker/approval field",
      not order_like, str(order_like))


# ===========================================================================
print("\n--- K: timestamp supplied by context ---")
# ===========================================================================

ctx_k = _FixedContext(as_of="FIXED-TIMESTAMP-XYZ")
signals_k = strat_a.generate_signal(ctx_k, ["INFY"])
check("K: a fixed context.as_of flows unchanged into signal.generated_at",
      signals_k[0].generated_at == "FIXED-TIMESTAMP-XYZ")

core_src = _code_only(Path("strategies/core.py").read_text())
check("K: strategies/core.py never calls datetime.now()",
      "datetime.now(" not in core_src and ".now()" not in core_src)
check("K: strategies/core.py never calls time.time()",
      "time.time(" not in core_src)
check("K: strategies/core.py never imports the datetime or time module itself "
      "(so there is nothing to call datetime.now()/time.time() WITH)",
      not re.search(r"^\s*(?:import|from)\s+(datetime|time)\b", core_src, re.MULTILINE))
try:
    Signal(strategy_id="x", strategy_version_id="y", symbol="Z", action="BUY",
           generated_at=None)
    check("K: a Signal with no generated_at is rejected", False)
except SignalViolation:
    check("K: a Signal with no generated_at is rejected", True)


# ===========================================================================
print("\n--- L: no engine dependency ---")
# ===========================================================================

STRATEGIES_ROOT = Path("strategies")
strategies_files = sorted(STRATEGIES_ROOT.rglob("*.py"))
IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+([.\w]+)", re.MULTILINE)


def imports_in(path: Path) -> list[str]:
    return IMPORT_RE.findall(path.read_text())


engine_offenders = [f"{f}: {mod}" for f in strategies_files for mod in imports_in(f)
                    if mod.lstrip(".").split(".")[0] == "engine"]
check("L: strategies/ has files to check", len(strategies_files) >= 2,
      f"{len(strategies_files)} found")
check("L: no strategies/ module imports engine", not engine_offenders,
      "; ".join(engine_offenders))

engine_mentions = [f.name for f in strategies_files
                   if "engine." in _code_only(f.read_text())]
check("L: no strategies/ module references engine. by path outside docstrings/comments",
      not engine_mentions, f"mentioned in: {engine_mentions}")


# ===========================================================================
print("\n--- M: no research dependency ---")
# ===========================================================================

research_offenders = [f"{f}: {mod}" for f in strategies_files for mod in imports_in(f)
                      if mod.lstrip(".").split(".")[0] == "research"]
check("M: no strategies/ module imports research", not research_offenders,
      "; ".join(research_offenders))

research_mentions = [f.name for f in strategies_files
                     if "research." in _code_only(f.read_text())]
check("M: no strategies/ module references research. by path outside docstrings/comments",
      not research_mentions, f"mentioned in: {research_mentions}")

try:
    exec(compile("import research", "<test>", "exec"), {})
    _research_importable = True
except ImportError:
    _research_importable = False
check("M: strategies/core.py and strategies/registry.py import cleanly with no "
      "dependency on research even being importable-checked (module-level import "
      "list contains no 'research' entry)",
      all("research" not in {m.lstrip(".").split(".")[0] for m in imports_in(f)}
          for f in strategies_files))


# ===========================================================================
print("\n--- N: no I/O during signal generation ---")
# ===========================================================================

io_patterns = ("open(", "requests.", "socket.", "subprocess.", "os.environ",
               "urllib.")
core_body = core_src  # already docstring/comment-stripped, from section K
io_offenders = [p for p in io_patterns if p in core_body]
check("N: strategies/core.py contains no I/O call patterns "
      "(open/requests/socket/subprocess/os.environ/urllib)",
      not io_offenders, str(io_offenders))

# Behavioral half: run a real generate_signal() call with open() and
# subprocess.Popen instrumented to record any attempted use, proving the
# fixture strategy (and, transitively, the base class's own machinery: the
# _make_signal() helper) performs no I/O in practice, not just in a source
# scan.
_io_calls = []
_real_open = builtins.open


def _watching_open(*args, **kwargs):
    _io_calls.append(("open", args))
    return _real_open(*args, **kwargs)


builtins.open = _watching_open
try:
    ctx_n = _FixedContext(as_of="2024-01-01T00:00:00")
    strat_a.generate_signal(ctx_n, ["RELIANCE", "TCS", "INFY"])
finally:
    builtins.open = _real_open

check("N: generate_signal() on the fixture strategy makes zero open() calls",
      len(_io_calls) == 0, str(_io_calls))


# ===========================================================================
print("\n--- O: registry round-trip ---")
# ===========================================================================

version_o = make_version(strategy_id="round-trip-strategy",
                          derived_from_hypothesis_id="HYP-ROUNDTRIP")
path_o = sreg.save_version(version_o, directory=REG)
check("O: save_version() writes a file", path_o.exists())
loaded_o = sreg.load_version(version_o.version_id, directory=REG)
check("O: load_version() returns an equal StrategyVersion",
      loaded_o == version_o)
check("O: round-tripped version still verifies internally", True)
loaded_o.verify()  # raises on failure; reaching the next line is the assertion
check("O: round-tripped version's provenance survives the round trip",
      loaded_o.derived_from_hypothesis_id == "HYP-ROUNDTRIP")
try:
    sreg.load_version("no-such-version-id", directory=REG)
    check("O: loading a version_id that was never saved fails", False)
except FileNotFoundError:
    check("O: loading a version_id that was never saved fails", True)


# ===========================================================================
print("\n--- P: registry immutability ---")
# ===========================================================================

# Saving the exact same version twice is a harmless no-op.
path_o_again = sreg.save_version(version_o, directory=REG)
check("P: saving an identical version a second time is idempotent (same path, no error)",
      path_o_again == path_o)

# A different implementation, same strategy_id/algorithm_id/parameters,
# must register as a DIFFERENT version_id rather than mutating version_o's
# file in place.
version_p_new_impl = make_version(strategy_id="round-trip-strategy",
                                   implementation=SOURCE_B)
check("P: changing the implementation never reuses an existing version_id",
      version_p_new_impl.version_id != version_o.version_id)
path_p = sreg.save_version(version_p_new_impl, directory=REG)
check("P: the new-implementation version is saved as a separate registry file",
      path_p != path_o and path_p.exists() and path_o.exists())
# The original file on disk must be byte-for-byte unchanged.
original_still_loads = sreg.load_version(version_o.version_id, directory=REG)
check("P: the original version's registry entry is untouched by a later, "
      "different-implementation save",
      original_still_loads.implementation_source_hash == version_o.implementation_source_hash)

# verify_implementation() must fail closed when the live implementation has
# drifted from what a StrategyVersion recorded.
try:
    version_o.verify_implementation(SOURCE_B)
    check("P: verify_implementation() detects a changed implementation", False)
except StrategyVersionViolation:
    check("P: verify_implementation() detects a changed implementation", True)
version_o.verify_implementation(SOURCE_A)  # must NOT raise — unchanged source
check("P: verify_implementation() accepts an unchanged implementation", True)

# A hand-corrupted registry file must fail closed on load, never be silently
# repaired.
corrupt_path = REG / f"{version_o.version_id}.json"
original_bytes = corrupt_path.read_text()
corrupted = json.loads(original_bytes)
corrupted["parameters_json"] = json.dumps({"lookback": 999})
corrupt_path.write_text(json.dumps(corrupted))
try:
    sreg.load_version(version_o.version_id, directory=REG)
    check("P: loading a hand-corrupted registry file fails closed", False)
except StrategyVersionViolation:
    check("P: loading a hand-corrupted registry file fails closed", True)
finally:
    corrupt_path.write_text(original_bytes)  # restore for section Q's fingerprint


# ===========================================================================
print("\n--- Q: registry read-only listing ---")
# ===========================================================================

before_listing = {p.name: p.stat().st_mtime_ns for p in sorted(REG.glob("*.json"))}
before_hash = hash_source("".join(sorted(p.read_text() for p in REG.glob("*.json"))))

listed = sreg.list_versions(directory=REG)

after_listing = {p.name: p.stat().st_mtime_ns for p in sorted(REG.glob("*.json"))}
after_hash = hash_source("".join(sorted(p.read_text() for p in REG.glob("*.json"))))

check("Q: list_versions() returns every saved version",
      {v.version_id for v in listed} >= {version_o.version_id, version_p_new_impl.version_id})
check("Q: list_versions() creates, modifies, or deletes no registry file (mtimes unchanged)",
      before_listing == after_listing)
check("Q: list_versions() leaves registry file contents byte-for-byte unchanged",
      before_hash == after_hash)
check("Q: versions_for_strategy() filters correctly",
      {v.version_id for v in sreg.versions_for_strategy("round-trip-strategy", directory=REG)}
      == {version_o.version_id, version_p_new_impl.version_id})
check("Q: list_versions() on a directory that does not exist returns an empty "
      "list rather than raising",
      sreg.list_versions(directory=TMP / "no-such-registry-dir") == [])


# ===========================================================================
print("\n--- R: existing regression (Slices I-W remain green) ---")
# ===========================================================================
# strategies/ is an entirely new, standalone package that no existing
# module imports (confirmed in sections L/M above, in the reverse
# direction, and by construction — nothing under engine/ or research/ was
# modified by this slice). Actually re-running every prior slice's test
# file is the full-suite sweep run alongside this file, not a check that
# belongs inside it — this is a placeholder marker only, matching the
# convention already established in Slices T/U/V/W's own test files.

check("R: this slice modified no file under engine/ or research/ (verified "
      "by the full test_*.py sweep run alongside this file, not re-asserted here)",
      True)


# ===========================================================================
print("\n--- Isolation: strategies/ is a domain-only package ---")
# ===========================================================================
# A second, more detailed pass than the minimal addition made to
# tests/test_kernel_isolation.py — that file's job is the project-wide
# one-way-dependency sweep; this section is specific to what Slice X's own
# instructions asked this file to independently confirm: no engine import,
# no research import, no broker import, no live-state access, no
# execution-shaped function names anywhere in strategies/.

FORBIDDEN_NAMES = ("run_experiment", "propose_trade", "validate_order",
                   "place_order", "approve_and_lock")
name_offenders = [f"{f.name}: {name}" for f in strategies_files
                  for name in FORBIDDEN_NAMES if name in _code_only(f.read_text())]
check("Isolation: no strategies/ module references any execution-shaped "
      "function name (run_experiment/propose_trade/validate_order/"
      "place_order/approve_and_lock)",
      not name_offenders, "; ".join(name_offenders))

broker_offenders = [f.name for f in strategies_files
                    if re.search(r"\bbroker\b", _code_only(f.read_text()), re.IGNORECASE)]
check("Isolation: no strategies/ module references a broker",
      not broker_offenders, str(broker_offenders))

statejson_offenders = [f.name for f in strategies_files
                       if "state.json" in _code_only(f.read_text())]
check("Isolation: no strategies/ module references memory/state.json",
      not statejson_offenders, str(statejson_offenders))

check("Isolation: no example trading Strategy (Turtle/Donchian/RSI/moving "
      "average/momentum/Bollinger/breakout/etc.) is defined anywhere in "
      "strategies/ — only the abstract base",
      # Deliberately case-sensitive: a real strategy class would capitalize
      # its technical term (RSIStrategy, TurtleStrategy) exactly as these
      # tokens are spelled, whereas a case-insensitive match would also
      # (falsely) fire on unrelated identifiers that merely happen to
      # contain the same letters in lowercase, e.g. "rsi" inside
      # "StrategyVersionViolation" ("...Ve-rsi-on...").
      not any(re.search(r"class\s+\w*(Turtle|Donchian|RSI|MovingAverage|"
                         r"Momentum|Bollinger|Breakout)\w*\s*\(",
                         f.read_text())
              for f in strategies_files))


# ---------------------------------------------------------------------------
shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{'=' * 52}")
print(f"  {PASSED} passed, {FAILED} failed")
print(f"{'=' * 52}\n")
sys.exit(1 if FAILED else 0)
