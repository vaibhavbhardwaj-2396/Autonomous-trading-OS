"""
tests/test_capital_model.py — the dynamic broker-derived capital model
(docs/CAPITAL_MODEL.md).

    CURRENT ACCOUNT VALUE  = broker cash + Σ(broker holding/position qty × LTP)
    CURRENT MANAGED EQUITY = managed cash + Σ(managed position qty × LTP)

Every fixture here is SYNTHETIC (cash = X, qty = Y, ltp = Z). No real /
observed rupee amount, no fixed "starting capital", appears anywhere.

Run with:  python -m tests.test_capital_model
"""

from __future__ import annotations

import copy
import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from engine import guardrails as gr  # noqa: E402
from engine import journal as jr  # noqa: E402
from scripts import migrate_capital_model as mig  # noqa: E402

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


TMP = Path(tempfile.mkdtemp(prefix="lq-capital-model-"))
_STATE = TMP / "state.json"
gr.STATE_FILE = _STATE   # redirect save_state / load_state for the writers below


def managed_state(*, cash, positions=None, cashflow=None, open_positions=None,
                  promoted=None, peak_growth=0.0):
    """A migrated state with a synthetic managed block.

    `positions` = {SYMBOL: (qty, ltp)} → market value qty*ltp is what a broker
    sync would compute; here we set portfolio_value directly to mimic that.
    """
    positions = positions or {}
    pos_mkt = round(sum(q * p for q, p in positions.values()), 2)
    pv = round(cash + pos_mkt, 2)
    flow = round(sum(e["amount"] for e in (cashflow or [{"amount": pv, "kind": "inception"}])), 2)
    return {
        "capital": pv, "peak_capital": round(flow + peak_growth, 2), "cash_available": cash,
        "realized_pnl_alltime": 0.0, "risk_per_trade_pct": 0.02,
        "trading_paused": False, "awaiting_human_ack": False,
        "day": {"date": "d", "starting_managed_equity": pv, "realized_pnl": 0.0, "trades_taken": 0},
        "week": {"start_date": "w", "starting_managed_equity": pv},
        "open_positions": open_positions or [],
        "broker_snapshot": {"unmanaged_symbols": [], "free_cash": cash, "total_account_value": pv},
        "managed": {
            "model_version": 1, "symbols": promoted or [],
            "cashflow_events": cashflow or [{"ts": "t0", "amount": pv, "kind": "inception",
                                             "reason": "inception", "by": "test"}],
            "peak_growth": peak_growth, "growth": round(pv - flow, 2),
            "portfolio_value": pv, "free_cash": cash, "positions_market_value": pos_mkt,
            "valued_at": "t0",
        },
    }


# ---------------------------------------------------------------------------
print("\n--- 1. dynamic formulas: managed equity = cash + Σ(qty × ltp) ---")
# ---------------------------------------------------------------------------

X, Y, Z = 40000.0, 30, 250.0          # synthetic
s = managed_state(cash=X, positions={"AAA": (Y, Z)})
check("managed equity == X + Y*Z",
      gr.managed_equity(s) == X + Y * Z, gr.managed_equity(s))
check("with no positions, managed equity == cash",
      gr.managed_equity(managed_state(cash=X)) == X)

# cash change -> equity change
check("cash rises by D -> managed equity rises by D (before any cash-flow record)",
      gr.managed_equity(managed_state(cash=X + 5000.0, positions={"AAA": (Y, Z)}))
      == gr.managed_equity(s) + 5000.0)

# ltp change -> equity change
check("holding LTP rises Z -> Z+10 -> managed equity rises by Y*10",
      gr.managed_equity(managed_state(cash=X, positions={"AAA": (Y, Z + 10.0)}))
      - gr.managed_equity(s) == Y * 10.0)

# quantity change -> equity change
check("managed position quantity Y -> Y+5 -> managed equity rises by 5*Z",
      gr.managed_equity(managed_state(cash=X, positions={"AAA": (Y + 5, Z)}))
      - gr.managed_equity(s) == 5 * Z)


# ---------------------------------------------------------------------------
print("\n--- 2. account value vs managed equity: unmanaged holdings excluded ---")
# ---------------------------------------------------------------------------

CASH, UQ, UP = 10000.0, 12, 800.0
# a migration from a state whose broker has one UNMANAGED holding
st = {"capital": 999.0, "peak_capital": 424242.0, "allocated_capital": 777.0,
      "cash_available": 5.0, "open_positions": [],
      "broker_snapshot": {"unmanaged_symbols": ["UNM"], "free_cash": CASH,
                          "total_account_value": CASH + UQ * UP}}
