#!/usr/bin/env python3
"""Read-only experiment replay benchmark.

Loads a registry contract, optionally narrows only its engineering benchmark
window in memory, runs the scientific simulator without persisting trades or
changing contract state, and reports wall/CPU/RSS plus hot-path counters.
"""

from __future__ import annotations

import argparse
import copy
import json
import resource
import time
from collections import defaultdict
from pathlib import Path

from research.contracts import Contract, REGISTRY_DIR
from research.experiments import runner
from research.store import AsOfView, Store


def _rss_bytes() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS reports bytes; Linux reports KiB.
    return int(value if value > 10_000_000 else value * 1024)


def benchmark(contract: Contract, store: Store, *, engine: str = "auto") -> dict:
    if engine == "bulk":
        from research.experiments import bulk_replay
        result = bulk_replay.simulate(contract, store)
        return {**result.profile, "contract_id": contract.id, "universe": contract.universe,
                "evaluation_start": contract.evaluation_start,
                "evaluation_end": contract.evaluation_end,
                "trade_count": len(result.trades),
                "net_pnl": round(sum(t["net_pnl"] for t in result.trades), 8)}
    if engine == "auto":
        started = time.perf_counter(); cpu_started = time.process_time()
        trades, profile = runner.simulate_profiled(contract, store)
        profile.setdefault("wall_seconds", round(time.perf_counter() - started, 6))
        profile.setdefault("cpu_seconds", round(time.process_time() - cpu_started, 6))
        return {**profile, "contract_id": contract.id, "universe": contract.universe,
                "evaluation_start": contract.evaluation_start,
                "evaluation_end": contract.evaluation_end,
                "trade_count": len(trades),
                "net_pnl": round(sum(t["net_pnl"] for t in trades), 8)}

    counters = defaultdict(float)
    originals = {
        "prices": AsOfView.prices,
        "universe": AsOfView.universe,
        "observations": AsOfView.observations,
        "metric": runner._metric_value,
        "signal": runner.entry_signal,
    }

    def timed_view(name):
        original = originals[name]

        def wrapped(self, *args, **kwargs):
            started = time.perf_counter()
            result = original(self, *args, **kwargs)
            counters[f"{name}_wall_seconds"] += time.perf_counter() - started
            counters[f"{name}_calls"] += 1
            counters[f"{name}_rows"] += len(result)
            return result
        return wrapped

    def metric(view, symbol, name, window_days=None):
        started = time.perf_counter()
        result = originals["metric"](view, symbol, name, window_days)
        counters["feature_wall_seconds"] += time.perf_counter() - started
        counters["feature_calls"] += 1
        return result

    def signal(view, symbol, entry_rule):
        started = time.perf_counter()
        result = originals["signal"](view, symbol, entry_rule)
        counters["signal_wall_seconds"] += time.perf_counter() - started
        counters["signal_calls"] += 1
        return result

    queries = {"select": 0, "total": 0}

    def trace(sql):
        queries["total"] += 1
        if sql.lstrip().upper().startswith("SELECT"):
            queries["select"] += 1

    AsOfView.prices = timed_view("prices")
    AsOfView.universe = timed_view("universe")
    AsOfView.observations = timed_view("observations")
    runner._metric_value = metric
    runner.entry_signal = signal
    store._unsafe_connection().set_trace_callback(trace)
    wall_started = time.perf_counter()
    cpu_started = time.process_time()
    try:
        trades = runner._simulate_legacy(contract, store)
    finally:
        wall = time.perf_counter() - wall_started
        cpu = time.process_time() - cpu_started
        store._unsafe_connection().set_trace_callback(None)
        AsOfView.prices = originals["prices"]
        AsOfView.universe = originals["universe"]
        AsOfView.observations = originals["observations"]
        runner._metric_value = originals["metric"]
        runner.entry_signal = originals["signal"]

    counters["db_select_queries"] = queries["select"]
    counters["db_total_statements"] = queries["total"]
    return {
        "engine": "legacy_point_in_time",
        "contract_id": contract.id,
        "universe": contract.universe,
        "evaluation_start": contract.evaluation_start,
        "evaluation_end": contract.evaluation_end,
        "wall_seconds": round(wall, 6),
        "cpu_seconds": round(cpu, 6),
        "peak_rss_bytes": _rss_bytes(),
        "trade_count": len(trades),
        "net_pnl": round(sum(t["net_pnl"] for t in trades), 8),
        "counters": {k: round(v, 6) for k, v in sorted(counters.items())},
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--contract-id", required=True)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--registry-dir", type=Path, default=REGISTRY_DIR)
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--engine", choices=("auto", "bulk", "legacy"), default="auto")
    args = parser.parse_args(argv)
    contract = copy.deepcopy(Contract.load(args.contract_id, args.registry_dir))
    if args.start:
        contract.evaluation_start = args.start
    if args.end:
        contract.evaluation_end = args.end
    with Store.open(args.db) if args.db else Store.open() as store:
        print(json.dumps(benchmark(contract, store, engine=args.engine), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
