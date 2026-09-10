"""
INDstocks (INDmoney) broker implementation.

API: https://api.indstocks.com — token in the `Authorization` header, no Bearer prefix.

Two things differ meaningfully from Kite and shape this module:

1. **Tokens expire every 24 hours** and are minted by logging into the INDstocks web UI,
   not by an OAuth redirect. There is no callback to automate, so the daily token arrives
   via Telegram (`token <value>`) and is stored by scripts/indstocks_auth.py. Simpler
   infrastructure than Kite — no Caddy, no domain, no callback server.

2. **Orders take a `security_id`, not a trading symbol.** So there's a symbol → id
   resolution step, cached locally. If the instrument master can't resolve a symbol, the
   order is refused rather than guessed at — placing an order against the wrong
   instrument id is a category of mistake that must never be possible.

`algo_id` is required by INDstocks and is how SEBI's algo-order tagging is satisfied.
"""

from __future__ import annotations

import os
import json
import time
from pathlib import Path
from typing import Optional

import requests

from .broker import Broker, OrderResult, Position

BASE_URL = "https://api.indstocks.com"
PROJECT_ROOT = Path(__file__).parent.parent
TOKEN_FILE = PROJECT_ROOT / "memory" / ".indstocks_session.json"
# Confirmed against api-docs.indstocks.com/instruments/ on 2026-09-07: there is no
# per-symbol /search endpoint at all. Symbol -> security_id resolution only exists as a
# bulk CSV download of the ENTIRE instrument master (GET /market/instruments), refreshed
# once and cached locally rather than re-downloaded per lookup.
INSTRUMENT_MASTER_CACHE = PROJECT_ROOT / "memory" / ".instrument_master.csv"
INSTRUMENT_MASTER_MAX_AGE = 24 * 3600

# INDstocks requires an algo id on every order — this is the SEBI algo-tagging mechanism.
ALGO_ID = {"NSE": "99999", "BSE": "9999999999999999"}

TOKEN_TTL_SECONDS = 24 * 3600
REQUEST_TIMEOUT = 20


