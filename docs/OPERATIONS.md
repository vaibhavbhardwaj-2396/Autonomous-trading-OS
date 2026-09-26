# Living Quant operations

This is the canonical operator entry point for the production system. Phase 11 is
locked: deployment, research, deterministic experiments, paper operation and
observability never authorize a live order.

## Runtime truth

Three signals determine each subsystem's effective state:

1. desired state in `control/component_state.json`;
2. an active scheduler entry (comments do not count);
3. a fresh successful heartbeat and ready dependencies.

`python -m scripts.watchdog` reconciles those signals every five minutes and writes
`control/watchdog_state.json`. The System screen and `GET /system/status` display this
authoritative result. `CONFIGURATION_DRIFT` means a component is desired RUNNING but its
schedule is missing. `BLOCKED` means an external dependency is absent. `STALE` means the
schedule exists but its heartbeat is overdue. State transitions generate exception-only
Telegram alerts; routine healthy cycles do not.

Component controls are persistent and audited. The Admin screen can set RUNNING, PAUSED or
DISABLED with a required reason. Pausing `RESEARCH_AI` does not pause deterministic
experiments; those are governed separately by `EXPERIMENTS`. Re-enabling a component does
not bypass budgets, eligibility, evidence or risk gates.

## Production schedule

- `deploy/broker.cron`: daily INDstocks session refresh and read-only reconciliation.
- `deploy/research.cron`: deterministic recording, bounded research worker, validation,
  paper heartbeat, daily Telegram digest and watchdog.
- `deploy/living-quant-api.service`: authenticated API under the restricted service user.

Install the canonical cron files without commenting jobs out as a control mechanism.
Pause through the component-control plane instead, so intent and reality remain visible.

## Safe checks

```bash
cd /root/trading-agent
venv/bin/python -m scripts.watchdog --no-notify
venv/bin/python -m research.validation --no-write
venv/bin/python -m engine.execute status
venv/bin/python scripts/indstocks_auth.py --status
systemctl status living-quant-api --no-pager
curl -fsS http://127.0.0.1:8787/health
```

The watchdog makes no broker or AI call. Validation is read-only with `--no-write`.
`engine.execute status` is also non-ordering. Never use broker APIs directly for orders.

## External dependencies

OpenAI research requires a server-side `OPENAI_API_KEY` in the root-owned `.env`. It is
never committed, returned by the API or sent to the browser. Without it, `RESEARCH_AI`
must remain PAUSED or appears BLOCKED; deterministic ingestion and experiments continue.
The worker loads this file only at its provider boundary. A future key installation should
be verified with one bounded research call and its recorded usage artifact.

INDstocks authentication is refreshed daily using TOTP/MPIN credentials in the same
root-only environment. Failure makes broker data stale and trading fail closed. The
complete broker balance is account context; autonomous capital remains only managed cash
and managed positions. Personal holdings are unmanaged and cannot be sold by the agent.

## Research queue and bottleneck

`GET /validation/status` reports funnel totals, 24-hour and seven-day velocity, research
families, backlog status counts and the current bottleneck. The September 2026 production
audit found 109 hypotheses/contracts: 104 DRAFT and 5 REPORTED, with zero evidence and zero
strategy versions. The bottleneck is therefore DRAFT-to-LOCKED review/promotion, not data
collection. Backpressure blocks more AI discovery when downstream queues are full; it does
not fabricate promotions or weaken evidence rules.

## Incident sequence

1. Read `/system/status` and `/runtime/status`; identify desired, scheduler, heartbeat and
   dependency state separately.
2. If broker state is stale or mismatched, do not trade. Run the read-only auth status and
   broker reconciliation diagnostics.
3. If a schedule is absent, restore the canonical cron entry and rerun the watchdog.
4. If a heartbeat is stale, run only that component's safe bounded command and inspect its
   persisted error. Do not declare recovery from process exit alone.
5. Pause the affected component with a reason if it can produce repeated failures or cost.
6. Confirm the transition in System and Telegram. Record material recovery evidence.

## Release and rollback

Only deploy a GitHub commit whose CI checks passed. `scripts/deploy_vps.sh --ref <sha>
--restart-api` fast-forwards to the exact SHA, runs safety checks and rolls back on failure.
After deployment verify the exact VPS SHA, API version, System status, auth boundaries,
CORS, dashboard assets and Telegram delivery. A successful deployment never unlocks Phase
11 and never proves broker authentication unless the broker verification itself succeeded.

See [Deployment](DEPLOYMENT.md), [API](API.md), [architecture](LIVING_QUANT_ARCHITECTURE.md),
[P0 recovery](P0_RUNTIME_RECOVERY.md) and [AI cost optimization](AI_COST_OPTIMIZATION.md).
