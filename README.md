# Trading Agent

A systematic, self-improving trading agent operating a live Zerodha account on NSE/BSE.
Built to think like a small hedge-fund analyst-trader: form a view from data, express it
through a formula-sized position, record the reasoning, measure the result, and update the
playbook on evidence.

Not directionally biased — long, short (via puts, once capital allows), and flat are all
valid outcomes. "No trade today" is a legitimate and frequently correct answer.

## Read order (the agent reads these every run, in this order)

| File | What it is |
|---|---|
| `memory/mission.md` | North star, objective function, honest targets, capital reality |
| `memory/guardrails.md` | **Hard risk rules.** Override everything else. Agent may never edit |
| `memory/strategy.md` | Current playbook: regime → signal → context → sizing → execute |
| `memory/portfolio_state.md` | Source of truth for capital and open positions |
| `memory/review_process.md` | How learning happens: daily/weekly/monthly review protocol |
| `memory/trade_log.md` | Every decision, with reasoning and outcome |
| `memory/research_log.md` | Per-run market analysis and regime classification |
| `docs/METHODOLOGY.md` | Quant methods reference — why the strategy is built this way |

## The run loop

Every scheduled run:

1. Read memory files — mission, guardrails, state, strategy.
2. **Check guardrails first.** Any limit breached → research and logging only, no trades.
3. Classify the market regime (Layer 1).
4. Generate candidates using the playbook that regime selects (Layer 2).
5. Confirm or veto against news, events, and institutional flow (Layer 3).
6. Size by formula from the stop distance (Layer 4).
7. Execute, set exits immediately, log before confirming (Layer 5).
8. Update state files; Telegram alert if trade-worthy or urgent (Layer 6).

## Setup

- `docs/SETUP.md` — Kite Connect app, Telegram bot, VPS, DNS
- `docs/VPS_DEPLOY.md` — deployment on the Vultr box, HTTPS, systemd, cron
- `docs/API.md` — the read-only dashboard API + frontend (`api/`, `frontend/`); observation
  only, no order/risk/research write path exists

Infrastructure: Vultr VPS (Mumbai, `trading-agent-v1`, static IP whitelisted with Zerodha
per SEBI's algo rules), Caddy for HTTPS on `tradingagent.bhardwajvaibhav.com`, Kite Connect
for execution, Telegram for alerts.

## Not financial advice

An experiment in autonomous systematic trading, with real money and real risk. Capital at
risk should never exceed what can be lost entirely.
