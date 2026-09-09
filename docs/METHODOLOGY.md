# Quant Methodology Reference

A working library of the quantitative methods this agent draws on. `strategy.md` says *what
the agent currently does*; this file explains *why those choices*, and what the upgrade
paths are. The agent should extend this file as it learns.

---

## 1. The core mathematics: expectancy

Everything else is decoration on this equation.

```
Expectancy (R) = (Win% × Avg Win in R) − (Loss% × Avg Loss in R)
```

1R = amount risked per trade. Expressing results in R rather than rupees makes trades of
different sizes comparable and removes the emotional pull of absolute numbers.

Worked example — a strategy that loses more often than it wins but is highly profitable:

```
Win rate 40%, Avg win 3R, Avg loss 1R
Expectancy = (0.40 × 3) − (0.60 × 1) = 1.20 − 0.60 = +0.60R per trade
```

Over 100 trades risking 2% each, that's +60R of risk units — substantial compounding.

The corollary that matters most: **win rate alone is meaningless.** A 90%-win-rate strategy
with 1:10 reward:risk is a losing strategy. This is why `guardrails.md` mandates a minimum
1.5:1 reward:risk — it protects expectancy structurally, independent of how good the
signals turn out to be.

**Costs enter here directly.** Every round trip subtracts from expectancy. At ₹10k with
options costing ~0.8% round trip, a strategy needs to clear that hurdle before it earns
anything — which is precisely why frequency is capped and scalping is banned.

---

## 2. Regime detection

**The premise:** markets exist in distinguishable states (trending, ranging, high-stress),
and a strategy profitable in one is often loss-making in another. Momentum and mean
reversion are literally opposite bets. Detecting the state first is what stops the agent
from running the wrong one.

### 2a. Threshold-based regime detection (current implementation — v1)

Simple, auditable rules on EMAs, ADX, ATR percentile, breadth and VIX. See `strategy.md`
Layer 1.

