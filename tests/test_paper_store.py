"""
tests/test_paper_store.py — paper/store.py's own storage-layer guarantees:
account bookkeeping, lot open/close atomicity, order idempotency
(dedupe_key), append-only enforcement on paper_orders/paper_trades, and the
paper_cycles idempotency ledger.

Run with:  python -m tests.test_paper_store
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.paper_fixtures import PaperTestEnv, FIXED_NOW  # noqa: E402
from paper.store import PaperStore, PaperStoreError, order_dedupe_key  # noqa: E402

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


NOW = FIXED_NOW.isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
print("\n--- account bookkeeping ---")
# ---------------------------------------------------------------------------

with PaperTestEnv(initial_capital=50_000.0) as env:
    account = env.store.get_account()
    check("a fresh account starts at the configured initial_capital",
          account["initial_capital"] == 50_000.0 and account["cash"] == 50_000.0)
    check("a fresh account has zero realized P&L and zero costs",
          account["realized_pnl_alltime"] == 0.0 and account["total_costs_alltime"] == 0.0)

    account2 = env.store.get_account()
    check("opening the store again does not re-initialize the account (singleton row)",
          account2["created_at"] == account["created_at"])

    journal_mode = env.store._conn.execute("PRAGMA journal_mode").fetchone()[0]
    check("the database is NOT in WAL mode (WAL requires write access to its "
          "-wal/-shm sidecar files even to service a plain SELECT, which "
          "breaks PaperStore.open_readonly() under a locked-down, non-root "
          "service account with no write grant on paper/ at all — see "
          "paper/schema.sql's own comment on this)",
          journal_mode.lower() != "wal")


# ---------------------------------------------------------------------------
print("\n--- lot open/close atomicity ---")
# ---------------------------------------------------------------------------

with PaperTestEnv(initial_capital=100_000.0) as env:
    lot_id = env.store.open_lot(
        strategy_id="s1", strategy_version_id="v1", symbol="INFY",
        entry_order_id="po_1", entry_price=100.0, quantity=10, opened_at=NOW, now=NOW)
    acct = env.store.get_account()
    check("opening a lot debits cash by entry_price * quantity",
          abs(acct["cash"] - 99_000.0) < 1e-6, f"cash={acct['cash']}")

    positions = env.store.list_positions()
    check("the open lot appears in list_positions() with the right avg entry price",
          len(positions) == 1 and positions[0]["avg_entry_price"] == 100.0)

    trade = env.store.close_lot(
        lot_id, exit_order_id="po_2", exit_price=110.0, closed_at=NOW,
        gross_pnl=100.0, costs=5.0, net_pnl=95.0, exit_reason="signal_exit", now=NOW)
    check("closing a lot returns the immutable trade row with the given P&L",
          trade["net_pnl"] == 95.0 and trade["gross_pnl"] == 100.0 and trade["costs"] == 5.0)

    acct2 = env.store.get_account()
    check("closing a lot credits cash by (exit proceeds - costs)",
          abs(acct2["cash"] - (99_000.0 + 1100.0 - 5.0)) < 1e-6, f"cash={acct2['cash']}")
    check("closing a lot accumulates realized_pnl_alltime and total_costs_alltime",
          acct2["realized_pnl_alltime"] == 95.0 and acct2["total_costs_alltime"] == 5.0)

    check("a closed lot no longer appears in list_positions()",
          env.store.list_positions() == [])

    try:
        env.store.close_lot(
            lot_id, exit_order_id="po_3", exit_price=120.0, closed_at=NOW,
            gross_pnl=0.0, costs=0.0, net_pnl=0.0, exit_reason="signal_exit", now=NOW)
        check("closing an already-CLOSED lot raises PaperStoreError", False,
              "no exception was raised")
    except PaperStoreError:
        check("closing an already-CLOSED lot raises PaperStoreError", True)

    try:
        env.store.close_lot(999999, exit_order_id="po_x", exit_price=1.0, closed_at=NOW,
                            gross_pnl=0.0, costs=0.0, net_pnl=0.0, exit_reason="x", now=NOW)
        check("closing a nonexistent lot id raises PaperStoreError", False)
    except PaperStoreError:
        check("closing a nonexistent lot id raises PaperStoreError", True)


# ---------------------------------------------------------------------------
print("\n--- order idempotency (dedupe_key) ---")
# ---------------------------------------------------------------------------

with PaperTestEnv() as env:
    kwargs = dict(
        cycle_id="cyc1", strategy_id="s1", strategy_version_id="v1", symbol="INFY",
        side="BUY", requested_quantity=10, signal_generated_at=NOW, requested_at=NOW,
        filled_at=NOW, fill_price=100.0, status="FILLED", reason=None, now=NOW,
    )
    order1 = env.store.insert_order(**kwargs)
    check("the first insert_order() call returns the new row", order1 is not None)

    order2 = env.store.insert_order(**kwargs)
    check("an identical insert_order() call (same cycle/version/symbol/side/signal time) "
          "returns None — a safe no-op, never a duplicate row", order2 is None)

    check("only one row actually exists in paper_orders",
          len(env.store.list_orders()) == 1)

    diff_cycle = dict(kwargs, cycle_id="cyc2")
    order3 = env.store.insert_order(**diff_cycle)
    check("the SAME signal in a DIFFERENT cycle_id is a genuinely new order, not a duplicate",
          order3 is not None and len(env.store.list_orders()) == 2)

    key_a = order_dedupe_key(cycle_id="c", strategy_version_id="v", symbol="INFY",
                             side="BUY", signal_generated_at=NOW)
    key_b = order_dedupe_key(cycle_id="c", strategy_version_id="v", symbol="INFY",
                             side="SELL", signal_generated_at=NOW)
    check("order_dedupe_key() is sensitive to `side` (BUY vs SELL never collide)",
          key_a != key_b)


# ---------------------------------------------------------------------------
print("\n--- append-only enforcement (paper_orders / paper_trades) ---")
# ---------------------------------------------------------------------------

with PaperTestEnv() as env:
    order = env.store.insert_order(
        cycle_id="cyc1", strategy_id="s1", strategy_version_id="v1", symbol="INFY",
        side="BUY", requested_quantity=10, signal_generated_at=NOW, requested_at=NOW,
        filled_at=NOW, fill_price=100.0, status="FILLED", reason=None, now=NOW,
    )
    try:
        env.store._conn.execute(
            "UPDATE paper_orders SET status='REJECTED' WHERE id=?", (order["id"],))
        check("directly UPDATE-ing a paper_orders row is refused by the append-only trigger",
              False, "no exception was raised")
    except sqlite3.DatabaseError as e:
        check("directly UPDATE-ing a paper_orders row is refused by the append-only trigger",
              "append-only" in str(e))

    try:
        env.store._conn.execute("DELETE FROM paper_orders WHERE id=?", (order["id"],))
        check("directly DELETE-ing a paper_orders row is refused by the append-only trigger",
              False)
    except sqlite3.DatabaseError as e:
        check("directly DELETE-ing a paper_orders row is refused by the append-only trigger",
              "append-only" in str(e))

    lot_id = env.store.open_lot(
        strategy_id="s1", strategy_version_id="v1", symbol="INFY",
        entry_order_id=order["paper_order_id"], entry_price=100.0, quantity=10,
        opened_at=NOW, now=NOW)
    trade = env.store.close_lot(
        lot_id, exit_order_id="po_exit", exit_price=110.0, closed_at=NOW,
        gross_pnl=100.0, costs=5.0, net_pnl=95.0, exit_reason="signal_exit", now=NOW)

    try:
        env.store._conn.execute(
            "UPDATE paper_trades SET net_pnl=0 WHERE id=?", (trade["id"],))
        check("directly UPDATE-ing a paper_trades row is refused by the append-only trigger",
              False)
    except sqlite3.DatabaseError as e:
        check("directly UPDATE-ing a paper_trades row is refused by the append-only trigger",
              "append-only" in str(e))


# ---------------------------------------------------------------------------
print("\n--- paper_cycles idempotency ledger ---")
# ---------------------------------------------------------------------------

with PaperTestEnv() as env:
    check("get_cycle() on an unknown cycle_id returns None",
          env.store.get_cycle("nope") is None)

    started = env.store.start_cycle("cyc-a", NOW)
    check("start_cycle() creates a RUNNING row", started["status"] == "RUNNING")

    started_again = env.store.start_cycle("cyc-a", NOW)
    check("start_cycle() on an already-started cycle_id is a no-op that returns "
          "the EXISTING row, not a fresh RUNNING row",
          started_again["started_at"] == started["started_at"])

    env.store.complete_cycle("cyc-a", {"orders_filled": 3}, NOW)
    completed = env.store.get_cycle("cyc-a")
    check("complete_cycle() sets status=COMPLETED and stores the summary as JSON",
          completed["status"] == "COMPLETED" and "orders_filled" in completed["summary_json"])

    env.store.start_cycle("cyc-b", NOW)
    env.store.fail_cycle("cyc-b", "boom", NOW)
    failed = env.store.get_cycle("cyc-b")
    check("fail_cycle() on a started cycle sets status=FAILED and records the note",
          failed["status"] == "FAILED" and failed["note"] == "boom")

    check("list_cycles() returns both cycles, most recently started first or by insertion",
          {c["cycle_id"] for c in env.store.list_cycles()} == {"cyc-a", "cyc-b"})


print(f"\n{'=' * 52}")
print(f"  {PASSED} passed, {FAILED} failed")
print(f"{'=' * 52}\n")
sys.exit(1 if FAILED else 0)
