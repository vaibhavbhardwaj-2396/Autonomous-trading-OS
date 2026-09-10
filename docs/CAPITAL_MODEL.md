# Capital model — from a fixed ₹10k scaffold to a dynamic broker account

Companion to `docs/BROKER_TRUTH.md`. Records where the legacy fixed-capital
concept is still used, why it is (still) a real safety boundary, and the
planned dynamic model.

## 1. The two things the ₹10,000 conflates

| Concept | What it should be | Today |
|---|---|---|
| **Brokerage account** | dynamic, broker-derived: `account_total_value`, `broker_free_cash`, `holdings_market_value` — changes as the user adds/withdraws funds and as holdings move | now surfaced correctly by `/account` (`api/broker_truth.py`) |
| **Currently authorised autonomous portfolio** | the capital the agent is *allowed to deploy* — a governed subset of the account, distinct from it | a fixed `allocated_capital` (₹10,000) in `memory/state.json`, set by hand |

The dashboard used to show `capital` / `allocated_capital` (both ₹10k) as
"Portfolio Value" / "Agent Book Value" — i.e. it told the user their
portfolio was worth ₹10k while their INDmoney account holds ≈ ₹63k. That
card is removed. `allocated_capital` now appears only as a small
**"Autonomous Mandate"** card, explained as *not* the account value.

## 2. Audit — where `allocated_capital` / `capital` are used

| Layer | Use | Genuine safety boundary, or scaffold? |
|---|---|---|
| `engine/execute.py:sync_from_broker` **(FROZEN)** | `allocated = state.get("allocated_capital", 10000.0)`; `agent_capital = allocated + realized_pnl_alltime`; `spendable = min(agent_capital - deployed, broker_free_cash)`; `jr.update_capital(agent_capital, spendable)` | **Boundary.** This is what stops the agent sizing trades against the whole ₹63k+ account. The comment at `execute.py:47` records the bug it fixed: on a ₹5.7L account it once told the agent it managed "57x its actual mandate". |
| `engine/guardrails.py` **(FROZEN)** | everything keys off `state["capital"]` (which `sync` sets to `allocated + realised P&L`): `risk_budget_per_trade = capital * risk_per_trade`, `max_total_open_risk = capital * MAX_TOTAL_OPEN_RISK`, tier unlocks (`allowed_instruments` vs `TIER_1_CAPITAL` …), drawdown ladder (`peak_capital`), daily/weekly loss caps, cash-after checks | **Boundary** (indirect — via `capital`). Every position-size and instrument-permission decision scales with it. |
| `engine/journal.py` | `render_portfolio_md` — "Allocated capital: ₹X ← set by Vaibhav" (human mirror) | reporting |
| `engine/briefing.py` | shows `sync['allocated_capital']` in the agent's run briefing | reporting |
| `scripts/telegram_inbox.py` | `status` command → "Allocation: ₹X" | reporting |
| `memory/guardrails.md` | documents it as "the agent's entire mandate. Only Vaibhav [may change it]" | policy |
| `memory/state.json` **(FROZEN)** | stores `allocated_capital`, `capital`, `peak_capital`, `cash_available` | state |
| `api/broker_truth.py:reconcile_book_value` | `expected_book_value = allocated + realized`; flags a `capital` figure poisoned with an account-total (`capital >> allocated`) | diagnostic (stale-state detector) |
| `api/data.py:get_account` | exposes `allocated_capital`, `expected_book_value` in the payload | presentation |
| `frontend/js/views.js` | **removed** the "Agent Book Value" / "Agent Allocated Capital" cards; `allocated_capital` now only the "Autonomous Mandate" card, explained | presentation |
| `paper/config.py` | `PAPER_INITIAL_CAPITAL` is *deliberately decoupled* — "never derived from `memory/state.json`'s real `allocated_capital`" | (not coupled) |

**Verdict: `allocated_capital` is a genuine, load-bearing execution-safety
boundary today, not merely legacy scaffolding.** The *value* (₹10,000) is a
scaffold; the *mechanism* (cap agent deployment / sizing at an explicit
authorised amount, independent of the account balance) is exactly the
`Broker account ≠ currently authorised autonomous portfolio` distinction we
want to keep. So this slice does **not** remove it — it stops the dashboard
*mislabelling* it as the user's capital.

## 3. Planned dynamic model (future, separately approved slice)

```
INDmoney account (dynamic, broker-derived)
    account_total_value      = free cash + Σ(holding LTP · qty)
    broker_free_cash
    holdings_market_value
        │
        │  explicit, governed promotion / reconciliation
        ▼
Currently authorised autonomous portfolio  (was: fixed allocated_capital)
    - a chosen fraction of, or an explicit rupee cap on, the account
    - or a set of specific holdings promoted into the managed set
    - re-evaluated on fund add/withdraw, never silently expanded by a sync
        │
        ▼
engine.guardrails sizes trades off THIS, not the account total
```

Requirements for that slice (all need explicit approval — they touch frozen
files):

1. `memory/state.json` schema: replace the fixed `allocated_capital` with a
   mandate *definition* (`{"kind": "fixed_rupees" | "account_fraction" |
   "promoted_holdings", ...}`) plus the last resolved value.
2. `engine/execute.py:sync_from_broker`: resolve the mandate against the
   live `account_total_value` / `broker_free_cash` each sync, still
   `spendable = min(resolved_mandate - deployed, broker_free_cash)`.
3. A **governed promotion process**: the human (or a future portfolio
   engine, via an explicit gate like the existing `approve SYMBOL` Telegram
   command) moves an unmanaged holding into the managed set. A sync alone
   must never do it.
4. Guardrails unchanged in shape — they already key off `capital`; only
   what `sync` writes into `capital` changes.

Until then: the fixed `allocated_capital` stays, the dashboard calls it
"Autonomous Mandate", and existing holdings stay **research-visible but
non-tradeable** (see `docs/BROKER_TRUTH.md` and PART D below).

## 4. Existing holdings — research-visible, non-tradeable (unchanged requirement)

- **Visible:** all broker holdings are in `broker_snapshot.unmanaged_symbols`
  (written by `sync_from_broker`) and surfaced by `/account` as
  `unmanaged_holdings {count, value}`. A future research holdings-source can
  read the same snapshot to analyse them, monitor thesis deterioration,
  compare against new ideas, and flag rebalancing/replacement candidates.
- **Non-tradeable:** `engine/guardrails.py` refuses any order in an
  `unmanaged_symbols` symbol unless it is in `overlap_approved_symbols`
  (the `approve SYMBOL` gate). `api/` has no order path at all. Research
  never imports a broker or `engine.execute`. None of that changes here.
- Research **flags**, it does not act — any promotion of a holding into the
  managed portfolio, or any sell, is the governed process in §3, never an
  automatic consequence of research output.
