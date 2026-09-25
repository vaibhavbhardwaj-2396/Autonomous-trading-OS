# Phase 10.5 — autonomous validation campaign

Phase 10.5 validates the complete research-to-paper machine under sustained,
capacity-aware operation. It does not authorize live promotion.

## Acceptance matrix

| Requirement | Evidence |
|---|---|
| Research-first loop | Existing observatory, hypothesis intake, immutable contracts, evaluator, evidence, strategy compiler, backtest and paper modules |
| Durable observations independent of LLM | Bitemporal append-only SQLite recorder; model calls occur only after deterministic capture |
| Persistent funnel | `research/validation.py` append-only snapshot ledger |
| Capacity allocation | `control/capacity.py`; exposed through resources and validation APIs |
| News/event plane | First-seen knowledge time, provider identity and versioned deterministic features |
| Provider abstraction | `ModelProvider`, provider registry and credential-mode metadata |
| AI trace | Model interaction artifacts include prompt, response, provider, model, trigger, status, measured tokens and latency |
| Broker abstraction | `BrokerCapabilities` plus normalized account and explicit unsupported history reads |
| Paper readiness | Explicit eligibility required; concrete blockers reported when absent |
| Validation UI | Funnel, capacity, blockers and history tab |
| Architecture and external study | `LIVING_QUANT_ARCHITECTURE.md`, `AI_TRADER_COMPARISON.md` |
| Live safety | No Phase 10.5 route imports or calls live execution; Phase 11 remains locked |

## Scheduled snapshot

The canonical research schedule captures one campaign snapshot after the daily
evening recorder. The snapshot is read-only with respect to research, strategy,
paper and live state.

```bash
venv/bin/python -m research.validation
```

## Honest completion rule

“Paper active” means at least one real immutable StrategyVersion has an explicit
paper-eligibility event and the paper runner has completed a cycle. If those
facts do not exist, the correct result is `BLOCKED` with the exact missing gate;
an empty or forced paper order is never evidence of completion.
