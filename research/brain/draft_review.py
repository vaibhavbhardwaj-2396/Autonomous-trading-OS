"""Bounded deterministic-first review of the existing draft backlog."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from .. import memory as rm
from ..contracts import REGISTRY_DIR
from ..store import Store
from . import draft_backlog

OUTCOMES = ("READY", "DUPLICATE", "INVALID", "INSUFFICIENT_DATA",
            "RETEST_LATER", "REJECTED_PRETEST", "BLOCKED")


def _reviewed_ids(store: Store) -> set[str]:
    return {r["payload"].get("contract_id") for r in
            rm.query_research_log(store, rm.DATASET_DRAFT_REVIEW, limit=None)}


def _price_count(store: Store, start: str, end: str) -> int:
    row = store._unsafe_connection().execute(
        "SELECT COUNT(*) n FROM prices WHERE session_date >= ? AND session_date <= ?",
        (start[:10], end[:10])).fetchone()
    return int(row["n"] if row else 0)


def review_batch(store: Store, *, registry_dir: Path = REGISTRY_DIR,
                 limit: int = 10) -> dict:
    reviewed = _reviewed_ids(store)
    drafts = draft_backlog.list_drafts(store, registry_dir=registry_dir, limit=None)
    selected = [d for d in reversed(drafts) if d["contract_id"] not in reviewed][:max(0, limit)]
    rows = []
    for item in selected:
        from ..contracts import Contract
        contract = Contract.load(item["contract_id"], registry_dir)
        problems = contract.check()
        prices = _price_count(store, contract.evaluation_start, contract.evaluation_end)
        checks = {"structurally_valid": not problems,
                  "exact_duplicate": item["exact_duplicate"],
                  "already_tested_duplicate": item["already_tested"],
                  "price_rows_in_window": prices,
                  "universe": contract.universe,
                  "testable": not problems and prices > 0}
        reasons = []
        if problems:
            outcome = "INVALID"; reasons.extend(problems)
        elif item["already_tested"]:
            outcome = "DUPLICATE"; reasons.append("an exact rule fingerprint was already tested")
        elif prices <= 0:
            outcome = "INSUFFICIENT_DATA"; reasons.append(
                "no price rows exist inside the declared evaluation window")
        else:
            outcome = "READY"; reasons.append("deterministic pre-test checks passed")
        rm.record_draft_review(store, contract_id=contract.id,
                               hypothesis_id=item.get("hypothesis_id"), outcome=outcome,
                               reasons=reasons, checks=checks)
        rows.append({"contract_id":contract.id, "hypothesis_id":item.get("hypothesis_id"),
                     "outcome":outcome, "reasons":reasons, "checks":checks})
    counts = Counter(r["outcome"] for r in rows)
    return {"processed":len(rows), "outcomes":dict(counts), "reviews":rows,
            "remaining_unreviewed":max(0, len(drafts) - len(reviewed) - len(rows))}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--db", type=Path)
    ap.add_argument("--registry-dir", type=Path, default=REGISTRY_DIR)
    args = ap.parse_args(argv)
    with Store.open(args.db) if args.db else Store.open() as store:
        print(json.dumps(review_batch(store, registry_dir=args.registry_dir,
                                      limit=args.limit), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
