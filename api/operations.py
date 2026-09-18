"""Read-only operational status across recorder, research worker and paper."""

from __future__ import annotations

import json
from pathlib import Path

from paper import config as paper_config
from paper.store import PaperStore
from research.recorder import RUN_LOG
from research.brain import worker


def _last_jsonl(path: Path) -> tuple[dict | None, int]:
    latest = None
    malformed = 0
    if not path.is_file():
        return None, 0
    for line in path.read_text(errors="replace").splitlines()[-500:]:
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                latest = row
            else:
                malformed += 1
        except json.JSONDecodeError:
            malformed += 1
    return latest, malformed


def get_operations_status() -> dict:
    recorder, malformed = _last_jsonl(RUN_LOG)
    paper = {"state": "NOT_INITIALIZED", "last_cycle": None}
    if paper_config.db_path().is_file():
        store = PaperStore.open_readonly()
        try:
            cycles = store.list_cycles(limit=1)
            paper = {"state": (cycles[0]["status"] if cycles else "IDLE"),
                     "last_cycle": cycles[0] if cycles else None}
        finally:
            store.close()
    source_failures = [s for s in (recorder or {}).get("sources", []) if not s.get("ok")]
    return {
        "research_worker": worker.worker_status(),
        "recorder": {"state": "MISSING" if recorder is None else (
            "DEGRADED" if source_failures or malformed else "HEALTHY"),
            "last_run": recorder, "malformed_recent_rows": malformed,
            "failed_sources": source_failures},
        "paper": paper,
    }
