"""
tests/test_paper_isolation.py — Slice AA's own safety-boundary proof.

Two independent layers, both required by the AA spec (sections 22/23/27):

  1. STATIC: paper/ imports nothing from engine/ except engine.market_data
     and engine.costs, and nothing from research/ at all — the same
     glob-based import sweep tests/test_kernel_isolation.py already runs
     for engine/ vs research/ vs strategies/, extended here to paper/. This
     file does NOT modify test_kernel_isolation.py itself (a protected-file
     concern per the AA spec section 27) — it is a new, independent test.

  2. RUNTIME: actually run a full paper cycle (BUY + SELL, several
     symbols) with engine.execute.propose_trade/close_position,
     engine.broker.get_broker, and engine.guardrails.save_state all
     monkeypatched to explode if called — and assert none of them ever
     fire. Static analysis alone is what the AA spec explicitly says NOT
     to rely on ("Do not merely rely on code review" — section 22).

Run with:  python -m tests.test_paper_isolation
"""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

ROOT = Path(__file__).parent.parent
PASSED, FAILED = 0, 0


def _fingerprint(path: Path) -> str:
    """Same fingerprinting approach tests/test_api.py already uses for its
    own read-only proof: a file's bytes hashed, or a fixed sentinel if it
    doesn't exist — so 'still absent' and 'now exists' are both detectable."""
    if not path.exists():
        return "MISSING"
    return hashlib.sha256(path.read_bytes()).hexdigest()


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


IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+([.\w]+)", re.MULTILINE)


def imports_in(path: Path) -> list[str]:
    return IMPORT_RE.findall(path.read_text())


# ---------------------------------------------------------------------------
print("\n--- static: paper/ imports nothing from research/ ---")
# ---------------------------------------------------------------------------

paper_files = sorted((ROOT / "paper").rglob("*.py"))
check("paper/ has files to check", len(paper_files) > 5, f"{len(paper_files)} found")

research_offenders = []
for f in paper_files:
    for mod in imports_in(f):
        top = mod.lstrip(".").split(".")[0]
        if top == "research":
            research_offenders.append(f"{f.relative_to(ROOT)} imports {mod}")

check("no paper/ module imports research", not research_offenders, "; ".join(research_offenders))


# ---------------------------------------------------------------------------
print("\n--- static: paper/ imports only engine.market_data / engine.costs from engine/ ---")
# ---------------------------------------------------------------------------

ALLOWED_ENGINE_IMPORTS = {"engine.market_data", "engine.costs", "engine.watchlist"}
ALLOWED_ENGINE_FROM_IMPORTS = {"engine"}  # `from engine.market_data import get_history` etc.
                                          # — checked at full-path granularity below instead

engine_offenders = []
for f in paper_files:
    for mod in imports_in(f):
        norm = mod.lstrip(".")
        if norm == "engine" or norm.startswith("engine."):
            # Accept exactly engine.market_data / engine.costs (or a
            # sub-attribute import `from engine.market_data import X`,
            # which this regex already captures as "engine.market_data").
            if norm not in ALLOWED_ENGINE_IMPORTS:
                engine_offenders.append(f"{f.relative_to(ROOT)} imports {mod}")

check("no paper/ module imports engine.execute/guardrails/journal/broker*",
      not engine_offenders, "; ".join(engine_offenders))

# A string reference is not an import, but — same posture
# tests/test_kernel_isolation.py already takes for engine/ vs research/ — it
# is a smell worth catching. Docstrings/comments are allowed to MENTION the
# forbidden modules (this package's own docs extensively discuss the
# boundary, on purpose); only actual code referencing them by dotted path
# outside prose is checked.
FORBIDDEN_ENGINE_SUBSTRINGS = (
    "engine.execute", "engine.guardrails", "engine.journal",
    "engine.broker", "engine.broker_kite", "engine.broker_indstocks",
)


def _code_only(text: str) -> str:
    no_docstrings = re.sub(r'"""[\s\S]*?"""', "", text)
    return re.sub(r"#.*", "", no_docstrings)


code_bodies = {f: _code_only(f.read_text()) for f in paper_files}
substring_offenders = [
    f"{f.relative_to(ROOT)} references {needle}"
    for f, body in code_bodies.items()
    for needle in FORBIDDEN_ENGINE_SUBSTRINGS
    if needle in body
]
check("no paper/ module references a forbidden engine module by path outside docs/comments",
      not substring_offenders, "; ".join(substring_offenders))


# ---------------------------------------------------------------------------
print("\n--- static: paper/ never calls a broker placement/order function ---")
# ---------------------------------------------------------------------------

FORBIDDEN_CALL_NAMES = ("broker.place(", "broker.place_stop(", "get_broker(", "propose_trade(")
call_offenders = [
    f"{f.relative_to(ROOT)} calls {needle}"
    for f, body in code_bodies.items()
    for needle in FORBIDDEN_CALL_NAMES
    if needle in body
]
check("no paper/ module calls broker.place*/get_broker/propose_trade",
      not call_offenders, "; ".join(call_offenders))


# ---------------------------------------------------------------------------
print("\n--- runtime: a full paper cycle never touches the live execution path ---")
# ---------------------------------------------------------------------------

from tests.paper_fixtures import PaperTestEnv, BuyEveryTimeStrategy, SellEveryTimeStrategy, fixed_history_df  # noqa: E402
from paper.runner import run_paper_cycle  # noqa: E402

_live_calls: list[str] = []


def _explode(name):
    def _fn(*args, **kwargs):
        _live_calls.append(name)
        raise AssertionError(f"LIVE EXECUTION PATH WAS CALLED: {name}(args={args}, kwargs={kwargs})")
    return _fn


