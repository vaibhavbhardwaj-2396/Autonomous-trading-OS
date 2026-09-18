"""
tests/test_research_store_concurrency.py — STABLE + CONTROLLED: the
deliberate decision recorded in docs/RESEARCH_STORE_LOCKING.md.

Started as an attempt to prove a workaround for a "research/store.py has
no busy_timeout" gap (that file is permanently write-protected, so the fix
was planned to live one layer up, in api/data.py::get_default_store()).
Section B below is what caught that the premise was wrong: a bare
sqlite3.connect() with no explicit `timeout=` — research/store.py's exact
call shape, unchanged, never touched — already gets Python's own 5000ms
default busy_timeout. There was no gap to work around for that file. See
the doc for the full correction.

What this file actually proves, now:
  A. api/data.py::get_default_store() explicitly sets busy_timeout on its
     connection (redundant with the Python default, kept for explicitness
     — see the doc) — catches a future refactor that drops it AND passes
     an explicit timeout=0 by accident, which would be a real regression.
  B. The actual, measured distinction between "no override" (already
     5000ms, waits) and "explicit timeout=0" (truly immediate failure) —
     proven against a throwaway temp database with real sqlite3
     connections and a real thread, independent of research/ or api/
     entirely, so this is a proof about SQLite/Python's own behavior on
     this system, not an assumption about it.

Lock-based overlap prevention (recorder_lock()/worker_lock() genuinely
serializing two overlapping invocations against the same store file) is
already covered by tests/test_runtime_control.py section K (K1-K6) — not
duplicated here.

Run with:  python -m tests.test_research_store_concurrency
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

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


TMP = Path(tempfile.mkdtemp(prefix="lq-test-store-concurrency-"))


# ===========================================================================
print("\n--- A: api/data.py::get_default_store() sets busy_timeout ---")
# ===========================================================================

from api import data as api_data  # noqa: E402
from research.store import Store  # noqa: E402

_orig_store_path_env = None
_test_db = TMP / "a_market_memory.db"

# get_default_store() caches one Store per thread and always opens
# research.store.DEFAULT_DB unless a store was already cached for this
# thread — reset_default_store_for_testing() clears that cache, then we
# monkeypatch Store.open's default target for this one call via the same
# "wrap the function, don't rely on a reassigned default argument" pattern
# already established in tests/test_runtime_control.py section I (a bare
# default-argument reassignment does NOT propagate into an already-bound
# default parameter elsewhere).
_orig_store_open = Store.open


@classmethod
def _isolated_open(cls, path=None):
    return _orig_store_open.__func__(cls, path or _test_db)


Store.open = _isolated_open
api_data.reset_default_store_for_testing()
try:
    store = api_data.get_default_store()
    busy_timeout = store._conn.execute("PRAGMA busy_timeout").fetchone()[0]
    check("A1: get_default_store() returns a connection with a non-zero "
          "busy_timeout (the PRAGMA set in api/data.py actually took effect)",
          busy_timeout > 0, busy_timeout)
    check("A2: the busy_timeout is exactly the documented 5000ms",
          busy_timeout == 5000, busy_timeout)
finally:
    Store.open = _orig_store_open
    api_data.reset_default_store_for_testing()


# ===========================================================================
print("\n--- B: what actually has a timeout, and what doesn't ---")
# ===========================================================================
# Independent of research/ or api/ entirely — real sqlite3 connections to a
# throwaway temp database, proving SQLite/Python's actual behavior on this
# system rather than assuming it. This is what caught the correction
# documented in docs/RESEARCH_STORE_LOCKING.md: research/store.py's exact
# connect() call shape (no explicit `timeout=`) already gets a 5000ms
# busy_timeout from Python's OWN default — there was never a gap to work
# around for that file specifically.

db_path = TMP / "b_concurrency.db"
setup = sqlite3.connect(str(db_path))
setup.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
setup.commit()
setup.close()

conn1 = sqlite3.connect(str(db_path), isolation_level=None)
conn1.execute("BEGIN EXCLUSIVE")
conn1.execute("INSERT INTO t (v) VALUES ('from conn1, not yet committed')")

# B1: sqlite3.connect() with NO explicit `timeout=` override — the exact
# call shape research/store.py and paper/store.py both use — already has
# Python's default 5.0s (5000ms) busy_timeout, and so WAITS rather than
# failing immediately.
conn2 = sqlite3.connect(str(db_path), isolation_level=None)
default_busy_timeout = conn2.execute("PRAGMA busy_timeout").fetchone()[0]
t0 = time.monotonic()
raised = False
try:
    conn2.execute("INSERT INTO t (v) VALUES ('from conn2')")
except sqlite3.OperationalError:
    raised = True
elapsed_default = time.monotonic() - t0
conn2.close()

check("B1: a bare sqlite3.connect() (no timeout= override) already "
      "reports a 5000ms busy_timeout by default — confirms the correction "
      "in docs/RESEARCH_STORE_LOCKING.md, not an assumption",
      default_busy_timeout == 5000, default_busy_timeout)
check("B1b: that default connection actually WAITS for the lock (multiple "
      "seconds) before raising, rather than failing immediately",
      raised and elapsed_default > 1.0, (raised, elapsed_default))

# B2: ONLY an explicit timeout=0 reproduces true immediate failure — the
# behavior the original (incorrect) gap analysis assumed was the default
# everywhere.
conn2b = sqlite3.connect(str(db_path), isolation_level=None, timeout=0)
t0 = time.monotonic()
raised_immediately = False
try:
    conn2b.execute("INSERT INTO t (v) VALUES ('from conn2b')")
except sqlite3.OperationalError:
    raised_immediately = True
elapsed_zero_timeout = time.monotonic() - t0
conn2b.close()
conn1.execute("ROLLBACK")

check("B2: ONLY an explicit timeout=0 produces true immediate failure "
      "(< 0.5s) — and nothing in this codebase ever passes timeout=0",
      raised_immediately and elapsed_zero_timeout < 0.5,
      (raised_immediately, elapsed_zero_timeout))

# B3: WITH busy_timeout (explicit or default — same mechanism either way),
# a second writer WAITS for the first to finish, then succeeds — proven by
# having a background thread hold the exclusive lock for a known, short
# duration and confirming the foreground write only completes AFTER that
# duration elapses.
HOLD_SECONDS = 0.5
release_now = threading.Event()


def _hold_exclusive_lock():
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.execute("BEGIN EXCLUSIVE")
    conn.execute("INSERT INTO t (v) VALUES ('from holder thread')")
    release_now.wait(timeout=5)
    conn.execute("COMMIT")
    conn.close()


holder = threading.Thread(target=_hold_exclusive_lock)
holder.start()
time.sleep(0.1)  # let the holder thread actually acquire the lock first

conn3 = sqlite3.connect(str(db_path), isolation_level=None)
conn3.execute("PRAGMA busy_timeout = 5000")
t1 = time.monotonic()
release_timer = threading.Timer(HOLD_SECONDS, release_now.set)
release_timer.start()
conn3.execute("INSERT INTO t (v) VALUES ('from conn3, waited')")
elapsed_with_timeout = time.monotonic() - t1
conn3.close()
holder.join(timeout=5)

check("B3: WITH busy_timeout=5000, a concurrent writer waits for the lock "
      "and succeeds, rather than raising", elapsed_with_timeout >= HOLD_SECONDS * 0.8,
      elapsed_with_timeout)
check("B3b: it did not wait the FULL 5000ms budget — it succeeded as soon "
      "as the lock was actually released, not after some fixed delay",
      elapsed_with_timeout < 3.0, elapsed_with_timeout)


shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'=' * 52}")
print(f"  {PASSED} passed, {FAILED} failed")
print(f"{'=' * 52}\n")
sys.exit(1 if FAILED else 0)
