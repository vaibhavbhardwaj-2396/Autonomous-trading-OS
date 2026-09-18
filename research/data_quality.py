"""Point-in-time data readiness report with explicit missing/stale states."""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass
from typing import Iterable

from .store import Store, TimeLike, to_dt


@dataclass(frozen=True)
class DataRequirement:
    dataset: str
    max_age_hours: float
    required: bool = True


DEFAULT_REQUIREMENTS = (
    DataRequirement("prices_eod", 96),
    DataRequirement("bse_announcement", 96, False),
    DataRequirement("index_membership", 24 * 45, False),
)


def _instant(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30)))
    return parsed


def build_data_quality_report(store: Store, as_of: TimeLike, *,
                              requirements: Iterable[DataRequirement] = DEFAULT_REQUIREMENTS) -> dict:
    inventory = store.quality_inventory(as_of)
    as_of_dt = to_dt(as_of, end_of_day=True)
    by_name = {row["dataset"]: row for row in inventory["observations"]}
    by_name["prices_eod"] = inventory["prices_eod"]
    checks = []
    for requirement in requirements:
        row = by_name.get(requirement.dataset) or {}
        latest = row.get("latest_knowledge_time")
        age = max(0.0, (as_of_dt - _instant(latest)).total_seconds() / 3600) if latest else None
        status = "MISSING" if not latest else ("STALE" if age > requirement.max_age_hours else "FRESH")
        checks.append({**asdict(requirement), "status": status, "age_hours": age,
                       "rows": int(row.get("n") or 0),
                       "entities": int(row.get("entities") or 0),
                       "latest_event_time": row.get("latest_event_time"),
                       "latest_knowledge_time": latest})
    required_bad = [c for c in checks if c["required"] and c["status"] != "FRESH"]
    optional_bad = [c for c in checks if not c["required"] and c["status"] != "FRESH"]
    overall = "BLOCKED" if required_bad or not inventory["append_only_enforced"] else (
        "DEGRADED" if optional_bad else "READY")
    return {"as_of": inventory["as_of"], "status": overall, "checks": checks,
            "append_only_enforced": inventory["append_only_enforced"],
            "missing_append_only_triggers": inventory["missing_append_only_triggers"]}
