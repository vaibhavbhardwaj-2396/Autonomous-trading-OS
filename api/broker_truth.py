"""
api/broker_truth.py — makes the read-only dashboard account path broker-aware.

WHY THIS EXISTS
---------------
The system migrated operationally from Zerodha/Kite to INDmoney/INDstocks
(`BROKER=indstocks`, `docs/BROKER_SWITCH.md`). During that migration, live
`engine.execute.sync_from_broker()` calls against INDstocks were failing
(HTTP 404), so `memory/state.json`'s `broker_snapshot` — and the `capital`
figure a sync also writes — were left holding **Kite-era** values (an
account/capital figure around ₹5.7L). `api/data.py.get_account()` then
served those stale numbers with no broker label and no freshness marker, so
the dashboard presented a stale Kite snapshot as if it were current
INDmoney truth.

This module does NOT fix the sync and does NOT touch `engine/execute.py` or
`engine/guardrails.py` (both frozen). It is a read-only classifier the API
layer uses to answer three questions the raw state cannot answer on its
own:

    1. Which broker is active right now?          -> active_broker()
    2. Is the cached broker_snapshot actually
       fresh, or is it stale / never-synced /
       from a different broker era?               -> classify_snapshot()
    3. Does `capital` still look like the agent's
       own book value (allocation + realised
       P&L), or has it been poisoned with an
       account-total figure?                      -> reconcile_book_value()

ISOLATION
---------
Imports nothing but the standard library. In particular it never imports
`engine.execute`, `engine.broker`, `engine.broker_kite`,
`engine.broker_indstocks`, `research.*` or `paper.*` — it cannot place an
order, cannot call a broker, and adds no new import edge into live
execution. It only ever reads a dict that `engine.guardrails.load_state()`
already produced.

CONFIG (all read from the environment at call time, never cached at import —
same convention as api/config.py)
-------------------------------------------------------------------------
    BROKER                                  active broker id (default: indstocks,
                                            matching engine/broker.py:get_broker)
    DASHBOARD_BROKER_SNAPSHOT_MAX_AGE_HOURS  a broker_snapshot older than this is
                                            "stale" (default: 24.0 — INDstocks
                                            tokens expire every 24h, so a healthy
                                            sync is always more recent than this)
    DASHBOARD_BROKER_CUTOVER                 optional ISO-8601 timestamp; any
                                            broker_snapshot synced at or before it
                                            is treated as pre-migration Kite-era
                                            and always "stale", regardless of age
    DASHBOARD_HOLDINGS_VALUE_FLOOR           holdings present but valued at or below
                                            this many rupees => the account total is
                                            withheld as cash-only (default: 1.0)
"""

from __future__ import annotations

import datetime as dt
import os
from typing import Optional

# The single authority for the active-broker default is
# engine/broker.py:get_broker() — `(name or os.environ.get("BROKER") or
# "indstocks")`. This module reads the same env var and mirrors the same
# default rather than importing engine.broker (which would pull the broker
# factory, and lazily its network-capable implementations, into the
# read-only API's import graph for no benefit — we only need the label).
DEFAULT_BROKER_ID = "indstocks"

ENV_BROKER = "BROKER"
ENV_SNAPSHOT_MAX_AGE_HOURS = "DASHBOARD_BROKER_SNAPSHOT_MAX_AGE_HOURS"
ENV_BROKER_CUTOVER = "DASHBOARD_BROKER_CUTOVER"
ENV_HOLDINGS_VALUE_FLOOR = "DASHBOARD_HOLDINGS_VALUE_FLOOR"

DEFAULT_SNAPSHOT_MAX_AGE_HOURS = 24.0
# A broker_snapshot with holdings present but a holdings valuation at or
# below this many rupees is treated as "the holdings were not priced" — the
# account total is then cash-only and is withheld (fail closed). ~₹1 so
# rounding never trips it; a real portfolio clears it by orders of magnitude.
DEFAULT_HOLDINGS_VALUE_FLOOR = 1.0