def _to_float(value, default: float = 0.0) -> float:
    """Coerce a response field to float, falling back to `default` for
    None/""/non-numeric — one malformed row must not crash a whole sync
    (engine.execute.sync_from_broker would turn any exception here into a
    do-not-trade circuit breaker). Only the confirmed numeric fields are
    parsed this way; nothing is invented."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


class INDstocksBroker(Broker):
    name = "indstocks"

    def __init__(self) -> None:
        self._token: Optional[str] = None
        self._instruments: dict = {}

    # -- auth ------------------------------------------------------------------

    def _load_token(self) -> Optional[str]:
        if self._token:
            return self._token
        env_token = os.environ.get("INDSTOCKS_ACCESS_TOKEN")
        if env_token:
            self._token = env_token
            return self._token
        if not TOKEN_FILE.exists():
            return None
        try:
            blob = json.loads(TOKEN_FILE.read_text())
        except json.JSONDecodeError:
            return None
        if time.time() - blob.get("saved_at", 0) > TOKEN_TTL_SECONDS:
            return None  # expired — treat as absent rather than sending a dead token
        self._token = blob.get("access_token")
        return self._token

    def is_authenticated(self) -> bool:
        if not self._load_token():
            return False
        try:
            self.funds()
            return True
        except Exception:
            return False

    def _headers(self) -> dict:
        token = self._load_token()
        if not token:
            raise RuntimeError(
                "No valid INDstocks token. Tokens expire every 24h — send today's token "
                "on Telegram with `token <value>`."
            )
        return {"Authorization": token, "Content-Type": "application/json"}

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        r = requests.get(f"{BASE_URL}{path}", headers=self._headers(),
                         params=params or {}, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, body: dict) -> dict:
        r = requests.post(f"{BASE_URL}{path}", headers=self._headers(),
                          json=body, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.json()

    # -- instrument resolution -------------------------------------------------

    def _ensure_instrument_master(self) -> None:
        """Load the symbol -> security_id table, downloading/refreshing the CSV master
        at most once a day. This is a bulk file, not a per-symbol network call — there is
        no live search endpoint on this API."""
        if self._instruments:
            return

        stale = (not INSTRUMENT_MASTER_CACHE.exists()
                or time.time() - INSTRUMENT_MASTER_CACHE.stat().st_mtime > INSTRUMENT_MASTER_MAX_AGE)
        if stale:
            r = requests.get(f"{BASE_URL}/market/instruments", headers=self._headers(),
                             params={"source": "equity"}, timeout=60)
            r.raise_for_status()
            INSTRUMENT_MASTER_CACHE.parent.mkdir(parents=True, exist_ok=True)
            INSTRUMENT_MASTER_CACHE.write_text(r.text)

        import csv
        table: dict[str, str] = {}
        with INSTRUMENT_MASTER_CACHE.open(newline="") as f:
            for row in csv.DictReader(f):
                exch = str(row.get("EXCH", "")).upper()
                sym = str(row.get("TRADING_SYMBOL", "")).upper()
                sid = row.get("SECURITY_ID")
                if exch and sym and sid:
                    table[f"{exch}:{sym}"] = str(sid)
        self._instruments = table

    def security_id(self, symbol: str, exchange: str = "NSE") -> Optional[str]:
        """Resolve a trading symbol to the id orders are placed against.

        Returns None if unresolvable — callers MUST refuse the order rather than
        substituting anything. An order against a wrong instrument id would buy something
        nobody intended, which is unrecoverable in a way a rejected order never is.
        """
        try:
            self._ensure_instrument_master()
        except Exception:
            return None
        return self._instruments.get(f"{exchange.upper()}:{symbol.upper()}")

    # -- reads -----------------------------------------------------------------

    def funds(self) -> float:
        """Cash available to spend on new CNC (cash-and-carry, i.e. delivery) equity buys.

        Confirmed against a live probe on 2026-09-02 — the real response shape is:
            {"status": "success", "data": {
                "sod_balance": ..., "withdrawal_balance": ...,
                "detailed_avl_balance": {"eq_cnc": ..., "eq_mis": ..., "eq_mtf": ...,
                                          "future": ..., "option_buy": ..., "option_sell": ...},
                ...}}
        There is no flat "available_balance" field — the old fallback chain was checking
        keys that don't exist in this API and silently returning 0.0. eq_cnc is the right
        number here because every order this agent places uses product="CNC".
        """
        data = self._get("/funds")
        d = data.get("data", data)
        detailed = d.get("detailed_avl_balance") or {}
        if "eq_cnc" in detailed:
            return float(detailed["eq_cnc"] or 0.0)
        # Fall back to older/flat shapes rather than assume the new one is universal.
        for k in ("available_balance", "availableBalance", "net_available", "cash",
                  "withdrawal_balance", "sod_balance"):
            if k in d:
                return float(d[k] or 0.0)
        raise ValueError(f"funds(): no recognised balance field in response: {sorted(d.keys())}")

    def _attach_live_prices(self, positions: list[Position]) -> list[Position]:
        """Attach a live `last_price` to each Position via the CONFIRMED
        `/market/quotes/ltp` endpoint (self.quote() — response shape verified
        2026-09-07: {"data": {"NSE_<sid>": {"live_price": N}}}).

        Why this is needed: `/portfolio/holdings` and `/portfolio/positions`
        do NOT return a price (confirmed against api-docs.indstocks.com on
        2026-09-02), but the Broker contract `engine.execute.sync_from_broker`
        relies on — and `engine/broker_kite.py` already satisfies — is
        *priced* holdings: it does `sum(h.last_price * h.quantity)` to value
        the account. Without this step every INDstocks holding is valued at
        ₹0 and `broker_snapshot.total_account_value` collapses to just the
        cash (this is exactly why the first successful sync reported
        ₹32.31 with 26 holdings present).

        A symbol that cannot be resolved to a security_id, or that the quote
        endpoint does not return, keeps `last_price = 0.0` — never a
        fabricated price (same discipline `quote()` itself already keeps). A
        `0.0` here means "unpriced", not "worthless": a caller computing a
        *total* must treat it that way — see `api/broker_truth.py`'s
        fail-closed guard, which withholds the account total when the
        holdings valuation came back at ~₹0.

        One batched GET (not one call per symbol). `quote()` and
        `security_id()` both swallow their own network errors, so this never
        raises — a quote outage degrades to unpriced holdings, never a failed
        sync.
        """
        if not positions:
            return positions
        try:
            prices = self.quote(sorted({p.symbol for p in positions if p.symbol}))
        except Exception:
            prices = {}
        for p in positions:
            px = prices.get(p.symbol)
            if px is not None and px > 0:
                p.last_price = float(px)
        return positions

    def holdings(self) -> list[Position]:
        """Delivery holdings in demat. Confirmed against
        api-docs.indstocks.com/portfolio_funds on 2026-09-02 — the real path
        is `/portfolio/holdings` (not `/holdings`) and each row carries
        `symbol` / `total_qty` / `avg_price` but NO price field. `last_price`
        is filled in afterwards from the confirmed quote endpoint
        (see _attach_live_prices) so the account valuation in
        `engine.execute.sync_from_broker` is real, not cash-only.
        """
        data = self._get("/portfolio/holdings")
        out = []
        for h in (data.get("data") or []):
            qty = _to_int(h.get("total_qty"))
            if qty <= 0:
                continue
            out.append(Position(
                symbol=str(h.get("symbol") or "").upper(),
                quantity=qty,
                average_price=_to_float(h.get("avg_price")),
                last_price=0.0,
                product="CNC",
            ))
        return self._attach_live_prices(out)

    def positions(self) -> list[Position]:
        """Open intraday / derivative positions. Confirmed against the
        official docs on 2026-09-02 — real path is `/portfolio/positions`
        (not `/positions`), rows carry `symbol` / `net_qty` / `avg_price` /
        `product` / `segment` and no price field. `last_price` is filled in
        from the confirmed quote endpoint (see _attach_live_prices)."""
        data = self._get("/portfolio/positions")
        out = []
        for p in (data.get("data") or []):
            qty = _to_int(p.get("net_qty"))
            if qty == 0:
                continue
            out.append(Position(
                symbol=str(p.get("symbol") or "").upper(),
                quantity=qty,
                average_price=_to_float(p.get("avg_price")),
                last_price=0.0,
                product=str(p.get("product") or ""),
                segment=str(p.get("segment") or "EQUITY"),
            ))
        return self._attach_live_prices(out)

    def quote(self, symbols: list[str], exchange: str = "NSE") -> dict:
        """Confirmed against api-docs.indstocks.com/MarketQuote/ on 2026-09-07:
        GET /market/quotes/ltp?scrip-codes=EXCH_SECURITYID (comma-separated for a batch),
        response {"data": {"NSE_3045": {"live_price": 792.5}}}. One call for every symbol
        requested, not one call per symbol.
        """
        code_to_sym: dict[str, str] = {}
        for sym in symbols:
            sid = self.security_id(sym, exchange)
            if sid:
                code_to_sym[f"{exchange.upper()}_{sid}"] = sym.upper()

        out: dict[str, float] = {}
        if not code_to_sym:
            return out
        try:
            data = self._get("/market/quotes/ltp", {"scrip-codes": ",".join(code_to_sym)})
            rows = data.get("data", {})
            for code, sym in code_to_sym.items():
                price = (rows.get(code) or {}).get("live_price")
                if price is not None:
                    out[sym] = float(price)
        except Exception:
            pass  # absent price, never a fabricated zero
        return out

    # -- writes ----------------------------------------------------------------

    def place(self, symbol: str, side: str, quantity: int, price: float,
              exchange: str = "NSE", product: str = "CNC",
              segment: str = "EQUITY", tag: str = "") -> OrderResult:
        sid = self.security_id(symbol, exchange)
        if not sid:
            return OrderResult(
                ok=False,
                error=f"Could not resolve security_id for {exchange}:{symbol}. "
                      f"Refusing to place an order rather than guess the instrument.",
            )

        body = {
            "txn_type": side.upper(),
            "exchange": exchange.upper(),
            "segment": segment.upper(),
            "product": product.upper(),
            "order_type": "LIMIT",
            "limit_price": round(float(price), 2),
            "validity": "DAY",
            "security_id": sid,
            "qty": int(quantity),
            "is_amo": False,
            "algo_id": ALGO_ID.get(exchange.upper(), ALGO_ID["NSE"]),
            "remarks": (tag or "agent")[:100],
        }

        try:
            data = self._post("/order", body)
        except Exception as e:
            return OrderResult(ok=False, error=f"{type(e).__name__}: {e}")

        if str(data.get("status", "")).lower() != "success":
            return OrderResult(ok=False, error=json.dumps(data)[:400], raw=data)

        d = data.get("data", {})
        return OrderResult(ok=True, order_id=str(d.get("order_id", "")),
                           status=str(d.get("order_status", "")), raw=data)

    def place_stop(self, symbol: str, side: str, quantity: int, trigger_price: float,
                   last_price: float, exchange: str = "NSE",
                   product: str = "CNC") -> OrderResult:
        """Protective exit via a GTT ('smart order') so it survives between runs."""
        sid = self.security_id(symbol, exchange)
        if not sid:
            return OrderResult(ok=False, error=f"Cannot resolve {symbol} for stop-loss")

        exit_side = "SELL" if side.upper() == "BUY" else "BUY"
        # A limit slightly through the trigger so the exit actually fills.
        limit = trigger_price * (0.995 if exit_side == "SELL" else 1.005)

        body = {
            "txn_type": exit_side,
            "exchange": exchange.upper(),
            "segment": "EQUITY",
            "product": product.upper(),
            "order_type": "LIMIT",
            "trigger_price": round(float(trigger_price), 2),
            "limit_price": round(limit, 2),
            "validity": "DAY",
            "security_id": sid,
            "qty": int(quantity),
            "algo_id": ALGO_ID.get(exchange.upper(), ALGO_ID["NSE"]),
            "remarks": "agent-stop",
        }

        for path in ("/gtt", "/smart-order"):
            try:
                data = self._post(path, body)
                if str(data.get("status", "")).lower() == "success":
                    d = data.get("data", {})
                    return OrderResult(ok=True, order_id=str(d.get("order_id", "")),
                                       status="GTT_PLACED", raw=data)
            except Exception:
                continue

        return OrderResult(
            ok=False,
            error="STOP-LOSS NOT PLACED — the position is unprotected. Alert Vaibhav "
                  "immediately and place the stop manually.",
        )

    def cancel(self, order_id: str) -> OrderResult:
        try:
            data = self._post("/order/cancel", {"order_id": order_id})
            ok = str(data.get("status", "")).lower() == "success"
            return OrderResult(ok=ok, order_id=order_id, raw=data,
                               error="" if ok else json.dumps(data)[:400])
        except Exception as e:
            return OrderResult(ok=False, error=f"{type(e).__name__}: {e}")
