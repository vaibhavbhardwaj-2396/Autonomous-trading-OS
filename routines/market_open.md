# Market open run (09:30 IST, Mon-Fri)

This is the cycle where orders actually get placed. Deliberately 15 minutes after the
09:15 open — the opening auction produces bad fills and false signals, and
`memory/strategy.md` bans trading it.

## Do

1. Read the briefing at `$BRIEFING_PATH` and this morning's entry in
   `memory/research_log.md` — your pre-market plan.

2. **Check the gate first.** If the briefing says new positions are blocked, or reports a
   broker error or state mismatch, then **manage existing positions only and place
   nothing new.** Log why and stop.

3. **Re-validate the plan against live prices.** Overnight plans go stale fast:
   - Has the candidate gapped past your entry? If it opened well above where you wanted
     in, the risk/reward has changed — usually that means skip it, not chase it.
   - Does the setup still hold at current prices?
   - Is the regime read still valid given how the market actually opened?

4. **Place trades that still qualify**, one at a time:

   ```bash
   python -m engine.execute propose --symbol SYMBOL --side BUY \
       --entry <live price> --stop <price> --target <price> \
       --thesis "one clear sentence" --regime "REGIME" --playbook "playbook"
   ```

   Check the returned JSON. `"placed": true` means it went through. Anything else means
   it did not — read the reasons, log them, and accept the outcome.

   **If the response shows the stop-loss failed to place, alert Vaibhav on Telegram
   immediately.** A position without a stop is a live guardrail violation.

5. **Manage open positions**: trail stops on winners where the strategy allows (only ever
   in the direction that reduces risk), exit anything whose thesis has broken.

6. Write what you did to `memory/trade_log.md` — the execute command does this
   automatically for orders, so add colour: why this one and not the others.

## Do not

- Chase a gap — a missed entry is not a loss
- Re-propose a rejected trade with tweaked numbers
- Place more than the guardrails permit (they'll refuse anyway, but don't try)
- Widen a stop under any circumstances

## Telegram

Message Vaibhav if a trade was placed or closed, or if something was blocked in a way he
should know about. Include symbol, size, entry, stop, target and the one-line thesis.
