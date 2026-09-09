"""
The tradeable universe.

Deliberately narrow to start: highly liquid Nifty large-caps where the spread won't
quietly eat a small account. strategy.md permits widening to Nifty 500 only once the
approach has a proven track record on this narrower list — so widening is a decision to
be justified in a weekly review, not a default.

The liquidity filter in screener.py still applies on top of this list; membership here is
necessary, not sufficient.
"""

# Liquid Nifty 50 constituents across sectors. Symbols are NSE trading symbols.
UNIVERSE = [
    # Financials
    "HDFCBANK", "ICICIBANK", "SBIN", "KOTAKBANK", "AXISBANK", "BAJFINANCE",
    # IT
    "TCS", "INFY", "HCLTECH", "WIPRO", "TECHM",
    # Energy / materials
    "RELIANCE", "ONGC", "TATASTEEL", "JSWSTEEL", "HINDALCO", "COALINDIA",
    # Consumer
    "ITC", "HINDUNILVR", "NESTLEIND", "TITAN", "ASIANPAINT",
    # Auto
    # NOTE: TATAMOTORS was removed — the ticker 404s on Yahoo after the demerger into
    # separate CV and PV entities. Symbols change; the screener already skips anything
    # that fails to fetch, but a dead entry wastes a network round trip every run.
    "MARUTI", "M&M", "BAJAJ-AUTO", "EICHERMOT",
    # Pharma / healthcare
    "SUNPHARMA", "DRREDDY", "CIPLA",
    # Infra / telecom / power
    "LT", "BHARTIARTL", "NTPC", "POWERGRID", "ULTRACEMCO", "ADANIPORTS",
]

# Minimum 20-day average traded value, in ₹ crore (strategy.md liquidity filter).
MIN_TRADED_VALUE_CR = 50.0
