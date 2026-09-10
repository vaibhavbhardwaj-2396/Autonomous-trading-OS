"""
scripts/migrate_capital_model.py — one-time migration to the dynamic
broker-derived capital model (docs/CAPITAL_MODEL.md).

    venv/bin/python scripts/migrate_capital_model.py --simulate   # dry run — writes NOTHING
    venv/bin/python scripts/migrate_capital_model.py --offline    # migrate from the last broker_snapshot
    venv/bin/python scripts/migrate_capital_model.py              # live broker READ + migrate
    venv/bin/python scripts/migrate_capital_model.py --revert <backup.bak>

Safe by construction:
  * places NO orders — only broker READ calls (funds / holdings / positions / quotes)
  * idempotent   — exits 0, unchanged, if state already has managed.model_version == 1
  * reversible   — backs state.json up to memory/state.json.pre-capital-model.<ts>.bak
  * auditable    — appends a full before/after record to memory/capital_model_migration.jsonl
  * promotes NO holdings — managed.symbols starts empty; existing holdings stay unmanaged

What it does:
  1. compute CURRENT ACCOUNT VALUE  = broker free cash + Σ(broker holding/position qty × LTP)
  2. compute CURRENT MANAGED EQUITY = broker free cash + Σ(agent open-position qty × LTP)
     (no promoted holdings yet, so this is just the free cash unless the agent already holds
      positions of its own)
  3. write state["managed"] with peak_growth = 0 and one "inception" cash-flow event equal to
     that managed equity  →  growth = 0  →  drawdown = 0 at cutover
  4. set state["capital"] / state["peak_capital"] / state["cash_available"] to the new
     managed baseline and DROP state["allocated_capital"] — the legacy Kite-era
     peak_capital survives only inside the audit record, never as a risk input
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from engine import guardrails as gr          # noqa: E402
from engine.journal import MANAGED_MODEL_VERSION, now_ist  # noqa: E402

STATE_FILE = PROJECT_ROOT / "memory" / "state.json"
AUDIT_LOG = PROJECT_ROOT / "memory" / "capital_model_migration.jsonl"


# ---------------------------------------------------------------------------
# Pure core — no I/O, unit-testable
# ---------------------------------------------------------------------------

def build_migration(state: dict, *, broker_free_cash: float,
                    priced_by_symbol: dict, account_total_value: float,
                    broker_name: str, mode: str, now: str,
                    priced_is_market: bool = True) -> tuple:
    """Return (new_state, audit_record) for the migration. Pure.

    `priced_by_symbol` maps SYMBOL -> current market value (qty × LTP) of that
    broker line. `priced_is_market` is False in --offline mode where an open
    position is valued at its cost basis until the next real sync corrects it.
    """
    new_state = json.loads(json.dumps(state))          # deep copy
    notes: list[str] = []

    open_syms = sorted({p["symbol"].upper() for p in new_state.get("open_positions", [])})
    managed_positions_value = 0.0
    unpriced: list[str] = []
    for s in open_syms:
        if s in priced_by_symbol and priced_by_symbol[s] > 0:
            managed_positions_value += float(priced_by_symbol[s])
        else:
            unpriced.append(s)
    if unpriced:
        notes.append(f"agent open position(s) with no live price at migration, "
                     f"valued at 0 until the next sync: {', '.join(unpriced)}")
    if not priced_is_market and open_syms:
        notes.append("--offline: agent positions valued at cost basis; the next live "
                     "sync recomputes them at market")

    managed_cash = round(float(broker_free_cash), 2)
    managed_equity = round(managed_cash + managed_positions_value, 2)

    before = {
        "capital": state.get("capital"),
        "peak_capital": state.get("peak_capital"),
        "allocated_capital": state.get("allocated_capital"),
        "cash_available": state.get("cash_available"),
        "drawdown_level": state.get("drawdown_level"),
    }

    new_state["managed"] = {
        "model_version": MANAGED_MODEL_VERSION,
        "symbols": [],                                  # NO automatic promotion
        "cashflow_events": [{
            "ts": now, "amount": managed_equity, "kind": "inception",
            "reason": "capital-model migration — inception baseline (broker-derived)",
            "by": "migration",
        }],
        "peak_growth": 0.0,
        "growth": 0.0,
        "portfolio_value": managed_equity,
        "free_cash": managed_cash,
        "positions_market_value": round(managed_positions_value, 2),
        "valued_at": now,
        "migrated_at": now,
        "migration_mode": mode,
    }

    # Legacy fields: mirror the new baseline; the Kite-era peak is GONE from state.
    new_state["capital"] = managed_equity
    new_state["peak_capital"] = managed_equity
    new_state["cash_available"] = managed_cash
    new_state.pop("allocated_capital", None)            # must not influence anything
    new_state["drawdown_level"] = gr.drawdown_level(new_state)   # NORMAL — growth is 0

    audit = {
        "ts": now,
        "action": "capital_model_migration",
        "model_version": MANAGED_MODEL_VERSION,
        "broker": broker_name,
        "mode": mode,
        "before": before,
        "broker_derived": {
            "account_total_value": round(float(account_total_value), 2),
            "broker_free_cash": managed_cash,
            "managed_positions_market_value": round(managed_positions_value, 2),
            "managed_symbols": open_syms,
            "unmanaged_preserved": bool((state.get("broker_snapshot") or {}).get("unmanaged_symbols")),
        },
        "after": {
            "managed": new_state["managed"],
            "capital": new_state["capital"],
            "peak_capital": new_state["peak_capital"],
            "cash_available": new_state["cash_available"],
            "allocated_capital_removed": True,
            "legacy_peak_capital_retained_only_here": before["peak_capital"],
        },
        "notes": notes,
    }
    return new_state, audit


# ---------------------------------------------------------------------------
# Broker read (no orders)
# ---------------------------------------------------------------------------

def _broker_read():
    from engine.broker import get_broker
    b = get_broker()
    free_cash = b.funds()
    holdings = b.holdings()
    positions = b.positions()
    priced = {}
    total = free_cash
    for line in list(holdings) + list(positions):
        mv = line.last_price * abs(line.quantity) if line.last_price > 0 else 0.0
        total += mv
        if line.symbol:
            priced[line.symbol.upper()] = priced.get(line.symbol.upper(), 0.0) + mv
    return b.name, float(free_cash), priced, float(total)


def _offline_read(state: dict):
    snap = state.get("broker_snapshot") or {}
    free_cash = snap.get("free_cash")
    total = snap.get("total_account_value")
    if free_cash is None or total is None:
        raise SystemExit("--offline needs a prior broker_snapshot with free_cash and "
                         "total_account_value; run a real sync first or drop --offline.")
    priced = {p["symbol"].upper(): p["entry"] * p["quantity"]
              for p in state.get("open_positions", [])}          # cost basis
    return "offline", float(free_cash), priced, float(total)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    if not STATE_FILE.exists():
        raise SystemExit(f"no state file at {STATE_FILE}")
    return json.loads(STATE_FILE.read_text())


def _print_report(before_state, new_state, audit, *, wrote: bool):
    b_eq = (before_state.get("managed") or {}).get("portfolio_value") or before_state.get("capital")
    print("=" * 68)
    print("  CAPITAL-MODEL MIGRATION" + ("  (SIMULATION — nothing written)" if not wrote else ""))
    print("=" * 68)
    print("\n-- pre-migration state --")
    print(f"   capital           : {before_state.get('capital')}")
    print(f"   peak_capital      : {before_state.get('peak_capital')}   <- legacy Kite-era ratchet")
    print(f"   allocated_capital : {before_state.get('allocated_capital')}")
    print(f"   open_positions    : {[p['symbol'] for p in before_state.get('open_positions', [])]}")
    d = audit["broker_derived"]
    print("\n-- broker-derived (current) --")
    print(f"   broker_free_cash              : {d['broker_free_cash']}")
    print(f"   account_total_value           : {d['account_total_value']}   "
          f"(cash + Σ holdings/positions × LTP)")
    print(f"   managed_positions_market_value: {d['managed_positions_market_value']}")
    m = new_state["managed"]
    print("\n-- calculated managed equity --")
    print(f"   managed cash                  : {m['free_cash']}")
    print(f"   + managed positions (market)  : {m['positions_market_value']}")
    print(f"   = MANAGED EQUITY              : {m['portfolio_value']}")
    print(f"   inception cash-flow event     : {m['cashflow_events'][0]['amount']}")
    print(f"   peak_growth                   : {m['peak_growth']}   (growth = 0 at cutover)")
    print("\n-- post-migration state --")
    print(f"   managed.model_version : {m['model_version']}")
    print(f"   managed.symbols       : {m['symbols']}   (NO holdings promoted)")
    print(f"   capital  (mirror)     : {new_state['capital']}")
    print(f"   peak_capital (mirror) : {new_state['peak_capital']}   <- ₹{audit['before']['peak_capital']} is GONE from state")
    print(f"   allocated_capital     : removed")
    summary = gr.status_summary(new_state)
    print("\n-- resulting risk / drawdown (guardrails.status_summary) --")
    print(f"   managed_equity        : {summary['managed_equity']}")
    print(f"   drawdown_pct          : {summary['drawdown_pct']}%")
    print(f"   drawdown_level        : {summary['drawdown_level']}")
    print(f"   risk_budget_per_trade : {summary['risk_budget_per_trade']}  "
          f"(= managed_equity × {summary['risk_per_trade_pct']}%)")
    print(f"   ladder_thresholds     : {summary['ladder_thresholds']}")
    legacy_gone = (audit['before']['peak_capital'] not in
                   (new_state['peak_capital'], new_state['capital'],
                    summary['peak_capital'], summary['managed_equity']))
    print(f"\n   legacy ₹{audit['before']['peak_capital']} peak no longer a risk input : "
          f"{'CONFIRMED' if legacy_gone else 'STILL PRESENT (investigate)'}")
    if audit["notes"]:
        print("\n-- notes --")
        for n in audit["notes"]:
            print(f"   * {n}")
    print()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--simulate", action="store_true", help="compute and print, write nothing")
    ap.add_argument("--offline", action="store_true", help="use the last broker_snapshot, no broker call")
    ap.add_argument("--revert", metavar="BACKUP", help="restore state.json from a .bak and exit")
    args = ap.parse_args(argv)

    if args.revert:
        bak = Path(args.revert)
        if not bak.is_file():
            print(f"no such backup: {bak}", file=sys.stderr)
            return 1
        shutil.copy2(bak, STATE_FILE)
        print(f"restored {STATE_FILE} from {bak}")
        return 0

    state = _load_state()
    if (state.get("managed") or {}).get("model_version") == MANAGED_MODEL_VERSION:
        print("already migrated (managed.model_version == "
              f"{MANAGED_MODEL_VERSION}) — nothing to do.")
        return 0

    now = now_ist().isoformat(timespec="seconds")
    if args.offline:
        broker_name, free_cash, priced, total = _offline_read(state)
        priced_is_market = False
    else:
        try:
            broker_name, free_cash, priced, total = _broker_read()
        except Exception as e:  # noqa: BLE001
            print(f"broker read failed: {type(e).__name__}: {e}\n"
                  f"fix the token or re-run with --offline.", file=sys.stderr)
            return 1
        priced_is_market = True

    new_state, audit = build_migration(
        state, broker_free_cash=free_cash, priced_by_symbol=priced,
        account_total_value=total, broker_name=broker_name,
        mode=("offline" if args.offline else "live"), now=now,
        priced_is_market=priced_is_market)

    _print_report(state, new_state, audit, wrote=not args.simulate)

    if args.simulate:
        print("SIMULATION — no files changed.")
        return 0

    ts_tag = now_ist().strftime("%Y%m%dT%H%M%S")
    backup = STATE_FILE.with_suffix(f".json.pre-capital-model.{ts_tag}.bak")
    shutil.copy2(STATE_FILE, backup)
    audit["after"]["backup"] = str(backup.relative_to(PROJECT_ROOT))

    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(new_state, indent=2))
    tmp.replace(STATE_FILE)

    with AUDIT_LOG.open("a") as f:
        f.write(json.dumps(audit, default=str) + "\n")

    print(f"migrated. backup: {backup.name}")
    print(f"audit record appended to: {AUDIT_LOG.relative_to(PROJECT_ROOT)}")
    print("revert with:  venv/bin/python scripts/migrate_capital_model.py --revert "
          f"{backup.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
