"""
STEP 3 — backfill. Runs in parallel with the recorder and has no deadline.

    python -m research.backfill membership
    python -m research.backfill prices --from 2019-01-01 --to 2026-08-31
    python -m research.backfill announcements --from 2019-01-01 --to 2026-08-31
    python -m research.backfill status

Deliberately separate from the recorder, because they have opposite urgency
profiles. The recorder is racing a clock: a missed window is gone. The backfill
is racing nothing at all — the bhavcopy archive and the announcement history
will be equally available in six months. Mixing them into one job would give the
unhurried work the power to break the urgent work.

Everything here is idempotent. The store's dedupe key is a hash of the fact
itself, so re-running a range that partially completed inserts only what is
genuinely missing. That matters: these are long jobs against free public
endpoints, they will be interrupted, and a backfill that cannot be safely
resumed is a backfill that never finishes.
"""

from __future__ import annotations

import sys
import json
import argparse
import datetime as dt
from pathlib import Path

from .store import Store, now_ist
from .sources.base import Http
from .sources import membership, prices_eod, announcements


def _date(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


# ---------------------------------------------------------------------------

def do_membership(store: Store, args) -> dict:
    """Fast, small, and the highest-value single import in the project: without
    point-in-time index membership every backtest silently studies the companies
    that survived, and that bias always flatters."""
    return {"index": membership.load(store), "fno": membership.load_fno(store)}


def do_prices(store: Store, args) -> dict:
    http = Http()
    done = {"n": 0}

    def progress(d: dt.date, r: dict) -> None:
        done["n"] += 1
        if done["n"] % 20 == 0:
            print(f"    {d}  ({done['n']} sessions, {r['rows_seen']} symbols)",
                  flush=True)

    return prices_eod.load_range(store, args.frm, args.to, http, progress)


def do_announcements(store: Store, args) -> dict:
    """Needs a symbol -> BSE scrip code map.

    Held back deliberately until STEP 0 has run: if BSE does not serve
    timestamped filings in useful volume for 2019-2026, this backfill is
    pointless and EXP-B1 has to be redesigned before it is locked. Running it
    first would burn days producing a dataset the experiment cannot use.
    """
    map_path = Path(args.scrip_map) if args.scrip_map else None
    if not map_path or not map_path.exists():
        return {
            "skipped": True,
            "reason": (
                "No scrip map supplied. Run `python research/probe/probe.py` "
                "first — it reports whether BSE serves usable timestamps at all. "
                "Then pass --scrip-map with a JSON {SYMBOL: scrip_code} built "
                "from the probe's confirmed universe."),
        }
    mapping = json.loads(map_path.read_text())
    return announcements.load_universe(store, mapping, args.frm, args.to,
                                       category=args.category, http=Http())


def do_status(store: Store, args) -> dict:
    st = store.stats()
    view = store.view(now_ist())
    st["coverage_checks"] = {
        "nifty500_today": len(view.universe("Nifty 500")),
        "nifty50_today": len(view.universe("Nifty 50")),
    }
    return st


COMMANDS = {
    "membership": do_membership,
    "prices": do_prices,
    "announcements": do_announcements,
    "status": do_status,
}


def main() -> None:
    ap = argparse.ArgumentParser(description="Living Quant backfill")
    ap.add_argument("command", choices=sorted(COMMANDS))
    ap.add_argument("--from", dest="frm", type=_date,
                    default=dt.date(2019, 1, 1))
    ap.add_argument("--to", dest="to", type=_date,
                    default=dt.date.today() - dt.timedelta(days=1))
    ap.add_argument("--category", default="Result")
    ap.add_argument("--scrip-map", default=None)
    ap.add_argument("--db", default=None)
    args = ap.parse_args()

    store = Store.open(args.db) if args.db else Store.open()
    started = now_ist()
    print(f"backfill [{args.command}] started {started:%Y-%m-%d %H:%M IST}", flush=True)

    try:
        result = COMMANDS[args.command](store, args)
    except KeyboardInterrupt:
        print("\ninterrupted — safe to resume, the store dedupes on re-run")
        store.close()
        sys.exit(130)

    elapsed = (now_ist() - started).total_seconds()
    print(json.dumps(result, indent=2, default=str))
    print(f"\nelapsed {elapsed:.1f}s")
    store.close()


if __name__ == "__main__":
    main()
