"""
paper/store.py — the paper engine's own SQLite storage layer.

Owns paper/paper_shadow.db (or wherever paper.config.db_path() points, e.g.
a temp file in tests). See paper/schema.sql for the full table layout and
paper/__init__.py for why this is a completely separate database from
research/market_memory.db and memory/state.json.

Mirrors research/store.py's shape (a Store class owning one connection,
`isolation_level=None` / explicit transactions, an idempotent
executescript() schema on open()) without importing anything from
research/ — paper/ has its own copy of the same small set of sound SQLite
conventions, not a dependency on the research package.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

from . import config as paper_config

SCHEMA_PATH = Path(__file__).parent / "schema.sql"
SCHEMA_VERSION = "1"


class PaperStoreError(RuntimeError):
    """Something about the paper store's own invariants was violated —
    e.g. closing a lot that is not open, or a cycle_id collision at the
    wrong status. Never raised for an ordinary business rejection (a
    rejected signal is recorded, not an exception) — only for a genuine
    programming/data error."""


def _short_hash(*parts: str) -> str:
    """sha256 of the given parts, truncated to 24 hex chars — the same
    truncated-sha256-of-canonical-parts convention strategies/core.py's
    hash_source()/compute_version_id() and research/contracts.py's
    content_hash() already use. Used here to derive paper_order_id /
    paper_trade_id deterministically from their own business fields, so two
    runs with identical inputs produce byte-identical ids too — not just
    byte-identical P&L — which is a strictly stronger, and equally cheap,
    determinism guarantee (see the module docstring and
    tests/test_paper_runner.py's determinism test)."""
    blob = "\x1f".join(parts)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


def order_dedupe_key(*, cycle_id: str, strategy_version_id: str, symbol: str,
                     side: str, signal_generated_at: str) -> str:
    """The uniqueness key that makes an order idempotent: the same signal,
    from the same StrategyVersion, on the same symbol, in the same cycle,
    can only ever produce one paper_orders row. See paper/schema.sql's
    paper_orders.dedupe_key and paper/runner.py."""
    return _short_hash(cycle_id, strategy_version_id, symbol, side, signal_generated_at)


def make_order_id(dedupe_key: str) -> str:
    return f"po_{dedupe_key}"


def make_trade_id(*, entry_order_id: str, exit_order_id: str) -> str:
    return f"pt_{_short_hash(entry_order_id, exit_order_id)}"


@dataclass
class PaperStore:
    _conn: sqlite3.Connection
    path: Path

    # -- lifecycle -------------------------------------------------------

    @classmethod
    def open(cls, path: Optional[Path] = None) -> "PaperStore":
        path = Path(path) if path is not None else paper_config.db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA_PATH.read_text())
        store = cls(_conn=conn, path=path)
        store._set_meta("schema_version", SCHEMA_VERSION)
        store._ensure_account()
        return store

    @classmethod
    def open_readonly(cls, path: Optional[Path] = None) -> "PaperStore":
        """Open strictly for reading: never creates the database file or
        its parent directory, and never runs schema DDL — so this needs no
        write permission anywhere, unlike open() above. This is what
        api/paper_data.py uses. The dashboard/API must never need write
        access to paper state; only paper/runner.py (invoked by cron or by
        hand, never by a web request) does, and it always goes through
        open() instead.

        This split exists because a production deployment reasonably runs
        the read-only dashboard API as a locked-down, non-root service
        account with read-only access to application state (the same
        posture it already had for memory/ and research/ before Slice AA
        existed) — open()'s implicit schema write breaks that isolation
        for no real benefit, since the schema is already guaranteed to
        exist by the time anything has actually written paper data.

        Raises FileNotFoundError if the database does not exist yet (e.g.
        no paper cycle and no eligibility decision has ever run) — callers
        should treat that as "nothing to show yet," not as a failure. See
        api.paper_data.get_default_paper_store().
        """
        resolved = Path(path) if path is not None else paper_config.db_path()
        if not resolved.is_file():
            raise FileNotFoundError(f"paper store not yet initialized: {resolved}")
        conn = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True,
                               isolation_level=None)
        conn.row_factory = sqlite3.Row
        return cls(_conn=conn, path=resolved)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "PaperStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """Explicit BEGIN/COMMIT/ROLLBACK — required because the connection
        is opened with isolation_level=None (autocommit), the same choice
        research/store.py makes. Anything that touches more than one table
        (closing a lot + writing its trade + crediting cash, chiefly) MUST
        go through this, or a crash mid-operation could leave the paper
        account inconsistent with its own lots/trades."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    def _set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    # -- account -----------------------------------------------------------

    def _ensure_account(self) -> None:
        row = self._conn.execute("SELECT id FROM paper_account WHERE id=1").fetchone()
        if row is not None:
            return
        cap = paper_config.initial_capital()
        now = _now_placeholder()
        self._conn.execute(
            "INSERT INTO paper_account "
            "(id, initial_capital, cash, realized_pnl_alltime, total_costs_alltime, "
            " created_at, updated_at) VALUES (1, ?, ?, 0, 0, ?, ?)",
            (cap, cap, now, now),
        )

    def get_account(self) -> dict:
        row = self._conn.execute(
            "SELECT initial_capital, cash, realized_pnl_alltime, total_costs_alltime, "
            "created_at, updated_at FROM paper_account WHERE id=1").fetchone()
        return dict(row)

    def _adjust_cash(self, *, cash_delta: float, realized_pnl_delta: float = 0.0,
                     costs_delta: float = 0.0, now: str) -> None:
        self._conn.execute(
            "UPDATE paper_account SET cash = cash + ?, "
            "realized_pnl_alltime = realized_pnl_alltime + ?, "
            "total_costs_alltime = total_costs_alltime + ?, updated_at = ? WHERE id=1",
            (cash_delta, realized_pnl_delta, costs_delta, now),
        )

    # -- orders --------------------------------------------------------------

    def insert_order(self, *, cycle_id: str, strategy_id: str, strategy_version_id: str,
                     symbol: str, side: str, requested_quantity: Optional[int],
                     signal_generated_at: str, requested_at: str,
                     filled_at: Optional[str], fill_price: Optional[float],
                     status: str, reason: Optional[str], now: str) -> Optional[dict]:
        """Insert one paper_orders row. Returns the inserted row as a dict,
        or None if this exact (cycle, version, symbol, side, signal time)
        was already recorded — the idempotency guarantee (see
        order_dedupe_key() and paper/runner.py)."""
        dedupe_key = order_dedupe_key(
            cycle_id=cycle_id, strategy_version_id=strategy_version_id,
            symbol=symbol, side=side, signal_generated_at=signal_generated_at)
        paper_order_id = make_order_id(dedupe_key)
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO paper_orders "
            "(paper_order_id, cycle_id, strategy_id, strategy_version_id, symbol, side, "
            " requested_quantity, signal_generated_at, requested_at, filled_at, "
            " fill_price, status, reason, dedupe_key, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (paper_order_id, cycle_id, strategy_id, strategy_version_id, symbol, side,
             requested_quantity, signal_generated_at, requested_at, filled_at,
             fill_price, status, reason, dedupe_key, now),
        )
        if cur.rowcount == 0:
            return None
        return self.get_order(paper_order_id)

    def get_order(self, paper_order_id: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM paper_orders WHERE paper_order_id=?", (paper_order_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_orders(self, *, strategy_version_id: Optional[str] = None,
                    limit: Optional[int] = None) -> list[dict]:
        sql = "SELECT * FROM paper_orders"
        args: list[Any] = []
        if strategy_version_id:
            sql += " WHERE strategy_version_id=?"
            args.append(strategy_version_id)
        sql += " ORDER BY created_at DESC, id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            args.append(limit)
        return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    # -- lots / positions ----------------------------------------------------

    def open_lot(self, *, strategy_id: str, strategy_version_id: str, symbol: str,
                entry_order_id: str, entry_price: float, quantity: int,
                opened_at: str, now: str) -> int:
        """Open one new lot and debit paper cash by its notional in the same
        transaction — a lot can never exist without the cash having been
        deducted, and vice versa."""
        with self._transaction():
            cur = self._conn.execute(
                "INSERT INTO paper_lots "
                "(strategy_id, strategy_version_id, symbol, entry_order_id, entry_price, "
                " quantity, opened_at, status, created_at) "
                "VALUES (?,?,?,?,?,?,?, 'OPEN', ?)",
                (strategy_id, strategy_version_id, symbol, entry_order_id, entry_price,
                 quantity, opened_at, now),
            )
            self._adjust_cash(cash_delta=-(entry_price * quantity), now=now)
            return cur.lastrowid

    def list_open_lots(self, *, strategy_version_id: str, symbol: str) -> list[dict]:
        """Open lots for one (version, symbol), oldest first — the FIFO
        order a SELL signal closes them in. See paper/portfolio.py."""
        rows = self._conn.execute(
            "SELECT * FROM paper_lots WHERE strategy_version_id=? AND symbol=? "
            "AND status='OPEN' ORDER BY id ASC",
            (strategy_version_id, symbol),
        ).fetchall()
        return [dict(r) for r in rows]

    def all_open_lots(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM paper_lots WHERE status='OPEN' ORDER BY id ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    def close_lot(self, lot_id: int, *, exit_order_id: str, exit_price: float,
                 closed_at: str, gross_pnl: float, costs: float, net_pnl: float,
                 exit_reason: str, now: str) -> dict:
        """Close one open lot, write its immutable paper_trades row, and
        credit paper cash by (exit proceeds - costs) — all in one
        transaction. Raises PaperStoreError if the lot does not exist or is
        already closed (a programming error upstream, never a normal
        business outcome)."""
        with self._transaction():
            row = self._conn.execute(
                "SELECT * FROM paper_lots WHERE id=?", (lot_id,)).fetchone()
            if row is None:
                raise PaperStoreError(f"no such paper lot: {lot_id}")
            lot = dict(row)
            if lot["status"] != "OPEN":
                raise PaperStoreError(
                    f"paper lot {lot_id} is not OPEN (status={lot['status']!r}) — "
                    f"cannot close it twice")

            self._conn.execute(
                "UPDATE paper_lots SET status='CLOSED', closed_at=?, "
                "exit_order_id=?, exit_price=? WHERE id=?",
                (closed_at, exit_order_id, exit_price, lot_id),
            )

            paper_trade_id = make_trade_id(
                entry_order_id=lot["entry_order_id"], exit_order_id=exit_order_id)
            self._conn.execute(
                "INSERT INTO paper_trades "
                "(paper_trade_id, lot_id, strategy_id, strategy_version_id, symbol, "
                " entry_order_id, exit_order_id, quantity, entry_price, exit_price, "
                " opened_at, closed_at, gross_pnl, costs, net_pnl, exit_reason, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (paper_trade_id, lot_id, lot["strategy_id"], lot["strategy_version_id"],
                 lot["symbol"], lot["entry_order_id"], exit_order_id, lot["quantity"],
                 lot["entry_price"], exit_price, lot["opened_at"], closed_at,
                 gross_pnl, costs, net_pnl, exit_reason, now),
            )

            self._adjust_cash(
                cash_delta=(exit_price * lot["quantity"]) - costs,
                realized_pnl_delta=net_pnl, costs_delta=costs, now=now,
            )
            return self.get_trade(paper_trade_id)

    def get_trade(self, paper_trade_id: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM paper_trades WHERE paper_trade_id=?", (paper_trade_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_trades(self, *, strategy_version_id: Optional[str] = None,
                    limit: Optional[int] = None) -> list[dict]:
        sql = "SELECT * FROM paper_trades"
        args: list[Any] = []
        if strategy_version_id:
            sql += " WHERE strategy_version_id=?"
            args.append(strategy_version_id)
        sql += " ORDER BY closed_at DESC, id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            args.append(limit)
        return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    def list_positions(self) -> list[dict]:
        """Aggregate open lots into one row per (strategy_version_id,
        symbol): total quantity, quantity-weighted average entry price, and
        the earliest lot's opened_at. Unrealized P&L is left for the caller
        to compute against paper_marks (see paper/portfolio.py.
        performance_summary / api paper endpoints) — this method is a pure
        read of paper_lots, nothing else."""
        rows = self._conn.execute(
            "SELECT strategy_id, strategy_version_id, symbol, "
            "SUM(quantity) AS quantity, "
            "SUM(quantity * entry_price) AS cost_basis, "
            "MIN(opened_at) AS opened_at, COUNT(*) AS lot_count "
            "FROM paper_lots WHERE status='OPEN' "
            "GROUP BY strategy_version_id, symbol "
            "ORDER BY strategy_version_id, symbol"
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            qty = d["quantity"] or 0
            d["avg_entry_price"] = (d["cost_basis"] / qty) if qty else None
            out.append(d)
        return out

    # -- marks ---------------------------------------------------------------

    def set_mark(self, symbol: str, price: float, as_of: str, now: str) -> None:
        self._conn.execute(
            "INSERT INTO paper_marks(symbol, price, as_of, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(symbol) DO UPDATE SET price=excluded.price, "
            "as_of=excluded.as_of, updated_at=excluded.updated_at",
            (symbol, price, as_of, now),
        )

    def get_marks(self) -> dict[str, dict]:
        rows = self._conn.execute("SELECT * FROM paper_marks").fetchall()
        return {r["symbol"]: dict(r) for r in rows}

    # -- cycles (idempotency) -------------------------------------------------

    def get_cycle(self, cycle_id: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM paper_cycles WHERE cycle_id=?", (cycle_id,)).fetchone()
        return dict(row) if row else None

    def start_cycle(self, cycle_id: str, now: str) -> dict:
        """INSERT OR IGNORE — if a row for this cycle_id already exists
        (any status), this is a no-op and the EXISTING row is returned, so
        the caller (paper/runner.py) can see it was already RUNNING/
        COMPLETED/FAILED and decide what to do rather than silently
        clobbering it."""
        self._conn.execute(
            "INSERT OR IGNORE INTO paper_cycles(cycle_id, started_at, status) "
            "VALUES (?, ?, 'RUNNING')", (cycle_id, now),
        )
        return self.get_cycle(cycle_id)

    def complete_cycle(self, cycle_id: str, summary: dict, now: str) -> None:
        self._conn.execute(
            "UPDATE paper_cycles SET status='COMPLETED', completed_at=?, "
            "summary_json=? WHERE cycle_id=?",
            (now, json.dumps(summary, default=str, sort_keys=True), cycle_id),
        )

    def fail_cycle(self, cycle_id: str, note: str, now: str) -> None:
        self._conn.execute(
            "UPDATE paper_cycles SET status='FAILED', completed_at=?, note=? "
            "WHERE cycle_id=?",
            (now, note, cycle_id),
        )

    def list_cycles(self, *, limit: Optional[int] = 20) -> list[dict]:
        sql = "SELECT * FROM paper_cycles ORDER BY started_at DESC"
        args: list[Any] = []
        if limit is not None:
            sql += " LIMIT ?"
            args.append(limit)
        return [dict(r) for r in self._conn.execute(sql, args).fetchall()]


def _now_placeholder() -> str:
    """Account creation (in _ensure_account, called from open()) happens
    before any caller has a chance to hand this Store a Clock — so it uses
    paper.clock.system_clock() directly, the one place in this module a
    live wall-clock read is unavoidable (it is timestamping "when was this
    paper account first created on disk", a one-time bookkeeping fact, not
    a trading decision). Every other timestamp in this module is supplied
    by the caller (paper/portfolio.py, paper/runner.py), which is where the
    injectable Clock actually lives."""
    from .clock import system_clock
    return system_clock().isoformat(timespec="seconds")
