# Phases 1–10 acceptance record

This release completes the autonomous research-to-paper system and its secure
operator control plane. It deliberately stops before live strategy activation.

| Phase | Acceptance evidence |
|---|---|
| 1 | `research.brain.worker`, opportunity lifecycle and queue-health tests bound work and apply downstream backpressure. |
| 2 | `/research/worker-status`, `/research/data-quality`, `/operations/status`, paper performance APIs and dashboard panels expose explicit missing/degraded/healthy states. |
| 3 | `.github/workflows/ci.yml` runs every Python test module and the dependency-free frontend suite; `scripts/deploy_vps.sh` checks CI and supports rollback. |
| 4 | `Store.quality_inventory()` is knowledge-time gated; `research.data_quality` reports freshness/coverage and verifies append-only triggers. |
| 5 | `strategies.spec` accepts only typed, whitelisted conditions/exits and compiles a deterministic, source-hashed `StrategyVersion`. |
| 6 | `research.strategy_factory` refuses anything below `ROBUST`, requires a verified reported contract, records immutable provenance and is idempotent. |
| 7 | Strategy replay remains point-in-time and now declares costs, two-sided slippage, stop-loss, target and maximum-hold exits. Existing experiment contracts retain discovery/validation/holdout firewalls. |
| 8 | The production algorithm is explicitly registered in both adapters. Paper cycles are bounded, locked, idempotent and reconcile typed exits through the paper ledger. |
| 9 | Paper allocation/accounting is isolated from live state. The live `engine.guardrails` and mandate boundaries are unchanged and remain the only route to any future proposed order. |
| 10 | Every operational endpoint is bearer-authenticated except liveness, CORS is allow-listed, observer credentials cannot invoke the two audited mode/provider writes, the administrator credential is separate and session-only in the UI, and the UI covers broker, research, data, paper, resources, AI and system health. |

The integrated acceptance test is `tests/test_phase_5_10.py`; existing focused
test modules remain the authoritative regression suite for phases 1–4 and each
underlying subsystem.

## Hard boundary after this release

Phase 11 is not complete and cannot be inferred from a green Phase 10 release.
No strategy is automatically paper-approved or live-enabled. No broker adapter
was added to the strategy factory. A live order still requires the existing
`python -m engine.execute propose ...` path, its independent guardrail validation,
and the separate human-gated live-readiness decision.