# Human-facing labels. The dashboard shows the label; code keys off the id.
_BROKER_LABELS = {
    "indstocks": "INDmoney / INDstocks",
    "kite": "Zerodha / Kite",
}

# The provenance string every freshness verdict carries — the exact function
# that writes memory/state.json's broker_snapshot. Named here, not guessed,
# so the dashboard can say where a number came from.
SNAPSHOT_SOURCE = "engine.execute.sync_from_broker"


# ---------------------------------------------------------------------------
# Active broker
# ---------------------------------------------------------------------------

def _broker_label(broker_id: str) -> str:
    return _BROKER_LABELS.get(broker_id, broker_id.replace("_", " ").title() or broker_id)


def active_broker() -> dict:
    """The broker the system is configured to trade through right now.

    `id` is the lowercase switch value (`indstocks` / `kite` / ...), `label`
    is what the dashboard renders. Reads `BROKER` from the environment with
    the same default as `engine.broker.get_broker()`; the dashboard API
    process does not normally have the trading `.env` loaded (by design —
    `deploy/trading-api.service` gives it only `deploy/api.env`), so set
    `BROKER=indstocks` in `deploy/api.env` too if you want this confirmed
    from config rather than defaulted. See `deploy/api.env.example`.
    """
    broker_id = (os.environ.get(ENV_BROKER) or "").strip().lower() or DEFAULT_BROKER_ID
    return {
        "id": broker_id,
        "label": _broker_label(broker_id),
        "from_config": bool((os.environ.get(ENV_BROKER) or "").strip()),
    }


# ---------------------------------------------------------------------------
# broker_snapshot freshness
# ---------------------------------------------------------------------------

def _max_age_hours() -> float:
    raw = (os.environ.get(ENV_SNAPSHOT_MAX_AGE_HOURS) or "").strip()
    if not raw:
        return DEFAULT_SNAPSHOT_MAX_AGE_HOURS
    try:
        val = float(raw)
        return val if val > 0 else DEFAULT_SNAPSHOT_MAX_AGE_HOURS
    except ValueError:
        return DEFAULT_SNAPSHOT_MAX_AGE_HOURS


def _cutover() -> Optional[dt.datetime]:
    raw = (os.environ.get(ENV_BROKER_CUTOVER) or "").strip()
    if not raw:
        return None
    return _parse_iso(raw)


def _parse_iso(value: str) -> Optional[dt.datetime]:
    try:
        parsed = dt.datetime.fromisoformat(value.strip())
    except (ValueError, AttributeError):
        return None
    return parsed


def _to_aware(moment: dt.datetime, reference: dt.datetime) -> dt.datetime:
    """Make a naive datetime comparable to `reference` by borrowing its
    tzinfo. state.json timestamps are written with an explicit +05:30 offset
    (engine.execute.sync_from_broker uses jr.now_ist()), so this is a
    defensive fallback, not the normal path."""
    if moment.tzinfo is None and reference.tzinfo is not None:
        return moment.replace(tzinfo=reference.tzinfo)
    return moment


