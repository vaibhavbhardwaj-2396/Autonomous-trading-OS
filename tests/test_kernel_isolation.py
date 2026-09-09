"""
The one-way dependency invariant.

    research/  MAY import from  engine/
    engine/    MAY NEVER import from  research/

This is the structural guarantee that the trading system cannot acquire a
dependency on the experimental layer. It is enforced here rather than by
convention because the convention will eventually be violated by someone in a
hurry, and the cost of that violation is that the research code becomes
load-bearing for live money without anyone deciding that it should.

The body does not know the brain exists until the brain has earned the right to
influence it. This test is what "does not know" means in practice.

Also checked: the research package must not depend on the parts of the engine
that hold live capital state, so that backfills and replays can run on a laptop
with no state.json, no broker token and no network to a broker.

Run with:  python -m tests.test_kernel_isolation
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

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


IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+([.\w]+)", re.MULTILINE)


def imports_in(path: Path) -> list[str]:
    return IMPORT_RE.findall(path.read_text())


# ---------------------------------------------------------------------------
print("\n--- engine/ must not import research/ ---")
# ---------------------------------------------------------------------------

engine_files = sorted((ROOT / "engine").glob("*.py"))
check("engine/ has files to check", len(engine_files) > 5, f"{len(engine_files)} found")

offenders = []
for f in engine_files:
    for mod in imports_in(f):
        if mod.split(".")[0] == "research":
            offenders.append(f"{f.name} imports {mod}")

check("no engine module imports research", not offenders, "; ".join(offenders))

# A string reference is not an import, but it is a smell worth catching early.
mentions = [f.name for f in engine_files if "research." in f.read_text()]
check("no engine module references a research module by path",
      not mentions, f"mentioned in: {mentions}")


# ---------------------------------------------------------------------------
print("\n--- research/ must not import live-capital modules ---")
# ---------------------------------------------------------------------------
# The store, sources and contracts have to be runnable with no state.json and no
# broker session. If they reach into guardrails or execute, a backfill on a
# laptop starts failing for reasons that have nothing to do with data.

FORBIDDEN = {"guardrails", "execute", "journal", "broker", "broker_kite",
             "broker_indstocks"}

research_files = sorted((ROOT / "research").rglob("*.py"))
check("research/ has files to check", len(research_files) >= 4, f"{len(research_files)}")

bad = []
for f in research_files:
    for mod in imports_in(f):
        parts = mod.lstrip(".").split(".")
        if "engine" in parts:
            tail = parts[-1]
            if tail in FORBIDDEN:
                bad.append(f"{f.relative_to(ROOT)} imports engine.{tail}")

check("research does not import guardrails/execute/journal/broker",
      not bad, "; ".join(bad))


# ---------------------------------------------------------------------------
print("\n--- strategies/ must not import engine/ or research/ ---")
# ---------------------------------------------------------------------------
# Phase 3 Slice X: strategies/ is a third top-level package, sibling to
# engine/ and research/, and it must depend on NEITHER — both a future
# research-side backtest adapter and a future engine-side live adapter are
# meant to depend on strategies/, not the other way around. Same check
# shape as the engine/research invariant above, extended to the new
# package. (tests/test_strategy_foundation.py carries a second, more
# detailed isolation section of its own — this is the minimal addition to
# the project's existing glob-based isolation sweep the slice asked for.)

strategies_root = ROOT / "strategies"
if strategies_root.exists():
    strategies_files = sorted(strategies_root.rglob("*.py"))
    check("strategies/ has files to check", len(strategies_files) > 0,
          f"{len(strategies_files)} found")

    strat_offenders = []
    for f in strategies_files:
        for mod in imports_in(f):
            top = mod.lstrip(".").split(".")[0]
            if top in ("engine", "research"):
                strat_offenders.append(f"{f.relative_to(ROOT)} imports {mod}")

    check("no strategies/ module imports engine or research",
          not strat_offenders, "; ".join(strat_offenders))

    # strategies/core.py's own module docstring — and a couple of plain #
    # comment blocks near boundary-relevant classes, e.g. the one motivating
    # StrategyContext as a Protocol — extensively DISCUSS why the package
    # avoids engine./research. (that discussion is the whole point of the
    # file) — so, unlike the engine/ files above, a raw substring search
    # would flag its own boundary documentation. Docstrings AND # comments
    # are stripped first (the same "prose is not an import" convention used
    # throughout this project's other isolation tests, e.g. tests/test_
    # research_investigator_evidence.py's handling of investigator.py's own
    # boundary comments) so this checks actual code for a stray reference,
    # not commentary.
    def _strat_code_only(text: str) -> str:
        no_docstrings = re.sub(r'"""[\s\S]*?"""', "", text)
        return re.sub(r"#.*", "", no_docstrings)

    strat_code_only = {f: _strat_code_only(f.read_text()) for f in strategies_files}
    strat_mentions = [
        f.name for f, body in strat_code_only.items()
        if "research." in body or "engine." in body
    ]
    check("no strategies/ module references engine./research. by path "
          "outside its own docstrings/comments",
          not strat_mentions, f"mentioned in: {strat_mentions}")
else:
    check("strategies/ package present", False, "strategies/ does not exist yet")


# ---------------------------------------------------------------------------
print("\n--- the research core imports cleanly in isolation ---")
# ---------------------------------------------------------------------------

try:
    from research.store import Store          # noqa: F401
    from research import contracts            # noqa: F401
    check("research.store and research.contracts import with no engine present", True)
except Exception as e:
    check("research.store and research.contracts import with no engine present",
          False, f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
print("\n--- research/ has no write path into the trading system ---")
# ---------------------------------------------------------------------------
# Not a hash check — those files change legitimately when Vaibhav changes a
# limit. This checks that no research module can WRITE anywhere outside its own
# package, which is the property that actually keeps the kernel frozen.
#
# Naming a frozen file in a docstring is fine and expected; opening one is not.
# An earlier version of this test conflated the two and failed on a comment,
# which is worse than useless — a test that cries wolf gets muted.

WRITE_CALL = re.compile(
    r"""(?:open\s*\(\s*|write_text\s*\(|\.write\s*\()""")
FROZEN_NAMES = ("guardrails", "execute.py", "state.json", "run_cycle.sh",
                "trades.jsonl", "settings.json", ".env")

violations = []
for f in research_files:
    for lineno, line in enumerate(f.read_text().splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#") or not WRITE_CALL.search(line):
            continue
        for name in FROZEN_NAMES:
            if name in line:
                violations.append(f"{f.relative_to(ROOT)}:{lineno} writes to {name}")

check("no research module opens a frozen file for writing",
      not violations, "; ".join(violations))

for rel in ("engine/guardrails.py", "engine/execute.py", "memory/state.json",
            "run_cycle.sh"):
    check(f"{rel} still present", (ROOT / rel).exists())

# The research store must live inside research/, not in memory/ alongside the
# agent's live state — so a research bug can never corrupt trading state. Check
# the resolved path, not the source text: an earlier version matched the word
# "Memory" in a docstring and failed for no reason.
from research.store import DEFAULT_DB  # noqa: E402

check("the research store lives inside research/, not beside live trading state",
      DEFAULT_DB.resolve().parent == (ROOT / "research").resolve(),
      str(DEFAULT_DB))


print(f"\n{'=' * 52}")
print(f"  {PASSED} passed, {FAILED} failed")
print(f"{'=' * 52}\n")
sys.exit(1 if FAILED else 0)
