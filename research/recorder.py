"""
STEP 2 — the recorder. The only clock-bound component in the project.

    python -m research.recorder --cycle intraday
    python -m research.recorder --cycle evening
    python -m research.recorder --cycle news

Everything else in the Living Quant can be built next month at no cost: the
bhavcopy archive, the membership CSV, the announcement history will all still be
there. This will not. Option-chain shape, intraday price paths and the moment a
headline became visible are published live and never archived. Every day this
does not run is a day that cannot be recovered at any price.

That is the whole reason it ships before the replay engine and before EXP-B1.

Operational rules, each of which exists because the alternative fails quietly
-----------------------------------------------------------------------------
1. Its own cron entry and its own log. It is NEVER called from run_cycle.sh, so
   a recorder failure cannot abort a trading cycle and a trading failure cannot
   skip a recording.
2. No broker dependency. The INDstocks token expires every 24 hours and arrives
   by Telegram; a recorder gated on it goes silent exactly when Vaibhav is busy.
3. One feed failing never stops the others. A source that raises is recorded as
   a failed SourceResult and the run continues — the days the other five would
   have covered are not recoverable later either.
4. Exit code is 0 unless EVERY source failed. A single dead feed is a warning,
   not an outage, and paging on it teaches you to ignore the pager.
"""

from __future__ import annotations

import sys
import json
import argparse
import datetime as dt
from pathlib import Path

from .store import Store, now_ist, iso
from .sources.base import Http, run_source, SourceResult
from .sources import (option_chain, quotes, news, decisions, prices_eod,
                      briefing_artifacts)

LOG_DIR = Path(__file__).parent.parent / "logs"
RUN_LOG = Path(__file__).parent / "recorder_runs.jsonl"

CYCLES = {
    # 10:15 / 12:30 / 14:30 — the surface while it is moving
    "intraday": ("option_chain", "quotes", "news"),
    # 18:30 — after the bhavcopy publishes
    "evening": ("prices_eod", "decisions", "briefings", "news", "option_chain"),
    # every 30 min — arrival times are the whole point, so poll often
    "news": ("news",),
    # one-off / manual
    "all": ("option_chain", "quotes", "news", "prices_eod", "decisions", "briefings"),
}

NEWS_POLL_MINUTES = 30


def _last_session(today: dt.date) -> dt.date:
    """The most recent weekday on or before today. NSE holidays simply have no
    bhavcopy, which the loader reports as a missing file rather than an error."""
    d = today
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def run_cycle(cycle: str, store: Store, http: Http | None = None) -> list[SourceResult]:
    http = http or Http()
    wanted = CYCLES.get(cycle)
    if not wanted:
        raise SystemExit(f"Unknown cycle {cycle!r}. Choose from: {', '.join(CYCLES)}")

    runners = {
        "option_chain": lambda: option_chain.load(store, http=http),
        "quotes":       lambda: quotes.load(store, http=http),
        "news":         lambda: news.load(store, http=http,
                                          poll_interval_minutes=NEWS_POLL_MINUTES),
        "decisions":    lambda: decisions.load(store),
        # Reads the markdown briefing.py already writes to logs/. A data
        # handoff, never an import — engine must not know research exists.
        "briefings":    lambda: briefing_artifacts.load(store),
        "prices_eod":   lambda: prices_eod.load_day(
                            store, _last_session(now_ist().date()), http),
    }
    return [run_source(name, runners[name]) for name in wanted]


def _persist_run(cycle: str, results: list[SourceResult]) -> None:
    """A record of what the recorder itself did, so a gap in the data can later
    be distinguished from a gap in the recording. Without this, a missing
    Tuesday is ambiguous forever."""
    row = {
        "ts": iso(now_ist()),
        "cycle": cycle,
        "sources": [{"source": r.source, "ok": r.ok, "seen": r.rows_seen,
                     "new": r.rows_new, "error": r.error,
                     "elapsed_s": r.elapsed_s} for r in results],
    }
    try:
        RUN_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(RUN_LOG, "a") as f:
            f.write(json.dumps(row, default=str) + "\n")
    except OSError:
        pass


def _notify(message: str) -> None:
    """Best-effort Telegram. Guarded hard: the notifier lives in the trading
    project's scripts/ and may be unconfigured, and a recorder that dies because
    it could not send a message about dying is worse than one that stays quiet."""
    try:
        sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
        from telegram_notify import send_message  # type: ignore
        send_message(message)
    except Exception:
        pass


def main() -> None:
    ap = argparse.ArgumentParser(description="Living Quant recorder")
    ap.add_argument("--cycle", default="intraday", choices=sorted(CYCLES))
    ap.add_argument("--db", default=None)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet-on-success", action="store_true",
                    help="Only print when something failed (for cron)")
    args = ap.parse_args()

    started = now_ist()
    store = Store.open(args.db) if args.db else Store.open()
    try:
        results = run_cycle(args.cycle, store)
    finally:
        pass

    _persist_run(args.cycle, results)
    failed = [r for r in results if not r.ok]
    total_new = sum(r.rows_new for r in results)

    if args.json:
        print(json.dumps({
            "cycle": args.cycle,
            "started": iso(started),
            "rows_new": total_new,
            "results": [r.__dict__ for r in results],
        }, indent=2, default=str))
    elif not (args.quiet_on_success and not failed):
        print(f"recorder [{args.cycle}] {started:%Y-%m-%d %H:%M IST}")
        for r in results:
            print(r.line())
        print(f"  {total_new} new rows | {len(failed)} source(s) failed")

    # Every source down means the network, the host or the schedule is broken,
    # and that IS worth waking someone for — unlike one flaky feed.
    if failed and len(failed) == len(results):
        _notify(f"🔴 Recorder [{args.cycle}]: every source failed. "
                f"Perishable data for this window is being lost. "
                f"First error: {failed[0].error}")
        store.close()
        sys.exit(1)

    store.close()
    sys.exit(0)


if __name__ == "__main__":
    main()
