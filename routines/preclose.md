# Pre-close run (15:15 IST, Mon-Fri)

Fifteen minutes before the 15:30 bell. Last chance to act on today's session.

## Do

1. Read the briefing at `$BRIEFING_PATH`.

2. **Decide hold-overnight vs close for each position:**
   - **Any position with earnings/results tomorrow must be closed today.** Gap risk
     through an announcement is not sizeable-around at this capital. This is not
     negotiable.
   - Is the thesis intact and the stop correctly placed for an overnight hold?
   - Any known macro event tomorrow (RBI, inflation print, major global event) that
     changes the risk of holding?
   - Intraday positions (`MIS` product) **must** be closed — they'll otherwise be
     force-squared-off by the broker at a price you didn't choose.

3. **Verify every open position has a live stop** at the broker. A position going into
   the night unprotected is the single worst state this system can be in. If a stop is
   missing, place it or close the position, and alert Vaibhav either way.

4. Close anything that needs closing:

   ```bash
   python -m engine.execute close --symbol SYMBOL --price <price> --reason "..."
   ```

5. Log decisions and reasoning to `memory/trade_log.md`.

## Do not

- Open new positions this late unless there's an exceptional, well-argued reason
- Leave an MIS position open into the close
- Hold through earnings

## Telegram

Message if you closed anything, or if a position is going overnight without a stop
(urgent).
