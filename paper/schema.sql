-- paper/paper_shadow.db — Slice AA's own storage. Completely separate from
-- research/market_memory.db (RESEARCH) and memory/state.json /
-- memory/trades.jsonl (LIVE) — see paper/__init__.py and
-- docs/PAPER_TRADING.md for the full LIVE / PAPER / RESEARCH boundary.
--
-- Orders and trades are append-only (enforced by trigger, the same pattern
-- research/schema.sql already uses) — a paper fill or a closed paper trade
-- is never rewritten or deleted, only ever added to. Account/lots/marks/
-- cycles are mutable CURRENT-STATE tables, the same role memory/state.json
-- plays for live trading (its own history lives in the append-only tables
-- that reference it, never in the mutable row itself).

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Paper account — one singleton row (id=1). cash decreases when a lot opens
-- and increases when a lot closes (by exit proceeds net of costs);
-- realized_pnl_alltime / total_costs_alltime accumulate exactly the way
-- engine.journal.close_position() accumulates the live equivalents.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS paper_account (
    id                   INTEGER PRIMARY KEY CHECK (id = 1),
    initial_capital      REAL    NOT NULL,
    cash                 REAL    NOT NULL,
    realized_pnl_alltime REAL    NOT NULL DEFAULT 0,
    total_costs_alltime  REAL    NOT NULL DEFAULT 0,
    created_at           TEXT    NOT NULL,
    updated_at           TEXT    NOT NULL
);

-- ---------------------------------------------------------------------------
-- Paper lots — the open/closed position ledger. One row per opened lot
-- (a single BUY fill). A SELL signal closes the OLDEST open lot for that
-- (strategy_version_id, symbol) first (FIFO) — see paper/portfolio.py.
-- Mutated only at close (status/closed_at/exit_order_id/exit_price); every
-- other field is set once, at open, and never touched again.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS paper_lots (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id          TEXT    NOT NULL,
    strategy_version_id  TEXT    NOT NULL,
    symbol               TEXT    NOT NULL,
    entry_order_id       TEXT    NOT NULL,
    entry_price          REAL    NOT NULL,
    quantity             INTEGER NOT NULL,
    opened_at            TEXT    NOT NULL,
    status               TEXT    NOT NULL DEFAULT 'OPEN',   -- OPEN | CLOSED
    closed_at            TEXT,
    exit_order_id        TEXT,
    exit_price           REAL,
    created_at           TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS lots_open_lookup
    ON paper_lots(strategy_version_id, symbol, status, id);

-- ---------------------------------------------------------------------------
-- Paper orders — every signal that reached the fill stage, whatever the
-- outcome (FILLED / REJECTED / SKIPPED). Append-only: an order's own
-- history is never edited after it is written, even a REJECTED one — that
-- rejection is itself the historical record of what the runner decided.
--
-- dedupe_key makes an order idempotent per (cycle, strategy version, symbol,
-- side, the exact signal timestamp) — INSERT OR IGNORE on this key is what
-- makes running the same paper cycle twice a safe no-op the second time
-- (see paper/runner.py and tests/test_paper_runner.py's idempotency tests).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS paper_orders (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_order_id       TEXT    NOT NULL UNIQUE,
    cycle_id             TEXT    NOT NULL,
    strategy_id          TEXT    NOT NULL,
    strategy_version_id  TEXT    NOT NULL,
    symbol               TEXT    NOT NULL,
    side                 TEXT    NOT NULL,             -- BUY | SELL
    requested_quantity   INTEGER,
    signal_generated_at  TEXT    NOT NULL,
    requested_at         TEXT    NOT NULL,
    filled_at            TEXT,
    fill_price           REAL,
    status               TEXT    NOT NULL,             -- FILLED | REJECTED | SKIPPED
    reason               TEXT,
    dedupe_key           TEXT    NOT NULL UNIQUE,
    created_at           TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS orders_strategy_version
    ON paper_orders(strategy_version_id, symbol, created_at);
CREATE INDEX IF NOT EXISTS orders_cycle ON paper_orders(cycle_id);

CREATE TRIGGER IF NOT EXISTS paper_orders_no_update
BEFORE UPDATE ON paper_orders
BEGIN SELECT RAISE(ABORT, 'paper_orders is append-only: it is never rewritten'); END;

CREATE TRIGGER IF NOT EXISTS paper_orders_no_delete
BEFORE DELETE ON paper_orders
BEGIN SELECT RAISE(ABORT, 'paper_orders is append-only: history is never deleted'); END;

-- ---------------------------------------------------------------------------
-- Paper trades — one immutable row per fully-closed lot (a round trip).
-- Written exactly once, when a lot's status flips to CLOSED, and never
-- touched again — the same "write once at close" discipline
-- research/experiments/strategy_backtest.py's SimulatedTrade already
-- follows, persisted here instead of only returned in-memory.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS paper_trades (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_trade_id       TEXT    NOT NULL UNIQUE,
    lot_id               INTEGER NOT NULL REFERENCES paper_lots(id),
    strategy_id          TEXT    NOT NULL,
    strategy_version_id  TEXT    NOT NULL,
    symbol               TEXT    NOT NULL,
    entry_order_id       TEXT    NOT NULL,
    exit_order_id        TEXT    NOT NULL,
    quantity             INTEGER NOT NULL,
    entry_price          REAL    NOT NULL,
    exit_price           REAL    NOT NULL,
    opened_at            TEXT    NOT NULL,
    closed_at            TEXT    NOT NULL,
    gross_pnl            REAL    NOT NULL,
    costs                REAL    NOT NULL,
    net_pnl              REAL    NOT NULL,
    exit_reason          TEXT    NOT NULL,
    created_at           TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS trades_strategy_version
    ON paper_trades(strategy_version_id, symbol, closed_at);

CREATE TRIGGER IF NOT EXISTS paper_trades_no_update
BEFORE UPDATE ON paper_trades
BEGIN SELECT RAISE(ABORT, 'paper_trades is append-only: it is never rewritten'); END;

CREATE TRIGGER IF NOT EXISTS paper_trades_no_delete
BEFORE DELETE ON paper_trades
BEGIN SELECT RAISE(ABORT, 'paper_trades is append-only: history is never deleted'); END;

-- ---------------------------------------------------------------------------
-- Paper marks — the latest known price per symbol, refreshed once per
-- runner cycle (see paper/runner.py) so /paper/positions and
-- /paper/performance can report unrealized P&L WITHOUT the read-only API
-- ever making a live market-data call of its own (the same discipline
-- api/data.py already applies to /regime — see that module's docstring).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS paper_marks (
    symbol       TEXT PRIMARY KEY,
    price        REAL NOT NULL,
    as_of        TEXT NOT NULL,     -- the price bar's own timestamp
    updated_at   TEXT NOT NULL      -- when the runner last refreshed this row
);

-- ---------------------------------------------------------------------------
-- Paper cycles — the idempotency ledger. One row per runner invocation.
-- Looked up by cycle_id BEFORE any signal is generated: a cycle_id already
-- marked COMPLETED makes the runner a safe, cheap no-op that returns the
-- stored summary rather than reprocessing (see paper/runner.py).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS paper_cycles (
    cycle_id      TEXT PRIMARY KEY,
    started_at    TEXT NOT NULL,
    completed_at  TEXT,
    status        TEXT NOT NULL,   -- RUNNING | COMPLETED | FAILED
    summary_json  TEXT,
    note          TEXT
);
