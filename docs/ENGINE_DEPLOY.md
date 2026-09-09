# Engine deployment — Claude Code + cron

Prerequisite: `docs/VPS_DEPLOY.md` complete (HTTPS working, callback service running,
`status.py` reporting READY after login).

## 1. Install the new Python dependencies

```bash
cd /root/trading-agent
venv/bin/pip install -r requirements.txt
```

This adds `pandas` and `yfinance`. Daily market data comes from Yahoo Finance, which is
free — so the ₹500/month Kite historical-data plan is **not** required. Kite is used for
live quotes, positions, margins and order placement only.

## 2. Verify the safety layer before anything else

```bash
venv/bin/python -m tests.test_guardrails
venv/bin/python -m tests.test_indicators
```

Both must report **0 failed**. If they don't, stop — do not proceed to live trading with
a failing guardrail suite.

## 3. Check data access works from the VPS

```bash
venv/bin/python -m engine.regime
```

You should get a JSON regime classification with real Nifty numbers. If it returns
`UNKNOWN` with a fetch error, Yahoo Finance isn't reachable — tell me and we'll switch the
data source. Note the failure mode is safe: unknown regime → no new positions.

```bash
venv/bin/python -m engine.execute status     # guardrails snapshot
venv/bin/python -m engine.execute sync       # reconcile with broker (needs a session)
```

## 4. Install Claude Code

```bash
curl -fsSL https://claude.ai/install.sh | bash
```

(If that isn't available, `npm install -g @anthropic-ai/claude-code` after
`apt install -y nodejs npm`.)

Then authenticate — run it once interactively so the credential is stored for cron:

```bash
cd /root/trading-agent
claude
```

Follow the login prompt (use your existing Claude subscription), then `/exit`. Confirm
headless mode works:

```bash
claude -p "Reply with exactly: agent ready" --permission-mode acceptEdits
```

## 5. Dry-run a cycle by hand before letting cron near it

```bash
chmod +x /root/trading-agent/run_cycle.sh
/root/trading-agent/run_cycle.sh premarket
```

Pre-market places no orders by design, so this is the safe one to test with. Read the log
in `logs/` and the briefing it generated. Check that `memory/research_log.md` got a new
entry.

When you're ready to test the execution path **without risking money**, ask the agent for
a dry run — `engine.execute propose --dry-run` validates and logs but sends no order.

## 6. Cron schedule

```bash
crontab -e
```

```cron
# Trading agent — all times IST (set timezone first: timedatectl set-timezone Asia/Kolkata)
30 8  * * 1-5  /root/trading-agent/run_cycle.sh premarket
45 8  * * 1-5  /root/trading-agent/scripts/../venv/bin/python /root/trading-agent/scripts/login_reminder.py >> /root/trading-agent/logs/login_reminder.log 2>&1
30 9  * * 1-5  /root/trading-agent/run_cycle.sh market_open
30 12 * * 1-5  /root/trading-agent/run_cycle.sh midday
15 15 * * 1-5  /root/trading-agent/run_cycle.sh preclose
0  16 * * 1-5  /root/trading-agent/run_cycle.sh daily_review
30 16 * * 5    /root/trading-agent/run_cycle.sh weekly_review
```

Note the login reminder fires at 08:45, *after* the pre-market research run — research
needs no Kite session, so the agent can do its homework while you're still asleep, and
only needs you logged in before the 09:30 execution cycle.

## 7. Market holidays

NSE holidays aren't in the cron. On a holiday the pre-market run will see stale data and
the trading cycles will find nothing actionable — wasteful but harmless. A holiday
calendar is a worthwhile addition later.

## Rollout suggestion

1. **Week 1 — observe only.** Comment out the `market_open`, `midday` and `preclose` cron
   lines. Let pre-market and daily review run. Read what the agent writes each day and
   judge whether its reasoning is sound *before* it has the ability to spend money.
2. **Week 2 — dry-run execution.** Enable the trading cycles but tell the agent (in
   `CLAUDE.md`) to use `--dry-run`. You'll see exactly what it would have done.
3. **Week 3 — live, minimum size.** Remove the dry-run instruction. Watch every trade.
4. **Then leave it alone** and let the weekly reviews do their work.

That sequencing costs three weeks and buys you the knowledge of whether the reasoning is
any good before real money depends on it. Skipping it is the most common way this kind of
project ends badly.

---

## Switching to INDmoney / INDstocks

The strategy, guardrails, screener and learning loop are broker-agnostic. Only
`engine/broker_*.py` knows which broker is in use.

**1. Set the broker in `.env`:**

```
BROKER=indstocks
```

(`BROKER=kite` switches back — the Zerodha path is kept working as a proven fallback.)

**2. Daily token.** INDstocks tokens expire every 24 hours and come from the web UI, so
there is no OAuth callback to automate. The flow is:

- Log in at indstocks.com → Access Tokens → generate today's token
- Send it on Telegram: `token <paste>`
- The bot verifies it against the broker and confirms your available funds

This is simpler infrastructure than Kite — no callback server, no Caddy, no subdomain
needed. You can leave those running for now; nothing depends on them once `BROKER=indstocks`.

**3. Verify from the VPS:**

```bash
venv/bin/python scripts/indstocks_auth.py --set <token> --verify
venv/bin/python -m engine.execute sync
```

`sync` should report your INDmoney account: the agent's allocation, real free cash, and
your pre-owned holdings listed as unmanaged (off-limits to trade).

**One thing to verify on first contact:** INDstocks orders take a `security_id`, not a
trading symbol, so `broker_indstocks.py` resolves symbols via a `/search` lookup and
caches the result. The exact response shape needs confirming against the live API — if
resolution fails, the order is *refused* rather than guessed, so the failure is safe but
will show up as "could not resolve security_id". Send me that error and I'll correct the
parser.
