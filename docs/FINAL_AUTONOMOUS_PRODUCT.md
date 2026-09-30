# Final autonomous operating product

This release connects the existing components into one explainable loop:

```text
deterministic observation
  → significance/deduplication gate
  → compact ResearchPacket
  → capability-routed research reasoning (when configured)
  → structured hypothesis and immutable ExperimentContract
  → bounded point-in-time experiment
  → family-adjusted evidence and negative memory
  → deterministic ROBUST Strategy Factory gate
  → explicit paper eligibility
  → scheduled portfolio simulation, exits and reconciliation
  → strategy-health transition
  → research-memory feedback
```

The ResearchPacket contains a stable packet ID, source artifact references,
knowledge timestamp, observed symbols/universe, deterministic features,
anomalies, configured events, related research, prior negative evidence, data
quality, novelty/significance result and one research question. Quiet arrivals
do not call AI.

The Overview product read model exposes Today in Research, real workflow nodes
for Research Director / Market Intelligence / Quant Research / Event
Intelligence / Independent Review / Strategy Factory, active human-readable
investigations, lineage, blockers and a unified timeline. Nodes report actual
work only. Admin exposes providers, capability routes, role routes, budgets,
health, traces and a bounded provider test. There remain exactly six top-level
areas.

Paper now reports NAV/equity, cash, exposure, drawdown, turnover, costs and
strategy attribution. Strategy health is deterministic (`HEALTHY`, `WATCH`,
`DEGRADING`, `INVALIDATED`); changes never mutate a StrategyVersion. A separate
scheduled bridge records health transitions in research memory, preserving the
paper package's isolation boundary.

The watchdog adds scientific-movement exceptions for runnable experiment
backlog, repeated provider failures and stale paper heartbeats when eligible
strategies exist. The daily Telegram digest reports system/research/paper
state, daily movement, evidence/strategy/paper totals, P&L, drawdown, AI usage,
compute state and the main blocker without an AI formatting call.

Provider selection can be evaluated with a bounded replay of compact historic
ResearchPackets. The deterministic rubric measures research usefulness,
latency and usage independently of trading P&L; results are traces rather than
automatic routing decisions. See [AI providers](AI_PROVIDERS.md).

## Deliberate locks and external dependency

- Phase 11 is still `LOCKED`; there is no UI/API order route or live activation.
- Paper eligibility remains explicit and audited.
- No production AI credential exists in source control. Until a supported VPS
  credential and route pass the bounded provider proof, the state is
  `NO_PROVIDER_CONFIGURED`; all deterministic work remains available.
- The seven-day AI observation window begins only after that proof succeeds.
