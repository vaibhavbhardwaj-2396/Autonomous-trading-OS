# Switching from Kite to INDstocks

The code change is **one line**. Everything else is verification, and the
verification is the part that matters — several response parsers in
`engine/broker_indstocks.py` were written against inferred field names, not
observed ones.

```text
engine/broker.py          get_broker() reads BROKER from .env
  ├── broker_kite.py      unchanged, kept as a proven fallback
  └── broker_indstocks.py the new path
```

Nothing above that line — guardrails, sizing, the screener, the regime
classifier, the learning loop, the whole research layer — knows or cares which
broker is configured. `tests/test_broker_probe.py` enforces that: no risk module
imports a broker, and the research layer never touches one at all.

One deliberate exception: **`engine/costs.py` is broker-specific and must be.**
Brokerage rates, the ₹18.50 DP charge and STT differ per broker. A cost model
claiming to be broker-agnostic would be lying, and the cost model is what
decides whether a trade clears its own charges.

---

## 1. Flip the broker

```bash
cd /root/trading-agent
nano .env
```

```
BROKER=indstocks
```

That is the entire code-level change. `BROKER=kite` switches back at any time.

---

## 2. Get today's token

INDstocks tokens expire every 24 hours and are minted from the web UI. There is
no OAuth redirect, which actually **simplifies** the infrastructure — the Caddy
reverse proxy, the callback server and the `bull.bhardwajvaibhav.com` subdomain
all become unnecessary. Leave them running or tear them down; nothing depends on
them once `BROKER=indstocks`.

1. Log in at indstocks.com → **Access Tokens** → generate today's token
2. Send it to the bot on Telegram: `token <paste>`

Or directly on the VPS:

```bash
venv/bin/python scripts/indstocks_auth.py --set <token> --verify
```

```
Token stored.
{ "valid": true, "age_hours": 0.0, "expires_in_hours": 24.0 }
✓ Broker reachable — available funds ₹...
```

If `--verify` fails, stop here. Everything below assumes a live session.

---

## 3. Probe the API contract — do this before trusting anything

```bash
venv/bin/python scripts/indstocks_probe.py
```

**Read-only by construction.** Only `requests.get` appears in the file, it
imports no broker module, and it references no order endpoint — all three
enforced by `tests/test_broker_probe.py`. It cannot place, modify or cancel an
order even if something goes wrong.

**Output is redacted** so it is safe to paste back to me: no token, client id,
PAN, bank or contact details at any nesting depth; balances reported as a
magnitude band rather than a number; lists as shape and count rather than
contents. Useful literals (`"CNC"`, `"success"`, `"EQ"`) are kept, because those
are exactly what a parser needs.

### The line that decides whether the agent can trade at all

```
✓ search INFY       HTTP 200  parser_works=True
    row keys: ['exchange', 'security_id', 'symbol', ...]
```

INDstocks orders take a `security_id`, not a trading symbol. `broker_indstocks.py`
resolves it through `/search` and **refuses the order** if resolution fails. That
is the safe failure — an order against the wrong instrument id is unrecoverable
in a way a rejected order never is — but it means a wrong parser presents as
"the agent cannot trade anything" rather than as a visible error.

If any symbol shows `parser_works=False`, send me the `row keys` line and I will
correct the parser. Do not guess at it.

---

## 4. Reconcile

```bash
venv/bin/python -m engine.execute sync
```

```json
{
  "broker": "indstocks",
  "allocated_capital": 10000.0,
  "agent_capital": 10000.0,
  "agent_spendable_cash": 0.0,
  "unmanaged_symbols": ["...your own holdings..."],
  "warnings": ["No spendable cash: broker free cash is ₹..."]
}
```

Two things to check:

- **`unmanaged_symbols` lists your pre-owned INDmoney holdings.** These are not
  the agent's capital and not its positions. The guardrails refuse any order in
  them unless you approve that specific symbol with `approve SYMBOL` on Telegram.
- **`agent_spendable_cash`** is `min(the agent's notional free cash, real broker
  cash)`. If it is 0, the account needs funding — the agent cannot spend money
  that is not there, whatever its allocation says.

---

## 5. Run the daily cycle

```bash
./run_cycle.sh premarket
```

Pre-market places no orders by design, so it is the safe one to test with. It
needs no broker session either — research runs before you have sent a token.

The trading cycles (`market_open`, `midday`, `preclose`) check
`get_broker().is_authenticated()` first and exit cleanly with a Telegram nudge if
today's token has not arrived.

---

## 6. What changes operationally

| | Kite | INDstocks |
|---|---|---|
| Daily auth | OAuth link, auto-exchanged | paste token on Telegram |
| Infrastructure | Caddy + callback server + subdomain | none |
| Token lifetime | until ~06:00 next day | 24h from issue |
| Order identifier | trading symbol | `security_id` (resolved + cached) |
| Algo tagging | not required | `algo_id` on every order |
| Stop-loss | GTT | GTT / smart-order |
| Market data | ₹500/mo for historical | not needed — daily data is free |

The `login_reminder.py` cron line still works; it just points at a different
auth path. You can drop it entirely if the Telegram nudge from the trading
cycles is enough.

---

## 7. Rollback

```bash
nano .env      # BROKER=kite
```

The Kite implementation is untouched and still tested. Switching back costs one
line, which is the entire point of having the abstraction.

---

## What this does NOT change

The research layer never reads a broker token, never calls `get_broker()`, and
never places an order — enforced by test, not convention. The recorder is
deliberately built on public sources so it keeps collecting on days you forget to
send a token. Those are exactly the days you would otherwise lose perishable data
permanently.