def classify_snapshot(broker_snapshot: Optional[dict], *,
                      now: Optional[dt.datetime] = None,
                      active_broker_id: Optional[str] = None) -> dict:
    """Turn `memory/state.json`'s `broker_snapshot` into an explicit
    freshness verdict.

    Returns a dict that is always safe to expose on the API:

        {
          "status": "fresh" | "stale" | "never_synced" | "unknown",
          "stale": bool,                 # True for everything except "fresh"
          "synced_at": <iso str | None>,
          "age_hours": <float | None>,
          "reason": <str | None>,        # why it is not fresh (None when fresh)
          "source": "engine.execute.sync_from_broker",
          "broker": <active broker label>,   # what a fresh figure WOULD be from
        }

    A snapshot is only "fresh" when there is an actual recent successful
    sync. "stale" / "never_synced" / "unknown" all set `stale=True`, and the
    caller must NOT present the snapshot's account figures as current.
    """
    now = now or dt.datetime.now(dt.timezone(dt.timedelta(hours=5, minutes=30)))
    broker = active_broker()
    active_id = (active_broker_id or broker["id"]).strip().lower()
    label = _broker_label(active_id)

    snap = broker_snapshot or {}
    synced_raw = snap.get("synced_at")

    base = {
        "synced_at": synced_raw if isinstance(synced_raw, str) else None,
        "age_hours": None,
        "source": SNAPSHOT_SOURCE,
        "broker": label,
    }

    if not synced_raw:
        return {**base, "status": "never_synced", "stale": True,
                "reason": "no successful broker sync has been recorded yet"}

    synced_at = _parse_iso(synced_raw) if isinstance(synced_raw, str) else None
    if synced_at is None:
        return {**base, "status": "unknown", "stale": True,
                "reason": f"broker_snapshot.synced_at is unreadable: {synced_raw!r}"}

    synced_at = _to_aware(synced_at, now)
    try:
        age_hours = round((now - synced_at).total_seconds() / 3600.0, 2)
    except TypeError:
        return {**base, "status": "unknown", "stale": True,
                "reason": "could not compare broker_snapshot.synced_at with the current time"}
    base["age_hours"] = age_hours

    # A snapshot that records which broker wrote it (forward-compatible — the
    # frozen sync does not write this today, but a future one might) and
    # disagrees with the active broker is stale by definition.
    snap_broker = str(snap.get("broker") or "").strip().lower()
    if snap_broker and snap_broker != active_id:
        return {**base, "status": "stale", "stale": True,
                "reason": (f"broker_snapshot was written for {_broker_label(snap_broker)}, "
                           f"but the active broker is {label}")}

    cutover = _cutover()
    if cutover is not None:
        cutover = _to_aware(cutover, synced_at if synced_at.tzinfo else now)
        try:
            pre_cutover = synced_at <= cutover
        except TypeError:
            pre_cutover = False
        if pre_cutover:
            return {**base, "status": "stale", "stale": True,
                    "reason": (f"broker_snapshot was synced at {synced_raw}, at or before the "
                               f"configured broker cutover ({cutover.isoformat()}) — treat as "
                               f"pre-migration Kite-era data")}

    if age_hours < 0:
        # Clock skew — do not treat a future timestamp as fresh.
        return {**base, "status": "unknown", "stale": True,
                "reason": f"broker_snapshot.synced_at is in the future ({synced_raw})"}

    max_age = _max_age_hours()
    if age_hours > max_age:
        return {**base, "status": "stale", "stale": True,
                "reason": (f"last successful broker sync is {age_hours:.1f}h old "
                           f"(dashboard treats anything over {max_age:.0f}h as stale)")}

    return {**base, "status": "fresh", "stale": False, "reason": None}


# ---------------------------------------------------------------------------
# book value ("capital") sanity
# ---------------------------------------------------------------------------

