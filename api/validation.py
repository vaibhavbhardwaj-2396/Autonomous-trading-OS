"""Read-only Phase 10.5 campaign API adapter."""

from __future__ import annotations

from research import validation
from research.store import Store

from .data import DataSourceError


def get_validation_status(store: Store, *, history_limit: int = 30) -> dict:
    try:
        current = validation.build_snapshot(store)
        history = validation.read_history(limit=history_limit)
    except Exception as exc:
        raise DataSourceError("validation campaign status is unavailable") from exc
    return {"current": current, "history": history, "history_count": len(history)}
