# Weekly review (Friday 16:30 IST)

The real analysis, and **the only cycle permitted to change `memory/strategy.md`.**

## Do

1. Read `memory/trade_log.md` in full for the week (and further back where needed),
   `memory/research_log.md`, `memory/review_process.md`, and `memory/strategy.md`.

2. **Get the statistics — do not compute them yourself.** Run:

   ```bash
   venv/bin/python -m engine.stats --days 7      # this week
   venv/bin/python -m engine.stats               # all time
   ```

   This computes win rate, average win/loss in R, expectancy, total R, max drawdown, and
   breakdowns by regime, playbook and symbol — from the structured trade record, not from
   prose. Recollection is survivorship-biased and mental arithmetic is unreliable; neither
   belongs anywhere near a decision to change the strategy.

   Expectancy = (Win% × AvgWin_R) − (Loss% × AvgLoss_R). **That is the number that
   matters, not weekly P&L.** Note the `expectancy_t_stat`: a positive expectancy with
   t < 2 is not yet distinguishable from luck.

   Add manually: capital start → end, Nifty 50 over the same period (relative
   performance), and total costs as a share of gross P&L.

3. **Breakdown analysis — where is the edge, if anywhere:**
   - Expectancy **by regime** (in the stats output) — is the classifier actually predictive?
   - Expectancy **by playbook** — momentum vs mean-reversion; is one carrying the other?
   - Groups marked ❌ in the output have too few trades to justify any change. Respect that.
   - Were regime classifications right in hindsight? Check each against what happened.
   - **Shadow HMM comparison:** read `memory/regime_log.jsonl`. Where the HMM disagreed
     with the threshold classifier, which was closer to what the market actually did? Do
     not promote it — just record the tally. Promotion needs ≥60 logged calls, clear
     out-performance, and Vaibhav's approval.
   - Did Layer 3 news vetoes save money or cost opportunity? Track both directions.

4. **Process grades**: count A/B/C/F days. Every C or F gets a written root cause.

5. **Keep / Adjust / Drop decisions.** These rules are binding:
   - **Minimum 20 trades** of a setup type before changing it on performance grounds.
     Below that, results are noise. A 40% win rate over 5 trades tells you nothing.
   - **One change at a time.** Two changes at once and you learn nothing about which
     mattered.
   - Every change needs: the statistic justifying it (with sample size), the hypothesis
     being tested, what result would disprove it, and a dated entry in the
     `strategy.md` change log.
   - **Always permitted regardless of sample size** (these reduce risk): tightening a
     risk parameter, removing a setup or instrument, adding a safety/liquidity filter.
   - **Never permitted**: anything in `guardrails.md`, loosening a risk limit, raising
     size, adding an instrument above the current capital tier.

   If sample size is below 20, the correct action is explicitly **no change**. Write that
   down. Restraint here is the whole point.

6. **Capital tier check.** If capital has crossed a threshold in `strategy.md`, flag it
   for Vaibhav's approval. Do not self-promote to a new tier.

7. **The honest question**, answered in writing: *"Is there evidence of a real edge here,
   or is this noise that happens to be positive?"* You are expected to be able to answer
   "no evidence yet" — and after a handful of weeks, that is the most likely correct
   answer.

8. Write the full review to `memory/research_log.md`, apply any justified change to
   `memory/strategy.md` (with change-log entry), and send a Telegram summary.

## Escalate to Telegram

- Three consecutive negative-expectancy weeks
- Any drawdown ladder level reached
- Capital crossed a tier threshold (needs approval)
- Evidence the strategy is degrading rather than improving
