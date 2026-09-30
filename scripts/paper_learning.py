"""Bridge paper outcomes into append-only research memory without coupling paper/."""
from __future__ import annotations
import json
from paper import config as paper_config
from paper.health import strategy_health
from paper.store import PaperStore
from research import memory as rm
from research.store import Store


def run() -> dict:
    if not paper_config.db_path().is_file():
        return {"status": "NO_PAPER_STORE", "events": 0}
    with Store.open() as research_store, PaperStore.open_readonly() as paper_store:
        prior = rm.query_research_log(research_store, rm.DATASET_NOTE, limit=None)
        latest = {}
        for row in prior:
            payload = row.get("payload") or {}
            if payload.get("learning_type") == "paper_strategy_health":
                latest[payload.get("strategy_version_id")] = payload.get("state")
        written = 0
        for health in strategy_health(paper_store):
            version_id = health["strategy_version_id"]
            if latest.get(version_id) == health["state"]:
                continue
            rm.record_research_note(research_store, source="scripts.paper_learning",
                note=f"Paper strategy {version_id} is {health['state']}: {health['reason']}.",
                extra={"learning_type": "paper_strategy_health", **health})
            written += 1
    return {"status": "OK", "events": written}


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