def _as_float(value) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def reconcile_book_value(state: dict) -> dict:
    """Does `state["capital"]` still look like the agent's OWN book value?

    By design (`engine.execute.sync_from_broker`), the agent's book value is
    `allocated_capital + realized_pnl_alltime` and NOTHING else — never the
    brokerage account total. A pre-migration bug computed it as
    `cash + holdings + open positions` instead, which on Vaibhav's personal
    ~₹5.7L account told the agent it managed ~57x its actual mandate. If a
    stale `state.json` still carries that number, this returns
    `reconciled=False` and the dashboard must flag it rather than show it.

    Tolerance is deliberately loose (25% or ₹500): between broker syncs a
    closed-but-not-yet-resynced trade legitimately drifts `capital` from
    `allocated + realised P&L` by that trade's P&L. This check is a
    "something is badly wrong" detector, not a to-the-rupee reconciliation.
    """
    allocated = _as_float(state.get("allocated_capital"))
    realized = _as_float(state.get("realized_pnl_alltime")) or 0.0
    capital = _as_float(state.get("capital"))
    snap = state.get("broker_snapshot") or {}
    total_account_value = _as_float(snap.get("total_account_value"))

    expected = None if allocated is None else round(allocated + realized, 2)

    out = {
        "capital": capital,
        "allocated_capital": allocated,
        "realized_pnl_alltime": realized,
        "expected_book_value": expected,
        "reconciled": True,
        "looks_like_account_total": False,
        "reason": None,
    }

    if expected is None or capital is None:
        out["reconciled"] = False
        out["reason"] = "allocated_capital or capital is missing from state — cannot verify book value"
        return out

    tolerance = max(500.0, 0.25 * abs(expected))
    if abs(capital - expected) > tolerance:
        out["reconciled"] = False
        out["reason"] = (
            f"capital (₹{capital:,.0f}) is not close to allocated_capital + realised P&L "
            f"(₹{expected:,.0f}) — it may be a stale pre-migration figure"
        )

    # Strong tell-tale of the specific bug: capital ≈ the whole account total
    # and both are far above the agent's mandate.
    if (total_account_value is not None
            and abs(capital - total_account_value) <= max(500.0, 0.02 * abs(total_account_value))
            and capital > max(abs(expected) * 2.0, abs(expected) + 5000.0)):
        out["looks_like_account_total"] = True
        out["reconciled"] = False
        out["reason"] = (
            f"capital (₹{capital:,.0f}) matches the whole brokerage account total "
            f"(₹{total_account_value:,.0f}) — allocated_capital is ₹{allocated:,.0f}; "
            f"the agent's mandate must never be the account balance"
        )

    return out


# ---------------------------------------------------------------------------
# Holdings valuation — was the account total actually computed, or is it
# cash-only? (fail closed)
# ---------------------------------------------------------------------------

def _holdings_value_floor() -> float:
    raw = (os.environ.get(ENV_HOLDINGS_VALUE_FLOOR) or "").strip()
    if not raw:
        return DEFAULT_HOLDINGS_VALUE_FLOOR
    try:
        val = float(raw)
        return val if val >= 0 else DEFAULT_HOLDINGS_VALUE_FLOOR
    except ValueError:
        return DEFAULT_HOLDINGS_VALUE_FLOOR


def holdings_valuation(state: dict) -> dict:
    """Is `broker_snapshot.total_account_value` a real account total, or did
    the holdings valuation collapse to ~₹0?

    `engine.execute.sync_from_broker` computes:

        total_account_value = broker_free_cash
                              + Σ(holding.last_price · qty)          # holdings
                              + Σ(position.last_price · |qty|)       # positions

    so `holdings_value = total_account_value − broker_free_cash`. If broker
    holdings exist (unmanaged_symbols, and/or the agent's own positions) but
    that difference is at or below `DASHBOARD_HOLDINGS_VALUE_FLOOR` (~₹1),
    every holding was priced at 0 — the "total" is cash-only and MUST NOT be
    presented as an account total (fail closed — the ₹32.31-with-26-holdings
    case). Returns:

        {
          "holdings_count": int,            # unmanaged holdings + agent positions
          "unmanaged_holdings_count": int,
          "holdings_value": float | None,   # total − free cash, when both known
          "complete": bool,                 # False -> the total is not verifiable
          "reason": str | None,
        }

    LIMITATION: this can only catch a *fully* unpriced valuation (holdings
    ≈ ₹0). A *partial* one (say 20 of 26 holdings priced) still yields a
    plausible-looking `holdings_value` and is not detectable from the
    persisted snapshot alone — closing that gap needs per-holding valuation
    recorded in the snapshot, which is an `engine.execute` change.
    """
    snap = state.get("broker_snapshot") or {}
    total = _as_float(snap.get("total_account_value"))
    free_cash = _as_float(snap.get("free_cash"))
    n_unmanaged = len(snap.get("unmanaged_symbols") or [])
    n_agent_positions = len(state.get("open_positions") or [])
    n_holdings = n_unmanaged + n_agent_positions

    holdings_value = None
    if total is not None and free_cash is not None:
        holdings_value = round(total - free_cash, 2)

    out = {
        "holdings_count": n_holdings,
        "unmanaged_holdings_count": n_unmanaged,
        "holdings_value": holdings_value,
        "complete": True,
        "reason": None,
    }
    if n_holdings <= 0:
        return out  # no holdings — total is just cash, and that's genuinely complete

    floor = _holdings_value_floor()
    if holdings_value is None:
        out["complete"] = False
        out["reason"] = (f"{n_holdings} broker holding(s) present but total_account_value "
                         f"or free_cash is missing from broker_snapshot")
    elif holdings_value <= floor:
        out["complete"] = False
        out["reason"] = (
            f"{n_holdings} broker holding(s) present but the holdings valued at ~₹0 "
            f"(total ₹{total:,.2f} ≈ free cash ₹{free_cash:,.2f}) — the account total is "
            f"cash-only and cannot be verified"
        )
    return out


