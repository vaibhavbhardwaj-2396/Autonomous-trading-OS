"""
api/data.py — the read adapters. Every function here is a thin wrapper over
an already-authoritative interface elsewhere in the repository; none of them
compute a statistic, a risk figure, or a classification that doesn't already
exist somewhere else. See each function's docstring for exactly which
existing module it reads through:

    account / positions / risk    -> engine.guardrails (load_state,
                                      status_summary — the same functions
                                      engine.execute itself calls)
    orders / trades                -> engine.journal.TRADES_JSONL, read
                                      directly (the same file
                                      engine.stats.compute() reads)
    regime                         -> engine.journal.REGIME_JSONL, the log
                                      of classifications an actual engine
                                      cycle already ran and recorded — NOT a
                                      live reclassification (that would mean
                                      a market-data fetch on every dashboard
                                      poll)
    research/drafts                -> research.brain.draft_backlog
    research/evidence              -> research.brain.digest's evidence
                                      section (Slice V)
    research/areas                 -> research.brain.research_areas
    strategies                     -> strategies.registry
    backtests                      -> research.memory's research_note log,
                                      filtered to notes
                                      research.experiments.strategy_backtest
                                      itself already records (see that
                                      module's "Evidence integration"
                                      section for why nothing richer than a
                                      completion note is persisted yet)

No function here ever calls engine.execute, engine.broker*, engine.guardrails.
validate_order/save_state, research.brain.hypothesis_intake.approve_and_lock,
research.experiments.runner.run_experiment, research.experiments.
strategy_backtest.run_backtest, strategies.registry.save_version, or any
other function whose job is to change state. Every import below is a
read-only entry point; grep this file for "import" and check each one
against the modules it names if that ever needs re-verifying.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Optional

from engine import guardrails as gr
from engine import journal as jr
from research.store import Store, iso, now_ist
from research import memory as rm
from research.brain import draft_backlog, research_areas
from research.brain import digest as digest_mod
from strategies import registry as sreg

PROJECT_ROOT = Path(__file__).parent.parent


class DataSourceError(RuntimeError):
    """An underlying, already-authoritative data source could not be read
    right now. Carries only a safe, generic public message — never the
    original exception's own text, which can contain a filesystem path or
    other internal detail. The api.app error handler returns this message,
    and only this message, to the client with HTTP 503."""


# ---------------------------------------------------------------------------
# The research Store — one connection cached PER THREAD, not reopened on
# every single request. Store.open() runs an idempotent
# CREATE-TABLE-IF-NOT-EXISTS schema script and a couple of trivial meta
# upserts on every call, which would be pointless repeated work on every
# request of a read-only service — but a raw sqlite3 connection is only
# valid on the thread that created it (Python's sqlite3 module enforces
# this and raises ProgrammingError otherwise), and a real WSGI server
# (Flask's own dev server included — this was found by load-testing this
# exact module with concurrent requests, not a hypothetical) can and does
# serve different requests on different threads. A single process-wide
# Store shared across threads intermittently 500s under concurrent load;
# threading.local() gives each thread its own connection to the same
# on-disk database instead, which is both safe and still avoids reopening
# per request within a given thread.
# ---------------------------------------------------------------------------

_store_local = threading.local()


def get_default_store() -> Store:
    store = getattr(_store_local, "store", None)
    if store is None:
        try:
            store = Store.open()
        except Exception as e:
            raise DataSourceError("the research store is unavailable") from e
        _store_local.store = store
    return store


def reset_default_store_for_testing() -> None:
    """Test-only: drop the cached Store for the CURRENT thread so the next
    get_default_store() call on this thread reopens (e.g. against a
    different path patched in for that test)."""
    _store_local.store = None


# ---------------------------------------------------------------------------
# JSONL readers — memory/trades.jsonl and memory/regime_log.jsonl are both
# append-only, newest-last logs written exclusively by engine.journal.
# record()/record_regime(). This is a deliberately independent, trivial
# re-implementation of the same defensive line-by-line parse
# engine.stats._load() already does, rather than importing that
# underscore-prefixed helper across a module boundary — the same choice
# research/experiments/strategy_backtest.py already made for evaluator.py's
# _t_stat (see that module's docstring for the reasoning). A handful of
# lines of "skip a line that isn't valid JSON" is not business logic worth
# coupling two modules over.
# ---------------------------------------------------------------------------

def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        text = path.read_text()
    except OSError as e:
        raise DataSourceError(f"{path.name} is unavailable") from e
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue  # one malformed line must never hide the rest of the log
        if not isinstance(parsed, dict):
            continue  # a bare JSON string/number/array is not a journal
            # record either — every real line engine.journal writes is a
            # JSON object, so this is still "malformed" for this log's
            # purposes even though it happens to parse as valid JSON.
        rows.append(parsed)
    return rows


# ---------------------------------------------------------------------------
# Account / positions / risk — all read through engine.guardrails, the same
# module engine.execute itself calls before ever placing an order.
# ---------------------------------------------------------------------------

def get_account() -> dict:
    """cash / portfolio_value / total_value / pnl_today, from
    engine.guardrails.load_state() — the exact file engine.execute reads
    and writes. `total_value` is the whole brokerage account (only known
    once a sync has run; None until then) — `portfolio_value` is the
    agent's own mandate only, matching CLAUDE.md's "book value = allocation
    + own realised P&L" distinction."""
    try:
        state = gr.load_state()
    except Exception as e:
        raise DataSourceError("account/portfolio state is unavailable") from e

    day = state.get("day", {}) or {}
    snap = state.get("broker_snapshot", {}) or {}
    return {
        "cash": state.get("cash_available"),
        "portfolio_value": state.get("capital"),
        "total_value": snap.get("total_account_value"),
        "pnl_today": day.get("realized_pnl"),
        "allocated_capital": state.get("allocated_capital"),
        "realized_pnl_alltime": state.get("realized_pnl_alltime"),
        "peak_capital": state.get("peak_capital"),
        "broker_synced_at": snap.get("synced_at"),
        "as_of": state.get("last_updated"),
    }


