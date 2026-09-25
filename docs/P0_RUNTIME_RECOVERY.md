# P0 runtime recovery audit

Audit time: 2026-09-25 23:50 IST. Production SHA at audit: `e2e80fc`.

## Finding

The organism was not operating end to end. The API and deterministic ingestion were alive,
but every AI research-worker cron entry and the paper cron entry had remained commented out
since 17 September with the label `PAUSED (token budget)`. No persisted global control file
existed, so the control plane reported its documented default `RUNNING`; scheduler reality and
the high-level label had diverged. The research database continued growing only after the six
deterministic recorder jobs were restored on 25 September.

Telegram was not failing. `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` were configured and the
Telegram `getMe` check succeeded. The notification-capable research process simply stopped
being scheduled. Existing worker telemetry contained 595 `not_attempted` notification results
and no recorded success or failure. The former notifier also kept no delivery audit, so an
older last-success timestamp cannot be reconstructed honestly.

OpenAI autonomous research is blocked because `OPENAI_API_KEY` is absent on the VPS. Consumer
ChatGPT login state is not an acceptable substitute. Until a developer API key is installed in
the server-only environment, production must report `OPENAI_PROVIDER_NOT_CONFIGURED` and the AI
worker must not be presented as active.

## Runtime matrix at incident audit

| Module | Expected | Actual | Last successful/attempt | Queue / dependency |
|---|---|---|---|---|
| Market recorder/news | scheduled | restored; producing observations | 25 Sep 23:25 manual recovery run | public feeds |
| Observatory | deterministic recorder input | data present; quote/option-chain sources degraded | 25 Sep | market sources |
| Discovery/hypothesis generation | 10-minute worker | scheduler paused | 17 Sep 13:11 | AI Gateway |
| Research allocator/experiment workers | 10-minute worker | scheduler paused | 17 Sep 13:11 | research store + AI for discovery |
| Evidence/Strategy Factory | worker downstream | no productive work; zero accepted evidence | 17 Sep | reported experiments |
| Paper engine | weekday 19:00 | scheduler paused, no eligible strategy | 16 Sep 19:00 | explicit paper eligibility |
| Broker sync | daily 07:15 | active and successful | 25 Sep 07:15 | INDstocks session |
| AI Gateway | provider-neutral | Anthropic CLI unavailable; OpenAI key missing | 17 Sep | developer API credential |
| Telegram | event driven | credentials valid; no scheduled producer | connectivity verified 25 Sep | Telegram API |
| Resource governor | request-time | active, LOW_LOAD | 25 Sep | OS metrics |
| Validation ledger | weekday 18:45 | active | 25 Sep | authoritative stores |

## Recovery design

- Audited notification preferences and delivery ledger, with no credentials in either.
- Admin-only test action with message label `LIVING QUANT SYSTEM TEST`.
- Deterministic daily research digest at 20:00 IST.
- Persistent Overview status bar, active-work panel, funnel, runtime matrix, model routing and
  notification health.
- Current OpenAI role mappings live in one persisted configuration object. Research modules
  continue to use the gateway, never provider SDK calls.
- Phase 11 remains locked; none of these controls can submit an order.

## Secure OpenAI configuration requirement

Set `OPENAI_API_KEY` only in `/root/trading-agent/.env` (mode `600`) on the VPS, then select
`openai` through the authenticated Admin AI control. Never commit it, paste it into frontend
configuration, log it, store ChatGPT cookies, or automate a consumer ChatGPT browser session.
After configuration, run one bounded discovery cycle and verify its model-interaction artifact,
token usage and trace before re-enabling the recurring worker schedule.
