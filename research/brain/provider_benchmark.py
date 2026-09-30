"""Bounded, P&L-independent evaluation of configured research AI targets.

The benchmark replays compact historical ResearchPackets, never raw logs.  It
scores only qualities needed before an idea can enter the deterministic
experiment factory; it does not promote, lock, trade, or change routing.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from typing import Callable, Iterable, Optional

from control import ai_budget
from research import memory as rm

from . import llm

MAX_CASES = 5
MAX_TARGETS = 4
BENCHMARK_PROMPT_VERSION = "provider-benchmark.v1"
REQUIRED_FIELDS = {
    "statement", "economic_mechanism", "expected_effect", "universe",
    "features", "time_horizon", "regime_assumption", "falsification_criteria",
    "required_datasets", "critic_findings",
}


def representative_packets(store, *, limit: int = 3) -> list[dict]:
    """Return newest admitted packets, capped so a benchmark cannot fan out."""
    limit = max(1, min(int(limit), MAX_CASES))
    rows = rm.query_research_log(store, rm.DATASET_RESEARCH_PACKET, limit=None)
    packets = [r.get("payload") or {} for r in rows]
    return [p for p in reversed(packets) if (p.get("significance") or {}).get("admitted")][:limit]


def _prompt(packet: dict) -> str:
    compact = {k: packet.get(k) for k in (
        "packet_id", "knowledge_timestamp", "symbols", "universe", "regime",
        "deterministic_features", "anomalies", "events", "related_research",
        "prior_negative_evidence", "data_quality", "research_question")}
    return json.dumps({
        "task": "Propose one falsifiable hypothesis and independently criticize it.",
        "input": compact,
        "required_output_fields": sorted(REQUIRED_FIELDS),
        "constraints": ["JSON only", "do not invent datasets", "do not use P&L as model-quality score"],
    }, sort_keys=True, default=str)


def score_response(text: str) -> dict:
    """Deterministic rubric; malformed output receives zero schema-dependent credit."""
    try:
        parsed = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return {"schema_valid": False, "specificity": 0, "falsifiability": 0,
                "experiment_constructibility": 0, "critic_usefulness": 0,
                "fingerprint": None}
    if not isinstance(parsed, dict):
        return {"schema_valid": False, "specificity": 0, "falsifiability": 0,
                "experiment_constructibility": 0, "critic_usefulness": 0,
                "fingerprint": None}
    present = {k for k in REQUIRED_FIELDS if parsed.get(k) not in (None, "", [], {})}
    schema_valid = REQUIRED_FIELDS <= present
    statement = str(parsed.get("statement") or "")
    mechanism = str(parsed.get("economic_mechanism") or "")
    falsification = str(parsed.get("falsification_criteria") or "")
    critic = str(parsed.get("critic_findings") or "")
    specificity = sum((len(statement) >= 40, bool(parsed.get("expected_effect")),
                       bool(parsed.get("time_horizon")), bool(parsed.get("regime_assumption"))))
    falsifiability = sum((len(falsification) >= 20,
                          any(token in falsification.lower() for token in
                              ("if ", "below", "above", "less", "greater", "zero", "negative"))))
    constructibility = sum((bool(parsed.get("universe")), bool(parsed.get("features")),
                            bool(parsed.get("required_datasets")), len(mechanism) >= 30))
    critic_usefulness = sum((len(critic) >= 30,
                             any(token in critic.lower() for token in
                                 ("leak", "confound", "sample", "regime", "cost", "alternative"))))
    normalized = " ".join(statement.lower().split())
    return {"schema_valid": schema_valid, "specificity": specificity,
            "falsifiability": falsifiability,
            "experiment_constructibility": constructibility,
            "critic_usefulness": critic_usefulness,
            "fingerprint": normalized or None}


def run_benchmark(store, targets: Iterable[dict], *, limit: int = 3,
                  invoke: Optional[Callable[[str, str, str], llm.ModelResponse]] = None) -> dict:
    """Compare at most four provider/model targets over at most five packets.

    The caller supplies ``invoke`` in tests. Production uses registered adapters
    and the normal daily AI budget. Results are append-only model-interaction
    artifacts; routing is never changed by benchmark results.
    """
    packets = representative_packets(store, limit=limit)
    bounded_targets = list(targets)[:MAX_TARGETS]
    if not packets:
        return {"status": "NO_REPRESENTATIVE_PACKETS", "cases": 0, "results": []}
    results = []
    for target in bounded_targets:
        provider, model = target.get("provider"), target.get("model")
        if provider not in llm.KNOWN_PROVIDERS or not isinstance(model, str) or not model.strip():
            results.append({"provider": provider, "model": model, "status": "INVALID_TARGET"})
            continue
        rows, fingerprints = [], []
        for packet in packets:
            prompt = _prompt(packet)
            allowed, reason = ai_budget.budget_allows(estimated_tokens=max(1, len(prompt) // 4))
            if not allowed:
                rows.append({"packet_id": packet.get("packet_id"), "status": "BUDGET_BLOCKED",
                             "reason": reason})
                break
            started = time.monotonic()
            try:
                response = (invoke(provider, model, prompt) if invoke else
                            llm.get_provider(provider).invoke(prompt, model=model))
                latency = round(time.monotonic() - started, 3)
                score = score_response(response.text)
                if score["fingerprint"]:
                    fingerprints.append(score["fingerprint"])
                tokens = (response.input_tokens or 0) + (response.output_tokens or 0)
                ai_budget.record_usage(tokens=tokens, expensive=True)
                rm.record_model_interaction(
                    store, provider=provider, model=model, purpose="provider_benchmark",
                    trigger="bounded_historical_replay", prompt=prompt, response=response.text,
                    status="ok", cycle_id=f"benchmark:{packet.get('packet_id')}",
                    input_tokens=response.input_tokens, output_tokens=response.output_tokens,
                    latency_seconds=latency, extra={"capability": "REASONING",
                    "prompt_version": BENCHMARK_PROMPT_VERSION, "validation": score,
                    "cost": None})
                rows.append({"packet_id": packet.get("packet_id"), "status": "OK",
                             "latency_seconds": latency, "tokens": tokens, "cost": None, **score})
            except llm.ProviderError as exc:
                rows.append({"packet_id": packet.get("packet_id"), "status": "ERROR",
                             "error": str(exc)[:500]})
        ok_rows = [r for r in rows if r.get("status") == "OK"]
        duplicates = sum(v - 1 for v in Counter(fingerprints).values() if v > 1)
        results.append({"provider": provider, "model": model,
                        "status": "COMPLETE" if len(ok_rows) == len(packets) else "PARTIAL",
                        "cases": rows,
                        "summary": {"schema_valid_rate": (sum(bool(r["schema_valid"]) for r in ok_rows) /
                                      len(ok_rows)) if ok_rows else None,
                                    "duplicate_rate": duplicates / len(ok_rows) if ok_rows else None,
                                    "mean_specificity": (sum(r["specificity"] for r in ok_rows) /
                                                         len(ok_rows)) if ok_rows else None,
                                    "mean_falsifiability": (sum(r["falsifiability"] for r in ok_rows) /
                                                            len(ok_rows)) if ok_rows else None,
                                    "mean_constructibility": (sum(r["experiment_constructibility"] for r in ok_rows) /
                                                               len(ok_rows)) if ok_rows else None,
                                    "mean_critic_usefulness": (sum(r["critic_usefulness"] for r in ok_rows) /
                                                               len(ok_rows)) if ok_rows else None,
                                    "mean_latency_seconds": (sum(r["latency_seconds"] for r in ok_rows) /
                                                             len(ok_rows)) if ok_rows else None,
                                    "tokens": sum(r["tokens"] for r in ok_rows), "cost": None}})
    return {"status": "COMPLETE", "cases": len(packets), "results": results,
            "selection_policy": "operator review; benchmark never changes routing automatically"}
