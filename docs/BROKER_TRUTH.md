# Broker truth — the dashboard and INDmoney / INDstocks

Audit + fix for the dashboard having shown **stale Kite-era account figures
(≈ ₹5.7L) as current INDmoney truth**. Companion to `docs/BROKER_SWITCH.md`
(the Kite → INDstocks switch itself) and `docs/API.md` (the read-only API).

Scope: `api/` + `frontend/` + config + tests only. `engine/guardrails.py`
and `engine/execute.py` are **not touched** — the frozen `sync_from_broker()`
is still the only thing that writes `memory/state.json`'s `broker_snapshot`.

---

## 1. The account data path (audited, not guessed)

```
frontend/js/views.js  (accountCardsHtml / tradingCardsHtml)
        │  apiGet("/account")
        ▼
api/app.py   @app.route("/account")  ->  data.get_account()      [GET only, @require_auth]
        ▼
api/data.py  get_account()
        ├─ engine.guardrails.load_state()      ->  reads memory/state.json
        └─ api.broker_truth.account_truth()    ->  pure classifier (NEW)
```

| Dashboard figure | Field | Origin in `state.json` | Written by |
|---|---|---|---|
| "Portfolio Value" | `portfolio_value` | `capital` | `engine.journal.update_capital()` ← `engine.execute.sync_from_broker()`; by design `= allocated_capital + realized_pnl_alltime` |
| "Cash Available" | `cash` | `cash_available` | same — `min(agent notional cash, broker free cash)` |
| (not shown pre-fix) | `total_value` | `broker_snapshot.total_account_value` | `sync_from_broker()` — `free_cash + Σ holdings + Σ positions` = the **whole** brokerage account |
| "P&L Today" | `pnl_today` | `day.realized_pnl` | `engine.journal` |
| — | `allocated_capital` | `allocated_capital` | **set by Vaibhav by hand**; `sync_from_broker()` reads it, never writes it |
| — | (no field) | — | **the broker name was recorded nowhere** |

## 2. What the dashboard was actually getting

**(c) stale Kite-era state, presented without any staleness marker.**

- `broker_snapshot` (and the `capital` a sync also writes) are only updated by
  `sync_from_broker()`, which runs from `engine.briefing.build()` (live cron
  cycles) or a manual `python -m engine.execute sync`.
- The live cron is **not installed** (see the cadence audit). And the INDstocks
  syncs that were attempted returned **HTTP 404**, so `sync_from_broker()`
  returned `{"ok": False}` and updated nothing.
- Net effect: `capital` ≈ `broker_snapshot.total_account_value` ≈ ₹5.7L and
  `broker_snapshot.synced_at` were all left at **Kite-era** values, and
  `/account` served `portfolio_value = capital = ₹5.7L` with no broker label
  and no freshness field. The user has ≈ ₹63,000 in INDmoney.

## 3. Is `BROKER=indstocks` active?

**Cannot be verified from the repo — `/root/trading-agent/.env` is `chmod 600`,
root-owned, and gitignored.** Verify on the VPS:

```bash
grep -E '^BROKER=' /root/trading-agent/.env      # expect: BROKER=indstocks
```

Evidence it is: `.env.example` defaults `BROKER=indstocks`; the INDstocks
auto-login cron is active; `docs/BROKER_SWITCH.md` documents the switch.
`engine/broker.py:get_broker()` defaults to `indstocks` when `BROKER` is unset,
so the active *code path* is INDstocks unless `.env` explicitly says `kite`.

## 4. Is the API/dashboard independently calling Kite?

**No.** Verified by import graph, not by reading prose:

- `api/app.py` imports only `auth, config, data, paper_data` — no broker.
- `api/data.py` imports `engine.guardrails`, `engine.journal`, `research.*`,
  `strategies.registry`, and now `api.broker_truth` — **no `engine.broker*`,
  no `engine.execute`.**
- `api/broker_truth.py` imports **standard library only**.
- The API only ever *reads* `memory/state.json`. It never constructs a broker,
  never makes a broker HTTP call, Kite or INDstocks.

`tests/test_broker_truth.py` (checks 6–7) and `tests/test_api.py` (sections D,
I) enforce this mechanically.

## 5. The `kite-callback` service — is anything still using it?

`scripts/kite_callback_server.py` exists to catch Zerodha's OAuth redirect and
call `kite_auth.exchange_request_token()`. It is only relevant when
`BROKER=kite`:

| Thing | Needs `kite-callback`? |
|---|---|
| `engine/broker_indstocks.py` (active path) | No — token via Telegram `token <value>` / `scripts/indstocks_auth.py`, no redirect |
| `engine/broker_kite.py` (**tested fallback — keep**) | Only if it is ever made the active broker again |
| `scripts/login_reminder.py` (cron 08:45) | References Kite directly, **not broker-aware** — see below |
| `api/` / dashboard | No |
| `research/`, `paper/` | No (never import a broker at all) |