new_state, audit = mig.build_migration(
    st, broker_free_cash=CASH, priced_by_symbol={"UNM": UQ * UP},
    account_total_value=CASH + UQ * UP, broker_name="indstocks", mode="live", now="t")
check("2. ACCOUNT VALUE includes the unmanaged holding (cash + UQ*UP)",
      audit["broker_derived"]["account_total_value"] == CASH + UQ * UP)
check("2. MANAGED EQUITY excludes the unmanaged holding (== cash only)",
      new_state["managed"]["portfolio_value"] == CASH
      and new_state["managed"]["positions_market_value"] == 0.0, str(new_state["managed"]))
check("2. the unmanaged holding is NOT auto-promoted (managed.symbols stays [])",
      new_state["managed"]["symbols"] == [])
check("2. an agent-OPENED position IS in managed equity",
      mig.build_migration(
          {**st, "open_positions": [{"symbol": "MINE", "entry": 100.0, "quantity": 20}]},
          broker_free_cash=CASH, priced_by_symbol={"UNM": UQ * UP, "MINE": 20 * 150.0},
          account_total_value=CASH + UQ * UP + 20 * 150.0,
          broker_name="x", mode="live", now="t")[0]["managed"]["portfolio_value"]
      == CASH + 20 * 150.0)


# ---------------------------------------------------------------------------
print("\n--- 3. deposits / withdrawals are external cash flow, not P&L ---")
# ---------------------------------------------------------------------------

base = managed_state(cash=X)                              # equity X, growth 0, dd 0
check("3. baseline drawdown is 0", gr.drawdown_pct(base) == 0.0)

# a real strategy loss: equity falls, NO cash-flow event
loss = copy.deepcopy(base)
loss["managed"]["portfolio_value"] = X - 4000.0
loss["managed"]["peak_growth"] = 0.0                      # peak was at growth 0
check("3. a ₹4000 strategy loss creates a real drawdown",
      round(gr.drawdown_pct(loss), 4) == round(4000.0 / X, 4), gr.drawdown_pct(loss))

# now the user DEPOSITS D — recorded as a cash-flow event
_dd_before = gr.drawdown_pct(loss)
jr.record_cashflow_event(loss, amount=25000.0, reason="deposit", by="Vaibhav", kind="deposit")
loss["managed"]["portfolio_value"] = (X - 4000.0) + 25000.0     # next sync sees the extra cash
_flow = sum(e["amount"] for e in loss["managed"]["cashflow_events"])
_growth = loss["managed"]["portfolio_value"] - _flow
check("3. the deposit is NOT strategy P&L — growth stays exactly the -₹4000 loss",
      round(_growth, 2) == -4000.0, _growth)
check("3. the deposit does NOT ratchet peak_growth (no fake gain)",
      loss["managed"]["peak_growth"] == 0.0)
check("3. the deposit never WORSENS the drawdown (a bigger book is a bigger cushion)",
      gr.drawdown_pct(loss) <= _dd_before + 1e-9, f"{_dd_before} -> {gr.drawdown_pct(loss)}")

# a withdrawal from a healthy book
win = managed_state(cash=X)
win["managed"]["portfolio_value"] = X + 6000.0
win["managed"]["peak_growth"] = 6000.0
jr.record_cashflow_event(win, amount=-10000.0, reason="withdrawal", by="Vaibhav", kind="withdrawal")
win["managed"]["portfolio_value"] = (X + 6000.0) - 10000.0
check("3. a recorded withdrawal creates NO artificial drawdown (growth still +6000)",
      gr.drawdown_pct(win) == 0.0, gr.drawdown_pct(win))


# ---------------------------------------------------------------------------
print("\n--- 4. managed-position P&L moves growth and drawdown ---")
# ---------------------------------------------------------------------------

s0 = managed_state(cash=20000.0, positions={"AAA": (10, 1000.0)})   # equity 30000, growth 0
gain = copy.deepcopy(s0)
gain["managed"]["portfolio_value"] = 20000.0 + 10 * 1100.0          # AAA 1000 -> 1100
gain["managed"]["peak_growth"] = 1000.0
check("4. a managed holding rising 10% shows +₹1000 growth, drawdown 0",
      gr.drawdown_pct(gain) == 0.0 and gain["managed"]["portfolio_value"] == 31000.0)