def get_positions() -> dict:
    """Open positions from the agent's own state — symbol/side/quantity/
    entry/stop/target/open_risk/thesis/opened, exactly as
    engine.journal.add_position() wrote them. current_price and
    unrealized_pnl are deliberately NOT included: the agent's state does
    not track a live mark price, and computing one would mean this
    read-only API calling a live market-data feed on every poll — out of
    scope for v1 (see docs/API.md)."""
    try:
        state = gr.load_state()
    except Exception as e:
        raise DataSourceError("position state is unavailable") from e

    positions = state.get("open_positions", []) or []
    items = [{
        "symbol": p.get("symbol"),
        "side": p.get("side"),
        "quantity": p.get("quantity"),
        "entry_price": p.get("entry"),
        "stop_price": p.get("stop"),
        "target_price": p.get("target"),
        "open_risk": p.get("open_risk"),
        "thesis": p.get("thesis"),
        "opened_at": p.get("opened"),
    } for p in positions]
    return {
        "positions": items,
        "count": len(items),
        "note": "current_price and unrealized_pnl are not tracked by v1 — see docs/API.md",
        "as_of": state.get("last_updated") or iso(now_ist()),
    }


def get_risk() -> dict:
    """engine.guardrails.status_summary() verbatim, plus a response
    timestamp — the exact guardrail/drawdown/tier snapshot the agent itself
    consults before every trade decision. No competing risk model is
    computed here."""
    try:
        summary = gr.status_summary()
    except Exception as e:
        raise DataSourceError("risk/guardrail state is unavailable") from e
    out = dict(summary)
    out["as_of"] = iso(now_ist())
    return out


# ---------------------------------------------------------------------------
# Orders / trades — engine.journal.TRADES_JSONL, the append-only structured
# record engine.stats.compute() itself is built from.
# ---------------------------------------------------------------------------

