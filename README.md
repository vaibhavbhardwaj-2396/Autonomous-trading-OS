# Living Quant — Autonomous Trading OS

Living Quant is a safety-gated research, strategy, backtest, paper-trading and
operator-control system connected to an INDmoney / INDstocks brokerage account.
Roadmap phases 0–10 are implemented. Phase 11 (human-approved live canary) is
deliberately not complete and a green deployment never authorizes an order.

Production:

- Dashboard: <https://autonomous-trading-os.netlify.app>
- API: <https://tradingbotapi.bhardwajvaibhav.com>
- Runtime: Vultr VPS, `/root/trading-agent`

## Safety boundary

The only permitted order path is `python -m engine.execute propose ...`.
It independently applies the protected guardrails. The web API has no order
endpoint. Administrator controls can change the global operating mode and the
research AI provider only; they cannot place, size, modify or cancel a trade.

`memory/guardrails.md` and `engine/guardrails.py` are protected. Existing
personal holdings are visible and included in total account valuation, but are
unmanaged and cannot be traded unless explicitly approved through the governed
overlap process.

## Broker-derived capital

Every successful reconciliation reads INDstocks directly:

```text
complete account value = broker free CNC cash
                       + current LTP × quantity for every holding/position

managed equity = all broker free CNC cash
               + current LTP × quantity for agent-managed positions
```

There is no fixed ₹10,000 benchmark in the migrated production model. The
complete brokerage balance is displayed, but personal pre-existing shares are
not silently converted into autonomous trading capital. A missing quote makes
the total incomplete and withholds it from the dashboard instead of publishing
a plausible but understated number. See [Capital model](docs/CAPITAL_MODEL.md)
and [Broker truth](docs/BROKER_TRUTH.md).

## INDstocks authentication and reconciliation

INDstocks tokens expire every 24 hours. The production schedule runs
`scripts/refresh_indstocks_session.sh` at 07:15 IST every day. It performs
TOTP/MPIN login, verifies the session against broker funds, runs a read-only
broker sync, and uses `flock` to prevent overlaps. No order command exists in
this job. Canonical schedule: `deploy/broker.cron`.

## Dashboard roles and UI

- **Observer:** the deployed dashboard token can read account, trading, paper,
  research, strategy, artifact and health data.
- **Administrator:** the separate `DASHBOARD_ADMIN_TOKEN` is entered on the
  **🔒 Admin** tab and retained only in browser `sessionStorage`. The observer
  token is rejected on administrator APIs.

The UI includes direct card navigation, a manual refresh action, broker
freshness in Operational Health, responsive layouts and explicit stale/error
states. Administrator secrets are never committed or embedded in the public
frontend.

## Roadmap status

| Phase | Outcome | Status |
|---|---|---|
| 0 | operating contract, inventory, delivery workflow | Complete |
| 1 | bounded research throughput and backpressure | Complete |
| 2 | worker/data/paper observability | Complete |
| 3 | isolated tests, CI, deploy and rollback gates | Complete |
| 4 | point-in-time market-data quality and provenance | Complete |
| 5 | typed strategy DSL and deterministic compiler | Complete |
| 6 | evidence-to-strategy factory | Complete |
| 7 | realistic backtests and holdout controls | Complete |
| 8 | paper factory, accounting and reconciliation | Complete |
| 9 | portfolio/risk integration within protected guardrails | Complete |
| 10 | secure Admin control plane and complete operator UI | Complete |
| 11 | live-readiness evidence, approval, canary and rollback rehearsal | Pending |

Detailed evidence: [Phases 1–10 acceptance](docs/PHASE_1_10_ACCEPTANCE.md).
Current plan: [Implementation roadmap](docs/IMPLEMENTATION_ROADMAP.md).

## Delivery and verification

GitHub Actions runs every `tests/test_*.py` module and the frontend suite.
`scripts/deploy_vps.sh` accepts only a successful CI SHA, fast-forwards the VPS,
runs a guardrail smoke test, restarts services and rolls back on failure.
Netlify publishes the frontend separately; the API and exact hosted asset are
verified after release.

Useful non-order checks:

```bash
venv/bin/python -m tests.test_guardrails
venv/bin/python -m engine.execute status
venv/bin/python scripts/indstocks_auth.py --status
venv/bin/python scripts/indstocks_auth.py --verify
```

Operational docs: [Deployment](docs/DEPLOYMENT.md), [API](docs/API.md),
[Delivery workflow](docs/DELIVERY_WORKFLOW.md).

## Release history (commit-wise)

| Commit | Delivered |
|---|---|
| `ea3c6ca` | research queue backpressure |
| `0302a00` | worker queue health telemetry |
| `de28ff0` | production API deployment readiness |
| `404d4ef` | roadmap continuation and frontend admission state |
| `937b6a2` | integrated roadmap phases 0–10 release |
| `56f9527` | daily broker auth/reconciliation, complete-valuation fail-close, separate Admin role, interactive UI and documentation refresh |

This project operates around real money. Process compliance is more important
than activity or profit; no-trade is always a valid result.
