#!/usr/bin/env python3
"""End-to-end read-model acceptance for the integrated operating product."""
from __future__ import annotations
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from api.app import create_app

passed = failed = 0
def check(name, condition):
    global passed, failed
    if condition:
        passed += 1; print(f"  ✓ {name}")
    else:
        failed += 1; print(f"  ✗ {name}")

old = os.environ.get("DASHBOARD_API_TOKEN")
os.environ["DASHBOARD_API_TOKEN"] = "final-product-test-token"
try:
    app = create_app()
    response = app.test_client().get(
        "/product/status", headers={"Authorization": "Bearer final-product-test-token"})
    body = response.get_json() or {}
    check("integrated product read model is available", response.status_code == 200)
    check("Phase 11 remains explicitly locked", body.get("phase_11", {}).get("state") == "LOCKED")
    check("Phase 11 lists explicit human approval", "explicit human approval" in body.get("phase_11", {}).get("requirements", []))
    nodes = {n.get("id") for n in body.get("organization", [])}
    check("organization maps to six real workflow nodes",
          nodes == {"director", "market", "quant", "events", "review", "factory"})
    check("operator view includes today, current work, timeline and blockers",
          all(k in body for k in ("today", "current_research", "timeline", "blockers")))
    check("provider absence is generic rather than OpenAI-specific",
          body.get("ai", {}).get("state") in ("READY", "NO_PROVIDER_CONFIGURED"))
    rules = {rule.rule: rule.methods for rule in app.url_map.iter_rules()}
    check("web API exposes no order-placement route",
          all(not (path.startswith("/order") and "POST" in methods) for path, methods in rules.items()))
finally:
    if old is None:
        os.environ.pop("DASHBOARD_API_TOKEN", None)
    else:
        os.environ["DASHBOARD_API_TOKEN"] = old

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
