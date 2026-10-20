# Living Quant deep status report — 20 October 2026

Audit window: 16:55–17:15 IST. Production baseline at the start of the audit:
`257bea5`. Remediation code: `b89f165`.

## Executive verdict

Living Quant is **operational infrastructure, but not yet a tradable autonomous
product**. The VPS, API, cron scheduler, recorder, broker authentication,
broker reconciliation, watchdog, paper scheduler and Telegram transport are
running. The research factory was alive at the heartbeat level but had made no
meaningful progress for 19 days because one exact-duplicate draft repeatedly
won the queue and consumed the single promotion attempt. The UI therefore
reported a healthier system than the product evidence justified.

The incident is not evidence that a profitable strategy was suppressed. The
best valid holdout result remains `EXP-07BB67-B`: 55 trades, research-notional
net P&L ₹33,006 and t-statistic 1.7629. It is correctly `PROMISING`, not
`ROBUST`, because it does not pass the pre-registered significance threshold.
The one StrategyVersion created before that correction remains quarantined and
is not paper eligible.

Live trading is independently blocked for a second, valid reason: the broker
reports ₹0 free cash and the governed book has `allocated_capital = 0`. The
account's ₹57,498.21 total value consists of 24 unmanaged personal holdings.
Those holdings are context only and cannot be treated as autonomous capital or
sold by the agent. No code fix can or should bypass that mandate.

## Current runtime truth

| Area | State at audit | Evidence | Assessment |
|---|---|---|---|
| VPS | Running | 48 days uptime; load 0.50; disk 48% used; ~726 MB available memory | Healthy |
| Release | `257bea5` at audit start | tracked VPS worktree clean | Reproducible, behind remediation |
| API | Running | `trading-api.service` active; local `/health` returned OK, API v1.7.0 | Healthy |
| Scheduler | Running | cron active; no failed systemd services | Healthy |
| Broker auth | Valid | refreshed 07:15 IST; ~14.2 hours remained; verification reached broker funds | Healthy at audit |
| Broker reconciliation | Complete | total ₹57,498.21; 24 holdings; no mismatches | Healthy, but no free cash |
| Governed capital | Blocked | allocated capital ₹0; managed equity ₹0; free cash ₹0 | Cannot trade |
| Live risk state | RED | peak governed capital ₹3,157.24 vs current ₹0; risk budget ₹0 | Correct fail-close |
| Recorder | Running | 3,417 observations in 7 days; 22,496 total observations | Productive |
| Research worker | Scheduled but stalled | fresh 10-minute heartbeats; last five actions all `skipped_duplicate` | Incident confirmed |
| Draft queue | Backpressured | 80 drafts; 79 actionable, 1 exact redundant duplicate | Starved discovery |
| AI provider | Verified during audit | Anthropic `claude-sonnet-5`; bounded proof succeeded in 1.493 s, 72 input + 9 output tokens | Credential/model work |
| AI research usage | Stale before audit | no successful request since 1 October; 0 calls in preceding 7 days | Was misleadingly labeled healthy |
| Experiment funnel | No recent output | 112 hypotheses; 28 reported experiments; 23 evidence records | Pipeline exists, no current flow |
| Strategy factory | No valid output | 1 quarantined StrategyVersion; 0 paper eligible | Correctly blocked |
| Paper engine | Running but idle | 25 completed cycles; latest 19 October; 0 eligible versions/signals/trades | Infrastructure pass, product fail |
| Telegram | Working | credentials/transport verified and messages received | Healthy |
| Phase 11/live | Locked | no robust paper evidence, zero capital, human gate required | Must remain locked |

## Why the pipeline did not reach paper trading

There were three separate causes, and conflating them made the dashboard look
better than the product:

1. **Queue livelock.** `build_action_queue()` offered the same redundant draft
   on every heartbeat. It ranked first, `attempt_autonomous_promotion()` refused
   it as a duplicate, and that refusal consumed the one promotion slot. The
   next heartbeat repeated the same path. No later draft could be promoted.
2. **Backpressure was counting historical noise as executable work.** All 80
   drafts counted against the discovery high-water mark, including the refused
   duplicate, so new AI discovery was withheld.
3. **The evidence gate has not passed.** Even when experiments ran previously,
   no legitimate result met the complete ROBUST criteria required to mint a
   paper-eligible strategy. Lowering that statistical threshold merely to
   manufacture a trade would invalidate the project.

An additional telemetry defect inflated visible activity: the worker persisted
a ResearchPacket on every heartbeat before it knew whether discovery would be
selected. This produced 67 apparent packets on 20 October while AI calls,
hypotheses and experiments were all zero.

## Remediation delivered in `b89f165`

- Exact duplicates now have a deterministic canonical predecessor. Redundant
  drafts remain in the immutable/auditable registry but are not executable
  promotion actions.
- Promotion checks the explicitly selected draft instead of an aggregate
  hypothesis duplicate flag, eliminating the permanent refusal loop.
- Queue telemetry separates total drafts, redundant drafts and actionable
  drafts. Discovery backpressure uses actionable work.
