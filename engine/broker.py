"""
Broker abstraction.

Everything above this line — guardrails, strategy, screener, journaling, the learning loop
— is broker-agnostic. Only this layer and its implementations know whether we're talking
to Zerodha or INDstocks. Swapping brokers should never touch a risk rule.

Set BROKER=indstocks (or kite) in .env.

The interface is deliberately small. A broker needs to answer five questions and do two
things:
    funds()      how much cash is actually available
    holdings()   what is held in demat
    positions()  what is open intraday/derivative
    quote()      what is it trading at
    place()      buy or sell
    place_stop() attach a protective exit that survives between runs
    cancel()     withdraw a resting order
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class OrderResult:
    ok: bool
    order_id: str = ""
    status: str = ""
    error: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class Position:
    symbol: str
    quantity: int
    average_price: float
    last_price: float = 0.0
    product: str = ""
    segment: str = "EQUITY"


class Broker(ABC):
    """Minimal contract every broker implementation must satisfy."""

    name: str = "abstract"

    @abstractmethod
    def is_authenticated(self) -> bool:
        """True only if a live, unexpired session exists. Never guess optimistically —
        a false positive here means a run proceeds and fails mid-order."""

    @abstractmethod
    def funds(self) -> float:
        """Actual spendable cash in the account, in rupees."""

    @abstractmethod
    def holdings(self) -> list[Position]:
        """Delivery holdings sitting in demat."""

    @abstractmethod
    def positions(self) -> list[Position]:
        """Open intraday / derivative positions."""

    @abstractmethod
    def quote(self, symbols: list[str], exchange: str = "NSE") -> dict:
        """{symbol: last_price}. Missing symbols are simply absent — callers must treat
        an absent price as unknown, never as zero."""

    @abstractmethod
    def place(self, symbol: str, side: str, quantity: int, price: float,
              exchange: str = "NSE", product: str = "CNC",
              segment: str = "EQUITY", tag: str = "") -> OrderResult:
        ...

    @abstractmethod
    def place_stop(self, symbol: str, side: str, quantity: int, trigger_price: float,
                   last_price: float, exchange: str = "NSE",
                   product: str = "CNC") -> OrderResult:
        """A protective exit that persists between scheduled runs. A position without one
        is a live guardrail violation, so implementations must report failure loudly
        rather than returning a soft success."""

    @abstractmethod
    def cancel(self, order_id: str) -> OrderResult:
        ...


def get_broker(name: Optional[str] = None) -> Broker:
    """Factory. Defaults to whatever BROKER says in .env, else indstocks."""
    name = (name or os.environ.get("BROKER") or "indstocks").strip().lower()

    if name == "indstocks":
        from .broker_indstocks import INDstocksBroker
        return INDstocksBroker()
    if name == "kite":
        from .broker_kite import KiteBroker
        return KiteBroker()

    raise ValueError(f"Unknown broker '{name}'. Set BROKER=indstocks or BROKER=kite.")
