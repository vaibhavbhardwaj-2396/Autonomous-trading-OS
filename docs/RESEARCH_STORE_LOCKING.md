# Research store concurrency — the deliberate decision record

STABLE + CONTROLLED (outcome 1). This document exists because the obvious,
symmetrical fix (add `PRAGMA busy_timeout` to `research/store.py::Store.open()`,
the way `paper/store.py` got one) is not available here — `research/store.py`
is permanently write-protected — and the investigation into a workaround
turned up a correction worth recording honestly: **the gap this was meant
to close never actually existed.**

## The correction — read this before the rest

The original plan (and an earlier status report in this session) claimed
`research/store.py`'s connections have no `busy_timeout`, unlike
`paper/store.py`'s after the fix. That claim was **wrong**, caught by the
regression test below rather than assumed away:

```python
>>> import sqlite3
>>> c = sqlite3.connect("some.db")          # research/store.py's exact call shape
>>> c.execute("PRAGMA busy_timeout").fetchone()[0]
5000
```

Python's `sqlite3.connect()` has a `timeout` parameter that defaults to
**5.0 seconds**, and setting it is exactly what `PRAGMA busy_timeout` does
internally — CPython calls `sqlite3_busy_timeout()` with that value at
connect time. `research/store.py::Store.open()`'s call
(`sqlite3.connect(str(path), isolation_level=None)`) never overrides
`timeout`, so it has had an effective 5000ms busy_timeout the entire time,
with zero code written for it. `paper/store.py`'s connections, before this
outcome's edit, had the identical property for the identical reason — my
added `PRAGMA busy_timeout = 5000` lines there and in `api/data.py` are
**confirmed redundant with the Python default**, not gap-filling.

Proven directly: `tests/test_research_store_concurrency.py` section B
opens a real connection with no timeout override, holds an exclusive lock
on another connection, and shows the "no override" connection waits ~5
seconds and only then raises — not immediately. Immediate failure only
happens with an *explicit* `timeout=0`, which nothing in this codebase
passes.

## What this means for the original three writers/readers

| Component | Explicit override? | Effective busy_timeout |
|---|---|---|
| `research/store.py::Store.open()` | none, never added | 5000ms (Python default) — unchanged, always been there |
| `paper/store.py::PaperStore.open()`/`open_readonly()` | `PRAGMA busy_timeout = 5000` added this outcome | 5000ms — same value as before, now explicit |
| `api/data.py::get_default_store()` | `PRAGMA busy_timeout = 5000` added this outcome | 5000ms — same value as before, now explicit |

No connection anywhere in this codebase has ever had `timeout=0`. There
was no live "database is locked on first contention" bug to fix, and
nothing observed in `research/worker_runs.jsonl` or
`research/recorder_runs.jsonl` ever suggested one.

## Why the edits were kept anyway, and why `research/store.py` still isn't touched

The `paper/store.py` and `api/data.py` edits are harmless (they reassert a
value that was already in effect) and are kept for two modest, real
reasons — not because they fix anything:

1. **Explicit over implicit.** A 5-second grace period that exists only
   because nobody happened to pass `timeout=0` is one accidental refactor
   away from silently disappearing (e.g. someone "cleans up" a connection
   call and adds `timeout=0` for a perceived performance reason, having no
   idea it removes lock-contention tolerance). An explicit `PRAGMA` line
   is self-documenting and survives that kind of change.
2. **A place to hang the regression test.** `tests/test_research_store_concurrency.py`
   section A now asserts `get_default_store()`'s connection reports a
   non-zero `busy_timeout` — a real, permanent guard against exactly the
   accidental-`timeout=0` scenario above, for the one connection (the
   dashboard API's) that would be hardest to diagnose if it regressed.

`research/store.py` remains untouched — not because a workaround was
needed and found, but because there was never a decision to make: its
existing, unmodified code already had the property everyone wanted it to
have. The permanent write-protection on that file was never actually
tested by this investigation, since no change to it was ever justified in
the first place.

## What every writer/reader actually depends on for overlap safety

To be clear about what genuinely changed in this outcome, separate from
this timeout investigation: `research/recorder.py` and `paper/runner.py`
gained real, new POSIX-flock locks (`recorder_lock()`, `paper_lock()`)
preventing two overlapping invocations of *the same entrypoint* from ever
running concurrently at all — that is what actually prevents the
5-second-grace-period from ever being tested in production, by making
same-process contention structurally impossible in the first place. The
5-second default is a safety net for the one case those locks don't cover
(a request-driven dashboard read landing during a cron writer's brief
commit window), not the primary mechanism.

## Regression test

`tests/test_research_store_concurrency.py`:

- **A**: `api/data.py::get_default_store()` returns a connection whose
  `busy_timeout` is actually 5000, not 0 — catches a future accidental
  `timeout=0` regression.
- **B**: an independent, from-scratch proof (no project code involved)
  that a bare `sqlite3.connect()` with no override already waits ~5s
  before raising "database is locked," and that only an explicit
  `timeout=0` produces the immediate-failure behavior this whole
  investigation initially assumed was the status quo.