def get_orders(*, limit: Optional[int] = 100) -> dict:
    """Order-related journal entries (ENTRY: an order that was actually
    placed; REJECTED: a proposal the guardrail gate refused before any
    order reached the broker) — the agent's own record of what it
    attempted, never a live broker orderbook (this API never calls the
    broker; see the module docstring)."""
    rows = _read_jsonl(jr.TRADES_JSONL)
    orders = [r for r in rows if r.get("kind") in ("ENTRY", "REJECTED")]
    orders.sort(key=lambda r: r.get("ts") or "", reverse=True)
    shown = orders[:limit] if limit is not None else orders
    items = [{
        "kind": r.get("kind"),
        "symbol": r.get("symbol"),
        "side": r.get("side"),
        "quantity": r.get("quantity"),
        "entry": r.get("entry"),
        "stop": r.get("stop"),
        "target": r.get("target"),
        "risk_amount": r.get("risk_amount"),
        "order_id": r.get("order_id"),
        "reasons": r.get("reasons"),
        "regime": r.get("regime"),
        "playbook": r.get("playbook"),
        "thesis": r.get("thesis"),
        "ts": r.get("ts"),
    } for r in shown]
    return {
        "orders": items,
        "shown_count": len(items),
        "total_count": len(orders),
        "truncated": len(orders) > len(items),
        "note": "the agent's own order journal, not a live broker orderbook",
        "as_of": iso(now_ist()),
    }


def get_trades(*, limit: Optional[int] = 100) -> dict:
    """Closed trades (EXIT journal entries) — symbol, exit price, pnl,
    r_multiple, reason, holding_days — the same rows
    engine.stats.compute() aggregates, returned here as individual records
    rather than as a computed statistic."""
    rows = _read_jsonl(jr.TRADES_JSONL)
    exits = [r for r in rows if r.get("kind") == "EXIT"]
    exits.sort(key=lambda r: r.get("ts") or "", reverse=True)
    shown = exits[:limit] if limit is not None else exits
    items = [{
        "symbol": r.get("symbol"),
        "exit_price": r.get("exit_price"),
        "pnl": r.get("pnl"),
        "r_multiple": r.get("r_multiple"),
        "regime": r.get("regime"),
        "reason": r.get("reason"),
        "holding_days": r.get("holding_days"),
        "ts": r.get("ts"),
    } for r in shown]
    return {
        "trades": items,
        "shown_count": len(items),
        "total_count": len(exits),
        "truncated": len(exits) > len(items),
        "as_of": iso(now_ist()),
    }


# ---------------------------------------------------------------------------
# Regime — the last LOGGED classification, not a live reclassification.
# ---------------------------------------------------------------------------

def get_regime() -> dict:
    """The most recent entry in engine.journal.REGIME_JSONL — i.e. what an
    actual engine cycle classified the market as, the last time one ran.
    Deliberately does NOT call engine.regime.classify_thresholds() itself:
    that function fetches live market data (engine.market_data.get_history,
    which hits yfinance with no `provider` supplied), which is exactly the
    kind of expensive, network-dependent work this endpoint must not
    trigger on every dashboard poll."""
    rows = _read_jsonl(jr.REGIME_JSONL)
    if not rows:
        return {
            "available": False,
            "reason": "no regime classification has been logged yet by a live engine cycle",
            "as_of": iso(now_ist()),
        }
    latest = rows[-1]
    return {
        "available": True,
        "regime": latest.get("regime"),
        "playbook": latest.get("playbook"),
        "confidence": latest.get("confidence"),
        "evidence": latest.get("evidence"),
        "shadow": latest.get("shadow"),
        "logged_at": latest.get("ts"),
        "note": "the last logged classification from a real engine cycle, not a live read",
        "as_of": iso(now_ist()),
    }


# ---------------------------------------------------------------------------
# Research — drafts / evidence / areas, all through research/brain/*'s own
# existing read-only surfaces.
# ---------------------------------------------------------------------------

def get_research_drafts(
    store: Store, *, limit: Optional[int] = draft_backlog.DEFAULT_BACKLOG_LIMIT,
    registry_dir: Optional[Path] = None,
) -> dict:
    """research.brain.draft_backlog.build_backlog() verbatim — the review
    surface Slice Q already built for exactly this purpose.

    `registry_dir` defaults to draft_backlog.REGISTRY_DIR, looked up fresh
    (not via build_backlog()'s own pre-bound default argument — the same
    "read the module attribute live" fix get_strategies() above already
    needed) so tests can point this at a temporary Contract registry
    without ever touching the real one."""
    directory = registry_dir if registry_dir is not None else draft_backlog.REGISTRY_DIR
    try:
        return draft_backlog.build_backlog(store, limit=limit, registry_dir=directory)
    except Exception as e:
        raise DataSourceError("research draft backlog is unavailable") from e