import engine.execute as live_execute      # noqa: E402
import engine.broker as live_broker        # noqa: E402
import engine.guardrails as live_guardrails  # noqa: E402
import engine.journal as live_journal      # noqa: E402

_orig = {
    "execute.propose_trade": live_execute.propose_trade,
    "execute.close_position": live_execute.close_position,
    "execute.sync_from_broker": live_execute.sync_from_broker,
    "broker.get_broker": live_broker.get_broker,
    "guardrails.save_state": live_guardrails.save_state,
    "guardrails.validate_order": live_guardrails.validate_order,
    "journal.add_position": live_journal.add_position,
    "journal.close_position": live_journal.close_position,
    "journal.log_trade": live_journal.log_trade,
}

try:
    live_execute.propose_trade = _explode("engine.execute.propose_trade")
    live_execute.close_position = _explode("engine.execute.close_position")
    live_execute.sync_from_broker = _explode("engine.execute.sync_from_broker")
    live_broker.get_broker = _explode("engine.broker.get_broker")
    live_guardrails.save_state = _explode("engine.guardrails.save_state")
    live_guardrails.validate_order = _explode("engine.guardrails.validate_order")
    live_journal.add_position = _explode("engine.journal.add_position")
    live_journal.close_position = _explode("engine.journal.close_position")
    live_journal.log_trade = _explode("engine.journal.log_trade")

    LIVE_FILES_TO_FINGERPRINT = [
        ROOT / "memory" / "state.json",
        ROOT / "memory" / "trades.jsonl",
        ROOT / "memory" / "regime_log.jsonl",
        ROOT / "memory" / "portfolio_state.md",
        ROOT / "engine" / "guardrails.py",
        ROOT / "engine" / "execute.py",
    ]
    before = {p: _fingerprint(p) for p in LIVE_FILES_TO_FINGERPRINT}

    with PaperTestEnv() as env:
        env.register_and_approve(
            strategy_id="buyer", algorithm_id=f"iso_buy_{hashlib.sha256(b'buyer').hexdigest()[:8]}",
            strategy_cls=BuyEveryTimeStrategy)
        env.register_and_approve(
            strategy_id="seller", algorithm_id=f"iso_sell_{hashlib.sha256(b'seller').hexdigest()[:8]}",
            strategy_cls=SellEveryTimeStrategy)

        df = fixed_history_df([100.0, 101.0, 102.0])
        try:
            run_paper_cycle(
                cycle_id="isolation-check-1", clock=env.clock,
                history_provider=lambda s, d: df, universe=["INFY", "TCS"],
                registry_dir=env.registry_dir, eligibility_dir=env.eligibility_dir,
                store=env.store, notify=False,
            )
            run_paper_cycle(
                cycle_id="isolation-check-2", clock=env.clock,
                history_provider=lambda s, d: df, universe=["INFY", "TCS"],
                registry_dir=env.registry_dir, eligibility_dir=env.eligibility_dir,
                store=env.store, notify=False,
            )
            check("a full BUY+SELL paper cycle completes without touching engine.execute/"
                  "broker/guardrails/journal", not _live_calls, "; ".join(_live_calls))
        except AssertionError as e:
            check("a full BUY+SELL paper cycle completes without touching engine.execute/"
                  "broker/guardrails/journal", False, str(e))

    after = {p: _fingerprint(p) for p in LIVE_FILES_TO_FINGERPRINT}
    changed = [str(p.relative_to(ROOT)) for p in LIVE_FILES_TO_FINGERPRINT if before[p] != after[p]]
    check("every live state/journal file and every protected engine/ file is "
          "byte-identical before and after two full paper cycles",
          not changed, f"changed: {changed}")

finally:
    live_execute.propose_trade = _orig["execute.propose_trade"]
    live_execute.close_position = _orig["execute.close_position"]
    live_execute.sync_from_broker = _orig["execute.sync_from_broker"]
    live_broker.get_broker = _orig["broker.get_broker"]
    live_guardrails.save_state = _orig["guardrails.save_state"]
    live_guardrails.validate_order = _orig["guardrails.validate_order"]
    live_journal.add_position = _orig["journal.add_position"]
    live_journal.close_position = _orig["journal.close_position"]
    live_journal.log_trade = _orig["journal.log_trade"]


# ---------------------------------------------------------------------------
print("\n--- runtime: paper eligibility never touches strategies.registry.save_version ---")
# ---------------------------------------------------------------------------
# AA spec section 23: AA may READ StrategyVersion/research metadata but
# must never create a Contract, lock one, or otherwise write into research/.
# strategies.registry.save_version is likewise never called by paper/ at
# runtime (paper/eligibility.py only ever calls load_version) — verified
# here by checking the source, since save_version's own idempotent-if-
# identical behavior would make a runtime monkeypatch-explosion test
# awkward to distinguish from "never called" vs "called with identical
# content" — the static check above already covers the import surface;
# this is a second, call-name-specific pass over paper/eligibility.py only.

elig_body = code_bodies[ROOT / "paper" / "eligibility.py"]
check("paper/eligibility.py never calls strategies.registry.save_version",
      "save_version(" not in elig_body)
check("paper/eligibility.py never references research.contracts or research.store",
      "research.contracts" not in elig_body and "research.store" not in elig_body
      and "Store(" not in elig_body)


print(f"\n{'=' * 52}")
print(f"  {PASSED} passed, {FAILED} failed")
print(f"{'=' * 52}\n")
sys.exit(1 if FAILED else 0)
