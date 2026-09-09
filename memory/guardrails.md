---
purpose: Hard risk rules. These override strategy, research, conviction, and targets every
single time. No run may trade past these limits regardless of how good the setup looks.
last_updated: 2026-09-02
authority: Only Vaibhav may change this file. The agent may never edit it.
---

# Guardrails (non-negotiable)

If any rule here conflicts with anything in `strategy.md`, `mission.md`, or the agent's own
analysis — **this file wins.** No exceptions, no "just this once," no "high conviction."

---

## 0. The agent's mandate (added after the first live run)

**The agent manages an ALLOCATION, not the account.**

The Zerodha account is Vaibhav's personal one. It holds long-term positions — equities and
sovereign gold bonds — that have nothing to do with this experiment. The first live sync
found ₹5.7 lakh across 21 holdings.

Therefore:

- `allocated_capital` in `memory/state.json` is the agent's entire mandate. Only Vaibhav
  changes it. Every risk limit below is a percentage **of that number**, never of the
  account balance.
- Agent book value = `allocated_capital` + realized P&L from the agent's own trades.
- Positions the agent did not open are **unmanaged**. They are not capital, not
  collateral, and not available for the agent to sell.
- **The agent may not trade any symbol held as an unmanaged position.** Quantities merge
  at the broker, so trading the same symbol would make P&L attribution impossible and
  could let an agent stop-loss sell shares it never bought. The two books stay disjoint.
- The agent can only spend cash that actually exists. If broker free cash is below the
  agent's notional cash, sizing is capped by the real figure.

## 1. Capital and drawdown limits

| Limit | Threshold | Action when hit |
|---|---|---|
| Per-trade risk | 2% of current capital | Position size is derived from this — see §3 |
| Daily loss cap | 5% of day's starting capital | Stop opening positions for the rest of the day |
| Weekly loss cap | 10% of week's starting capital | Stop opening positions for the rest of the week |
| Drawdown from peak | 10% / 15% / 20% | Staged escalation ladder — see §1a |

- "Peak capital" = the highest end-of-day total account value ever recorded in
  `portfolio_state.md`. It ratchets up and never down.
- These caps are checked at the **start of every run** before any other logic. If a cap is
  breached, the run does research and logging only.
- Two consecutive losing days → new entries paused; flag for review in the Telegram summary.
  Resume requires Vaibhav's acknowledgement.

---

## 1a. Drawdown escalation ladder

The point of three stages rather than one cliff: catch a failing strategy early, and force a
real diagnosis at each level instead of drifting toward the floor. At ₹10,000 capital these
are ₹1,000 / ₹1,500 / ₹2,000 from peak.

### 🟡 AMBER — 10% drawdown from peak

**Immediate:** stop opening new positions. Existing positions continue to be managed
normally against their existing stops (do not panic-close; the stops were set for a reason).

**Required diagnostic**, written to `research_log.md` and sent via Telegram:
1. List every losing trade contributing to the drawdown, with its thesis and regime at entry.
2. **Was each trade rule-compliant?** Count A/B/C/F process grades across the drawdown.
3. Was the regime classification correct in hindsight for each?
4. Is the loss concentrated (one bad setup type / one regime / one stock) or spread evenly?
5. **Variance or broken edge?** Compare realised results against what the strategy's own
   measured expectancy and variance predict. A drawdown inside the expected range is noise;
   one outside it is evidence something changed.
6. Did anything external change — market regime shift, volatility spike, a data or
   execution problem?

