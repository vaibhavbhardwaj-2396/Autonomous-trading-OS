# HKUDS AI-Trader comparison

Reviewed sources:

- [HKUDS/AI-Trader official repository](https://github.com/HKUDS/AI-Trader)
- [AI-Trader research paper](https://arxiv.org/abs/2512.10971)

The comparison is architectural, not an endorsement of its strategies or live
results. The paper's central warning—that risk control is a major determinant
of robustness and that LLM agents commonly show weak returns/risk management—is
consistent with Living Quant's fail-closed posture.

| Area | AI-Trader | Living Quant | Decision |
|---|---|---|---|
| Product shape | FastAPI/backend and React UI with separate workers | Flask API, zero-build frontend, isolated cron workers | Keep separation; do not introduce a frontend build chain solely for parity |
| Worker control | Capacity-aware worker throttling and visible experiment progress | Ratio-based governor plus five-state capacity plan | Adopt explicit capacity allocation and progress visibility |
| Experiments | Agent experiments and benchmarking | Immutable contracts, point-in-time replay, multiple-comparison ledger | Keep Living Quant's stronger falsification/firewall rules |
| Paper trading | Broker/signal-oriented paper workflows | Broker-free isolated simulation with explicit eligibility | Keep hard isolation and explicit eligibility |
| Observability | UI progress and worker state | Artifacts, operations, resource and validation views | Extend funnel, blockers, lineage and AI activity |
| Storage | PostgreSQL production, SQLite local | SQLite WAL on one VPS | Retain SQLite until measured write contention or scale justifies migration |
| Social features | Copy trading, leaderboards and sharing | None | Do not adopt; incentives conflict with research honesty |
| Live promotion | Product-specific execution workflows | No automatic Phase 10.5 promotion | Keep Phase 11 human-gated |

## Adopted patterns

1. Separate API/UI availability from background research work.
2. Make worker capacity and experiment throughput visible.
3. Preserve paper workflows as first-class operational surfaces.
4. Treat model-provider choice as an adapter concern, not strategy logic.

## Rejected patterns

1. Leaderboards, social proof and copy-trading incentives.
2. Automatic promotion from an attractive result to live capital.
3. LLM reasoning as a substitute for point-in-time data or deterministic risk.
4. A database migration without measured need on the current one-VPS topology.

## Resulting advantage

Living Quant is deliberately less theatrical and more auditable: append-only
knowledge times, immutable hypotheses, explicit evidence gates, isolated paper
state, broker-independent safety, and a permanent no-shortcut edge from research
to live execution.
