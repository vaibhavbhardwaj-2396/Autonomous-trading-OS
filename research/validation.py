"""Phase 10.5 autonomous validation campaign ledger and scorecard.

The campaign is an observer of existing authoritative stores.  A snapshot is
append-only JSONL so funnel history survives process restarts and can be
compared over time.  It does not approve research, promote a strategy, run a
paper cycle, or touch the broker/live engine.
"""

from __future__ import annotations

import argparse
import fcntl
import json
from collections import Counter
from pathlib import Path
from typing import Optional

from control import capacity
from paper import config as paper_config
from paper import eligibility
from paper.store import PaperStore
from strategies import registry as strategy_registry

from . import contracts
from . import memory as rm
from .store import Store, iso, now_ist

ROOT = Path(__file__).resolve().parent
LEDGER_PATH = ROOT / "validation_campaign.jsonl"
LOCK_PATH = ROOT / ".validation_campaign.lock"

FUNNEL_STAGES = (
    "observations", "detections", "hypotheses", "locked_experiments",
    "reported_experiments", "evidence", "strategy_versions", "backtests",
    "paper_eligible", "paper_cycles", "paper_trades",
)


def _dataset_counts(store: Store) -> dict[str, int]:
    return {row["dataset"]: int(row["n"]) for row in store.stats()["observations"]}


def _paper_counts() -> tuple[dict, list[str]]:
    blockers = []
    eligible = eligibility.list_paper_eligible()
    counts = {"paper_eligible": len(eligible), "paper_cycles": 0, "paper_trades": 0}
    path = paper_config.db_path()
    if path.is_file():
        store = PaperStore.open_readonly()
        try:
            counts["paper_cycles"] = len(store.list_cycles(limit=None))
            counts["paper_trades"] = len(store.list_trades(limit=None))
        finally:
            store.close()
    if not eligible:
        blockers.append(
            "No StrategyVersion has an explicit paper-eligibility approval; "
            "paper execution remains correctly inactive.")
    return counts, blockers


def _model_economics(store: Store) -> dict:
    rows = rm.query_research_log(store, rm.DATASET_MODEL_INTERACTION, limit=None)
    payloads = [r.get("payload") or {} for r in rows]
    status = Counter(p.get("status") or "unknown" for p in payloads)
    input_tokens = sum(int(p.get("input_tokens") or 0) for p in payloads)
    output_tokens = sum(int(p.get("output_tokens") or 0) for p in payloads)
    measured_latency = [float(p["latency_seconds"]) for p in payloads
                        if p.get("latency_seconds") is not None]
    return {
        "calls": len(payloads),
        "status": dict(status),
        "measured_input_tokens": input_tokens,
        "measured_output_tokens": output_tokens,
        "mean_latency_seconds": (round(sum(measured_latency) / len(measured_latency), 3)
                                 if measured_latency else None),
        "cost": None,
        "cost_note": "Not computed unless the provider reports usage and a versioned price is configured.",
    }


def build_snapshot(store: Store, *, resource_state: Optional[dict] = None,
                   registry_dir: Optional[Path] = None,
                   contract_dir: Optional[Path] = None) -> dict:
    """Build one honest campaign snapshot without mutating any source."""
    datasets = _dataset_counts(store)
    contract_rows = contracts.registry(contract_dir or contracts.REGISTRY_DIR)
    versions = strategy_registry.list_versions(registry_dir or strategy_registry.REGISTRY_DIR)
    paper_counts, blockers = _paper_counts()

    statuses = Counter(c.status for c in contract_rows)
    backtests = [r for r in rm.query_research_log(store, rm.DATASET_NOTE, limit=None)
                 if r.get("source") == "research.experiments.strategy_backtest"]
    funnel = {
        "observations": int(store.stats().get("observations_total") or 0),
        "detections": datasets.get(rm.DATASET_ANOMALY, 0),
        "hypotheses": datasets.get(rm.DATASET_HYPOTHESIS, 0),
        "locked_experiments": sum(statuses[s] for s in ("locked", "running", "reported")),
        "reported_experiments": statuses["reported"],
        "evidence": datasets.get(rm.DATASET_EVIDENCE, 0),
        "strategy_versions": len(versions),
        "backtests": len(backtests),
        **paper_counts,
    }

    if funnel["hypotheses"] and not funnel["locked_experiments"]:
        blockers.append("Hypotheses exist, but no immutable experiment contract has reached LOCKED.")
    if funnel["locked_experiments"] and not funnel["reported_experiments"]:
        blockers.append("Locked experiments exist, but none has reached REPORTED with completed evidence.")
    if funnel["strategy_versions"] and not funnel["backtests"]:
        blockers.append("StrategyVersions exist, but no persisted strategy-backtest completion record exists.")

    conversions = {}
    for left, right in zip(FUNNEL_STAGES, FUNNEL_STAGES[1:]):
        denominator = funnel[left]
        conversions[f"{left}_to_{right}"] = (
            round(funnel[right] / denominator, 4) if denominator else None)

    return {
        "schema_version": 1,
        "captured_at": iso(now_ist()),
        "phase": "10.5",
        "live_promotion": {"state": "LOCKED", "reason": "Phase 11 requires an explicit human decision."},
        "funnel": funnel,
        "conversion": conversions,
        "contract_status": dict(statuses),
        "research_economics": _model_economics(store),
        "capacity": capacity.plan(resource_state),
        "paper_readiness": {
            "ready": paper_counts["paper_eligible"] > 0,
            "eligible_versions": paper_counts["paper_eligible"],
            "blockers": blockers,
        },
        "data": {"datasets": datasets, "dataset_count": len(datasets)},
    }


def append_snapshot(snapshot: dict, *, path: Path = LEDGER_PATH,
                    lock_path: Path = LOCK_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        with open(path, "a") as out:
            out.write(json.dumps(snapshot, sort_keys=True, default=str) + "\n")


def read_history(*, path: Path = LEDGER_PATH, limit: int = 30) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows[-max(0, limit):]


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="Capture a Phase 10.5 validation snapshot")
    ap.add_argument("--db", type=Path)
    ap.add_argument("--ledger", type=Path, default=LEDGER_PATH)
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args(argv)
    with Store.open(args.db) as store:
        snapshot = build_snapshot(store)
    if not args.no_write:
        append_snapshot(snapshot, path=args.ledger,
                        lock_path=args.ledger.with_suffix(".lock"))
    print(json.dumps(snapshot, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
