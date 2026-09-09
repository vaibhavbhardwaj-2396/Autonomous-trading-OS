"""
tests/test_paper_fills.py — paper/fills.py's deterministic fill model:
same-bar-close, no future data, no fabricated price on missing data.

Run with:  python -m tests.test_paper_fills
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd  # noqa: E402

from tests.paper_fixtures import fixed_history_df  # noqa: E402
from paper.fills import compute_fill  # noqa: E402

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


# ---------------------------------------------------------------------------
print("\n--- normal cases ---")
# ---------------------------------------------------------------------------

df = fixed_history_df([100.0, 101.0, 102.5])
fill = compute_fill(df)
check("compute_fill() returns the MOST RECENT bar's close, not the first or any other",
      fill is not None and fill.price == 102.5)
check("compute_fill() returns that bar's own index/time as bar_time",
      fill.bar_time == df.index[-1])

df_one_row = fixed_history_df([55.5])
fill_one = compute_fill(df_one_row)
check("a single-row history still produces a valid fill (no off-by-one on len==1)",
      fill_one is not None and fill_one.price == 55.5)


# ---------------------------------------------------------------------------
print("\n--- determinism ---")
# ---------------------------------------------------------------------------

df_a = fixed_history_df([100.0, 101.0, 102.0])
df_b = fixed_history_df([100.0, 101.0, 102.0])
fill_a = compute_fill(df_a)
fill_b = compute_fill(df_b)
check("computing a fill twice from identical (but separately constructed) data "
      "gives byte-identical results", fill_a.price == fill_b.price
      and fill_a.bar_time == fill_b.bar_time)


# ---------------------------------------------------------------------------
print("\n--- no future data / no fabricated price ---")
# ---------------------------------------------------------------------------

check("compute_fill(None) returns None, never a fabricated price",
      compute_fill(None) is None)

empty_df = pd.DataFrame({"close": []})
check("compute_fill() on an empty DataFrame returns None",
      compute_fill(empty_df) is None)

nan_df = fixed_history_df([100.0, 101.0])
nan_df.loc[nan_df.index[-1], "close"] = float("nan")
check("compute_fill() refuses to fill on a NaN close (the most recent bar is unusable), "
      "even though earlier bars have valid closes — no silent fallback to a stale price",
      compute_fill(nan_df) is None)

zero_df = fixed_history_df([100.0, 0.0])
check("compute_fill() refuses a non-positive close (0 is nonsense, never treated as a real fill)",
      compute_fill(zero_df) is None)

negative_df = fixed_history_df([100.0, -5.0])
check("compute_fill() refuses a negative close",
      compute_fill(negative_df) is None)


class _NoCloseColumn:
    """Something that isn't a proper history DataFrame at all (e.g. a
    misbehaving/misconfigured HistoryProvider) — compute_fill() must fail
    closed (None), never raise, and never fabricate a price."""

    def __len__(self):
        return 3

    def __getattr__(self, name):
        raise AttributeError(name)


check("compute_fill() on a malformed/unexpected object returns None rather than raising",
      compute_fill(_NoCloseColumn()) is None)


print(f"\n{'=' * 52}")
print(f"  {PASSED} passed, {FAILED} failed")
print(f"{'=' * 52}\n")
sys.exit(1 if FAILED else 0)
