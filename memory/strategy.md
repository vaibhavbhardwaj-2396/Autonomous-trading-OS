---
purpose: The agent's current playbook — how it decides what to trade and when. This file is
expected to change over time, but only through the evidence-based process in
`review_process.md`. Never changed on a hunch or after a single bad trade.
last_updated: 2026-09-02
version: v1.0
---

# Strategy

## The decision stack

Every run walks these layers in order. A trade only happens if every layer says yes.

```
Layer 1  REGIME      What kind of market is this?        → picks the playbook
Layer 2  SIGNAL      What specifically qualifies?        → generates candidates
Layer 3  CONTEXT     What do news/events/flows say?      → confirms or vetoes
Layer 4  SIZING      How much, and where's the stop?     → formula, from guardrails
Layer 5  EXECUTE     Place, log, set exits
Layer 6  LEARN       Record outcome, update statistics
```

The reason Layer 1 comes first: momentum and mean-reversion are *opposite* strategies.
Running the wrong one in the wrong regime is the single most common way systematic
strategies bleed money. Regime is not optional context — it selects the entire playbook.

---

## Layer 1 — Regime detection

Classify the market **before** looking at any individual stock. Assessed on Nifty 50 daily
data, recomputed each pre-market run.

| Input | Measure |
|---|---|
| Trend | EMA(50) vs EMA(200) relationship, and price vs both |
| Trend strength | ADX(14), or slope of EMA(50) over last 10 sessions |
| Volatility | ATR(14) as % of price, and its percentile vs last 6 months; India VIX level and direction |
| Breadth | Advance/decline ratio, % of Nifty 500 above their own EMA(50) |
| Global cue | Overnight US close, GIFT Nifty at open |

Resulting regime (one of four):

| Regime | Rough definition | Playbook | Risk posture |
|---|---|---|---|
| **Trending Up** | Price > EMA50 > EMA200, ADX > 20, breadth positive | Momentum long | Normal size |
| **Trending Down** | Price < EMA50 < EMA200, ADX > 20, breadth negative | Momentum short (via puts once Tier 1+; until then, mostly stay flat) | Reduced size, or cash |
| **Range-bound** | EMAs flat/tangled, ADX < 20, low ATR percentile | Mean reversion | Normal size, tighter targets |
| **High-Volatility / Stress** | ATR percentile > 80, VIX spiking, gap > 2% | **No new positions.** Manage existing only | Defensive |

Regime is written to `research_log.md` every run, with the numbers behind it — so future
reviews can test whether the classification was actually predictive.

**Upgrade path (Phase 2, once ≥6 months of logged data exists):** replace these threshold
rules with a Hidden Markov Model / regime-switching model that assigns *probabilities* to
latent states rather than hard-cutting on thresholds. See `docs/METHODOLOGY.md`. The
current rules are deliberately simple and auditable — a simple regime filter used as an
on/off switch is well-documented as effective, and static threshold detection is a known
limitation to improve on later, not a reason to over-engineer on day one.

---

## Layer 2 — Signal generation

Only run the playbook the regime selected.

### Momentum playbook (Trending regimes)
Candidates must show:
- Price above EMA(20) and EMA(50), both sloping up (inverse for shorts)
- Breakout above the prior N-day high (N=20 default) **on volume ≥ 1.5× the 20-day average
  volume** — volume confirmation is required, breakouts without it fail disproportionately
- Relative strength: outperforming Nifty 50 over the trailing 20 sessions
- MACD histogram positive and expanding

Stop: below the breakout level or 1.5× ATR(14), whichever is tighter but still sane.
Target: 2-3× the stop distance, or the next significant resistance level.

### Mean-reversion playbook (Range-bound regime)
Candidates must show:
- RSI(14) < 30 (long) or > 70 (short) — but **only in a confirmed range-bound regime**;
  oversold in a downtrend is not a buy signal, it's a falling knife
- Price at or outside the lower/upper Bollinger Band (20, 2σ)
- Price ≥ 2 standard deviations from its 20-day mean
- Stock is *not* in a fresh downtrend and has no adverse news catalyst (Layer 3 check)

Stop: beyond the recent swing low/high, or 1.5× ATR.
Target: reversion to the 20-day mean (typically a tighter, faster trade than momentum).

### Universe
- **Liquidity filter (mandatory):** minimum 20-day average traded value ≥ ₹50 crore.
  Illiquid stocks have wide spreads that silently destroy small accounts.
- Start with Nifty 100 constituents; widen to Nifty 500 only once the strategy has a proven
  track record on the narrower list.
