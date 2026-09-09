-- Market Memory — bitemporal, append-only.
--
-- Two clocks on every fact:
--   event_time     when it was true in the world
--   knowledge_time the earliest moment we could have known it
--
-- Every read an experiment makes is gated on knowledge_time, never event_time.
-- That gate is what makes "what did we believe on 3 May 2024" an answerable
-- question rather than a hopeful one.
--
-- Append-only is enforced by triggers below, not by convention. A correction is
-- a NEW row with a later knowledge_time and a `supersedes` pointer. Nothing in
-- this database is ever updated or deleted, because a research store whose
-- history can be quietly rewritten is worth less than no store at all.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Generic observations: announcements, corporate actions, membership,
-- shareholding, news arrivals, agent decision context, LLM judgments.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS observations (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset        TEXT    NOT NULL,
    entity         TEXT    NOT NULL,           -- symbol, index name, or '_market'
    event_time     TEXT    NOT NULL,           -- ISO8601 with offset
    event_ts       INTEGER NOT NULL,           -- unix seconds
    knowledge_time TEXT    NOT NULL,           -- ISO8601 with offset
    knowledge_ts   INTEGER NOT NULL,           -- unix seconds  <-- THE GATE
    confidence     TEXT    NOT NULL DEFAULT 'observed',
                                               -- observed | derived | inferred_floor
    source         TEXT    NOT NULL,
    source_ref     TEXT,                       -- URL / file / row id, for audit
    payload        TEXT    NOT NULL,           -- JSON
    revision       INTEGER NOT NULL DEFAULT 0,
    supersedes     INTEGER REFERENCES observations(id),
    ingested_at    TEXT    NOT NULL,
    dedupe_key     TEXT    NOT NULL
);

-- Re-running a backfill must be a no-op, not a duplicate. A genuine correction
-- has different content, so it produces a different key and lands as a new row.
CREATE UNIQUE INDEX IF NOT EXISTS obs_dedupe    ON observations(dedupe_key);
CREATE INDEX IF NOT EXISTS obs_gate            ON observations(dataset, entity, knowledge_ts);
CREATE INDEX IF NOT EXISTS obs_dataset_gate    ON observations(dataset, knowledge_ts);
CREATE INDEX IF NOT EXISTS obs_event           ON observations(dataset, entity, event_ts);

-- ---------------------------------------------------------------------------
-- Prices get their own narrow, typed table. 4,000 symbols x 2,000 sessions of
-- JSON payloads would be unusably slow, and prices are the one dataset read on
-- every single replay step.
--
-- `adjusted` matters more than it looks. adjusted=0 is the close as printed on
-- the day, which is the only series that was never retroactively rewritten by a
-- later split or bonus. adjusted=1 is a convenience series and is knowingly
-- contaminated with future corporate actions -- experiments must opt into it.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS prices_eod (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol         TEXT    NOT NULL,
    session_date   TEXT    NOT NULL,           -- YYYY-MM-DD
    open           REAL,
    high           REAL,
    low            REAL,
    close          REAL,
    volume         INTEGER,
    traded_value   REAL,
    adjusted       INTEGER NOT NULL DEFAULT 0,
    knowledge_time TEXT    NOT NULL,
    knowledge_ts   INTEGER NOT NULL,
    source         TEXT    NOT NULL,
    ingested_at    TEXT    NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS px_key  ON prices_eod(symbol, session_date, source, adjusted);
CREATE INDEX IF NOT EXISTS px_gate        ON prices_eod(symbol, knowledge_ts);
CREATE INDEX IF NOT EXISTS px_session     ON prices_eod(session_date);

-- ---------------------------------------------------------------------------
-- Append-only enforcement. Not a convention -- a constraint.
--
-- The no-lookahead deletion test operates on a COPY with these triggers dropped;
-- that is the only sanctioned way anything is ever removed.
-- ---------------------------------------------------------------------------

CREATE TRIGGER IF NOT EXISTS observations_no_update
BEFORE UPDATE ON observations
BEGIN SELECT RAISE(ABORT, 'observations is append-only: append a revision instead'); END;

CREATE TRIGGER IF NOT EXISTS observations_no_delete
BEFORE DELETE ON observations
BEGIN SELECT RAISE(ABORT, 'observations is append-only: history is never deleted'); END;

CREATE TRIGGER IF NOT EXISTS prices_no_update
BEFORE UPDATE ON prices_eod
BEGIN SELECT RAISE(ABORT, 'prices_eod is append-only: append a new source row instead'); END;

CREATE TRIGGER IF NOT EXISTS prices_no_delete
BEFORE DELETE ON prices_eod
BEGIN SELECT RAISE(ABORT, 'prices_eod is append-only: history is never deleted'); END;

-- ---------------------------------------------------------------------------
-- Experiment trade-level results — Phase 1 Slice A.
--
-- Written by research/experiments/runner.py (not built in this slice; this
-- slice only adds the table and Store.append_experiment_result). One row per
-- simulated trade a LOCKED Contract's rules produced when walked across a
-- Replay. contract_id refers to research.contracts.Contract.id — not a
-- foreign key, because contracts live as JSON files in research/registry/,
-- not in this database.
--
-- Append-only and idempotent on (contract_id, trade_seq): re-running a
-- runner against the same contract must skip trades it already scored, never
-- duplicate or overwrite one. A contract's result history is exactly as
-- immutable as the contract itself.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS experiment_results (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id    TEXT    NOT NULL,
    trade_seq      INTEGER NOT NULL,
    entity         TEXT    NOT NULL,           -- symbol
    entry_time     TEXT    NOT NULL,
    exit_time      TEXT    NOT NULL,
    entry_price    REAL    NOT NULL,
    exit_price     REAL    NOT NULL,
    quantity       INTEGER NOT NULL,
    gross_pnl      REAL    NOT NULL,
    costs          REAL    NOT NULL,           -- from engine.costs, never estimated
    net_pnl        REAL    NOT NULL,
    r_multiple     REAL,
    exit_reason    TEXT,                       -- 'target' | 'stop' | 'time_exit' | ...
    ingested_at    TEXT    NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS exp_results_key      ON experiment_results(contract_id, trade_seq);
CREATE INDEX        IF NOT EXISTS exp_results_contract ON experiment_results(contract_id);

CREATE TRIGGER IF NOT EXISTS experiment_results_no_update
BEFORE UPDATE ON experiment_results
BEGIN SELECT RAISE(ABORT, 'experiment_results is append-only: history is never rewritten'); END;

CREATE TRIGGER IF NOT EXISTS experiment_results_no_delete
BEFORE DELETE ON experiment_results
BEGIN SELECT RAISE(ABORT, 'experiment_results is append-only: history is never deleted'); END;
