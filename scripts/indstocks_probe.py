"""
INDstocks API contract probe — READ ONLY.

    venv/bin/python scripts/indstocks_probe.py

Why this exists
---------------
`engine/broker_indstocks.py` was written against the documented endpoints, but
the exact JSON field names in the responses were inferred, not observed. Every
reader has a fallback chain like:

    for k in ("available_balance", "availableBalance", "net_available", "cash"):

That is a guess wearing a seatbelt. It works or it silently returns 0.0, and a
funds reader that returns 0.0 instead of raising is exactly the kind of quiet
wrong answer that ends with the agent believing it has no money — or worse,
believing it has some.

This probe calls each READ endpoint once with your real token and reports the
field names that actually came back, so the parsers can be fixed against reality
before a single rupee is at risk.

SAFETY
------
* Only HTTP GET. There is no code path here that can POST, and therefore none
  that can place, modify or cancel an order. Verified by test_broker_probe.
* Output is REDACTED: no token, no account or client id, no PAN, no bank
  details, and holdings are reported as shapes and counts rather than contents.
  You are going to paste this into a chat window; it should be safe to.
* Values are summarised by TYPE and magnitude, not printed raw, except where a
  literal value is needed to fix a parser (status strings, segment codes).
"""

from __future__ import annotations

import re
import sys
import json
import argparse
import datetime as dt
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

import requests  # noqa: E402

BASE_URL = "https://api.indstocks.com"
TIMEOUT = 20

# Read-only endpoints, with the params broker_indstocks.py actually sends.
READ_ENDPOINTS = [
    ("funds",     "/funds",     {}),
    ("holdings",  "/holdings",  {}),
    ("positions", "/positions", {}),
    ("orders",    "/orders",    {}),
    ("profile",   "/profile",   {}),
]

SEARCH_SYMBOLS = ["INFY", "RELIANCE", "TATASTEEL"]

# Anything matching these key names is never printed, at any nesting depth.
SENSITIVE_KEY = re.compile(
    r"(token|auth|secret|password|pan|aadha|bank|account_?(no|num|id)|"
    r"client_?(id|code)|ucc|dp_?id|email|mobile|phone|address|dob|nominee)",
    re.IGNORECASE)

# Short literal values that are genuinely useful for fixing a parser.
SAFE_LITERAL = re.compile(r"^(EQ|NSE|BSE|CNC|MIS|NRML|INTRADAY|MARGIN|DELIVERY|"
                          r"EQUITY|DERIVATIVE|BUY|SELL|success|failure|error|"
                          r"COMPLETE|PENDING|REJECTED|CANCELLED|OPEN|true|false)$",
                          re.IGNORECASE)


def describe(value, key: str = "", depth: int = 0):
    """Type-and-shape description with sensitive values stripped.

    The goal is a document that tells me exactly how to write the parser and
    tells a stranger nothing about the account.
    """
    if key and SENSITIVE_KEY.search(key):
        return "<redacted>"
    if depth > 4:
        return "<deeper>"

    if isinstance(value, dict):
        return {k: describe(v, k, depth + 1) for k, v in list(value.items())[:40]}
    if isinstance(value, list):
        if not value:
            return "[] (empty)"
        return [describe(value[0], key, depth + 1),
                f"... {len(value)} item(s) total, first shown"]
    if value is None:
        return "null"
    if isinstance(value, bool):
        return f"bool({value})"
    if isinstance(value, (int, float)):
        # Magnitude tells me it's a price vs a quantity vs an id, without
        # revealing the balance.
        av = abs(value)
        band = ("0" if av == 0 else "<1" if av < 1 else "1-99" if av < 100 else
                "100-9,999" if av < 10_000 else "10k-9.9L" if av < 1_000_000 else "1M+")
        return f"{type(value).__name__}(~{band})"
    if isinstance(value, str):
        s = value.strip()
        if SAFE_LITERAL.match(s):
            return f'str("{s}")'
        if re.fullmatch(r"[\d\-:T .+/]{6,32}", s):
            return f"str(datelike, len={len(s)})"
        if s.isdigit():
            return f"str(numeric, len={len(s)})"
        return f"str(len={len(s)})"
    return type(value).__name__