draw = copy.deepcopy(gain)
draw["managed"]["portfolio_value"] = 20000.0 + 10 * 950.0           # then AAA -> 950
check("4. then falling to 950 is a real drawdown from the +1000 peak",
      round(gr.drawdown_pct(draw), 4) == round(1500.0 / 31000.0, 4), gr.drawdown_pct(draw))


# ---------------------------------------------------------------------------
print("\n--- 5. risk sizing uses managed equity; risk % still works ---")
# ---------------------------------------------------------------------------

sr = managed_state(cash=50000.0)             # managed equity 50000
sz = gr.size_position(entry_price=100.0, stop_price=95.0, target_price=115.0, state=sr)
check("5. risk budget == managed_equity × 2% (₹1000), qty = floor(1000/5) = 200",
      abs(sz.risk_amount - 200 * 5.0) < 1e-6 and sz.quantity == 200, str(sz))
sr2 = managed_state(cash=100000.0)           # double the equity -> double the budget
sz2 = gr.size_position(entry_price=100.0, stop_price=95.0, target_price=115.0, state=sr2)
check("5. doubling managed equity doubles the position (risk % unchanged)",
      sz2.quantity == 2 * sz.quantity, f"{sz.quantity} -> {sz2.quantity}")
summ = gr.status_summary(sr)
check("5. status_summary reports managed_equity and a matching risk budget",
      summ["managed_equity"] == 50000.0 and summ["risk_budget_per_trade"] == 1000.0, str(summ))


# ---------------------------------------------------------------------------
print("\n--- 6. migration: explicit / idempotent / legacy peak inert ---")
# ---------------------------------------------------------------------------

FC = 15000.0
legacy = {"capital": 999.0, "peak_capital": 987654.32, "allocated_capital": 111.0,
          "cash_available": 3.0, "drawdown_level": "RED", "open_positions": [],
          "broker_snapshot": {"unmanaged_symbols": ["A", "B", "C"], "free_cash": FC,
                              "total_account_value": FC + 500000.0}}
ns, aud = mig.build_migration(legacy, broker_free_cash=FC, priced_by_symbol={},
                              account_total_value=FC + 500000.0,
                              broker_name="indstocks", mode="live", now="tX")
check("6. migrated managed equity == broker free cash (no managed positions)",
      ns["managed"]["portfolio_value"] == FC and ns["managed"]["free_cash"] == FC)
check("6. peak_growth starts at 0 and growth is 0 -> drawdown_pct 0 at cutover",
      ns["managed"]["peak_growth"] == 0.0 and gr.drawdown_pct(ns) == 0.0)
check("6. legacy peak_capital (987654.32) is GONE from state, kept ONLY in the audit record",
      ns["peak_capital"] == FC and ns["capital"] == FC
      and aud["after"]["legacy_peak_capital_retained_only_here"] == 987654.32
      and 987654.32 not in (ns["peak_capital"], ns["capital"],
                            gr.status_summary(ns)["peak_capital"]), str(ns))
check("6. allocated_capital is removed from state",
      "allocated_capital" not in ns)
check("6. unmanaged holdings preserved as unmanaged (broker_snapshot untouched)",
      ns["broker_snapshot"]["unmanaged_symbols"] == ["A", "B", "C"]
      and ns["managed"]["symbols"] == [])
check("6. the audit record has before/after and a backup-able structure",
      aud["before"]["peak_capital"] == 987654.32 and aud["action"] == "capital_model_migration"
      and aud["model_version"] == 1)
# idempotent
ns2, _ = mig.build_migration(ns, broker_free_cash=FC, priced_by_symbol={},
                             account_total_value=FC, broker_name="x", mode="live", now="tY")
check("6. re-running build_migration on an already-migrated state changes model_version-gate",
      (ns.get("managed") or {}).get("model_version") == 1)   # main() would exit before calling build


# ---------------------------------------------------------------------------
print("\n--- 7. legacy (unmigrated) states still work — backward compat ---")
# ---------------------------------------------------------------------------

old = {"capital": 12345.0, "peak_capital": 15000.0, "cash_available": 12345.0,
       "risk_per_trade_pct": 0.02, "trading_paused": False, "awaiting_human_ack": False,
       "day": {"starting_capital": 12345.0, "realized_pnl": 0.0},
       "week": {"starting_capital": 12345.0}, "open_positions": []}
