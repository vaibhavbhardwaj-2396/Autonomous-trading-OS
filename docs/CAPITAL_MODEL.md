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

**Verdict (superseded by §3):** the *mechanism* — the agent sizes/risks
against an explicitly-authorised subset of the account, never the whole
brokerage balance — is genuine and is KEPT. The *implementation* — a fixed
₹10,000 `allocated_capital` rupee value, `capital = allocated + realised
P&L`, `peak_capital` ratcheting on that fixed base — was legacy scaffolding
and has been **replaced** by the dynamic broker-derived model (§3). The
authorised subset is now: the agent's own positions + promoted holdings +
the broker free cash, all valued at current LTP. `allocated_capital` is
removed from `state.json` by the migration. The account-vs-authority
distinction now lives in the managed/unmanaged *holdings* split, not in a
rupee number.

## 2a. Two legacy internal-state artifacts on the VPS (post first real sync)

After the first successful INDmoney sync the VPS `memory/state.json` is:

```
allocated_capital : (key absent)          capital            : 10000.0
cash_available    : 32.31                 peak_capital       : 570447.95   ← legacy
realized_pnl_alltime : 0.0                broker_snapshot.*  : correct & fresh
```

### (a) `allocated_capital` key is absent — harmless, not a bug

**No code writes `allocated_capital`.** It is only ever seeded
(`memory/state.json.template`) or hand-set. `engine.execute.sync_from_broker`
reads it as `float(state.get("allocated_capital", 10000.0))` — so an absent
key means the live engine uses **₹10,000**, computes `capital = 10000 + 0`,
and `engine.journal.update_capital` persists `capital` but **never writes
`allocated_capital` back**. So the key stays absent while `capital` is fully
consistent with the ₹10k default. `engine.execute sync`'s *response dict*
reports `allocated_capital: 10000` because it applied the same default when
reading.

**Nothing to fix in the kernel.** The dashboard's
`reconcile_book_value()` was falsely reporting "book value cannot be
reconciled" purely because the key wasn't explicitly present — **that check
is now corrected** to apply the same `DEFAULT_ALLOCATED_CAPITAL = 10_000.0`
the engine uses (`allocated_capital_set` records that the key was absent, as
a calm note, not a warning). Setting the key explicitly in `state.json` is a
future governed change, not required for correctness.

### (b) `peak_capital = 570447.95` — a genuine legacy artifact (KEPT, documented)

`engine.journal.update_capital` **only ever raises** `peak_capital`
(`if capital > peak: peak = capital`). Before the broker migration, the
pre-mandate sync briefly computed `capital` as the whole ~₹5.7L brokerage
account, so `peak_capital` ratcheted to `570447.95` and **stayed** there
after `capital` fell back to ₹10k. `engine.guardrails.drawdown_level` (which
the live engine reads) therefore measures a **~98% drawdown** against a dead
peak → would classify **RED** (full stop, human decision) *if the live cron
were running* (it is not).

- **Diagnostic vs execution:** the dashboard warning was diagnostic-only;
  the peak itself **would** gate live execution (safely — it halts trading —
  but on a bogus basis).
- **Superseded** — the dynamic model in §3 is now IMPLEMENTED. The legacy
  ratchet is neutralised by the migration (`peak_capital` set to the current
  broker-derived managed equity; the old value survives only in
  `memory/capital_model_migration.jsonl`).

## 3. The dynamic broker-derived capital model (IMPLEMENTED)

The active broker (INDmoney / INDstocks) is the source of truth. Nothing
below is a stored, historical, or fixed rupee amount.

### Two figures, recomputed from the broker on every successful sync

```
CURRENT ACCOUNT VALUE   = broker free cash
                        + Σ(broker holding/position qty × current LTP)

CURRENT MANAGED EQUITY  = managed cash
                        + Σ(managed position qty × current LTP)
     managed cash       = the broker free cash  (all of it — NO ring-fenced grant,
                          no cash_grant, no fixed number)
     managed positions  = the agent's own open_positions
                        + any holdings a GOVERNED process has promoted into
                          state["managed"]["symbols"]  (empty by default;
                          a sync never promotes)
```

`broker free cash` ← `INDstocksBroker.funds()` → `/funds.detailed_avl_balance.eq_cnc`.
`current LTP` ← `INDstocksBroker.holdings()/positions()` → `/market/quotes/ltp`
(both confirmed endpoints).

- User deposits cash → next sync sees more `broker free cash` → managed
  equity rises automatically. No number to update.
- User withdraws cash → managed equity falls automatically.
- A holding's price or quantity moves → account value (and managed equity,
  if it's a managed position) moves on the next sync.
- **Unmanaged holdings** are in ACCOUNT VALUE and visible to research, but
  never in MANAGED EQUITY — they are only summed into `managed_positions`
  once explicitly promoted.

### Deposits / withdrawals — an explicit cash-flow ledger

`state["managed"]["cashflow_events"]` = `[{ts, amount, kind, reason, by}]`
(deposit `amount > 0`, withdrawal `amount < 0`). Written ONLY by
`engine.journal.record_cashflow_event` — a governed human/portfolio-engine
action, **never inferred by a sync**.

```
Σ cashflow      = Σ(cashflow_events.amount)          # net external capital ever added
growth          = managed_portfolio_value − Σ cashflow   # cumulative STRATEGY P&L
peak_growth     = max(peak_growth, growth)           # ratchet on P&L, not raw equity
drawdown_pct    = max(0, peak_growth − growth) / (Σ cashflow + peak_growth)
```

A deposit raises `managed_portfolio_value` and `Σ cashflow` by the same
amount → `growth` is unchanged → no artificial drawdown and no fake gain.
The migration writes one `kind: "inception"` event equal to the managed
equity at cutover, so `growth = 0` and `drawdown = 0` on day one.

An unrecorded deposit/withdrawal (broker cash moved with no trade and no
cash-flow event) is **warned** on the next sync — never silently absorbed
as P&L.

### Risk — `guardrails` runs on managed equity

`risk_budget_per_trade = managed_equity × RISK_PER_TRADE` (2% / 1%,
unchanged). Position sizing, the concentration cap, the total-open-risk
ceiling, the daily/weekly loss baselines and the AMBER/ORANGE/RED drawdown
ladder all key off `managed_equity(state)` and `_ladder_peak(state)` (=
`Σ cashflow + peak_growth`). `TIER_*_CAPITAL` stay as policy thresholds,
compared against the *dynamic* equity.

### Migration (`scripts/migrate_capital_model.py`)

Explicit, idempotent (`managed.model_version == 1` → skip), reversible
(`.bak` + `--revert`), auditable (`memory/capital_model_migration.jsonl`).
Reads the broker (funds/holdings/positions/quotes — **no orders**), builds
`state["managed"]`, sets `capital` / `peak_capital` / `cash_available` to the
new baseline, **drops `allocated_capital`**, and records the old
`peak_capital` only in the audit log. `--simulate` writes nothing.

### Backward compatibility

A state with **no `managed` block** (fresh install, `test_guardrails.py`
fixtures) still takes the legacy `allocated_capital + realised P&L` path in
`engine/execute.py` and the legacy `capital` / `peak_capital` reads in
`engine/guardrails.py`, byte-for-byte unchanged. The migrated live state
uses the dynamic model exclusively.

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