- A ResearchPacket is persisted only when a DISCOVER action is actually
  selected; idle heartbeats no longer fabricate product activity.
- Provider health becomes `STALE` after 24 hours without a successful request.
  The Overview and runtime matrix now distinguish configured credentials from
  verified recent connectivity.
- Regression tests prove two duplicate drafts produce one deterministic
  promotion, that the canonical draft promotes, and that a backpressured cycle
  produces zero ResearchPackets.

Local release verification passed all 68 Python test modules and all 84
frontend assertions. The AI provider's built-in bounded production proof also
passed. CI/deployment evidence is recorded in the release section below.

## Capital, broker and order safety

The current runtime is INDstocks/INDmoney, while the newly supplied
`AGENTS.md` describes a Zerodha/Kite account and a ₹10,000 allocation. This is
a material operating-contract mismatch. Production code, authentication,
reconciliation and the README currently describe INDstocks. The untracked
`AGENTS.md` was not committed or used to mutate broker state. Before Phase 11,
one broker and one capital mandate must be selected and made consistent across
the runbook, secrets, state schema, reconciliation and human approval record.

Regardless of that mismatch, today's live result is unambiguous:

- complete broker account value: ₹57,498.21;
- broker free CNC cash: ₹0;
- governed allocation: ₹0;
- agent-managed positions: none;
- unmanaged personal holdings: 24;
- permissible new order size: ₹0.

The system must not use the entire account balance as trading capital. Market
value of personal holdings is not cash and is explicitly outside the agent's
mandate. A broker balance can bound liquidity, but it cannot grant authority.

## What is complete and what is not

### Complete as software

- deterministic market/news capture and point-in-time store;
- governed hypothesis contracts and experiment execution;
- evidence evaluation, strategy versioning and paper eligibility gates;
- scheduled paper cycles and reconciliation;
- separate observer/admin API roles and operator UI;
- daily broker authentication and read-only reconciliation;
- resource, scheduler, worker and Telegram telemetry;
- CI, guarded VPS deployment and rollback path.

### Incomplete as a product outcome

- no statistically ROBUST strategy;
- no paper-eligible strategy;
- no paper trades and therefore no longitudinal paper performance;
- no governed cash allocation and no broker free cash;
- no live canary approval or rollback rehearsal;
- unresolved INDstocks-versus-Zerodha operating contract;
- no basis for claiming autonomous trading readiness.

## Recovery plan

### P0 — restore honest flow (this release)

1. Deploy `b89f165` only after exact-SHA GitHub CI passes.
2. Restart the read-only API and run one bounded worker heartbeat.
3. Confirm the duplicate draft is skipped without consuming promotion capacity.
4. Confirm a later actionable draft is promoted or a precise evidence/budget
   refusal is recorded.
5. Confirm the dashboard shows the AI proof as recent and exposes actionable
   versus redundant backlog.

### P1 — drain and evaluate, without weakening science

1. Let the existing rolling lock budget admit at most 20 general experiments
   per seven days and preserve the five reserved confirmation slots.
2. Run experiments one at a time under the existing hard deadline and resource
   governor; do not bulk-lock all 79 actionable drafts.
3. Add explicit deterministic draft retirement criteria for stale, invalid or
   economically redundant ideas. Do not count never-locked retirement as a
   statistical comparison.
4. Calibrate research priorities from recorded outcomes, not from activity
   counts.
5. Require confirmation/holdout evidence before any StrategyVersion becomes
   paper eligible.

### P2 — produce a real paper record

1. Once a valid StrategyVersion is eligible, run the unchanged paper engine on
   schedule with zero live-order capability.
2. Accumulate enough signals, fills, costs, exits and failure cases to judge
   execution quality and drawdown behaviour.
3. Publish conversion and rejection reasons in the dashboard; a zero should
   say whether it reflects no opportunity, a safety refusal or a failure.
4. Rehearse expired auth, missing quotes, process crash, stale state, API
   outage and deployment rollback.

### P3 — resolve the human-only boundaries

1. Select INDstocks or Zerodha as the canonical broker contract.
2. Establish a non-zero explicit governed allocation and actual broker cash if
   live trading is still desired; never infer authority from holdings value.
3. Approve one exact paper-proven canary StrategyVersion, risk budget and
   rollback rule.
4. Keep Phase 11 locked until all three decisions are recorded. A green deploy
   or a successful AI call is not trading authorization.

## Release evidence

| Gate | Result |
|---|---|
| Python suite | 68/68 modules passed |
| Frontend suite | 84/84 assertions passed |
| Guardrail files | unchanged |
| Order placement | none |
| Provider proof | Anthropic success; 81 total tokens; no credential exposed |
| GitHub CI | pending at report creation |
| VPS deployment | pending at report creation |
| Post-deploy worker proof | pending at report creation |

The project should now be judged on funnel movement and evidence quality, not
heartbeat count. “Running” means a scheduler is alive; “working” means useful
artifacts advance through experiments and paper under the gates. Before this
incident fix, only the first statement was true.