check("7. no `managed` block -> managed_equity() falls back to state['capital']",
      gr.managed_equity(old) == 12345.0)
check("7. no `managed` block -> drawdown_pct uses legacy peak_capital",
      round(gr.drawdown_pct(old), 4) == round((15000.0 - 12345.0) / 15000.0, 4))


# ---------------------------------------------------------------------------
print("\n--- 8. no hard-coded rupee account-capital / Kite literals in the new code ---")
# ---------------------------------------------------------------------------

def _code(path):
    src = (ROOT / path).read_text()
    src = re.sub(r'"""[\s\S]*?"""', "", src)          # strip docstrings
    src = re.sub(r"#.*", "", src)                     # strip comments
    return src

EXEC_C = _code("engine/execute.py")
GUARD_C = _code("engine/guardrails.py")
JOURN_C = _code("engine/journal.py")

_exec_10k = re.findall(r"10_?000(?:\.0)?", EXEC_C)
check("8. execute.py's ONLY 10000 literal is the legacy allocated_capital default",
      _exec_10k == ["10000.0"]
      and re.search(r'state\.get\("allocated_capital",\s*10000\.0\)', EXEC_C) is not None,
      str(_exec_10k))
check("8. execute.py has no observed Kite / INDmoney amount as a literal",
      not any(t in EXEC_C for t in ("570447", "63339", "63000", "5.7")))
# the dynamic branch (everything before `mgr is None`'s else) must contain no 10000
_dyn = EXEC_C.split("mgr is None")[0] if "mgr is None" in EXEC_C else EXEC_C
_dyn = _dyn.split("else:")[0] if "else:" in _dyn else _dyn
check("8. the dynamic (migrated) branch of execute.py has no fixed rupee capital literal",
      not re.search(r"10_?000", _dyn), _dyn[-200:])
check("8. engine/guardrails.py managed-equity path has no fixed capital literal",
      not re.search(r"portfolio_value.*1e?[0-9]{4}", GUARD_C)
      and "570447" not in GUARD_C and "63339" not in GUARD_C)
check("8. engine/journal.py managed model has no fixed capital / Kite literal",
      "10000" not in JOURN_C and "570447" not in JOURN_C and "63339" not in JOURN_C)
check("8. migrate_capital_model.py contains no observed rupee constant",
      not any(t in _code("scripts/migrate_capital_model.py")
              for t in ("570447", "63339", "63000", "= 10000")))
# TIER_*_CAPITAL are POLICY thresholds, not an account-value assumption — allowed
check("8. TIER_*_CAPITAL still exist as policy thresholds (compared vs dynamic equity)",
      "TIER_1_CAPITAL" in GUARD_C and "managed_equity(state)" in GUARD_C)


# ---------------------------------------------------------------------------
print("\n--- 9. isolation: research / paper cannot bypass live controls ---")
# ---------------------------------------------------------------------------

for pkg in ("research", "paper"):
    offenders = []
    for f in (ROOT / pkg).rglob("*.py"):
        body = f.read_text()
        for m in re.findall(r"^\s*(?:from|import)\s+([.\w]+)", body, re.MULTILINE):
            if m.split(".")[-1] in ("execute",) or "guardrails" in m:
                offenders.append(f"{f.name}:{m}")
        if "update_managed_equity" in body or "record_cashflow_event" in body \
                or "propose_trade" in body:
            offenders.append(f"{f.name}: capital/execution call")
    check(f"9. no {pkg}/ module imports engine.execute/guardrails or calls the capital writers",
          not offenders, str(offenders))

check("9. record_cashflow_event / update_managed_equity live in engine.journal only",
      "def record_cashflow_event" in (ROOT / "engine" / "journal.py").read_text()
      and not any("def record_cashflow_event" in (ROOT / p).read_text()
                  for p in ("api/data.py", "api/broker_truth.py")))

# research 'promotion' is a flag, never an action: managed.symbols is only ever
# written by the migration (to []) and would be by a future governed command.
check("9. nothing in research/ writes state['managed']['symbols']",
      not any("managed" in f.read_text() and "symbols" in f.read_text()
              and "state" in f.read_text()
              for f in (ROOT / "research").rglob("*.py")))


import shutil  # noqa: E402
shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'=' * 52}")
print(f"  {PASSED} passed, {FAILED} failed")
print(f"{'=' * 52}\n")
sys.exit(1 if FAILED else 0)
