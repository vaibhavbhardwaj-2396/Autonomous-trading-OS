"""
Safety tests for the INDstocks probe and the broker switch.

The probe talks to a LIVE brokerage account. The only property that really
matters is that it cannot place an order — not "does not currently", but cannot,
by construction. That is checked here by reading the source, because a runtime
test would have to hit the live API to prove it.

Also checks that the probe's redaction actually redacts, since the whole point
of the output is that it is safe to paste into a chat window.

Run with:  python -m tests.test_broker_probe
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

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


PROBE = ROOT / "scripts" / "indstocks_probe.py"
SRC = PROBE.read_text()

# ---------------------------------------------------------------------------
print("\n--- The probe cannot place an order ---")
# ---------------------------------------------------------------------------

for forbidden in ("requests.post", "requests.put", "requests.delete", "requests.patch",
                  "session.post", "_post(", ".post("):
    check(f"no {forbidden}", forbidden not in SRC)

check("no order endpoints referenced",
      not any(p in SRC for p in ('"/order"', "'/order'", "/order/cancel", "/gtt",
                                 "/smart-order")))
# Naming the module in a docstring is fine; IMPORTING it is not, because that
# would put place() one attribute access away from a live account.
_probe_imports = re.findall(r"^\s*(?:from|import)\s+([.\w]+)", SRC, re.MULTILINE)
check("does not import the broker implementation",
      not any("broker" in m for m in _probe_imports), str(_probe_imports))

methods = re.findall(r"requests\.(\w+)\(", SRC)
check("every HTTP call is a GET", set(methods) <= {"get"}, str(set(methods)))


# ---------------------------------------------------------------------------
print("\n--- Redaction ---")
# ---------------------------------------------------------------------------

sys.path.insert(0, str(ROOT / "scripts"))
import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location("indstocks_probe", PROBE)
mod = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(mod)
    loaded = True
except Exception as e:
    loaded = False
    print(f"      (probe module did not import: {type(e).__name__}: {e})")

check("probe module imports", loaded)

if loaded:
    d = mod.describe
    payload = {
        "access_token": "eyJhbGciOi.super.secret",
        "client_id": "AB1234",
        "pan_number": "ABCDE1234F",
        "bank_account_no": "50100123456789",
        "email": "vaibhav@example.com",
        "available_balance": 57342.19,
        "qty": 25,
        "symbol": "INFY",
        "product": "CNC",
        "status": "success",
        "nested": {"auth_key": "shh", "ltp": 1612.4},
        "holdings": [{"symbol": "INFY", "avg_price": 1500.0, "pan": "XXXXX1111X"}],
    }
    out = d(payload)
    blob = str(out)

    for secret in ("super.secret", "AB1234", "ABCDE1234F", "50100123456789",
                   "vaibhav@example.com", "shh", "XXXXX1111X"):
        check(f"redacted: {secret[:14]}", secret not in blob, blob[:200])

    check("balance magnitude shown, value hidden",
          "57342" not in blob and "10k-9.9L" in blob, str(out.get("available_balance")))
    check("useful literals preserved (they are what fix a parser)",
          'str("INFY")' not in blob and out.get("product") == 'str("CNC")'
          and out.get("status") == 'str("success")', str(out))
    check("quantities keep their magnitude band", "1-99" in str(out.get("qty")))
    check("nested sensitive keys are caught at depth",
          "shh" not in str(out.get("nested")), str(out.get("nested")))
    check("lists report shape and count, not contents",
          isinstance(out.get("holdings"), list)
          and "item(s) total" in str(out["holdings"][1]), str(out.get("holdings")))


# ---------------------------------------------------------------------------
print("\n--- The broker switch itself ---")
# ---------------------------------------------------------------------------

from engine.broker import get_broker  # noqa: E402

b_ind = get_broker("indstocks")
b_kite = get_broker("kite")
check("indstocks broker constructs", b_ind.name == "indstocks")
check("kite is still available as a fallback", b_kite.name == "kite")
check("both satisfy the same interface",
      all(hasattr(b_ind, m) and hasattr(b_kite, m)
          for m in ("funds", "holdings", "positions", "quote", "place",
                    "place_stop", "cancel", "is_authenticated")))

check("an unauthenticated broker reports False rather than guessing",
      b_ind.is_authenticated() is False)

# The rest of the system must not care which broker is configured.
# costs.py is deliberately NOT in this list: brokerage rates, DP charges and
# STT differ per broker, so a cost model that claimed to be broker-agnostic
# would be lying. Everything that reasons about RISK must be agnostic; the
# thing that computes CHARGES must not be.
for mod_name in ("guardrails", "screener", "regime", "stats", "watchlist"):
    src = (ROOT / "engine" / f"{mod_name}.py").read_text()
    imports = re.findall(r"^\s*(?:from|import)\s+([.\w]+)", src, re.MULTILINE)
    check(f"engine/{mod_name}.py imports no broker module",
          not any("broker" in m for m in imports), str(imports))

check("execute.py reaches the broker only through the factory",
      "get_broker()" in (ROOT / "engine" / "execute.py").read_text()
      and "INDstocksBroker" not in (ROOT / "engine" / "execute.py").read_text())

# Again: an import, not a word. Several research modules explain IN PROSE that
# they deliberately avoid the broker, and a test that fails on its own
# documentation is worse than no test.
_offenders = []
for f in (ROOT / "research").rglob("*.py"):
    for m in re.findall(r"^\s*(?:from|import)\s+([.\w]+)", f.read_text(), re.MULTILINE):
        if "broker" in m:
            _offenders.append(f.name)
    if "get_broker(" in f.read_text():
        _offenders.append(f"{f.name}:get_broker()")
check("the research layer never imports or calls a broker",
      not _offenders,
      f"{_offenders} — the recorder must not depend on a token that expires "
      f"every 24h, or it goes silent on the days nobody sends one")


print(f"\n{'=' * 52}")
print(f"  {PASSED} passed, {FAILED} failed")
print(f"{'=' * 52}\n")
sys.exit(1 if FAILED else 0)
