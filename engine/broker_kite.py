"""
Zerodha Kite implementation of the Broker interface.

Kept working alongside INDstocks rather than deleted: the Kite path is fully deployed and
tested on the VPS, so it's a proven fallback if the INDstocks API disappoints. Switching
is one line in .env.
"""

from __future__ import annotations

import sys
from pathlib import Path

from .broker import Broker, OrderResult, Position

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))


class KiteBroker(Broker):
    name = "kite"

    def _kite(self):
        from kite_auth import get_kite_client  # noqa
        return get_kite_client()

    def is_authenticated(self) -> bool:
        try:
            from kite_auth import has_valid_token_today  # noqa
            return has_valid_token_today()
        except Exception:
            return False

    def funds(self) -> float:
        m = self._kite().margins(segment="equity")
        return float(m.get("available", {}).get("live_balance", 0.0))

    def holdings(self) -> list[Position]:
        return [
            Position(
                symbol=h["tradingsymbol"].upper(),
                quantity=int(h.get("quantity", 0)),
                average_price=float(h.get("average_price", 0)),
                last_price=float(h.get("last_price", 0)),
                product="CNC",
            )
            for h in self._kite().holdings()
            if int(h.get("quantity", 0)) > 0
        ]

    def positions(self) -> list[Position]:
        return [
            Position(
                symbol=p["tradingsymbol"].upper(),
                quantity=int(p.get("quantity", 0)),
                average_price=float(p.get("average_price", 0)),
                last_price=float(p.get("last_price", 0)),
                product=str(p.get("product", "")),
            )
            for p in self._kite().positions().get("net", [])
            if int(p.get("quantity", 0)) != 0
        ]

    def quote(self, symbols: list[str], exchange: str = "NSE") -> dict:
        try:
            q = self._kite().quote([f"{exchange}:{s.upper()}" for s in symbols])
        except Exception:
            return {}
        out = {}
        for key, val in q.items():
            sym = key.split(":", 1)[-1].upper()
            if val.get("last_price") is not None:
                out[sym] = float(val["last_price"])
        return out

    def place(self, symbol: str, side: str, quantity: int, price: float,
              exchange: str = "NSE", product: str = "CNC",
              segment: str = "EQUITY", tag: str = "") -> OrderResult:
        try:
            kite = self._kite()
            oid = kite.place_order(
                variety=kite.VARIETY_REGULAR,
                exchange=exchange,
                tradingsymbol=symbol.upper(),
                transaction_type=side.upper(),
                quantity=int(quantity),
                product=kite.PRODUCT_MIS if product.upper() in ("MIS", "INTRADAY")
                else kite.PRODUCT_CNC,
                order_type=kite.ORDER_TYPE_LIMIT,
                price=round(float(price), 2),
                validity=kite.VALIDITY_DAY,
                tag=(tag or "agent")[:20],
            )
            return OrderResult(ok=True, order_id=str(oid), status="PLACED")
        except Exception as e:
            return OrderResult(ok=False, error=f"{type(e).__name__}: {e}")

    def place_stop(self, symbol: str, side: str, quantity: int, trigger_price: float,
                   last_price: float, exchange: str = "NSE",
                   product: str = "CNC") -> OrderResult:
        exit_side = "SELL" if side.upper() == "BUY" else "BUY"
        try:
            kite = self._kite()
            gtt_id = kite.place_gtt(
                trigger_type=kite.GTT_TYPE_SINGLE,
                tradingsymbol=symbol.upper(),
                exchange=exchange,
                trigger_values=[round(float(trigger_price), 2)],
                last_price=round(float(last_price), 2),
                orders=[{
                    "transaction_type": exit_side,
                    "quantity": int(quantity),
                    "order_type": kite.ORDER_TYPE_LIMIT,
                    "product": kite.PRODUCT_MIS if product.upper() in ("MIS", "INTRADAY")
                    else kite.PRODUCT_CNC,
                    "price": round(trigger_price * (0.995 if exit_side == "SELL" else 1.005), 2),
                }],
            )
            return OrderResult(ok=True, order_id=str(gtt_id), status="GTT_PLACED")
        except Exception as e:
            return OrderResult(
                ok=False,
                error=f"STOP-LOSS NOT PLACED ({type(e).__name__}: {e}) — position is "
                      f"unprotected. Alert Vaibhav immediately.",
            )

    def cancel(self, order_id: str) -> OrderResult:
        try:
            kite = self._kite()
            kite.cancel_order(variety=kite.VARIETY_REGULAR, order_id=order_id)
            return OrderResult(ok=True, order_id=order_id)
        except Exception as e:
            return OrderResult(ok=False, error=f"{type(e).__name__}: {e}")
