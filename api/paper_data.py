"""
api/paper_data.py — read adapters for Slice AA's paper/shadow engine.

Same discipline api/data.py's own module docstring states for the live
data: every function here is a thin, read-only wrapper over paper/store.py
— nothing here computes a number paper/portfolio.py hasn't already
computed and persisted, and nothing here ever calls paper.runner.
run_paper_cycle() (that would mean a dashboard poll triggering a paper
trading cycle — a write-shaped side effect a read-only API must never have,
the same reason api/data.py's own get_regime() refuses to call
engine.regime.classify_thresholds() live).

Kept as its own module, deliberately not merged into api/data.py, so the
LIVE / PAPER separation the AA spec asks for (section 17: "do not mix
paper positions with real positions... do not alter existing live
endpoint semantics to silently include paper data") is visible at the file
level, not just inside one large module. api/app.py imports both.
"""

from __future__ import annotations

import threading
from typing import Optional

from paper.store import PaperStore

_paper_store_local = threading.local()


class PaperDataSourceError(RuntimeError):
    """Mirrors api.data.DataSourceError's own contract: a safe, generic
    public message only — never the underlying exception's text, which can
    carry a filesystem path. api/app.py's existing DataSourceError handler
    also catches this (see that module)."""


def get_default_paper_store() -> PaperStore:
    store = getattr(_paper_store_local, "store", None)
    if store is None:
        try:
            store = PaperStore.open()
        except Exception as e:
            raise PaperDataSourceError("the paper store is unavailable") from e
        _paper_store_local.store = store
    return store


def reset_default_paper_store_for_testing() -> None:
    """Test-only — see api.data.reset_default_store_for_testing()'s
    identical role for the research Store."""
    _paper_store_local.store = None


def get_paper_account(store: Optional[PaperStore] = None) -> dict:
    store = store or get_default_paper_store()
    try:
        account = store.get_account()
    except Exception as e:
        raise PaperDataSourceError("paper account state is unavailable") from e
    return {
        "initial_capital": account["initial_capital"],
        "cash": account["cash"],
        "realized_pnl_alltime": account["realized_pnl_alltime"],
        "total_costs_alltime": account["total_costs_alltime"],
        "updated_at": account["updated_at"],
        "label": "PAPER — simulated capital, no real money",
    }


def get_paper_positions(store: Optional[PaperStore] = None) -> dict:
    from paper.portfolio import PaperPortfolio

    store = store or get_default_paper_store()
    try:
        positions = PaperPortfolio(store).positions_with_marks()
    except Exception as e:
        raise PaperDataSourceError("paper position state is unavailable") from e
    return {
        "positions": positions,
        "count": len(positions),
        "label": "PAPER — simulated positions, no real broker holding",
    }


def get_paper_orders(*, limit: Optional[int] = 100,
                     store: Optional[PaperStore] = None) -> dict:
    store = store or get_default_paper_store()
    try:
        orders = store.list_orders(limit=limit)
    except Exception as e:
        raise PaperDataSourceError("paper order journal is unavailable") from e
    return {
        "orders": orders,
        "shown_count": len(orders),
        "note": "the paper engine's own simulated order journal — never a real broker orderbook",
    }


def get_paper_trades(*, limit: Optional[int] = 100,
                     store: Optional[PaperStore] = None) -> dict:
    store = store or get_default_paper_store()
    try:
        trades = store.list_trades(limit=limit)
    except Exception as e:
        raise PaperDataSourceError("paper trade journal is unavailable") from e
    return {
        "trades": trades,
        "shown_count": len(trades),
        "note": "simulated round trips only — no real money changed hands",
    }


def get_paper_strategies(*, eligibility_dir=None, registry_dir=None) -> dict:
    """Every currently paper-eligible StrategyVersion, joined with its own
    registry metadata (algorithm_id, parameters) — paper.eligibility's own
    audit fields (actor/reason/ts) plus strategies.registry's definition,
    never a live guess at either."""
    from paper import eligibility as pelig
    from strategies import registry as sreg

    try:
        eligible = pelig.list_paper_eligible(directory=eligibility_dir)
        reg_dir = registry_dir if registry_dir is not None else sreg.REGISTRY_DIR
        items = []
        for event in eligible:
            try:
                version = sreg.load_version(event["version_id"], directory=reg_dir)
                parameters = version.parameters
            except Exception:
                parameters = None
            items.append({
                "strategy_id": event["strategy_id"],
                "version_id": event["version_id"],
                "algorithm_id": event.get("algorithm_id"),
                "parameters": parameters,
                "approved_by": event.get("actor"),
                "approved_reason": event.get("reason"),
                "approved_at": event.get("ts"),
            })
    except Exception as e:
        raise PaperDataSourceError("paper eligibility registry is unavailable") from e
    return {"strategies": items, "count": len(items)}


def get_paper_performance(store: Optional[PaperStore] = None) -> dict:
    from paper.portfolio import PaperPortfolio

    store = store or get_default_paper_store()
    try:
        summary = PaperPortfolio(store).performance_summary()
    except Exception as e:
        raise PaperDataSourceError("paper performance summary is unavailable") from e
    summary["label"] = "PAPER — simulation only, not a live-approval signal"
    return summary
