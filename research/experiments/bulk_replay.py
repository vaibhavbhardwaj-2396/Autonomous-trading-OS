"""Bounded bulk execution for deterministic price-rule experiments.

The legacy replay asks SQLite for one symbol at one session thousands of
times.  This module resolves the exact point-in-time universe first, fetches
only the contract's symbols/date range, computes supported price features in
compact arrays, then runs the unchanged position lifecycle locally.

It deliberately refuses (and lets the caller use the legacy path) when price
history is not canonical immutable EOD data: duplicate symbol/session rows,
late-arriving rows, or event-frequency conditions.  That makes the fast path
an equivalence-preserving optimization, never a guess about revision meaning.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import resource
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from ..brain.observatory import MIN_OBSERVATIONS, WINDOW_DAYS
from ..contracts import Contract
from ..replay import DEFAULT_AS_OF_TIME
from ..store import IST, Store, to_dt, ts

FEATURE_VERSION = "bulk_price_features_v1"
BATCH_SIZE = 400


@dataclass
class BulkResult:
    trades: list[dict]
    profile: dict


class BulkReplayUnsupported(RuntimeError):
    """Dataset/contract needs the exact legacy bitemporal replay path."""


def _rss_bytes() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if value > 10_000_000 else value * 1024)


def _gate(day: str) -> int:
    parsed = dt.date.fromisoformat(day)
    return int(dt.datetime.combine(parsed, DEFAULT_AS_OF_TIME, tzinfo=IST).timestamp())


def _timed(profile: dict, stage: str, started: float) -> None:
    profile["stages"][stage] = round(time.perf_counter() - started, 6)


def _trading_days(store: Store, start: str, end: str) -> list[str]:
    rows = store._unsafe_connection().execute(
        "SELECT DISTINCT session_date FROM prices_eod "
        "WHERE adjusted=0 AND session_date>=? AND session_date<=? "
        "ORDER BY session_date", (start[:10], end[:10])).fetchall()
    return [str(r[0]) for r in rows]


def _membership_rows(store: Store, end_gate: int) -> list[dict]:
    rows = store._unsafe_connection().execute(
        "SELECT entity,event_ts,knowledge_ts,payload FROM observations "
        "WHERE dataset='index_membership' AND knowledge_ts<=? AND event_ts<=? "
        "ORDER BY event_ts ASC, knowledge_ts ASC, id ASC", (end_gate, end_gate),
    ).fetchall()
    result = []
    for row in rows:
        payload = json.loads(row["payload"])
        result.append({"entity": row["entity"], "event_ts": int(row["event_ts"]),
                       "knowledge_ts": int(row["knowledge_ts"]), "payload": payload,
                       "valid_to_ts": ts(payload["valid_to"]) if payload.get("valid_to") else None})
    return result


def _resolve_universes(store: Store, contract: Contract,
                       days: list[str]) -> tuple[list[list[str]], int]:
    if contract.universe == "watchlist":
        from engine.watchlist import UNIVERSE
        symbols = list(dict.fromkeys(UNIVERSE))
        return [symbols for _ in days], 0
    rows = _membership_rows(store, _gate(days[-1])) if days else []
    relevant = [r for r in rows if r["payload"].get("index_name") == contract.universe]
    universes = []
    for day in days:
        gate = _gate(day)
        live: set[str] = set()
        for row in relevant:
            if row["event_ts"] > gate or row["knowledge_ts"] > gate:
                continue
            symbol = row["payload"].get("symbol") or row["entity"]
            if row["valid_to_ts"] is None or row["valid_to_ts"] > gate:
                live.add(symbol)
            else:
                live.discard(symbol)
        universes.append(sorted(live))
    return universes, len(relevant)


def _required_rows(entry_rule: dict) -> int:
    needed = 2
    for condition in entry_rule["conditions"]:
        window = int(condition.get("window_days") or WINDOW_DAYS)
        if condition["metric"] == "volume_zscore":
            needed = max(needed, window + 1)
        elif condition["metric"] == "price_move_zscore":
            needed = max(needed, window + 2)
    return needed


def _load_start(store: Store, start: str, required_rows: int) -> str:
    rows = store._unsafe_connection().execute(
        "SELECT DISTINCT session_date FROM prices_eod "
        "WHERE adjusted=0 AND session_date<? AND knowledge_ts<=? "
        "ORDER BY session_date DESC LIMIT ?", (start[:10], _gate(start[:10]), required_rows),
    ).fetchall()
    return str(rows[-1][0]) if rows else start[:10]


def _load_prices(store: Store, symbols: list[str], start: str, end: str) -> tuple[list[dict], int]:
    rows: list[dict] = []
    query_count = 0
    conn = store._unsafe_connection()
    for offset in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[offset:offset + BATCH_SIZE]
        marks = ",".join("?" for _ in batch)
        sql = (
            "SELECT symbol,session_date,open,high,low,close,volume,knowledge_ts "
            "FROM prices_eod INDEXED BY px_replay_lookup "
            f"WHERE symbol IN ({marks}) AND adjusted=0 "
            "AND session_date>=? AND session_date<=? AND knowledge_ts<=? "
            "ORDER BY symbol,session_date"
        )
        args = [*batch, start, end, _gate(end)]
        rows.extend(dict(r) for r in conn.execute(sql, args).fetchall())
        query_count += 1
    return rows, query_count


def _canonical_or_raise(rows: list[dict]) -> None:
    seen = set()
    for row in rows:
        key = (row["symbol"], row["session_date"])
        if key in seen:
            raise BulkReplayUnsupported("duplicate symbol/session price rows require legacy replay")
        seen.add(key)
        if int(row["knowledge_ts"]) > _gate(str(row["session_date"])):
            raise BulkReplayUnsupported("late-arriving price rows require legacy replay")


def _rolling_z(values: np.ndarray, window: int, *, first_index: int) -> np.ndarray:
    out = np.full(len(values), np.nan, dtype=np.float64)
    finite = np.isfinite(values)
    clean = np.where(finite, values, 0.0)
    sums = np.concatenate(([0.0], np.cumsum(clean)))
    squares = np.concatenate(([0.0], np.cumsum(clean * clean)))
    counts = np.concatenate(([0], np.cumsum(finite.astype(np.int64))))
    for i in range(first_index, len(values)):
        lo = max(first_index, i - window)
        n = i - lo
        if n < MIN_OBSERVATIONS or not finite[i] or counts[i] - counts[lo] != n:
            continue
        mean = (sums[i] - sums[lo]) / n
        variance = max(0.0, (squares[i] - squares[lo]) / n - mean * mean)
        std = math.sqrt(variance)
        if std > 1e-12:
            out[i] = (values[i] - mean) / std
    return out


def _symbol_features(rows: list[dict], conditions: list[dict]) -> dict[str, np.ndarray]:
    close = np.asarray([r["close"] if r["close"] is not None else np.nan for r in rows], dtype=np.float64)
    volume = np.asarray([r["volume"] if r["volume"] is not None else np.nan for r in rows], dtype=np.float64)
    returns = np.full(len(rows), np.nan, dtype=np.float64)
    if len(rows) > 1:
        valid = np.isfinite(close[1:]) & np.isfinite(close[:-1]) & (close[:-1] != 0)
        returns[1:][valid] = (close[1:][valid] - close[:-1][valid]) / close[:-1][valid]
    result = {"close": close, "return_1d": returns}
    for condition in conditions:
        metric = condition["metric"]
        window = int(condition.get("window_days") or WINDOW_DAYS)
        key = f"{metric}:{window}"
        if key in result:
            continue
        if metric == "volume_zscore":
            result[key] = _rolling_z(volume, window, first_index=0)
        elif metric == "price_move_zscore":
            # Index zero has no return. Baselines begin with return[1].
            result[key] = _rolling_z(returns, window, first_index=1)
    return result


def _forward_fill(matrix: np.ndarray) -> np.ndarray:
    if not matrix.size:
        return matrix
    mask = np.isnan(matrix)
    indices = np.where(~mask, np.arange(matrix.shape[0])[:, None], 0)
    np.maximum.accumulate(indices, axis=0, out=indices)
    return matrix[indices, np.arange(matrix.shape[1])[None, :]]


def _operator(values: np.ndarray, op: str, target: float) -> np.ndarray:
    if op == ">": return values > target
    if op == ">=": return values >= target
    if op == "<": return values < target
    if op == "<=": return values <= target
    if op == "==": return values == target
    if op == "!=": return values != target
    raise BulkReplayUnsupported(f"unsupported operator {op!r}")


def simulate(contract: Contract, store: Store) -> BulkResult:
    wall_started, cpu_started = time.perf_counter(), time.process_time()
    profile = {"engine": FEATURE_VERSION, "stages": {}, "db_query_count": 0,
               "rows_processed": 0, "symbols_processed": 0, "cache_hits": 0,
               "cache_misses": 0, "temporary_allocation_bytes": 0}
    entry_rule = json.loads(contract.entry_rule)
    exit_rule = json.loads(contract.exit_rule)
    conditions = entry_rule["conditions"]
    if any(c["metric"] == "event_frequency_zscore" for c in conditions):
        raise BulkReplayUnsupported("event-frequency joins are not yet bulk-equivalent")

    started = time.perf_counter()
    days = _trading_days(store, str(contract.evaluation_start), str(contract.evaluation_end))
    profile["db_query_count"] += 1
    _timed(profile, "trading_calendar", started)
    if not days:
        profile.update({"wall_seconds": time.perf_counter() - wall_started,
                        "cpu_seconds": time.process_time() - cpu_started,
                        "peak_rss_bytes": _rss_bytes()})
        return BulkResult([], profile)

    started = time.perf_counter()
    universes, membership_rows = _resolve_universes(store, contract, days)
    profile["db_query_count"] += 0 if contract.universe == "watchlist" else 1
    _timed(profile, "pit_universe_resolution", started)
    symbols = sorted({symbol for universe in universes for symbol in universe})
    profile["symbols_processed"] = len(symbols)
    profile["membership_rows"] = membership_rows

    started = time.perf_counter()
    required = _required_rows(entry_rule)
    load_start = _load_start(store, days[0], required)
    profile["db_query_count"] += 1
    rows, queries = _load_prices(store, symbols, load_start, days[-1])
    profile["db_query_count"] += queries
    _canonical_or_raise(rows)
    profile["rows_processed"] = len(rows)
    profile["estimated_bytes_read"] = sum(
        64 + len(str(r["symbol"])) + len(str(r["session_date"])) for r in rows)
    _timed(profile, "bulk_price_retrieval", started)

    started = time.perf_counter()
    all_days = sorted({str(r["session_date"]) for r in rows} | set(days))
    day_index = {day: i for i, day in enumerate(all_days)}
    symbol_index = {symbol: i for i, symbol in enumerate(symbols)}
    shape = (len(all_days), len(symbols))
    close = np.full(shape, np.nan); high = np.full(shape, np.nan); low = np.full(shape, np.nan)
    feature_matrices: dict[str, np.ndarray] = {
        "return_1d": np.full(shape, np.nan), "close": close}
    for condition in conditions:
        key = (condition["metric"] if condition["metric"] in ("close", "return_1d")
               else f"{condition['metric']}:{int(condition.get('window_days') or WINDOW_DAYS)}")
        feature_matrices.setdefault(key, np.full(shape, np.nan))
    grouped: dict[str, list[dict]] = {symbol: [] for symbol in symbols}
    for row in rows:
        if row["symbol"] in grouped:
            grouped[row["symbol"]].append(row)
    for symbol, symbol_rows in grouped.items():
        j = symbol_index[symbol]
        features = _symbol_features(symbol_rows, conditions)
        for k, row in enumerate(symbol_rows):
            i = day_index[str(row["session_date"])]
            close[i, j] = row["close"] if row["close"] is not None else np.nan
            high[i, j] = row["high"] if row["high"] is not None else np.nan
            low[i, j] = row["low"] if row["low"] is not None else np.nan
            feature_matrices["return_1d"][i, j] = features["return_1d"][k]
            for key, values in features.items():
                if key in feature_matrices:
                    feature_matrices[key][i, j] = values[k]
    close = _forward_fill(close); high = _forward_fill(high); low = _forward_fill(low)
    feature_matrices["close"] = close
    for key in list(feature_matrices):
        if key != "close":
            feature_matrices[key] = _forward_fill(feature_matrices[key])
    eval_rows = np.asarray([day_index[d] for d in days], dtype=np.int64)
    close, high, low = close[eval_rows], high[eval_rows], low[eval_rows]
    for key in list(feature_matrices):
        feature_matrices[key] = feature_matrices[key][eval_rows]
    allocation = close.nbytes + high.nbytes + low.nbytes
    allocation += sum(v.nbytes for k, v in feature_matrices.items() if k != "close")
    profile["temporary_allocation_bytes"] = allocation
    profile["cache_misses"] = len(symbols)
    profile["cache_hits"] = max(0, len(days) * len(symbols) - len(symbols))
    _timed(profile, "feature_construction", started)

    started = time.perf_counter()
    signals = np.ones((len(days), len(symbols)), dtype=bool)
    for condition in conditions:
        metric = condition["metric"]
        key = metric if metric in ("close", "return_1d") else f"{metric}:{int(condition.get('window_days') or WINDOW_DAYS)}"
        values = feature_matrices[key]
        signals &= np.isfinite(values) & _operator(values, condition["op"], condition["value"])
    membership = np.zeros_like(signals)
    for i, universe in enumerate(universes):
        membership[i, [symbol_index[s] for s in universe if s in symbol_index]] = True
    signals &= membership
    _timed(profile, "signal_generation", started)
    profile["stages"]["cross_sectional_calculations"] = 0.0
    profile["stages"]["event_joins"] = 0.0
    profile["stages"]["walk_forward"] = 0.0
    profile["stages"]["robustness_passes"] = 0.0

    from engine.costs import equity_round_trip
    started = time.perf_counter(); cost_seconds = 0.0
    open_positions: dict[int, dict] = {}; trades: list[dict] = []

    def close_trade(j: int, pos: dict, exit_price: float, exit_time: dt.datetime,
                    reason: str) -> None:
        nonlocal cost_seconds
        cost_started = time.perf_counter()
        costs = equity_round_trip(pos["entry_price"], pos["quantity"]).total
        cost_seconds += time.perf_counter() - cost_started
        gross = (exit_price - pos["entry_price"]) * pos["quantity"]
        risk = pos["entry_price"] - pos["stop_price"] if pos.get("stop_price") is not None else None
        trades.append({"entity": symbols[j], "entry_time": pos["entry_time"],
            "exit_time": exit_time, "entry_price": pos["entry_price"],
            "exit_price": exit_price, "quantity": pos["quantity"], "gross_pnl": gross,
            "costs": costs, "net_pnl": gross - costs,
            "r_multiple": ((exit_price - pos["entry_price"]) / risk if risk and risk > 0 else None),
            "exit_reason": reason})

    for i, day in enumerate(days):
        instant = dt.datetime.combine(dt.date.fromisoformat(day), DEFAULT_AS_OF_TIME, tzinfo=IST)
        for j in list(open_positions):
            pos = open_positions[j]; exit_price = None; reason = None
            if pos.get("stop_price") is not None and np.isfinite(low[i, j]) and low[i, j] <= pos["stop_price"]:
                exit_price, reason = pos["stop_price"], "stop"
            elif pos.get("target_price") is not None and np.isfinite(high[i, j]) and high[i, j] >= pos["target_price"]:
                exit_price, reason = pos["target_price"], "target"
            elif ("max_hold_days" in exit_rule and pos["days_held"] >= exit_rule["max_hold_days"]
                  and np.isfinite(close[i, j])):
                exit_price, reason = float(close[i, j]), "time_exit"
            if exit_price is not None:
                close_trade(j, pos, float(exit_price), instant, reason); del open_positions[j]
            else:
                pos["days_held"] += 1
        # Preserve the legacy universe order because it also determines the
        # deterministic trade_seq persistence order.
        for symbol in universes[i]:
            j = symbol_index.get(symbol)
            if j is None or not signals[i, j]:
                continue
            if j in open_positions or not np.isfinite(close[i, j]):
                continue
            entry = float(close[i, j]); quantity = max(int(100_000.0 / entry), 1)
            open_positions[j] = {"entry_price": entry, "entry_time": instant,
                "quantity": quantity,
                "stop_price": entry * (1 - exit_rule["stop_loss_pct"] / 100) if "stop_loss_pct" in exit_rule else None,
                "target_price": entry * (1 + exit_rule["target_pct"] / 100) if "target_pct" in exit_rule else None,
                "days_held": 0}
    if days:
        instant = dt.datetime.combine(dt.date.fromisoformat(days[-1]), DEFAULT_AS_OF_TIME, tzinfo=IST)
        i = len(days) - 1
        for j, pos in list(open_positions.items()):
            if np.isfinite(close[i, j]):
                close_trade(j, pos, float(close[i, j]), instant, "evaluation_end")
    _timed(profile, "portfolio_simulation", started)
    profile["stages"]["transaction_costs"] = round(cost_seconds, 6)
    profile["wall_seconds"] = round(time.perf_counter() - wall_started, 6)
    profile["cpu_seconds"] = round(time.process_time() - cpu_started, 6)
    profile["peak_rss_bytes"] = _rss_bytes()
    profile["sessions_processed"] = len(days)
    return BulkResult(trades, profile)