Why start here rather than something fancier: it's transparent (every classification can be
explained and audited afterward), it needs no training data, and there's real evidence that
even a crude regime filter used as an on/off switch adds value — the
[hybrid AI trading paper (arXiv 2026)](https://arxiv.org/html/2601.19504v1) used nothing
more than a 20-day rolling average return as a bullish/bearish switch to gate trade
execution, and still reported meaningful improvement over buy-and-hold. That same paper
lists "static regime detection lacking probabilistic approaches" as a known limitation —
which is exactly the upgrade below.

### 2b. Hidden Markov Models / regime-switching (Phase 2 upgrade)

**The idea:** rather than hard-cutting on thresholds, model the market as having *latent
(hidden) states* that can't be observed directly — you only observe returns and volatility,
and infer the probability of being in each state.

- Typical setup: 2-4 hidden states fitted on features like daily returns, realized
  volatility, and volume. States usually emerge as something like: low-vol bull, high-vol
  bear, and one or two transitional/choppy states.
- The model produces a **transition matrix** — the probability of moving from each state to
  each other state — plus a probability distribution over today's state.
- Practical value over thresholds: (a) it gives *confidence*, not just a label, so position
  size can scale with regime certainty; (b) it can flag rising probability of transition
  *before* the threshold rules flip, which is exactly when threshold systems whipsaw.

**Status: implemented and running in SHADOW MODE** (`engine/regime_hmm.py`).

An earlier version of this document said the HMM had to wait for six months of data. That
was wrong, and worth correcting: years of Nifty daily history are freely available, so the
model can be *fitted* today. What can't be known yet is whether it improves **our**
decisions — that requires it to have called regimes on days we actually traded.

So it runs alongside the threshold classifier on every cycle. Both calls are written to
`memory/regime_log.jsonl`. The thresholds drive all trading; the HMM drives nothing.

What it adds when it works:
- **Probabilities, not labels** — position size could eventually scale with certainty
- **A transition matrix** — rising probability of a regime change is visible *before* the
  threshold rules flip, which is exactly where threshold systems whipsaw

**Promotion criteria** (a weekly-review decision, needs Vaibhav's approval):
≥60 logged regime calls, AND the HMM's classification better correlated with forward
5-day Nifty returns than the threshold classifier's. It earns promotion by being more
*accurate*, never by being more sophisticated.

Implementation notes: 3 Gaussian states over daily return, 5-day realised volatility and
range; fitted on ~7 years; refitted weekly (daily refits add noise, not information);
fixed random seed so classifications are reproducible. States are labelled by sorting on
mean return — the model has no idea what "bullish" means, it just finds clusters.

**Known failure mode:** HMMs are fitted on history and assume the state structure is stable.
A genuinely novel market environment (a first-of-its-kind shock) will be misclassified with
high confidence. This is why `guardrails.md` §6 has a hard volatility circuit breaker that
sits *outside* the model — no statistical model gets to override "the market gapped 3%,
stop trading."

Reference reading: [QuantStart on HMM regime detection](https://www.quantstart.com/articles/market-regime-detection-using-hidden-markov-models-in-qstrader/),
[Regime-Switching Factor Investing with HMMs (MDPI)](https://www.mdpi.com/1911-8074/13/12/311).

---

## 3. The two primary edge families

### Momentum / trend-following
**Premise:** price trends persist longer than random-walk theory predicts, driven by
gradual information diffusion, institutional accumulation over multiple days, and herding.

- Typically **low win rate (35-45%), high reward:risk (2-4R)**. Profitability comes from a
  few large winners; most trades are small losses.
- Requires the psychological/systematic discipline to take many small losses. An agent is
  actually *better suited* to this than a human — no ego, no loss aversion.
- Works in: Trending regimes. Fails badly in: choppy ranges (repeated false breakouts).
- Volume confirmation matters — breakouts without volume expansion fail disproportionately.

### Mean reversion
**Premise:** short-term price moves overshoot and revert to a statistical mean.

- Typically **high win rate (60-70%), low reward:risk (0.5-1.5R)**. Many small wins,
  occasionally a large loss when a "reversion" turns out to be a genuine new trend.
- The catastrophic failure mode: buying oversold in what is actually a sustained downtrend
  — the falling knife. This is why `strategy.md` only permits mean reversion **in a
  confirmed range-bound regime**, never as a standalone signal.
- Works in: Range-bound regimes. Fails badly in: strong trends and crashes.

They are complementary precisely because their failure modes are opposite — which is the
entire argument for Layer 1.

---

## 4. Sentiment and news as a filter

The [arXiv hybrid system](https://arxiv.org/html/2601.19504v1) combined technical indicators
+ an XGBoost directional classifier + FinBERT news sentiment, and notably used sentiment as
a **veto** ("risk shield") — blocking trades when sentiment fell below a threshold — rather
than as an entry trigger.

That asymmetry is the right design and is what `strategy.md` Layer 3 implements:
- Negative news **vetoes** a technically valid setup.
- Positive news does **not** create a setup that the technicals don't support.

Rationale: news is priced in fast and is heavily gamed; by the time retail reads a positive
headline, the move has usually happened. But adverse news genuinely raises the probability
of a gap against you — asymmetric information value justifies asymmetric usage.

Institutional-flow proxies worth tracking (public, free, India-specific): FII/DII daily
flows, bulk and block deals, delivery percentage, index rebalancing announcements.

---

## 5. Position sizing theory

**Fixed fractional (what this agent uses):** risk a constant % of current capital per trade.
Position size falls automatically in drawdown and rises with gains — geometric compounding
with built-in defence. Simple, robust, hard to blow up with.

**Kelly criterion:** the theoretically growth-optimal fraction:
```
f* = (bp − q) / b     where b = odds, p = win prob, q = 1−p
```
Deliberately **not used here.** Full Kelly assumes you know your true edge precisely — you
don't, especially with a small sample — and it produces brutal drawdowns (50%+ is normal
under full Kelly). Practitioners who use it at all use quarter- or half-Kelly. At 2% fixed
fractional, this agent sits well below even that, which is the correct conservatism for a
strategy with no verified track record.

**Why 2% and not more:** at 2% risk per trade, a 10-loss streak costs ~18% of capital —
survivable, and it keeps the hard 20% drawdown floor from being reachable by ordinary bad
luck rather than genuine strategy failure.

---

## 6. Known ways this whole approach fails

Recorded honestly, so the agent can watch for them:

1. **Over-fitting to noise** — changing rules on small samples until the strategy is tuned
   to history that will never repeat. Mitigated by the 20-trade minimum in
   `review_process.md`.
2. **Regime misclassification at turning points** — every regime model lags at exactly the
   moment it matters most. Mitigated by hard volatility circuit breakers.
3. **Cost drag** — at small capital, frequent trading converts a positive gross edge into a
   negative net one. Mitigated by the 3× cost rule and low frequency.
4. **Black swan / gap risk** — stops don't protect against overnight gaps; the fill can be
   far past the stop. Mitigated by position limits, no holding through earnings, and
   capping total open risk.
5. **Backtest ≠ future.** Indian market coverage on this point is blunt: past algo
   performance is not predictive, and a 10% max-drawdown design target is a more useful
   goal than any return figure ([Business Standard](https://www.business-standard.com/finance/personal-finance/algo-trading-understand-risks-have-realistic-return-expectations-124122001145_1.html)).
6. **Survivorship bias in the agent's own memory** — remembering the good calls. Mitigated
   by requiring statistics computed from `trade_log.md`, never from recollection.
7. **Silent technical failure** — API down, cron not firing, stop order never placed. The
   most boring and most likely failure. Mitigated by circuit breakers and heartbeat alerts.

---

## 7. Data sources

**Kite Connect API** (₹500/month for market data; order placement free for personal use) —
live quotes, historical candles, option chain, positions, margins.

**Free alternatives for research** (usable without a Kite data subscription, and without
even being logged in):
- [`nsepython`](https://pypi.org/project/nsepython/) — NSE India data wrapper (quotes,
  option chain, corporate actions, bulk deals)
- Yahoo Finance via `yfinance` — daily OHLCV for NSE/BSE symbols (`.NS` / `.BO` suffixes)
- NSE/BSE official sites — announcements, results calendar, FII/DII flows, index changes

Research (Layer 3) requires **no Zerodha session**, so the agent can do full pre-market
analysis whether or not the daily login has happened yet.

---

## 8. Ideas parked for later

Not implemented, recorded so they aren't lost:
- **Pairs trading / statistical arbitrage** — cointegrated pairs (e.g. two banks), market
  neutral. Needs capital for two simultaneous legs; revisit at Tier 2+.
- **Volatility regime trading** — India VIX term structure as a signal.
- **Options greeks-based strategies** — delta-neutral, theta harvesting. Tier 3, needs
  writing permissions and real margin.
- **ML directional classifier** — the arXiv paper's XGBoost approach (~63% next-day
  directional accuracy). Only worth attempting with a substantial labelled history; very
  easy to over-fit. Not before Phase 2.
- **Intraday/higher-frequency execution** — structurally blocked by cost drag at current
  capital; revisit only at much larger size.
