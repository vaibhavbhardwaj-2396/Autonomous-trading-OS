"""
api/artifacts.py — the unified, read-only artifact aggregation layer.
INTELLIGENT + EFFICIENT + TRACEABLE (outcome 2), Part D/G.

Every artifact type here is a normalized VIEW over an already-existing,
already-authoritative structure — never a second copy of the data:

    model_interaction   -> research.memory's DATASET_MODEL_INTERACTION
                            (research/brain/llm.py is the only writer)
    detection            -> research.memory's DATASET_ANOMALY
                            (research/brain/observatory.py's findings)
    hypothesis           -> research.memory's DATASET_HYPOTHESIS (a claim,
                            NOT a Contract — see research/memory.py's own
                            docstring on why the two are kept distinct)
    experiment_result   -> research.memory's DATASET_VERDICT
                            (research/experiments/evaluator.py's scored
                            result for one locked Contract)
    evidence            -> research.memory's DATASET_EVIDENCE
                            (research/experiments/comparison.py's
                            cross-experiment read)
    opportunity_event   -> research.memory's DATASET_OPPORTUNITY_EVENT
                            (the Autonomous Research Control Plane's audit
                            trail)
    note                -> research.memory's DATASET_NOTE
    discovery_search    -> research.memory's DATASET_DISCOVERY_SEARCH
    experiment          -> research.contracts.registry() — the locked,
                            hash-tested Contract itself
    system_event        -> control.runtime's own persisted mode-change
                            history (RUNNING/PAUSED/SAFE_MODE/STOPPED)

All of the observation-backed types share ONE read path
(research.memory.query_research_log, itself a thin wrapper over
Store.view().observations() — no new query logic, no new schema).

Two "artifact" categories intentionally NOT included in this first pass,
disclosed rather than silently missing: "decision" (paper/shadow cycle
outcomes — paper.store.PaperStore's own cycles/orders tables are the
authoritative source and are already exposed via /paper/* routes;
unifying them under /artifacts is a reasonable follow-up, not done here)
and a dedicated "risk"/"execution" artifact (this deployment has no live
executions to show — engine.execute is never imported from anywhere
research/paper/api touches, by design).

No function here ever imports engine.execute, engine.guardrails.
validate_order/save_state, research.brain.hypothesis_intake.
approve_and_lock, or anything else that changes state — same convention
as api/data.py, and worth re-grepping if that claim ever needs
re-verifying.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from research import memory as rm
from research.store import Store, iso, now_ist
from research.contracts import Contract, REGISTRY_DIR
from research.contracts import registry as contract_registry
from control import runtime as ctrl

from .data import DataSourceError

OBSERVATION_ARTIFACT_TYPES = {
    "model_interaction": rm.DATASET_MODEL_INTERACTION,
    "detection": rm.DATASET_ANOMALY,
    "hypothesis": rm.DATASET_HYPOTHESIS,
    "experiment_result": rm.DATASET_VERDICT,
    "evidence": rm.DATASET_EVIDENCE,
    "opportunity_event": rm.DATASET_OPPORTUNITY_EVENT,
    "note": rm.DATASET_NOTE,
    "discovery_search": rm.DATASET_DISCOVERY_SEARCH,
}
_DATASET_TO_TYPE = {v: k for k, v in OBSERVATION_ARTIFACT_TYPES.items()}

NON_OBSERVATION_ARTIFACT_TYPES = ("experiment", "system_event")
ALL_ARTIFACT_TYPES = tuple(OBSERVATION_ARTIFACT_TYPES) + NON_OBSERVATION_ARTIFACT_TYPES


def _summarize(artifact_type: str, payload: dict) -> str:
    """A short, human label for the LIST view only — the full content is
    what get_artifact()'s detail call returns. Never truncates anything
    that matters for correctness; this is purely a display string."""
    if artifact_type == "model_interaction":
        return (f"{payload.get('provider')}/{payload.get('model')} — "
                f"{payload.get('purpose')} ({payload.get('status')})")
    if artifact_type == "detection":
        return f"{payload.get('metric')} z={payload.get('z_score')} (entity {payload.get('entity', '')})"
    if artifact_type == "hypothesis":
        return (payload.get("claim") or "")[:160]
    if artifact_type == "experiment_result":
        return f"contract {payload.get('contract_id')} — {payload.get('outcome') or ''}"
    if artifact_type == "evidence":
        return f"{payload.get('hypothesis_id')} — {payload.get('verdict') or payload.get('status') or ''}"
    if artifact_type == "opportunity_event":
        return f"{payload.get('event_type')} — {payload.get('opportunity_id')}"
    if artifact_type == "note":
        return (payload.get("note") or payload.get("text") or "")[:160]
    if artifact_type == "discovery_search":
        return f"discovery search for {payload.get('hypothesis_id')}"
    return str(payload)[:160]


def _observation_artifacts(store: Store, dataset: str) -> list[dict]:
    artifact_type = _DATASET_TO_TYPE[dataset]
    rows = rm.query_research_log(store, dataset, limit=None)
    rows = sorted(rows, key=lambda r: r["id"], reverse=True)
    out = []
    for r in rows:
        payload = r.get("payload") or {}
        out.append({
            "id": f"{artifact_type}:{r['id']}",
            "type": artifact_type,
            "timestamp": r.get("event_time"),
            "cycle_id": payload.get("cycle_id"),
            "status": payload.get("status"),
            "source": r.get("source"),
            "summary": _summarize(artifact_type, payload),
        })
    return out


def _experiment_artifacts(*, registry_dir: Path) -> list[dict]:
    try:
        contracts = contract_registry(registry_dir)
    except Exception as e:
        raise DataSourceError("contract registry is unavailable") from e
    contracts = sorted(contracts, key=lambda c: c.locked_at or "", reverse=True)
    return [{
        "id": f"experiment:{c.id}",
        "type": "experiment",
        "timestamp": c.locked_at,
        "cycle_id": None,
        "status": c.status,
        "source": "research.contracts",
        "summary": f"{c.id} — {c.title} ({c.status})",
    } for c in contracts]


def _system_event_artifacts() -> list[dict]:
    state = ctrl.get_state()
    events = list(state.get("history") or [])
    events.append({"mode": state.get("mode"), "reason": state.get("reason"),
                   "actor": state.get("actor"), "changed_at": state.get("changed_at")})
    events.sort(key=lambda e: e.get("changed_at") or "", reverse=True)
    out = []
    for i, e in enumerate(events):
        out.append({
            "id": f"system_event:{e.get('changed_at')}::{i}",
            "type": "system_event",
            "timestamp": e.get("changed_at"),
            "cycle_id": None,
            "status": e.get("mode"),
            "source": "control.runtime",
            "summary": f"organism mode -> {e.get('mode')}"
                      f"{' (' + e['reason'] + ')' if e.get('reason') else ''}",
        })
    return out


def list_artifacts(
    store: Store, *, type: Optional[str] = None, limit: int = 50, offset: int = 0,
    registry_dir: Optional[Path] = None,
) -> dict:
    """Every artifact, newest first, optionally filtered to one `type`.
    Pagination (`limit`/`offset`) is applied AFTER merging every requested
    type's own timestamp-sorted list — see Part N: a list page shows
    metadata only (this function never returns a full prompt/response;
    that's get_artifact()'s job)."""
    if type is not None and type not in ALL_ARTIFACT_TYPES:
        raise DataSourceError(
            f"unknown artifact type {type!r} — expected one of {ALL_ARTIFACT_TYPES}")
    types_to_fetch = [type] if type else list(ALL_ARTIFACT_TYPES)

    items: list[dict] = []
    for t in types_to_fetch:
        if t in OBSERVATION_ARTIFACT_TYPES:
            items += _observation_artifacts(store, OBSERVATION_ARTIFACT_TYPES[t])
        elif t == "experiment":
            items += _experiment_artifacts(registry_dir=registry_dir or REGISTRY_DIR)
        elif t == "system_event":
            items += _system_event_artifacts()

    items.sort(key=lambda a: a.get("timestamp") or "", reverse=True)
    total = len(items)
    page = items[offset:offset + max(0, limit)]
    return {
        "artifacts": page, "shown_count": len(page), "total_count": total,
        "offset": offset, "limit": limit, "truncated": total > offset + len(page),
        "as_of": iso(now_ist()),
    }


def get_artifact(
    store: Store, artifact_id: str, *, registry_dir: Optional[Path] = None,
) -> Optional[dict]:
    """The full artifact — including, for a model_interaction, the actual
    prompt/context/response text (see research.memory.
    record_model_interaction's bounding — the ONLY truncation applied is
    the one already baked in at write time, never a second one here).

    Returns None for "malformed id" / "unknown type" / "not found" — a
    CLIENT-input problem, deliberately not a raised DataSourceError (that
    stays reserved for genuine backend unavailability, mapped to HTTP 503
    by api/app.py's existing handler; a bad artifact id should be a 404,
    not a 503 — the route is responsible for turning None into that)."""
    if ":" not in artifact_id:
        return None
    artifact_type, key = artifact_id.split(":", 1)

    if artifact_type in OBSERVATION_ARTIFACT_TYPES:
        dataset = OBSERVATION_ARTIFACT_TYPES[artifact_type]
        try:
            row_id = int(key)
        except ValueError:
            return None
        rows = rm.query_research_log(store, dataset, limit=None)
        match = next((r for r in rows if r["id"] == row_id), None)
        if match is None:
            return None
        return {"id": artifact_id, "type": artifact_type, "timestamp": match.get("event_time"),
               "source": match.get("source"), "payload": match.get("payload")}

    if artifact_type == "experiment":
        directory = registry_dir or REGISTRY_DIR
        try:
            c = Contract.load(key, directory)
        except Exception:
            return None
        return {"id": artifact_id, "type": "experiment", "timestamp": c.locked_at,
               "source": "research.contracts", "payload": c.to_dict()}

    if artifact_type == "system_event":
        match = next((e for e in _system_event_artifacts() if e["id"] == artifact_id), None)
        if match is None:
            return None
        return {"id": artifact_id, "type": "system_event", "timestamp": match["timestamp"],
               "source": "control.runtime", "payload": match}

    return None
