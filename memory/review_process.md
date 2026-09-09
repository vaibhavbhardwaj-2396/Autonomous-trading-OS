---
purpose: How the agent learns. Defines the daily and weekly review protocols, what gets
measured, and the rules governing when a strategy change is allowed. This is the mechanism
that turns a static script into an agent that actually improves.
last_updated: 2026-09-02
---

# Review Process

The core discipline: **measure before changing.** The fastest way to destroy a working
strategy is to modify it after every losing trade. Changes here are gated on statistics,
not on recency or feeling.

---

## Daily review (end of day, after market close)

Fast, mechanical, ~one page written to `research_log.md`.

**1. Rule compliance audit — the most important question of the day**

For each trade taken today:
- Did it follow every layer of `strategy.md`? (Regime checked? Signal fully qualified?
  Context clear? Size computed by formula? Stop set at entry?)
- Was any guardrail bent, even slightly?

Grade the day on **process, not P&L**:

| Grade | Meaning |
|---|---|
| A | Every rule followed. (A losing day can score A.) |
| B | Followed rules, minor execution sloppiness (late stop, poor fill) |
| C | A rule was bent |
| F | A guardrail was breached — escalate to Telegram immediately |

A profitable day that scores C or F is a **failure** and must be logged as one. Making money
by breaking rules is the most dangerous thing that can happen, because it teaches the wrong
lesson.

**2. Position management**
- Open positions: still valid? Stop still correctly placed? Any approaching target?
- Anything that invalidated the original thesis? (Thesis broken = exit, regardless of P&L.)

**3. State update**
- Update `portfolio_state.md`: capital, cash, open positions, day's P&L, peak capital,
  consecutive-loss-day counter, distance to each guardrail limit.

**4. Tomorrow's prep**
- Current regime and whether it's shifting
- Watchlist candidates that are close to qualifying
- Known events tomorrow (earnings, macro prints, expiry)

**Daily reviews may NOT change `strategy.md`.** They observe and record only. This is
deliberate — daily data is far too noisy to justify rule changes.

---

## Weekly review (Friday after close)

The real analysis. Written to `research_log.md` and sent as a Telegram summary.

**1. Performance statistics** — computed from `trade_log.md`, not from memory:

```
Trades taken                     Win rate (%)
Average win (in R)               Average loss (in R)
Expectancy per trade (in R)      Total R gained/lost
Largest win / largest loss       Max drawdown this week
Capital: start → end (%)         Nifty 50 same period (%)  → relative performance
Total costs paid (₹)             Costs as % of gross P&L
```

**2. Breakdown analysis** — where the edge is or isn't:
- Expectancy **by regime** (is the regime classifier actually working?)
- Expectancy **by playbook** (momentum vs mean-reversion — is one carrying the other?)
- Expectancy **by setup type** and by holding period
- Were regime classifications correct in hindsight?
- Did Layer 3 (news/context) vetoes save money or cost missed opportunity? Track both.

**3. Process grade summary** — count of A/B/C/F days. Any C or F gets a written root cause.

**4. Keep / Adjust / Drop decisions** — the only place `strategy.md` may change.

---

## Rules governing strategy changes

These are binding. They exist because the classic failure mode of a self-modifying trading
system is over-fitting to noise.

**Minimum sample before any rule may be changed on performance grounds: 20 trades** of that
specific setup type. Below that, results are statistically meaningless — a 40% win rate over
5 trades tells you nothing.

**One change at a time.** Never modify multiple parameters in the same week. If two things
change and results improve, you've learned nothing about which one mattered.

**Every change must be written with:**
- The statistic that justifies it (with sample size)
- The specific hypothesis being tested
- What result would prove it wrong
- The date it was made, in the `strategy.md` change log

**Changes that are always allowed regardless of sample size** (these reduce risk, so they
can't make things structurally worse):
- Tightening a risk parameter
- Removing an instrument or setup from the allowed list
- Adding a liquidity or safety filter

**Changes that are never allowed by the agent:**
- Anything in `guardrails.md`
- Loosening any risk limit
- Raising position size beyond guardrail formulas
- Adding an instrument above the current capital tier

**The "it didn't work" trap:** if a setup has negative expectancy over ≥20 trades, drop or
revise it. If it has negative expectancy over 4 trades, that is noise — leave it alone. The
discipline to *not act* on small samples is what makes the learning loop real rather than
performative.

---

## Monthly review (last trading day of the month)

Everything in the weekly review, plus:
- Month vs Nifty 50; drawdown profile; expectancy trend across weeks
- Is the strategy improving, flat, or degrading? Three consecutive negative-expectancy
  weeks → pause new entries and escalate for human review.
- Capital tier check — has capital crossed a threshold in `strategy.md`? If so, **flag for
  Vaibhav's approval**, do not self-promote.
- Cost audit: total costs as % of gross P&L. If costs exceed 30% of gross profit, the
  strategy is trading too frequently for its size — reduce frequency.
- An honest written answer to: *"Is there evidence of a real edge here, or is this noise
  that happens to be positive?"* The agent is expected to be able to say "no evidence yet."

---

## Escalation triggers (Telegram, immediately)

- Any guardrail breach (F grade)
- 🟡 Drawdown reaching 10% from peak → pause new entries, run the Amber diagnostic
  (`guardrails.md` §1a) and send it
- 🟠 Drawdown reaching 15% from peak → pause, full strategy audit, risk halved to 1% on
  resume, human acknowledgement mandatory
- 🔴 Drawdown reaching 20% from peak → full stop, human decision required
- Kite API failure preventing position management while positions are open
- Any order that fills unexpectedly or at a materially different price
- Three consecutive negative-expectancy weeks
- Capital crossing a tier threshold (approval needed)
