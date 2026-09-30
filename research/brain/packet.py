"""Deterministic significance gate and compact ResearchPacket artifact."""

from __future__ import annotations

import hashlib
import json
from typing import Optional

from .. import memory as rm

PACKET_VERSION = "research-packet.v1"
SIGNIFICANT_Z = 2.0
NEGATIVE_VERDICTS = {"WEAK", "INCONCLUSIVE", "CONTRADICTED", "REDUNDANT"}


def build_packet(digest: dict, *, research_question: Optional[str] = None) -> dict:
    """Reduce a bounded digest to the evidence needed for one reasoning call.

    The gate is intentionally deterministic. A packet is admitted when it
    contains a statistically notable anomaly or actionable/new evidence.
    Mere data arrival never causes an AI call.
    """
    anomalies = list((digest.get("anomalies") or {}).get("shown") or [])
    evidence = list((digest.get("evidence") or {}).get("hypotheses") or [])
    significant = [a for a in anomalies if abs(float(a.get("z_score") or 0)) >= SIGNIFICANT_Z]
    actionable = [e for e in evidence if e.get("verdict") in ("PROMISING", "CONTRADICTED")]
    negatives = [e for e in evidence if e.get("verdict") in NEGATIVE_VERDICTS]
    symbols = sorted({a.get("entity") for a in significant
                      if a.get("entity") and a.get("entity") != rm.MARKET_ENTITY})
    source_artifacts = [f"detection:{a.get('anomaly_id')}" for a in significant]
    source_artifacts += [f"hypothesis:{e.get('hypothesis_id')}" for e in actionable]
    novelty = 1.0 if significant else (0.6 if actionable else 0.0)
    admitted = bool(significant or actionable)
    question = research_question or (
        f"Which falsifiable market effect best explains {len(significant)} significant anomaly/anomalies?"
        if significant else
        "What independent validation would most efficiently resolve the actionable evidence?"
    )
    core = {
        "version": PACKET_VERSION, "knowledge_timestamp": digest.get("as_of"),
        "source_artifacts": source_artifacts[:30], "symbols": symbols[:50],
        "universe": "observed symbols" if symbols else "configured research universe",
        "regime": None,
        "deterministic_features": [{"entity": a.get("entity"), "metric": a.get("metric"),
                                    "value": a.get("value"), "baseline": a.get("baseline"),
                                    "z_score": a.get("z_score")} for a in significant[:20]],
        "anomalies": significant[:20], "events": [],
        "related_research": actionable[:10], "prior_negative_evidence": negatives[:10],
        "data_quality": {"firewall": (digest.get("provenance") or {}).get("data_firewall"),
                         "digest_truncated": any((digest.get(k) or {}).get("truncated", False)
                                                 for k in ("anomalies", "evidence", "exact_duplicates"))},
        "research_question": question, "novelty_score": novelty,
        "significance": {"admitted": admitted,
                         "significant_anomaly_count": len(significant),
                         "actionable_evidence_count": len(actionable),
                         "threshold_abs_z": SIGNIFICANT_Z},
    }
    fingerprint = hashlib.sha256(json.dumps(core, sort_keys=True, default=str,
                                            separators=(",", ":")).encode()).hexdigest()[:16]
    return {"packet_id": f"RP-{fingerprint.upper()}", **core}


def persist_packet(store, packet: dict) -> Optional[int]:
    if not (packet.get("significance") or {}).get("admitted"):
        return None
    return rm.record_research_packet(store, packet)