# ---------------------------------------------------------------------------
# One call the API layer uses
# ---------------------------------------------------------------------------

def account_truth(state: dict, *, now: Optional[dt.datetime] = None) -> dict:
    """Everything the dashboard needs to present account figures honestly,
    computed from `state` (an `engine.guardrails.load_state()` dict) plus
    the active-broker config. Pure; no I/O beyond reading env vars.

        {
          "broker":            {"id", "label", "from_config"},
          "snapshot":          <classify_snapshot() dict>,
          "book_value":        <reconcile_book_value() dict>,
          "holdings":          <holdings_valuation() dict>,
          "account_value_status": "fresh" | "incomplete" | "stale" | "never_synced" | "unknown",
          "safe_total_value":  <float | None>,   # account total, ONLY if fresh AND holdings verified
          "safe_broker_free_cash": <float | None>,   # ONLY if fresh (a directly-confirmed field)
          "safe_unmanaged_value": <float | None>,    # value of unmanaged holdings, ONLY if verified
          "unmanaged_holdings_count": int,
          "warnings":          [<str>, ...],     # human-readable, for a dashboard banner
        }
    """
    broker = active_broker()
    snapshot = classify_snapshot(state.get("broker_snapshot"), now=now,
                                 active_broker_id=broker["id"])
    book_value = reconcile_book_value(state)
    holdings = holdings_valuation(state)

    snap = state.get("broker_snapshot") or {}
    fresh = snapshot["status"] == "fresh"
    # A verifiable account total needs BOTH a fresh sync AND a holdings
    # valuation that actually happened.
    total_verified = fresh and holdings["complete"]
    safe_total = _as_float(snap.get("total_account_value")) if total_verified else None
    # free cash is a single directly-confirmed field (funds() -> eq_cnc) —
    # trustworthy whenever the sync itself is fresh, even if holdings pricing
    # failed.
    safe_free_cash = _as_float(snap.get("free_cash")) if fresh else None
    safe_unmanaged_value = (holdings["holdings_value"]
                            if (total_verified and holdings["holdings_value"] is not None
                                and holdings["unmanaged_holdings_count"] > 0)
                            else None)

    if not fresh:
        account_value_status = snapshot["status"]
    elif not holdings["complete"]:
        account_value_status = "incomplete"
    else:
        account_value_status = "fresh"

    warnings: list[str] = []
    if not fresh:
        warnings.append(
            f"Broker account data is {snapshot['status'].replace('_', ' ')} "
            f"({snapshot['reason']}). Showing the agent's last known book value, not a "
            f"live {broker['label']} balance."
        )
    elif not holdings["complete"]:
        warnings.append(
            f"Broker sync is current, but the account total could not be verified: "
            f"{holdings['reason']}. Free cash is shown; the account total is not."
        )
    if not book_value["reconciled"]:
        warnings.append(f"Book value not reconciled: {book_value['reason']}.")

    return {
        "broker": broker,
        "snapshot": snapshot,
        "book_value": book_value,
        "holdings": holdings,
        "account_value_status": account_value_status,
        "safe_total_value": safe_total,
        "safe_broker_free_cash": safe_free_cash,
        "safe_unmanaged_value": safe_unmanaged_value,
        "unmanaged_holdings_count": holdings["unmanaged_holdings_count"],
        "warnings": warnings,
    }
