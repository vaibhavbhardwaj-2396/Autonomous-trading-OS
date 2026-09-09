"""
PERISHABLE — the agent's own decision context.

The cheapest research asset in the project, because the agent already produces
all of it and currently throws most of it away.

Every run, engine/regime.py classifies the market and logs both the threshold
call and the shadow HMM's opinion to memory/regime_log.jsonl. Every pre-market
run, engine/screener.py builds a full candidate set with a dozen indicators per
name — and engine/briefing.py renders it to a markdown file in logs/ that
run_cycle.sh deletes after 60 days.

That candidate set is a prospective record of what a systematic screen surfaced
on a given morning, with no hindsight in it whatsoever. It is exactly the kind
of data that is impossible to reconstruct later and trivial to keep now.

This module ingests what already exists on disk. It computes nothing and calls
no network, so it cannot fail in a way that matters and cannot drift from what
the agent actually saw.

Note it reads memory/*.jsonl as FILES. It does not import engine.journal, which
would pull in guardrails and require a live state.json — the research layer has
to stay runnable on a laptop with no trading state at all.
"""

from __future__ import annotations

import json
import hashlib
import datetime as dt
from pathlib import Path
from typing import Optional

from ..store import Store, to_dt, now_ist

PROJECT_ROOT = Path(__file__).parent.parent.parent
REGIME_LOG = PROJECT_ROOT / "memory" / "regime_log.jsonl"
TRADES_LOG = PROJECT_ROOT / "memory" / "trades.jsonl"

REGIME_DATASET = "agent_regime_call"
CANDIDATE_DATASET = "agent_candidate_set"
TRADE_DATASET = "agent_trade_record"
SOURCE = "agent_self"


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # one bad line must not hide the rest of the history
    return out


def parse_regime_rows(records: list[dict]) -> list[dict]:
    """regime_log.jsonl -> store rows.

    knowledge_time == the moment the agent made the call. This is one of the few
    feeds where that is exactly right with no judgment involved: the agent knew
    what it concluded at the instant it concluded it.
    """
    rows = []
    for rec in records:
        ts = rec.get("ts")
        if not ts:
            continue
        when = to_dt(ts)
        rows.append({
            "dataset": REGIME_DATASET,
            "entity": "_market",
            "event_time": when,
            "knowledge_time": when,
            "source": SOURCE,
            "confidence": "observed",
            "payload": {
                "regime": rec.get("regime"),
                "playbook": rec.get("playbook"),
                "confidence": rec.get("confidence"),
                "evidence": rec.get("evidence") or {},
                # The shadow HMM's disagreements with the threshold classifier
                # are the dataset that eventually decides whether it gets
                # promoted. Keeping them is the whole point of shadow mode.
                "shadow": rec.get("shadow") or None,
            },
        })
    return rows


def parse_trade_rows(records: list[dict]) -> list[dict]:
    """trades.jsonl -> store rows. Entries, exits and — importantly —
    guardrail REJECTIONS, which are a record of what the agent wanted to do and
    was not allowed to. Rejections are the closest thing to a control group this
    project will ever have."""
    rows = []
    for rec in records:
        ts = rec.get("ts")
        if not ts:
            continue
        when = to_dt(ts)
        rows.append({
            "dataset": TRADE_DATASET,
            "entity": (rec.get("symbol") or "_none").upper(),
            "event_time": when,
            "knowledge_time": when,
            "source": SOURCE,
            "confidence": "observed",
            "payload": rec,
        })
    return rows


def parse_candidate_rows(candidates: list[dict], as_of: dt.datetime,
                         cycle: str = "premarket", regime: str = "",
                         playbook: str = "") -> list[dict]:
    """The screener's output for one run.

    Called by the briefing hook (see docs/RESEARCH_DEPLOY.md). One row per
    candidate, carrying every indicator that justified it, so a later study can
    ask 'what did this screen actually surface, and what happened next' without
    re-running anything.
    """
    rows = []
    for c in candidates:
        sym = str(c.get("symbol") or "").upper()
        if not sym:
            continue
        rows.append({
            "dataset": CANDIDATE_DATASET,
            "entity": sym,
            "event_time": as_of,
            "knowledge_time": as_of,
            "source": SOURCE,
            "confidence": "observed",
            "payload": {
                "cycle": cycle,
                "regime": regime,
                "playbook": playbook or c.get("playbook"),
                "score": c.get("score"),
                "direction": c.get("direction"),
                "suggested_entry": c.get("suggested_entry"),
                "suggested_stop": c.get("suggested_stop"),
                "suggested_target": c.get("suggested_target"),
                "reward_risk": c.get("reward_risk"),
                "reasons": c.get("reasons"),
                "indicators": c.get("indicators") or {},
            },
        })
    return rows


def record_candidates(candidates: list[dict], regime: str = "", playbook: str = "",
                      cycle: str = "premarket", store: Optional[Store] = None) -> int:
    """The one function engine/briefing.py calls.

    Deliberately swallows everything. A research recorder must never be able to
    fail a trading run — the same discipline engine/regime.py already applies to
    its own logging.
    """
    own = store is None
    try:
        store = store or Store.open()
        rows = parse_candidate_rows(candidates, now_ist(), cycle, regime, playbook)
        n = store.append_many(rows)
        if own:
            store.close()
        return n
    except Exception:
        return 0


def load(store: Store) -> dict:
    regime_rows = parse_regime_rows(_read_jsonl(REGIME_LOG))
    trade_rows = parse_trade_rows(_read_jsonl(TRADES_LOG))
    n1 = store.append_many(regime_rows)
    n2 = store.append_many(trade_rows)
    return {
        "rows_seen": len(regime_rows) + len(trade_rows),
        "rows_new": n1 + n2,
        "regime_calls": len(regime_rows),
        "trade_records": len(trade_rows),
        "regime_log_present": REGIME_LOG.exists(),
        "trades_log_present": TRADES_LOG.exists(),
    }