class Probe:
    def __init__(self, token: str) -> None:
        self._headers = {"Authorization": token, "Content-Type": "application/json"}
        self.results: list[dict] = []

    def get(self, path: str, params: dict) -> dict:
        """The ONLY request method in this file. GET only, by construction."""
        r = requests.get(f"{BASE_URL}{path}", headers=self._headers,
                         params=params, timeout=TIMEOUT)
        return {"status_code": r.status_code,
                "body": r.json() if r.headers.get("content-type", "").startswith(
                    "application/json") else {"_raw_text_len": len(r.text)}}

    def endpoint(self, name: str, path: str, params: dict) -> dict:
        entry = {"name": name, "path": path}
        try:
            res = self.get(path, params)
            entry["http"] = res["status_code"]
            entry["shape"] = describe(res["body"])
            entry["top_level_keys"] = (sorted(res["body"].keys())
                                       if isinstance(res["body"], dict) else None)
        except Exception as e:
            entry["error"] = f"{type(e).__name__}: {e}"
        self.results.append(entry)
        return entry

    def search(self, symbol: str) -> dict:
        """The single most important call here.

        Orders take a security_id, not a trading symbol. broker_indstocks.py
        resolves it via /search and REFUSES the order if resolution fails —
        which is the safe failure, but it means a wrong parser here turns into
        'the agent cannot trade anything' rather than a visible error.
        """
        entry = {"name": f"search:{symbol}", "path": "/search"}
        try:
            res = self.get("/search", {"query": symbol, "exchange": "NSE"})
            body = res["body"]
            entry["http"] = res["status_code"]
            entry["shape"] = describe(body)

            rows = body.get("data") if isinstance(body, dict) else None
            if isinstance(rows, list) and rows:
                first = rows[0]
                entry["row_keys"] = sorted(first.keys()) if isinstance(first, dict) else None
                # These two literals decide whether the current parser works.
                entry["symbol_field_candidates"] = {
                    k: v for k, v in first.items()
                    if isinstance(v, str) and v.upper() == symbol.upper()}
                entry["id_field_candidates"] = {
                    k: f"str(len={len(str(v))})" for k, v in first.items()
                    if "id" in k.lower() or "code" in k.lower()}
                entry["current_parser_would_work"] = bool(
                    str(first.get("symbol", "")).upper() == symbol.upper()
                    and (first.get("security_id") or first.get("securityId")))
            else:
                entry["note"] = "no 'data' list in response — parser needs rewriting"
        except Exception as e:
            entry["error"] = f"{type(e).__name__}: {e}"
        self.results.append(entry)
        return entry


def main() -> int:
    ap = argparse.ArgumentParser(description="INDstocks read-only contract probe")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    from scripts.indstocks_auth import token_status, TOKEN_FILE  # noqa
    status = token_status()
    if not status.get("valid"):
        print(f"No valid token: {status.get('reason')}\n\n"
              f"Get today's token from indstocks.com → Access Tokens, then:\n"
              f"  venv/bin/python scripts/indstocks_auth.py --set <token>\n",
              file=sys.stderr)
        return 2

    token = json.loads(TOKEN_FILE.read_text())["access_token"]
    probe = Probe(token)

    print("=" * 64)
    print("  INDSTOCKS CONTRACT PROBE — READ ONLY, NO ORDERS POSSIBLE")
    print(f"  {dt.datetime.now():%Y-%m-%d %H:%M}  token age {status['age_hours']}h")
    print("=" * 64)

    for name, path, params in READ_ENDPOINTS:
        e = probe.endpoint(name, path, params)
        mark = "✓" if e.get("http") == 200 else "✗"
        print(f"\n{mark} {name:<10} {path:<12} HTTP {e.get('http', '—')}")
        if e.get("error"):
            print(f"    {e['error']}")
        elif e.get("top_level_keys"):
            print(f"    top-level keys: {e['top_level_keys']}")

    for sym in SEARCH_SYMBOLS:
        e = probe.search(sym)
        mark = "✓" if e.get("current_parser_would_work") else "✗"
        print(f"\n{mark} search {sym:<10} HTTP {e.get('http', '—')}  "
              f"parser_works={e.get('current_parser_would_work')}")
        if e.get("row_keys"):
            print(f"    row keys: {e['row_keys']}")
        if e.get("id_field_candidates"):
            print(f"    id-ish fields: {e['id_field_candidates']}")

    print("\n" + "-" * 64)
    print("  FULL REDACTED SHAPES (paste this back to me)")
    print("-" * 64)
    print(json.dumps(probe.results, indent=2, default=str))

    ok = sum(1 for r in probe.results if r.get("http") == 200)
    resolvable = sum(1 for r in probe.results if r.get("current_parser_would_work"))
    print(f"\n{ok}/{len(probe.results)} endpoints answered. "
          f"{resolvable}/{len(SEARCH_SYMBOLS)} symbols resolve with the current parser.")
    if resolvable < len(SEARCH_SYMBOLS):
        print("\nSymbol resolution is incomplete. Orders will be REFUSED rather than\n"
              "guessed — safe, but the agent cannot trade until the parser matches\n"
              "the row keys above. Send me this output and I will correct it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