**Finding:** with `BROKER=indstocks`, `kite-callback.service` is **operationally
idle** — nothing in the active path touches it (`docs/BROKER_SWITCH.md` §2 says
the same: "the callback server ... become[s] unnecessary ... nothing depends on
[it] once `BROKER=indstocks`").

**It is NOT removed by this change.** The Kite *code* (`engine/broker_kite.py`,
`scripts/kite_auth.py`) stays — it is a deliberate, tested fallback
(`tests/test_broker_probe.py`: "kite is still available as a fallback"). Only
the always-on *service* is redundant, and disabling a systemd unit is an
operational decision, not a code change.

**Separate, safe operational step (run only after confirming `BROKER=indstocks`):**

```bash
# 1. Confirm the active broker and that nothing is mid-Kite-login
grep -E '^BROKER=' /root/trading-agent/.env
sudo systemctl status kite-callback --no-pager

# 2. Stop and disable it (reversible: `systemctl enable --now kite-callback`)
sudo systemctl disable --now kite-callback

# 3. (optional) the 08:45 login-reminder cron line is Kite-specific — if the
#    live trading cron is ever installed, replace/remove that one line; it is
#    harmless while the live cron is not installed.
```

Leave Caddy and the `tradingagent.bhardwajvaibhav.com` vhost alone — the
dashboard API's own Caddy vhost (`tradingbotapi....`) is unrelated.

## 6. The fix (`api/broker_truth.py`)

A pure, read-only classifier the API layer calls from `get_account()`. No
network, no broker import, no engine.execute import.

- **`active_broker()`** — `{"id","label","from_config"}` from the `BROKER` env
  var (default `indstocks`, matching `engine/broker.py`). Label:
  `INDmoney / INDstocks` or `Zerodha / Kite`.
- **`classify_snapshot()`** — turns `broker_snapshot` into
  `status ∈ {fresh, stale, never_synced, unknown}` + a human `reason` +
  `source: "engine.execute.sync_from_broker"`. `fresh` requires a real, recent
  successful sync; **everything else sets `stale: true`**.
  - stale if `synced_at` is older than `DASHBOARD_BROKER_SNAPSHOT_MAX_AGE_HOURS`
    (default **24** — INDstocks tokens live 24h so a healthy sync is always
    fresher)
  - stale if `synced_at` ≤ `DASHBOARD_BROKER_CUTOVER` (optional; the moment the
    INDstocks switch went live — set it and no Kite-era snapshot can ever read
    as fresh)
  - stale if the snapshot records a `broker` that isn't the active one
    (forward-compatible; the frozen sync doesn't write this yet)
- **`reconcile_book_value()`** — is `capital` still `≈ allocated_capital +
  realized_pnl_alltime` (25% / ₹500 tolerance for unsynced-close drift), or has
  it been poisoned with an account-total figure? Returns `reconciled` +
  `looks_like_account_total` + `expected_book_value`.

### `/account` response — additive, back-compatible

Everything the v1 contract had is still there. New / changed:

| Field | Meaning |
|---|---|
| `total_value` | the brokerage account total **only when `account_value_status == "fresh"`**, else `null` |
| `broker_free_cash` | same gating |
| `broker` | `{"id","label","from_config"}` — the active broker |
| `broker_snapshot` | `{status, stale, synced_at, age_hours, reason, source, broker}` |
| `account_value_status` | `fresh` / `stale` / `never_synced` / `unknown` |
| `expected_book_value` | `allocated_capital + realized_pnl_alltime` |
| `book_value_reconciled` | `false` when `capital` has drifted from / been poisoned relative to the above |
| `book_value_note` | why, when not reconciled |
| `account_warnings` | `[]`, or human-readable strings for a dashboard banner |

`allocated_capital` is passed straight through, untouched, always. Nothing in
this change can rewrite it, and it is never derived from `total_value`.

### Dashboard (`frontend/`)

- A **"Broker"** card on Overview and Trading showing `INDmoney / INDstocks`
  (green when fresh, red + status when not).
- "Portfolio Value" falls back to `expected_book_value` (with a ⚠) when
  `book_value_reconciled` is false — so a poisoned `capital` is never shown.
- A full-width notice above the cards whenever `account_value_status != "fresh"`.

## 7. Operational follow-up (NOT done by this change)

The code now *labels* stale data honestly. To get **fresh** INDmoney data on
the dashboard, one of:

1. A successful `python -m engine.execute sync` on the VPS under
   `BROKER=indstocks` with a valid token (this writes a fresh `broker_snapshot`
   and recomputes `capital` from `allocated_capital + realised P&L`). If it
   still 404s, the INDstocks endpoint paths in `engine/broker_indstocks.py`
   need re-checking against the live API — out of scope here.
2. Or reset `memory/state.json` deliberately: set `allocated_capital` to the
   agent's *mandate* (NOT the ₹63k account total — Vaibhav decides the number),
   `capital` / `peak_capital` / `cash_available` to the same, zero the P&L
   fields, and clear `broker_snapshot` (`synced_at: null`). Then the dashboard
   reads `never_synced` until a real sync runs — which is the honest state.

Set `DASHBOARD_BROKER_CUTOVER` in `deploy/api.env` to the INDstocks cutover
timestamp regardless — it is the belt-and-braces guarantee against a Kite-era
snapshot ever reading as fresh.
