"""Unified deterministic activity feed over authoritative telemetry."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from research.brain.worker import RUN_LOG as WORKER_LOG
from research.recorder import RUN_LOG as RECORDER_LOG
from research.store import Store, iso, now_ist

from . import artifacts


def _jsonl(path: Path, limit: int = 100) -> list[dict]:
    rows = []
    try:
        lines = path.read_text(errors="replace").splitlines()[-limit:]
    except OSError:
        return rows
    for line in lines:
        try:
            row = json.loads(line)
            if isinstance(row, dict): rows.append(row)
        except json.JSONDecodeError:
            continue
    return rows


def get_activity(store: Store, *, limit: int = 50, offset: int = 0,
                 kind: Optional[str] = None) -> dict:
    items = []
    for row in _jsonl(RECORDER_LOG):
        sources = row.get("sources") or []
        new_rows = sum(int(s.get("rows_new") or 0) for s in sources)
        items.append({"id": f"recorder:{row.get('finished_at') or row.get('ts')}",
                      "timestamp": row.get("finished_at") or row.get("ts"),
                      "kind": "DATA", "status": "SKIPPED" if row.get("skipped") else
                      ("DEGRADED" if any(not s.get("ok") for s in sources) else "COMPLETED"),
                      "summary": row.get("skip_reason") or
                      f"Market recorder completed; {new_rows} observations persisted",
                      "artifact_id": None})
    for row in _jsonl(WORKER_LOG):
        actions = int(row.get("actions_attempted") or 0)
        items.append({"id": f"worker:{row.get('run_id') or row.get('worker_id')}",
                      "timestamp": row.get("finished_at"), "kind": "RESEARCH",
                      "status": str(row.get("outcome") or "UNKNOWN").upper(),
                      "summary": row.get("no_work_reason") or
                      f"Research heartbeat completed; {actions} actions attempted",
                      "artifact_id": None})
    artifact_page = artifacts.list_artifacts(store, limit=200, offset=0)
    for row in artifact_page["artifacts"]:
        items.append({"id": f"artifact:{row['id']}", "timestamp": row.get("timestamp"),
                      "kind": row.get("type", "ARTIFACT").upper(),
                      "status": row.get("status") or "RECORDED",
                      "summary": row.get("summary"), "artifact_id": row["id"]})
    if kind:
        items = [x for x in items if x["kind"].casefold() == kind.casefold()]
    items.sort(key=lambda x: str(x.get("timestamp") or ""), reverse=True)
    total = len(items)
    page = items[offset:offset + max(0, limit)]
    return {"activity": page, "total_count": total, "shown_count": len(page),
            "limit": limit, "offset": offset, "as_of": iso(now_ist())}
