#!/usr/bin/env python3
from __future__ import annotations
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from research import memory as rm
from research.brain import llm
from research.brain.provider_benchmark import run_benchmark, score_response
from research.store import Store

passed = failed = 0
def check(name, condition):
    global passed, failed
    if condition:
        passed += 1; print(f"  ✓ {name}")
    else:
        failed += 1; print(f"  ✗ {name}")

valid = {"statement": "Abnormal volume after a positive disclosure predicts delayed continuation.",
         "economic_mechanism": "Slow information diffusion causes delayed repricing after the public event.",
         "expected_effect": "positive five-day excess return", "universe": "Nifty 500",
         "features": ["volume_z", "event_sentiment"], "time_horizon": "5 sessions",
         "regime_assumption": "liquid non-crisis sessions",
         "falsification_criteria": "Reject if holdout net expectancy is below or equal to zero.",
         "required_datasets": ["prices_eod", "announcements"],
         "critic_findings": "Check event-time leakage, sector confounding, sample size and transaction costs."}

score = score_response(json.dumps(valid))
check("valid structured hypothesis passes the benchmark schema", score["schema_valid"])
check("rubric measures specificity/falsifiability/constructibility/critique", all(
    score[k] > 0 for k in ("specificity", "falsifiability", "experiment_constructibility", "critic_usefulness")))
check("malformed output receives no schema credit", not score_response("not json")["schema_valid"])

with tempfile.TemporaryDirectory() as td:
    store = Store.open(Path(td) / "research.db")
    packet = {"packet_id": "RP-TEST", "knowledge_timestamp": "2026-09-30T10:00:00+05:30",
              "source_artifacts": ["detection:1"], "symbols": ["ABC"], "universe": "Nifty 500",
              "regime": "RANGE_BOUND", "deterministic_features": [], "anomalies": [], "events": [],
              "related_research": [], "prior_negative_evidence": [], "data_quality": {"firewall": "PASS"},
              "research_question": "Does abnormal volume persist?", "novelty_score": 1.0,
              "significance": {"admitted": True}}
    rm.record_research_packet(store, packet)
    def fake(provider, model, prompt):
        return llm.ModelResponse(json.dumps(valid), 100, 50)
    with patch("research.brain.provider_benchmark.ai_budget.budget_allows", return_value=(True, None)), \
         patch("research.brain.provider_benchmark.ai_budget.record_usage"):
        result = run_benchmark(store, [{"provider": "openai", "model": "test-model"}], invoke=fake)
    check("benchmark is bounded and completes one historical packet", result["cases"] == 1 and result["status"] == "COMPLETE")
    check("benchmark reports schema, latency, tokens and unknown cost", result["results"][0]["summary"]["tokens"] == 150 and result["results"][0]["summary"]["cost"] is None)
    check("benchmark persists an auditable model-interaction trace", len(rm.query_research_log(store, rm.DATASET_MODEL_INTERACTION, limit=None)) == 1)
    check("benchmark never changes model routing automatically", "never changes routing" in result["selection_policy"])
    store.close()

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