**Resume conditions** — new entries may restart if *either*:
- The diagnostic identifies a **specific, fixed cause** (a process bug, a rule that was
  bent, a setup type that's clearly broken and has now been removed), **or**
- The evidence says ordinary variance *and* process grades were A/B throughout.

Otherwise it stays paused until Vaibhav acknowledges. Resume at **normal** size — never
increased.

### 🟠 ORANGE — 15% drawdown from peak

**Immediate:** stop opening new positions. **Auto-resume is no longer available** — restart
requires Vaibhav's explicit acknowledgement, without exception.

**Required, in addition to the Amber diagnostic:**
1. Full strategy audit: expectancy per setup type and per regime over all trades to date.
2. Explicit written verdict on the central question — **is the edge broken, or is this
   variance?** The agent must be willing to write "there is no evidence of an edge here."
3. Identify the single highest-conviction change (drop a setup, tighten a filter, restrict
   to one regime) — one change, not five.
4. Check for execution/infrastructure faults: were stops actually placed on every trade? Did
   every scheduled run fire? Was any data stale?

**On resume, risk per trade is halved to 1%** and stays there until the strategy has
recovered to within 5% of peak capital. Rationale: if confidence in the edge has genuinely
dropped, size should drop with it. Position sizing tracks conviction, and conviction is now
lower — that is the honest response, and it's the mathematical opposite of doubling down.

### 🔴 RED — 20% drawdown from peak (hard floor)

**Immediate and absolute: full stop.** No new positions under any circumstance. Alert
Vaibhav via Telegram immediately. Existing positions: manage to their pre-existing stops or
close them — Vaibhav's call, not the agent's.

**Doubling down at this level is forbidden.** Recorded explicitly so the option is never on
the table:
- After a 20% drawdown you have *less* evidence the strategy works than you had at the
  start. Increasing size at the point of maximum evidence-against is backwards.
- Recovery arithmetic is asymmetric and punishing: −20% needs +25% to break even, −40%
  needs +67%, −50% needs +100%. Increasing size after losses is what converts a recoverable
  drawdown into an unrecoverable one.
- This is the martingale trap, and it is the single most common way retail accounts reach
  zero.

**The only three permitted outcomes**, and all require Vaibhav's decision:
1. **Stop permanently** — the experiment did not work; capital preserved. This is a
   legitimate and honourable outcome, not a failure to be avoided at all costs.
2. **Fix and restart small** — a specific, identified, *fixed* fault (a process bug, an
   execution failure, a strategy rule now removed) justifies restarting at **1% risk per
   trade** with a written hypothesis for what will be different. Not larger size. Not "more
   aggressive to recover."
3. **Pause and observe** — no trading for a defined period, agent continues research and
   logging only, to see whether the market regime that caused the damage passes.

**"Add more capital to recover faster" is not one of the options.** Adding funds to a
strategy with unproven or negative expectancy increases the amount at risk without
addressing why it lost. Fresh capital may only be added *after* a decision to fix-and-
restart has been made and the fix is in place — as new capital for a corrected strategy,
never as ammunition for the one that just failed.

### Recovery
Once capital recovers to within 5% of peak, risk per trade returns to the standard 2% and
the ladder resets. Peak capital itself never resets downward.

## 2. Instrument permissions (gated by capital)

The agent may only trade what its current capital actually supports. Current tier is
determined by capital in `portfolio_state.md`. Full tier definitions live in `strategy.md`.

**At current capital (₹10,000) — Tier 0:**
- ✅ Allowed: NSE/BSE equity cash (delivery and intraday)
- ❌ Forbidden: all options (long or short), all futures, all leveraged/margin products

**Permanently forbidden at any capital without explicit written approval from Vaibhav:**
- Naked/uncovered option writing (undefined loss potential this account cannot absorb)
- Any position where maximum theoretical loss exceeds the per-trade risk limit in §1
- Buying far-OTM "lottery ticket" options as a strategy — cheap premium is cheap because
  the probability-weighted payoff is poor, and at small size the flat ₹20/order cost makes
  it worse
- Averaging down into a losing position
- Trading on margin/leverage of any kind (MIS/CO leverage included) beyond what the
  per-trade risk limit permits

## 3. Position sizing (formulaic — never discretionary)

Every position size is computed, not chosen:

```
risk_amount   = current_capital × 0.02          # 2% per trade
stop_distance = |entry_price − stop_price|
quantity      = floor(risk_amount / stop_distance)
position_cost = quantity × entry_price
```

Then all of the following must hold, or the trade is rejected:

- `position_cost` ≤ 40% of current capital (no single position dominates the book)
- Free cash after entry ≥ max(₹1,000, 10% of capital)
- Maximum **3 open positions** at once at Tier 0/1; the sum of open risk (in ₹) across all
  positions must never exceed 6% of capital
- `stop_distance` must be wide enough that expected move clears costs — see §5

If `quantity` computes to 0, there is no trade. Do not round up to 1.

## 4. Stop-losses and exits (mandatory at entry)

- **No position is ever opened without a predefined stop-loss and target**, both recorded
  in `trade_log.md` at entry time, before the order goes in.
- Stops may only ever be moved in the direction that reduces risk (trailing up on a long,
  down on a short). **Widening a stop is forbidden.**
- A stop that is hit is honoured. The agent does not re-enter the same idea on the same day
  after being stopped out ("no revenge re-entry").
- Minimum reward:risk on any new position: **1.5:1**. If the target isn't at least 1.5×
  the stop distance away, the trade doesn't qualify.

## 5. Cost awareness (critical at this account size)

Reference costs at Zerodha (verify periodically — these change):

| Segment | Round-trip cost on a ~₹5,000 position |
|---|---|
| Equity delivery | ~₹11-12 (~0.22%) — ₹0 brokerage, mostly STT |
| Equity intraday | ~₹6-7 (~0.13%) |
| Options (₹7,000 premium) | ~₹55-60 (~0.8%) — the flat ₹20/order dominates at small size |

**Rule:** the expected move to target must be at least **3× the round-trip cost** of the
trade. This structurally rules out scalping and high-frequency churn at this capital — the
cost drag would eat the edge. Fewer, better positions.

## 6. Circuit breakers (stop and escalate, don't improvise)

The agent must place **no trades** and log the reason if any of these occur:

- It cannot read or confirm current capital / open positions from `portfolio_state.md`
- Kite API returns errors, times out, or reports account state that disagrees with
  `portfolio_state.md`
- An order partially fills or fills at a price materially different from expected — stop,
  log, alert. Do not "fix" it with more orders.
- Market-wide circuit breaker, trading halt, or extreme gap (>3% index move) — reassess,
  do not trade the chaos
- A guardrail limit in §1 has been breached
- The agent's own reasoning is uncertain — **when in doubt, do nothing.** No-trade is
  always an available, valid, unpunished decision.

## 7. Operational safety

- Credentials live only in `.env` on the VPS, never in git, never in logs, never in
  Telegram messages.
- Order placement only from the whitelisted static IP (the Vultr box), per SEBI's algo
  rules for API-based retail trading.
- Every order placed is logged to `trade_log.md` **before** confirmation is awaited, so a
  crash mid-order still leaves a record.
- Rate limits respected (Zerodha: 10 orders/second cap; this strategy should never come
  close).

## 8. What the agent may and may not change

| File | Agent may edit? |
|---|---|
| `strategy.md` | ✅ Yes — with statistical justification per `review_process.md` |
| `trade_log.md`, `research_log.md`, `portfolio_state.md` | ✅ Yes — must update every run |
| `mission.md` | ⚠️ Only to record results/learnings, never to change targets or limits |
| `guardrails.md` | ❌ **Never.** Vaibhav only. |

If the agent believes a guardrail is wrong, it writes that argument into the weekly review
for a human to consider. It does not act on it.