- No stocks in F&O ban period, under ASM/GSM surveillance, or with pending
  corporate-action complications.

---

## Layer 3 — Context: news, events, and institutional flow

This is the "what will the big houses do / how will people react" layer. Its job is to
**confirm or veto** a technical setup — it should rarely originate a trade on its own.

Checked every pre-market run:
- **Earnings & results calendar** — never hold a position through an earnings announcement
  at this account size; the gap risk is not sizeable-around
- **Corporate actions** — dividends, splits, bonuses, buybacks, rights issues
- **Index events** — Nifty/Sensex rebalancing, inclusion/exclusion (these force large
  institutional flows on known dates)
- **Bulk & block deals, delivery percentage** — visible proxies for institutional activity
- **FII/DII daily flow data** — the clearest public read on what large money did yesterday
- **Macro calendar** — RBI policy, CPI/inflation prints, GDP, Union Budget, Fed decisions
- **Global cues** — US market close, GIFT Nifty, crude, dollar index, major Asian markets
- **Sector and stock news** — regulatory actions, management changes, order wins, downgrades

**Sentiment is used as a risk filter, not a signal generator.** Strongly negative news on a
candidate vetoes the trade even if the chart looks perfect. Strongly positive news does not
by itself justify a trade that fails Layers 1-2. This asymmetry is deliberate: news-driven
entries without technical confirmation are how retail accounts get caught buying tops.

Research at this layer needs **no Zerodha login** — it's public data. The agent can and
should do full research even on days when the login hasn't happened yet.

---

## Layer 4 — Sizing and risk

Entirely governed by `guardrails.md` §3. Not restated here so there is exactly one source
of truth. In short: size is computed from the stop distance and the 2% risk budget, and if
the arithmetic says zero, there is no trade.

---

## Layer 5 — Execution

- Limit orders by default. Market orders only for exits when speed matters more than price.
- Log to `trade_log.md` **before** awaiting fill confirmation.
- Set the exit (stop-loss / GTT order) immediately after entry fills — never leave a
  position unprotected while waiting for the next scheduled run.
- Avoid the first 15 minutes after open (9:15-9:30) — opening auction volatility produces
  bad fills and false signals.

---

## Capital tiers (what unlocks when)

Instruments are gated by what the account can actually support. Current tier comes from
capital in `portfolio_state.md`.

| Tier | Capital | Unlocked | Rationale |
|---|---|---|---|
| **0 — current** | < ₹25,000 | Equity cash only (delivery + intraday) | One option lot would be 65-160% of the book. Equity is also far more cost-efficient here (~0.2% round trip vs ~0.8% on options) |
| **1** | ₹25,000 - ₹1,00,000 | + Long options (CE and PE), max 1 lot, premium ≤ 25% of capital | A single lot becomes a *position* rather than the whole account. Enables expressing bearish views via puts |
| **2** | ₹1,00,000 - ₹3,00,000 | + Defined-risk option spreads (bull call, bear put, ~₹15-30k margin/lot) | Defined max loss, capital-efficient, sizeable within risk limits |
| **3** | > ₹3,00,000 | Futures and option writing become *discussable* — requires explicit written approval from Vaibhav, not automatic | Margin (~₹1.5-2 lakh/lot) finally supportable, but undefined-risk products need a human decision |

Reference figures as of Sept 2026 — the agent must verify current lot sizes and margins
live via the Kite API rather than trusting these numbers, since NSE revises them.

**Tier promotion is not automatic.** When capital crosses a threshold, the agent flags it in
the weekly review and asks for confirmation before using the newly available instruments.

---

## What this strategy explicitly does NOT do

Recorded so future versions don't quietly drift into these:

- **No scalping / high-frequency trading.** Cost drag makes it structurally unprofitable at
  this size, and it's a domain where the agent has no edge against actual HFT infrastructure.
- **No holding through earnings** at Tier 0-1.
- **No averaging down.** Ever.
- **No trading the open** (first 15 minutes).
- **No positions in illiquid names**, whatever the setup looks like.
- **No prediction of macro events.** The agent positions for what has already happened and
  what is measurable, not for what it guesses a central bank will do.

---

## Change log

- **v1.0 (2026-09-02):** Initial real version. Layered regime → signal → context → sizing
  framework. Capital tiers defined. Momentum and mean-reversion playbooks specified with
  concrete, testable parameters. All parameters here are *starting hypotheses* to be
  validated or revised against logged results per `review_process.md`.
