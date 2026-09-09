"""
Exact transaction costs — INDmoney / INDstocks.

Replaces the flat percentage estimate that was previously baked into guardrails.py. That
estimate said 0.22% round trip on equity delivery. The real figure at ₹10,000 capital is
about 0.79%, because it omitted the DP charge: a FLAT ₹18.50 + GST levied on every
delivery sell, per scrip. Flat fees are invisible in a percentage model and dominate at
small position sizes — here it is over 40% of the total cost of a round trip.

Getting this wrong isn't cosmetic. The guardrail rule "expected move must clear 3x costs"
was being evaluated against a number 3.6x too small, so trades that could not actually pay
for themselves were passing the check.

Source: https://www.chittorgarh.com/brokerage_charges/indmoney/182/ (verify periodically —
brokers change these, and a stale cost model silently re-introduces the same bug).
"""

from __future__ import annotations

from dataclasses import dataclass, asdict

GST_RATE = 0.18
SEBI_RATE = 0.000001          # ₹10 per crore

# Equity
EQ_BROKERAGE_PCT = 0.001      # 0.1% ...
EQ_BROKERAGE_CAP = 20.0       # ... or ₹20, whichever is LOWER
EQ_DELIVERY_STT = 0.001       # 0.1% both sides
EQ_INTRADAY_STT_SELL = 0.00025
EQ_EXCHANGE_TXN = 0.0000297   # NSE
EQ_STAMP_BUY = 0.00015        # 0.015%, buy side only
DP_CHARGE = 18.50             # flat, per scrip, on delivery SELL only (+GST)

# F&O
FO_BROKERAGE_FLAT = 20.0      # per executed order
FO_OPTIONS_STT_SELL = 0.001   # on premium
FO_OPTIONS_EXCHANGE = 0.00035  # approx, on premium
FO_STAMP_BUY = 0.00003


@dataclass
class CostBreakdown:
    brokerage: float
    stt: float
    exchange: float
    stamp: float
    sebi: float
    dp: float
    gst: float
    total: float
    pct_of_turnover: float

    def to_dict(self) -> dict:
        return {k: round(v, 4) for k, v in asdict(self).items()}


def _eq_brokerage(value: float) -> float:
    return min(value * EQ_BROKERAGE_PCT, EQ_BROKERAGE_CAP)


def equity_round_trip(entry_price: float, quantity: int,
                      intraday: bool = False) -> CostBreakdown:
    """Full buy-then-sell cost for an equity position."""
    buy_value = entry_price * quantity
    sell_value = buy_value  # cost estimate is on entry value; exit price is unknown here
    turnover = buy_value + sell_value

    brokerage = _eq_brokerage(buy_value) + _eq_brokerage(sell_value)

    if intraday:
        stt = sell_value * EQ_INTRADAY_STT_SELL
        dp = 0.0
    else:
        stt = (buy_value + sell_value) * EQ_DELIVERY_STT
        dp = DP_CHARGE  # only on the delivery sell

    exchange = turnover * EQ_EXCHANGE_TXN
    stamp = buy_value * EQ_STAMP_BUY
    sebi = turnover * SEBI_RATE
    gst = (brokerage + exchange + sebi + dp) * GST_RATE

    total = brokerage + stt + exchange + stamp + sebi + dp + gst
    return CostBreakdown(
        brokerage=brokerage, stt=stt, exchange=exchange, stamp=stamp,
        sebi=sebi, dp=dp, gst=gst, total=total,
        pct_of_turnover=(total / buy_value * 100) if buy_value else 0.0,
    )


def options_round_trip(premium: float, lot_size: int, lots: int = 1) -> CostBreakdown:
    """Full buy-then-sell cost for a long options position. No DP charge (not delivery),
    but the flat ₹20/order brokerage bites hard on small premiums."""
    value = premium * lot_size * lots
    turnover = value * 2

    brokerage = FO_BROKERAGE_FLAT * 2
    stt = value * FO_OPTIONS_STT_SELL
    exchange = turnover * FO_OPTIONS_EXCHANGE
    stamp = value * FO_STAMP_BUY
    sebi = turnover * SEBI_RATE
    gst = (brokerage + exchange + sebi) * GST_RATE

    total = brokerage + stt + exchange + stamp + sebi + gst
    return CostBreakdown(
        brokerage=brokerage, stt=stt, exchange=exchange, stamp=stamp,
        sebi=sebi, dp=0.0, gst=gst, total=total,
        pct_of_turnover=(total / value * 100) if value else 0.0,
    )


def cost_as_pct(entry_price: float, quantity: int, intraday: bool = False) -> float:
    """Round-trip cost as a fraction of position value — what guardrails.py needs."""
    if quantity <= 0 or entry_price <= 0:
        return 1.0  # nonsense input: make it fail the clearance test
    return equity_round_trip(entry_price, quantity, intraday).total / (entry_price * quantity)


def viability_report(capital_levels=None) -> str:
    """How badly costs bite at different account sizes. The honest answer to 'is ₹10,000
    enough?' — the drag doesn't vanish with scale, it goes from crippling to merely
    significant, and frequency matters more than size."""
    capital_levels = capital_levels or [10_000, 25_000, 50_000, 100_000, 250_000]
    risk_pct, stop_pct = 0.02, 0.03

    lines = [
        "Equity delivery — round-trip cost vs risk budget",
        "(2% risk per trade, 3% stop distance, 2:1 reward:risk)",
        "",
        "| Capital | Risk/trade | Position | Costs | % of position | % of risk | % of target profit |",
        "|---|---|---|---|---|---|---|",
    ]
    for cap in capital_levels:
        risk = cap * risk_pct
        position = risk / stop_pct
        price = 500.0
        qty = max(int(position / price), 1)
        cb = equity_round_trip(price, qty)
        target_profit = risk * 2
        lines.append(
            f"| ₹{cap:,} | ₹{risk:,.0f} | ₹{qty * price:,.0f} | ₹{cb.total:,.0f} | "
            f"{cb.pct_of_turnover:.2f}% | {cb.total / risk * 100:.0f}% | "
            f"{cb.total / target_profit * 100:.0f}% |"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    print(viability_report())
    print()
    cb = equity_round_trip(500.0, 13)
    print("Worked example — ₹6,500 delivery position:")
    for k, v in cb.to_dict().items():
        print(f"  {k:18} {v}")