def get_research_evidence(store: Store, *, limit: Optional[int] = digest_mod.DEFAULT_EVIDENCE_LIMIT) -> dict:
    """The `evidence` section of research.brain.digest.build_digest() — the
    same bounded, deterministic PROMISING/WEAK/INCONCLUSIVE/CONTRADICTED
    summary Slice V built for the Research AI, read here instead of fed to
    a model. No comparison/evaluator logic is reimplemented; this is the
    digest's own output, unpicked to one field."""
    try:
        d = digest_mod.build_digest(store, now_ist(), evidence_limit=limit)
    except Exception as e:
        raise DataSourceError("research evidence is unavailable") from e
    return d["evidence"]


def get_research_areas(store: Store) -> dict:
    """research.brain.research_areas.groups_as_dicts() verbatim."""
    try:
        areas = research_areas.groups_as_dicts(store)
    except Exception as e:
        raise DataSourceError("research areas are unavailable") from e
    return {"areas": areas, "count": len(areas), "as_of": iso(now_ist())}


# ---------------------------------------------------------------------------
# Strategies / backtests
# ---------------------------------------------------------------------------

def get_strategies(*, limit: Optional[int] = None, registry_dir: Optional[Path] = None) -> dict:
    """Every StrategyVersion in strategies/registry/, via
    strategies.registry.list_versions() — the same read-only registry
    listing Slice X's own tests use. Includes derived_from_hypothesis_id
    (the provenance link back to research) wherever it was set.

    `registry_dir` defaults to strategies.registry.REGISTRY_DIR, looked up
    fresh (as a live module attribute, not a pre-bound default argument) so
    tests can point this at a temporary directory without ever touching the
    real registry."""
    directory = registry_dir if registry_dir is not None else sreg.REGISTRY_DIR
    try:
        versions = sreg.list_versions(directory=directory)
    except Exception as e:
        raise DataSourceError("strategy registry is unavailable") from e

    versions = sorted(versions, key=lambda v: v.version_id)
    shown = versions[:limit] if limit is not None else versions
    items = [{
        "strategy_id": v.strategy_id,
        "algorithm_id": v.algorithm_id,
        "version_id": v.version_id,
        "parameters": v.parameters,
        "implementation_source_hash": v.implementation_source_hash,
        "derived_from_hypothesis_id": v.derived_from_hypothesis_id,
    } for v in shown]
    return {
        "strategies": items,
        "shown_count": len(items),
        "total_count": len(versions),
        "truncated": len(versions) > len(items),
        "as_of": iso(now_ist()),
    }


def get_backtests(store: Store, *, limit: Optional[int] = 50) -> dict:
    """Strategy backtest completion notes: research.experiments.
    strategy_backtest.run_backtest() records one research_note per
    completed run (source="research.experiments.strategy_backtest"); this
    reads that existing log rather than creating any new storage. The full
    signal/trade/stat detail of a run is NOT persisted anywhere in this
    repository yet (see strategy_backtest.py's own "Evidence integration"
    docstring section), so it is not available here — this is a documented
    v1 limitation, not an oversight."""
    try:
        rows = rm.query_research_log(store, rm.DATASET_NOTE)
    except Exception as e:
        raise DataSourceError("backtest records are unavailable") from e

    bt_rows = [r for r in rows if r.get("source") == "research.experiments.strategy_backtest"]
    bt_rows.sort(key=lambda r: (r.get("event_ts") or 0, r.get("id") or 0), reverse=True)
    shown = bt_rows[:limit] if limit is not None else bt_rows
    items = [{
        "strategy_id": r["payload"].get("strategy_id"),
        "version_id": r["payload"].get("version_id"),
        "algorithm_id": r["payload"].get("algorithm_id"),
        "n_trades": r["payload"].get("n_trades"),
        "summary": r["payload"].get("note"),
        "recorded_at": r.get("event_time"),
    } for r in shown]
    return {
        "backtests": items,
        "shown_count": len(items),
        "total_count": len(bt_rows),
        "truncated": len(bt_rows) > len(items),
        "note": "completion-note summaries only — full run detail is not persisted yet",
        "as_of": iso(now_ist()),
    }
