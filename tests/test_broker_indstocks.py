"""
tests/test_broker_indstocks.py — response-parsing tests for the INDstocks
(INDmoney) broker adapter, engine/broker_indstocks.py.

Focus: the reader methods that feed engine.execute.sync_from_broker's
account valuation — funds(), holdings(), positions(), quote() — parsed
against the CONFIRMED response shapes (the field names in each method's
docstring, dated from the live probe / official docs), plus the fix for
account_total_value collapsing to cash-only:

  holdings()/positions() now attach a live last_price from the confirmed
  /market/quotes/ltp endpoint, because /portfolio/holdings and
  /portfolio/positions carry no price field and engine.execute
  (unchanged, frozen) values the account as sum(last_price * qty).

No network: every HTTP call (self._get) is stubbed with a canned response.

Run with:  python -m tests.test_broker_indstocks
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from engine.broker_indstocks import INDstocksBroker  # noqa: E402
from engine.broker import Position  # noqa: E402

PASSED, FAILED = 0, 0


def check(name, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  ✓ {name}")
    else:
        FAILED += 1
        print(f"  ✗ {name}")
        if detail:
            print(f"      {detail}")


class StubBroker(INDstocksBroker):
    """INDstocksBroker with _get() and instrument resolution stubbed.

    `responses` maps a path -> the JSON dict that path returns.
    `prices` maps SYMBOL -> live_price for the quote path; a symbol absent
    from `prices` is simply not returned (never a fabricated zero), exactly
    as the real /market/quotes/ltp behaves.
    """

    def __init__(self, responses: dict, prices: dict | None = None):
        super().__init__()
        self._responses = responses
        self._prices = prices or {}
        self._get_calls: list = []

    def _get(self, path: str, params=None):
        self._get_calls.append((path, params))
        if path == "/market/quotes/ltp":
            codes = (params or {}).get("scrip-codes", "").split(",")
            data = {}
            for code in codes:
                sym = self._code_to_sym.get(code)
                if sym and sym in self._prices:
                    data[code] = {"live_price": self._prices[sym]}
            return {"status": "success", "data": data}
        if path in self._responses:
            return self._responses[path]
        raise AssertionError(f"unexpected GET {path}")

    # Deterministic fake symbol<->security_id resolution: SYM -> NSE_<hash>,
    # and only for symbols we were told a price for OR that appear in a
    # holding (so an "unresolvable" symbol can be simulated by giving it no
    # price and marking it unresolvable).
    _unresolvable: set = set()

    def security_id(self, symbol: str, exchange: str = "NSE"):
        symu = symbol.upper()
        if symu in self._unresolvable:
            return None
        return f"sid{abs(hash(symu)) % 100000}"

    @property
    def _code_to_sym(self):
        # rebuilt each quote() call by the real quote() via security_id();
        # here we expose a reverse map for the stubbed _get above.
        return getattr(self, "_last_code_map", {})

    def quote(self, symbols, exchange: str = "NSE"):
        # mirror the real quote() but record the code->sym map for _get
        code_to_sym = {}
        for sym in symbols:
            sid = self.security_id(sym, exchange)
            if sid:
                code_to_sym[f"{exchange.upper()}_{sid}"] = sym.upper()
        self._last_code_map = code_to_sym
        return super().quote(symbols, exchange)


# ---------------------------------------------------------------------------
print("\n--- funds(): confirmed shape detailed_avl_balance.eq_cnc ---")
# ---------------------------------------------------------------------------

b = StubBroker({"/funds": {"status": "success", "data": {
    "sod_balance": 100.0, "withdrawal_balance": 50.0,
    "detailed_avl_balance": {"eq_cnc": 32.31, "eq_mis": 0.0, "future": 0.0},
}}})
check("funds() returns eq_cnc (the CNC buying power), not sod/withdrawal",
      b.funds() == 32.31, str(b.funds()))

b2 = StubBroker({"/funds": {"data": {"detailed_avl_balance": {"eq_cnc": "0"}}}})
check("funds() coerces a string '0' to 0.0", b2.funds() == 0.0)

b3 = StubBroker({"/funds": {"data": {"nothing_recognisable": 1}}})
try:
    b3.funds()
    check("funds() raises (never silently 0.0) when no known balance field is present", False)
except ValueError:
    check("funds() raises (never silently 0.0) when no known balance field is present", True)


# ---------------------------------------------------------------------------
print("\n--- holdings(): symbol/total_qty/avg_price + live last_price ---")
# ---------------------------------------------------------------------------

HOLDINGS = {"status": "success", "data": [
    {"symbol": "RELIANCE", "total_qty": 3, "avg_price": 2800.0},
    {"symbol": "INFY", "total_qty": 10, "avg_price": 1400.0},
    {"symbol": "ZERO", "total_qty": 0, "avg_price": 5.0},          # filtered out
]}

b = StubBroker({"/portfolio/holdings": HOLDINGS},
               prices={"RELIANCE": 3000.0, "INFY": 1500.0})
h = b.holdings()
check("holdings() drops zero-quantity rows", len(h) == 2)
check("holdings() parses symbol / quantity / average_price from the confirmed fields",
      h[0].symbol == "RELIANCE" and h[0].quantity == 3 and h[0].average_price == 2800.0, str(h[0]))
check("holdings() attaches a live last_price from the quote endpoint (RELIANCE 3000)",
      h[0].last_price == 3000.0, str(h[0]))
check("holdings() market value = last_price * qty is now real, not zero",
      sum(p.last_price * p.quantity for p in h) == 3000.0 * 3 + 1500.0 * 10,
      str([(p.symbol, p.last_price, p.quantity) for p in h]))
check("holdings() hit /market/quotes/ltp exactly once (one batched call, not per-symbol)",
      sum(1 for c in b._get_calls if c[0] == "/market/quotes/ltp") == 1, str(b._get_calls))


# ---------------------------------------------------------------------------
print("\n--- holdings(): a symbol with no quote stays UNPRICED (0.0), never faked ---")
# ---------------------------------------------------------------------------

b = StubBroker({"/portfolio/holdings": HOLDINGS}, prices={"RELIANCE": 3000.0})  # INFY missing
h = b.holdings()
infy = [p for p in h if p.symbol == "INFY"][0]
check("holdings(): a holding the quote endpoint didn't return keeps last_price = 0.0",
      infy.last_price == 0.0, str(infy))
check("holdings(): the priced holding is still priced (partial pricing is per-symbol)",
      [p for p in h if p.symbol == "RELIANCE"][0].last_price == 3000.0)


# ---------------------------------------------------------------------------
print("\n--- holdings(): unresolvable symbol -> unpriced, no crash ---")
# ---------------------------------------------------------------------------

b = StubBroker({"/portfolio/holdings": {"data": [
    {"symbol": "SGBAUG28", "total_qty": 5, "avg_price": 6000.0},
]}}, prices={})
b._unresolvable = {"SGBAUG28"}
h = b.holdings()
check("holdings(): a symbol with no security_id resolves to last_price 0.0, no exception",
      len(h) == 1 and h[0].last_price == 0.0, str(h))


# ---------------------------------------------------------------------------
print("\n--- holdings(): malformed values coerce safely ---")
# ---------------------------------------------------------------------------

b = StubBroker({"/portfolio/holdings": {"data": [
    {"symbol": "aaa", "total_qty": "7", "avg_price": None},
    {"symbol": None, "total_qty": 2, "avg_price": "abc"},
]}}, prices={"AAA": 10.0})
try:
    h = b.holdings()
    ok = True
except Exception as e:
    ok = False
    h = []
check("holdings(): string qty '7' and null/garbage avg_price do not crash the parser", ok)
if ok:
    check("holdings(): 'aaa' -> symbol upper-cased, qty 7, avg_price 0.0",
          h[0].symbol == "AAA" and h[0].quantity == 7 and h[0].average_price == 0.0, str(h))
    check("holdings(): a row with a non-numeric avg_price 'abc' -> average_price 0.0",
          h[1].average_price == 0.0, str(h[1]))


# ---------------------------------------------------------------------------
print("\n--- holdings(): empty / missing data -> [] ---")
# ---------------------------------------------------------------------------

check("holdings() on {'data': []} -> []", StubBroker({"/portfolio/holdings": {"data": []}}).holdings() == [])
check("holdings() on {} -> []", StubBroker({"/portfolio/holdings": {}}).holdings() == [])


# ---------------------------------------------------------------------------
print("\n--- positions(): net_qty/product/segment + live last_price ---")
# ---------------------------------------------------------------------------

b = StubBroker({"/portfolio/positions": {"data": [
    {"symbol": "TATASTEEL", "net_qty": 100, "avg_price": 150.0, "product": "MIS", "segment": "EQUITY"},
    {"symbol": "FLAT", "net_qty": 0, "avg_price": 1.0},        # filtered
]}}, prices={"TATASTEEL": 160.0})
p = b.positions()
check("positions() drops net_qty == 0 rows", len(p) == 1)
check("positions() parses symbol/net_qty/avg_price/product/segment",
      p[0].symbol == "TATASTEEL" and p[0].quantity == 100 and p[0].product == "MIS"
      and p[0].segment == "EQUITY", str(p[0]))
check("positions() attaches a live last_price (TATASTEEL 160)", p[0].last_price == 160.0)


# ---------------------------------------------------------------------------
print("\n--- quote(): confirmed shape data.<EXCH_sid>.live_price ---")
# ---------------------------------------------------------------------------

b = StubBroker({}, prices={"INFY": 1499.5, "RELIANCE": 2950.25})
q = b.quote(["INFY", "RELIANCE", "NOSUCH"])
check("quote() returns {SYMBOL: live_price} for the symbols the endpoint knew",
      q == {"INFY": 1499.5, "RELIANCE": 2950.25}, str(q))
check("quote() simply omits an unknown symbol (never a fabricated 0.0)", "NOSUCH" not in q)


# ---------------------------------------------------------------------------
print("\n--- _attach_live_prices(): mutates in place, resilient ---")
# ---------------------------------------------------------------------------

b = StubBroker({}, prices={"INFY": 1500.0})
positions = [Position(symbol="INFY", quantity=2, average_price=1400.0),
             Position(symbol="TCS", quantity=1, average_price=3500.0)]
out = b._attach_live_prices(positions)
check("_attach_live_prices returns the same list, prices attached where known",
      out is positions and positions[0].last_price == 1500.0 and positions[1].last_price == 0.0)
check("_attach_live_prices([]) -> [] (no quote call)", b._attach_live_prices([]) == [])


# ---------------------------------------------------------------------------
print("\n--- isolation: the adapter change adds no new import / order path ---")
# ---------------------------------------------------------------------------

SRC = (ROOT / "engine" / "broker_indstocks.py").read_text()
_code = re.sub(r'"""[\s\S]*?"""', "", SRC)
_imports = re.findall(r"^\s*(?:from|import)\s+([.\w]+)", _code, re.MULTILINE)
check("engine/broker_indstocks.py imports nothing from research/ or paper/",
      not any(m.split(".")[0] in ("research", "paper") for m in _imports), str(_imports))
check("holdings()/positions() price via the existing quote() only (no new endpoint constant)",
      _code.count("/market/quotes/ltp") <= 1
      and "_attach_live_prices" in _code, "pricing must reuse quote(), not a new path")

import subprocess  # noqa: E402
_prot = subprocess.run(
    ["git", "-C", str(ROOT), "diff", "--name-only", "HEAD", "--",
     "engine/guardrails.py", "engine/execute.py", "run_cycle.sh"],
    capture_output=True, text=True)
check("engine/guardrails.py, engine/execute.py, run_cycle.sh are unmodified",
      _prot.stdout.strip() == "", f"changed: {_prot.stdout.strip()!r}")


print(f"\n{'=' * 52}")
print(f"  {PASSED} passed, {FAILED} failed")
print(f"{'=' * 52}\n")
sys.exit(1 if FAILED else 0)
