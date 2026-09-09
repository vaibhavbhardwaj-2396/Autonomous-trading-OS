"""
The Market Memory — append-only bitemporal store.

    store = Store.open()
    store.append(dataset="announcement", entity="INFY", ...)

    view = store.view("2023-03-14")     # the ONLY way an experiment reads
    view.prices("INFY", days=200)       # nothing known after 2023-03-14 exists

Design rules, in the order they matter:

1. Nothing is ever updated or deleted. Corrections append a revision. Enforced by
   SQLite triggers, not by discipline.
2. Reads go through a view bound to an `as_of`. The connection is private, so an
   experiment cannot accidentally reach past its own horizon.
3. knowledge_time is compared as a unix integer, never as a string. ISO strings
   with mixed offsets sort lexicographically in ways that are wrong just often
   enough to be dangerous.

This module imports nothing from engine/. The research package depends on the
trading system; the trading system must never depend on it.
"""

from __future__ import annotations

import json
import sqlite3
import hashlib
import datetime as dt
from pathlib import Path
from typing import Any, Iterable, Optional, Union

RESEARCH_ROOT = Path(__file__).parent
SCHEMA_PATH = RESEARCH_ROOT / "schema.sql"
DEFAULT_DB = RESEARCH_ROOT / "market_memory.db"

SCHEMA_VERSION = "1"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

TimeLike = Union[str, dt.date, dt.datetime]


# ---------------------------------------------------------------------------
# Time handling — one function, used everywhere, so there is one place to be
# wrong rather than fifteen.
# ---------------------------------------------------------------------------

def to_dt(value: TimeLike, *, end_of_day: bool = False) -> dt.datetime:
    """Coerce anything time-shaped into a timezone-aware datetime in IST.

    A bare date is ambiguous: as an `as_of` it should mean end of that day, as an
    event it should mean the start. Callers say which; there is no default that
    is right in both directions.
    """
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=IST)
    if isinstance(value, dt.date):
        t = dt.time(23, 59, 59) if end_of_day else dt.time(0, 0, 0)
        return dt.datetime.combine(value, t, tzinfo=IST)
    if isinstance(value, str):
        s = value.strip().replace("Z", "+00:00")
        date_only = len(s) == 10
        try:
            parsed = dt.datetime.fromisoformat(s)
        except ValueError:
            parsed = dt.datetime.fromisoformat(s[:10])
            date_only = True
        if date_only and end_of_day:
            parsed = parsed.replace(hour=23, minute=59, second=59)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=IST)
    raise TypeError(f"Cannot interpret {value!r} as a time")


def ts(value: TimeLike, *, end_of_day: bool = False) -> int:
    return int(to_dt(value, end_of_day=end_of_day).timestamp())


def iso(value: TimeLike, *, end_of_day: bool = False) -> str:
    return to_dt(value, end_of_day=end_of_day).isoformat(timespec="seconds")


def now_ist() -> dt.datetime:
    return dt.datetime.now(IST)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class AppendOnlyViolation(RuntimeError):
    pass


