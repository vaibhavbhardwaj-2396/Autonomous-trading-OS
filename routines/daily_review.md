# Daily review (16:00 IST, Mon-Fri)

After the close. Observe and record — **this cycle may not change `memory/strategy.md`.**
Daily data is far too noisy to justify rule changes, and the discipline of not acting on
it is what keeps the learning loop honest.

## Do

1. Read the briefing at `$BRIEFING_PATH` and today's entries in `memory/trade_log.md`.

2. **Rule-compliance audit — the most important part of the day.**

   For every trade taken today, check honestly:
   - Was the regime checked before the setup?
   - Did the signal fully qualify, or did you stretch it?
   - Was the news/context layer actually consulted?
   - Was size computed by the formula (i.e. did it go through `engine.execute propose`)?
   - Was a stop set at entry?
   - Was anything bent, even slightly?

   Then grade the day:

   | Grade | Meaning |
   |---|---|
   | A | Every rule followed. **A losing day can score A.** |
   | B | Rules followed, minor execution sloppiness (late stop, poor fill) |
   | C | A rule was bent |
   | F | A guardrail was breached — escalate to Telegram immediately |

   **A profitable day that scores C or F is a failure and must be logged as one.** Making
   money by breaking rules teaches exactly the wrong lesson and is the most dangerous
   outcome available.

3. **Position review:** each open position's status, whether the thesis holds, whether
   stops are correctly placed for overnight.

4. **Prep tomorrow:** current regime and whether it's shifting; candidates close to
   qualifying; known events tomorrow (earnings, macro prints, expiry).

5. **Write the daily entry** to `memory/research_log.md`: the grade with justification,
   what happened, what you'd do differently, what to watch tomorrow.

6. **Send the daily summary to Telegram** — keep it short:
   - Capital, day's P&L, drawdown from peak
   - Trades taken (or "no trades — reason")
   - Open positions with unrealised R
   - Process grade
   - One line on tomorrow

## Do not

- Change `memory/strategy.md` (weekly review only)
- Grade yourself on P&L instead of process
- Gloss over a bent rule because the trade worked