class Store:
    """Owns the connection. Hands out views, not cursors."""

    def __init__(self, conn: sqlite3.Connection, path: Path) -> None:
        self._conn = conn
        self._conn.row_factory = sqlite3.Row
        self.path = path

    # -- lifecycle -----------------------------------------------------------

    @classmethod
    def open(cls, path: Optional[Path] = None) -> "Store":
        path = Path(path or DEFAULT_DB)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), isolation_level=None)
        conn.executescript(SCHEMA_PATH.read_text())
        store = cls(conn, path)
        store._set_meta("schema_version", SCHEMA_VERSION)
        store._set_meta("created_at", store._get_meta("created_at") or iso(now_ist()))
        return store

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- meta ----------------------------------------------------------------

    def _set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    def _get_meta(self, key: str) -> Optional[str]:
        row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    # -- writes --------------------------------------------------------------

    @staticmethod
    def _dedupe_key(dataset: str, entity: str, event_time: str, knowledge_time: str,
                    source: str, payload: str) -> str:
        blob = "\x1f".join([dataset, entity, event_time, knowledge_time, source, payload])
        return hashlib.sha256(blob.encode()).hexdigest()

    def append(
        self,
        dataset: str,
        entity: str,
        event_time: TimeLike,
        knowledge_time: TimeLike,
        source: str,
        payload: dict,
        source_ref: Optional[str] = None,
        confidence: str = "observed",
        supersedes: Optional[int] = None,
        revision: int = 0,
    ) -> Optional[int]:
        """Append one observation. Returns the new row id, or None if this exact
        fact was already stored — which makes every backfill safely re-runnable.

        knowledge_time is the caller's responsibility and is the single most
        consequential field in the database. Getting it wrong does not raise; it
        produces a study that quietly cheats. Each source module documents its
        own rule.
        """
        ev_iso, kn_iso = iso(event_time), iso(knowledge_time)
        blob = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
        key = self._dedupe_key(dataset, entity, ev_iso, kn_iso, source, blob)

        cur = self._conn.execute(
            "INSERT OR IGNORE INTO observations "
            "(dataset, entity, event_time, event_ts, knowledge_time, knowledge_ts, "
            " confidence, source, source_ref, payload, revision, supersedes, "
            " ingested_at, dedupe_key) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (dataset, entity, ev_iso, ts(event_time), kn_iso, ts(knowledge_time),
             confidence, source, source_ref, blob, revision, supersedes,
             iso(now_ist()), key),
        )
        return cur.lastrowid if cur.rowcount else None

    def append_many(self, rows: Iterable[dict]) -> int:
        """Bulk append. Returns the number of genuinely new rows."""
        n = 0
        self._conn.execute("BEGIN")
        try:
            for r in rows:
                if self.append(**r) is not None:
                    n += 1
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return n

    def append_price(
        self,
        symbol: str,
        session_date: TimeLike,
        knowledge_time: TimeLike,
        source: str,
        open_: Optional[float] = None,
        high: Optional[float] = None,
        low: Optional[float] = None,
        close: Optional[float] = None,
        volume: Optional[int] = None,
        traded_value: Optional[float] = None,
        adjusted: bool = False,
    ) -> Optional[int]:
        sd = to_dt(session_date).date().isoformat()
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO prices_eod "
            "(symbol, session_date, open, high, low, close, volume, traded_value, "
            " adjusted, knowledge_time, knowledge_ts, source, ingested_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (symbol.upper(), sd, open_, high, low, close, volume, traded_value,
             1 if adjusted else 0, iso(knowledge_time), ts(knowledge_time),
             source, iso(now_ist())),
        )
        return cur.lastrowid if cur.rowcount else None

    def append_prices(self, rows: Iterable[dict]) -> int:
        n = 0
        self._conn.execute("BEGIN")
        try:
            for r in rows:
                if self.append_price(**r) is not None:
                    n += 1
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return n

    # -- experiment results ---------------------------------------------------
    # Phase 1 Slice A. Written by research/experiments/runner.py (not built
    # yet — this slice adds only the storage). Not gated through AsOfView: a
    # result row is the OUTPUT of an already-run, already-as-of-safe replay,
    # not raw market data that could itself leak future information.

    def append_experiment_result(
        self,
        contract_id: str,
        trade_seq: int,
        entity: str,
        entry_time: TimeLike,
        exit_time: TimeLike,
        entry_price: float,
        exit_price: float,
        quantity: int,
        gross_pnl: float,
        costs: float,
        net_pnl: float,
        r_multiple: Optional[float] = None,
        exit_reason: Optional[str] = None,
    ) -> Optional[int]:
        """One simulated trade from a locked Contract's rules, replayed.

        Idempotent on (contract_id, trade_seq) — returns None and writes
        nothing if this trade_seq for this contract already has a result.
        """
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO experiment_results "
            "(contract_id, trade_seq, entity, entry_time, exit_time, entry_price, "
            " exit_price, quantity, gross_pnl, costs, net_pnl, r_multiple, "
            " exit_reason, ingested_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (contract_id, int(trade_seq), entity.upper(), iso(entry_time), iso(exit_time),
             float(entry_price), float(exit_price), int(quantity), float(gross_pnl),
             float(costs), float(net_pnl), r_multiple, exit_reason, iso(now_ist())),
        )
        return cur.lastrowid if cur.rowcount else None

    def experiment_results(self, contract_id: str) -> list[dict]:
        """All simulated trades for one contract, oldest first. evaluator.py
        (not built yet) reads through this rather than raw SQL, so the query
        lives in one place."""
        rows = self._conn.execute(
            "SELECT * FROM experiment_results WHERE contract_id = ? "
            "ORDER BY trade_seq ASC",
            (contract_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- reads ---------------------------------------------------------------

    def view(self, as_of: TimeLike) -> "AsOfView":
        """The only sanctioned read path. Everything downstream of this call is
        blind to anything the world had not published by `as_of`."""
        return AsOfView(self._conn, to_dt(as_of, end_of_day=True))

    def stats(self) -> dict:
        c = self._conn
        obs = c.execute(
            "SELECT dataset, COUNT(*) n, MIN(event_time) lo, MAX(event_time) hi "
            "FROM observations GROUP BY dataset ORDER BY n DESC").fetchall()
        px = c.execute(
            "SELECT COUNT(*) n, COUNT(DISTINCT symbol) syms, "
            "MIN(session_date) lo, MAX(session_date) hi FROM prices_eod").fetchone()
        return {
            "path": str(self.path),
            "schema_version": self._get_meta("schema_version"),
            "observations": [dict(r) for r in obs],
            "observations_total": sum(r["n"] for r in obs),
            "prices": dict(px) if px else {},
        }

    # -- test support --------------------------------------------------------

    def _unsafe_connection(self) -> sqlite3.Connection:
        """Private. Used only by backfill loaders and the no-lookahead test
        harness, which needs to build a truncated copy. Experiments never touch
        this — if an experiment can reach the connection, the as_of gate is
        advisory rather than structural."""
        return self._conn

    def truncated_snapshot(self, as_of: TimeLike, dest: Path) -> "Store":
        """A physical copy with every row published after `as_of` deleted.

        This exists for one purpose: the no-lookahead deletion test. Run a
        computation against this and against the full store at the same `as_of`
        and compare. If the outputs differ, the computation used data it should
        not have been able to see — and unlike a code review, this notices
        whether or not anyone was paying attention.

        Note it uses SQLite's backup API rather than copying the file. In WAL
        mode a plain file copy can miss everything sitting in the -wal sidecar,
        producing an empty database that then silently "passes" every test.
        """
        dest = Path(dest)
        if dest.exists():
            dest.unlink()
        for sidecar in (dest.with_suffix(dest.suffix + "-wal"),
                        dest.with_suffix(dest.suffix + "-shm")):
            if sidecar.exists():
                sidecar.unlink()

        target = sqlite3.connect(str(dest), isolation_level=None)
        self._conn.backup(target)

        for table, prefix in (("observations", "observations"), ("prices_eod", "prices")):
            for op in ("update", "delete"):
                target.execute(f"DROP TRIGGER IF EXISTS {prefix}_no_{op}")

        gate = ts(as_of, end_of_day=True)
        target.execute("DELETE FROM observations WHERE knowledge_ts > ?", (gate,))
        target.execute("DELETE FROM prices_eod WHERE knowledge_ts > ?", (gate,))
        target.commit()
        target.close()
        return Store.open(dest)


# ---------------------------------------------------------------------------
# The as-of view
# ---------------------------------------------------------------------------

class AsOfView:
    """A read handle frozen at one instant.

    Every query this object issues carries `knowledge_ts <= as_of`. There is no
    method that returns anything else, and there is no accessor for the
    underlying connection. That is the structural half of the no-lookahead
    guarantee; the deletion test in tests/test_no_lookahead.py is the half that
    can actually fail.
    """

    __slots__ = ("_conn", "as_of", "_gate")

    def __init__(self, conn: sqlite3.Connection, as_of: dt.datetime) -> None:
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "as_of", as_of)
        object.__setattr__(self, "_gate", int(as_of.timestamp()))

    def __repr__(self) -> str:
        return f"<AsOfView as_of={self.as_of.isoformat()}>"

    # -- observations --------------------------------------------------------

    def observations(
        self,
        dataset: str,
        entity: Optional[str] = None,
        event_from: Optional[TimeLike] = None,
        event_to: Optional[TimeLike] = None,
        latest_only: bool = True,
        limit: Optional[int] = None,
    ) -> list[dict]:
        """Observations visible at as_of.

        latest_only keeps the most recent revision *that was visible at as_of* —
        not the most recent revision that exists. A correction filed after as_of
        is invisible, which is the entire point: on that date, we believed the
        earlier number.
        """
        sql = ["SELECT * FROM observations WHERE dataset = ? AND knowledge_ts <= ?"]
        args: list[Any] = [dataset, self._gate]

        if entity:
            sql.append("AND entity = ?")
            args.append(entity)
        if event_from:
            sql.append("AND event_ts >= ?")
            args.append(ts(event_from))
        if event_to:
            sql.append("AND event_ts <= ?")
            args.append(ts(event_to, end_of_day=True))

        sql.append("ORDER BY event_ts ASC, knowledge_ts ASC, id ASC")
        if limit and not latest_only:
            sql.append("LIMIT ?")
            args.append(int(limit))

        rows = [self._row(r) for r in self._conn.execute(" ".join(sql), args)]

        if latest_only:
            newest: dict[tuple, dict] = {}
            for r in rows:
                k = (r["entity"], r["event_time"])
                prev = newest.get(k)
                if prev is None or r["knowledge_ts"] >= prev["knowledge_ts"]:
                    newest[k] = r
            rows = sorted(newest.values(), key=lambda r: (r["event_ts"], r["id"]))
            if limit:
                rows = rows[: int(limit)]
        return rows

    def latest(self, dataset: str, entity: str) -> Optional[dict]:
        rows = self.observations(dataset, entity)
        return rows[-1] if rows else None

    @staticmethod
    def _row(r: sqlite3.Row) -> dict:
        d = dict(r)
        try:
            d["payload"] = json.loads(d["payload"])
        except (json.JSONDecodeError, TypeError):
            pass
        return d

    # -- prices --------------------------------------------------------------

    def prices(
        self,
        symbol: str,
        days: Optional[int] = None,
        start: Optional[TimeLike] = None,
        end: Optional[TimeLike] = None,
        adjusted: bool = False,
        source: Optional[str] = None,
    ) -> list[dict]:
        sql = ["SELECT * FROM prices_eod WHERE symbol = ? AND knowledge_ts <= ? "
               "AND adjusted = ?"]
        args: list[Any] = [symbol.upper(), self._gate, 1 if adjusted else 0]

        if source:
            sql.append("AND source = ?")
            args.append(source)
        if start:
            sql.append("AND session_date >= ?")
            args.append(to_dt(start).date().isoformat())
        if end:
            sql.append("AND session_date <= ?")
            args.append(to_dt(end).date().isoformat())

        sql.append("ORDER BY session_date DESC")
        if days:
            sql.append("LIMIT ?")
            args.append(int(days))

        rows = [dict(r) for r in self._conn.execute(" ".join(sql), args)]
        return list(reversed(rows))

    def history(self, symbol: str, days: int = 260, adjusted: bool = False):
        """A DataFrame shaped exactly like engine.market_data.get_history returns:
        DatetimeIndex, columns open/high/low/close/volume.

        This is the seam. Point `get_history(..., as_of=...)` at this and the
        entire existing indicator stack — rsi, adx, atr, macd, the screener, the
        regime classifier — runs unmodified on replayed data.
        """
        import pandas as pd

        rows = self.prices(symbol, days=days, adjusted=adjusted)
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        df["session_date"] = pd.to_datetime(df["session_date"])
        df = df.set_index("session_date")[["open", "high", "low", "close", "volume"]]
        return df.dropna(how="all")

    def universe(self, index_name: str, dataset: str = "index_membership") -> list[str]:
        """Point-in-time index constituents.

        Membership carries two dates that differ by weeks: the effective date
        (event_time) and the press-release date (knowledge_time). Both gates are
        applied — a stock counts as a member only if it was effective by as_of
        AND the change had been published by as_of.
        """
        rows = self._conn.execute(
            "SELECT entity, payload FROM observations "
            "WHERE dataset = ? AND knowledge_ts <= ? AND event_ts <= ? "
            "ORDER BY event_ts ASC",
            (dataset, self._gate, self._gate),
        ).fetchall()

        live: set[str] = set()
        for r in rows:
            p = json.loads(r["payload"])
            if p.get("index_name") != index_name:
                continue
            valid_to = p.get("valid_to")
            still_in = (not valid_to) or ts(valid_to) > self._gate
            if still_in:
                live.add(p["symbol"])
            else:
                live.discard(p["symbol"])
        return sorted(live)
