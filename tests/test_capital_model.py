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
            "model_version": jr.MANAGED_MODEL_VERSION, "symbols": promoted or [],
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
      and aud["model_version"] == jr.MANAGED_MODEL_VERSION)
# idempotent — a full migration stamps the current model version, and main()
# short-circuits on a state that already carries it.
ns2, _ = mig.build_migration(ns, broker_free_cash=FC, priced_by_symbol={},
                             account_total_value=FC, broker_name="x", mode="live", now="tY")
check("6. a freshly migrated state carries the current MANAGED_MODEL_VERSION",
      (ns.get("managed") or {}).get("model_version") == jr.MANAGED_MODEL_VERSION)


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


# ---------------------------------------------------------------------------
print("\n--- 10. daily/weekly loss-cap baselines migrate to managed equity ---")
# ---------------------------------------------------------------------------
#
# The live-migration blocker: a state migrated to the managed model whose
# day/week dicts were created BEFORE `starting_managed_equity` existed. They
# still carry the legacy fixed `starting_capital` (₹10,000 scaffold) and the
# guardrail path used to fall back to it — reporting a ~99% weekly loss on a
# ~₹32 book. All amounts below are synthetic.


def v1_stuck(*, managed_eq, day_sc=10000.0, week_sc=10000.0, day_realized=0.0,
             cashflow=None, unmanaged=None, peak_capital=None):
    """A migrated (model_version 1) state whose day/week baselines were never
    re-based: `starting_capital` present, `starting_managed_equity` ABSENT."""
    cf = cashflow if cashflow is not None else [
        {"ts": "2026-09-01T00:00:00+05:30", "amount": managed_eq, "kind": "inception",
         "reason": "migration inception", "by": "migration"}]
    flow = round(sum(e["amount"] for e in cf), 2)
    return {
        "capital": managed_eq, "cash_available": managed_eq,
        "peak_capital": peak_capital if peak_capital is not None else round(flow, 2),
        "realized_pnl_alltime": 0.0, "risk_per_trade_pct": 0.02,
        "trading_paused": False, "awaiting_human_ack": False, "open_positions": [],
        "day": {"date": "2026-09-08", "starting_capital": day_sc,
                "realized_pnl": day_realized, "trades_taken": 0},
        "week": {"start_date": "2026-09-07", "starting_capital": week_sc},
        "broker_snapshot": {"unmanaged_symbols": unmanaged or [],
                            "free_cash": managed_eq, "total_account_value": managed_eq},
        "managed": {
            "model_version": 1, "symbols": [], "cashflow_events": cf,
            "peak_growth": 0.0, "growth": round(managed_eq - flow, 2),
            "portfolio_value": managed_eq, "free_cash": managed_eq,
            "positions_market_value": 0.0, "valued_at": "2026-09-10T09:00:00+05:30",
        },
    }


def corrected(st, eq):
    return mig.build_corrective(st, managed_equity=eq, now="2026-09-10T12:00:00+05:30")[0]


# --- A / B: no legacy authority -> no artificial daily OR weekly loss --------
X = 37000.0
stuck = v1_stuck(managed_eq=X)
gate = gr.check_can_open_positions(stuck)
summ = gr.status_summary(stuck)
check("A. migrated state, managed equity X, no starting_managed_equity -> NO daily loss reason",
      not any("daily loss" in r for r in gate.reasons), gate.reasons)
check("A. daily_loss_headroom is X·5% (managed-equity based), not 10000-based",
      summ["daily_loss_headroom"] == round(X * 0.05, 2), summ["daily_loss_headroom"])
check("B. same state -> NO weekly loss reason (was a ~99% false positive)",
      not any("weekly loss" in r for r in gate.reasons), gate.reasons)
check("B. weekly_loss_headroom is X·10%, not (X − 10000)-based",
      summ["weekly_loss_headroom"] == round(X * 0.10, 2), summ["weekly_loss_headroom"])
check("B. the whole gate is clear for a healthy migrated book",
      gate.allowed is True, gate.reasons)

# inception cash-flow event dated AFTER a stale week start must still be excluded
late_inception = v1_stuck(managed_eq=X, cashflow=[
    {"ts": "2026-09-09T10:00:00+05:30", "amount": X, "kind": "inception",
     "reason": "migration", "by": "migration"}])
check("B. an inception event dated after week_start is still excluded from weekly P&L",
      gr._period_realized_change(late_inception, late_inception["week"], X) == 0.0)

# --- C: starting_capital == 10000 is ignored for a migrated state -----------
c_small = v1_stuck(managed_eq=500.0, day_sc=10000.0, week_sc=10000.0)
check("C. day/week.starting_capital == 10000 but migrated -> baseline is managed equity (500)",
      gr._period_start_equity(c_small, c_small["day"]) == 500.0
      and gr._period_start_equity(c_small, c_small["week"]) == 500.0)
check("C. no false loss cap despite the literal 10000 sitting in state",
      not any("loss cap" in r for r in gr.check_can_open_positions(c_small).reasons),
      gr.check_can_open_positions(c_small).reasons)

# --- D / E: rollover snapshots CURRENT managed equity, dynamically ----------
roll = managed_state(cash=44000.0)
roll["day"]["date"] = "1999-01-01"
roll["managed"]["portfolio_value"] = roll["capital"] = 44000.0
jr.roll_day_if_needed(roll)
check("D. day rollover snapshots starting_managed_equity == managed.portfolio_value (44000)",
      roll["day"]["starting_managed_equity"] == 44000.0, roll["day"])
roll["day"]["date"] = "1999-01-02"
roll["managed"]["portfolio_value"] = roll["capital"] = 51000.0
jr.roll_day_if_needed(roll)
check("D. a later rollover snapshots the NEW managed equity (51000) — dynamic, not fixed",
      roll["day"]["starting_managed_equity"] == 51000.0, roll["day"])

wk = managed_state(cash=30000.0)
wk["day"]["date"] = "1999-01-01"
wk.pop("week", None)
wk["managed"]["portfolio_value"] = wk["capital"] = 30000.0
jr.roll_day_if_needed(wk)
check("E. week rollover snapshots starting_managed_equity == managed.portfolio_value (30000)",
      wk["week"]["starting_managed_equity"] == 30000.0, wk.get("week"))
check("E. legacy state rollover leaves starting_managed_equity None (legacy path stays legacy)",
      jr.roll_day_if_needed({"capital": 5000.0, "day": {"date": "1999-01-01"},
                             "peak_capital": 5000.0})["day"]["starting_managed_equity"] is None)

# --- F / G: deposits & withdrawals are cash flow, NEVER trading P&L ---------
dep = corrected(v1_stuck(managed_eq=20000.0), 20000.0)
jr.record_cashflow_event(dep, amount=15000.0, reason="top-up", by="V", kind="deposit",
                         now="2026-09-11T10:00:00+05:30")
dep["managed"]["portfolio_value"] = dep["managed"]["free_cash"] = dep["capital"] = 35000.0
check("F. a deposit raises managed cash & equity (20000 -> 35000)",
      gr.managed_equity(dep) == 35000.0)
check("F. the deposit is EXCLUDED from weekly trading P&L (week_pnl == 0, no artificial profit)",
      gr._period_realized_change(dep, dep["week"], 35000.0) == 0.0)
check("F. deposit creates no weekly loss reason",
      not any("weekly loss" in r for r in gr.check_can_open_positions(dep).reasons))

wd = corrected(v1_stuck(managed_eq=40000.0), 40000.0)
jr.record_cashflow_event(wd, amount=-12000.0, reason="drawdown", by="V", kind="withdrawal",
                         now="2026-09-11T10:00:00+05:30")
wd["managed"]["portfolio_value"] = wd["managed"]["free_cash"] = wd["capital"] = 28000.0
check("G. a withdrawal lowers managed cash & equity (40000 -> 28000)",
      gr.managed_equity(wd) == 28000.0)
check("G. the withdrawal is EXCLUDED from weekly trading P&L (week_pnl == 0, no artificial loss)",
      gr._period_realized_change(wd, wd["week"], 28000.0) == 0.0)
check("G. withdrawal creates NO artificial weekly loss cap",
      not any("weekly loss" in r for r in gr.check_can_open_positions(wd).reasons),
      gr.check_can_open_positions(wd).reasons)

# --- H: REAL managed trading P&L still moves the loss caps correctly -------
tl = corrected(v1_stuck(managed_eq=50000.0), 50000.0)
tl["managed"]["portfolio_value"] = tl["capital"] = 44000.0        # −₹6,000 (−12%), no cash flow
check("H. a real −12% managed trading loss (no cash flow) -> weekly week_pnl == −6000",
      gr._period_realized_change(tl, tl["week"], 44000.0) == -6000.0)
check("H. that real loss DOES trip the weekly loss cap (≥10%)",
      any("weekly loss" in r for r in gr.check_can_open_positions(tl).reasons),
      gr.check_can_open_positions(tl).reasons)
td_hit = corrected(v1_stuck(managed_eq=50000.0, day_realized=-2600.0), 50000.0)   # −5.2%
td_near = corrected(v1_stuck(managed_eq=50000.0, day_realized=-2400.0), 50000.0)  # −4.8%
check("H. a real daily realized loss −2600 on 50000 (>5%) trips the daily cap",
      any("daily loss" in r for r in gr.check_can_open_positions(td_hit).reasons))
check("H. a real daily realized loss −2400 on 50000 (<5%) does NOT trip it",
      not any("daily loss" in r for r in gr.check_can_open_positions(td_near).reasons))

# --- I: unmanaged broker holdings have ZERO influence on managed loss caps --
base_i = corrected(v1_stuck(managed_eq=25000.0), 25000.0)
big_unm = corrected(v1_stuck(managed_eq=25000.0, unmanaged=[f"U{i}" for i in range(40)]), 25000.0)
big_unm["broker_snapshot"]["total_account_value"] = 25000.0 + 900000.0
check("I. 40 unmanaged holdings worth ~₹9L -> identical daily & weekly baselines",
      gr._period_start_equity(base_i, base_i["day"]) == gr._period_start_equity(big_unm, big_unm["day"])
      and gr._period_start_equity(base_i, base_i["week"]) == gr._period_start_equity(big_unm, big_unm["week"]))
check("I. unmanaged holdings change neither the loss headrooms nor the gate reasons",
      gr.status_summary(base_i)["weekly_loss_headroom"] == gr.status_summary(big_unm)["weekly_loss_headroom"]
      and gr.check_can_open_positions(big_unm).reasons == gr.check_can_open_positions(base_i).reasons)

# --- J: the ₹10,000 baseline cannot reappear in the migrated guardrail path -
j = v1_stuck(managed_eq=250.0, day_sc=10000.0, week_sc=10000.0)
j_txt = json.dumps(gr.status_summary(j)) + " " + " ".join(gr.check_can_open_positions(j).reasons)
check("J. no '10000' anywhere in status_summary output or gate reasons for a migrated state",
      "10000" not in j_txt, j_txt)
check("J. _period_start_equity returns the managed figure (250), never starting_capital (10000)",
      gr._period_start_equity(j, j["day"]) == 250.0 and gr._period_start_equity(j, j["week"]) == 250.0)
jc = corrected(j, 250.0)
check("J. the corrective migration also strips 10000 from day/week.starting_capital",
      jc["day"]["starting_capital"] == 250.0 and jc["week"]["starting_capital"] == 250.0
      and jc["day"]["starting_managed_equity"] == 250.0)

# --- K: no observed live amount is hard-coded in the new code --------------
def _codestrip(path):
    src = (ROOT / path).read_text()
    src = re.sub(r'"""[\s\S]*?"""', "", src)
    return re.sub(r"#.*", "", src)

_G, _J, _M = _codestrip("engine/guardrails.py"), _codestrip("engine/journal.py"), \
    _codestrip("scripts/migrate_capital_model.py")
_forbidden = ("32.31", "63339", "63307", "570447", "570000", "63000", "5.7L")
check("K. no observed live amount (32.31 / 63,339 / 63,307 / 570,447 / …) in the new code",
      not any(t in _G or t in _J or t in _M for t in _forbidden),
      [t for t in _forbidden if t in _G or t in _J or t in _M])
_helpers = _G[_G.index("def _period_start_equity"):_G.index("def drawdown_pct")]
check("K. the new guardrail helpers hard-code no rupee literal",
      not re.search(r"\b\d{3,}\b", _helpers), re.findall(r"\b\d{3,}\b", _helpers))

# --- L: legacy peak_capital stays inert for a migrated state --------------
lp = v1_stuck(managed_eq=15000.0, peak_capital=570447.95)
check("L. _ladder_peak ignores the legacy peak_capital ratchet (uses Σcashflow + peak_growth)",
      gr._ladder_peak(lp) == 15000.0 and gr._ladder_peak(lp) != 570447.95)
lsum = gr.status_summary(lp)
check("L. status_summary peak_capital is the cash-flow-adjusted peak (15000), drawdown NORMAL",
      lsum["peak_capital"] == 15000.0 and lsum["drawdown_level"] == "NORMAL")
check("L. 570447.95 appears nowhere in the migrated status_summary",
      "570447" not in json.dumps(lsum))
lc = corrected(lp, 15000.0)
check("L. the corrective migration does not touch peak_capital / peak_growth / growth",
      lc["peak_capital"] == 570447.95 and lc["managed"]["peak_growth"] == 0.0
      and lc["managed"]["growth"] == lp["managed"]["growth"])

# --- M: legacy / unmigrated states keep their legacy behaviour ------------
legacy_m = {"capital": 8000.0, "peak_capital": 8200.0, "cash_available": 8000.0,
            "risk_per_trade_pct": 0.02, "trading_paused": False, "awaiting_human_ack": False,
            "day": {"starting_capital": 10000.0, "realized_pnl": -450.0},
            "week": {"starting_capital": 10000.0}, "open_positions": []}
check("M. legacy state (no managed block): _period_start_equity USES starting_capital (10000)",
      gr._period_start_equity(legacy_m, legacy_m["day"]) == 10000.0
      and gr._period_start_equity(legacy_m, legacy_m["week"]) == 10000.0)
check("M. legacy daily cap unchanged: −450 on 10000 = 4.5% -> no daily reason",
      not any("daily loss" in r for r in gr.check_can_open_positions(legacy_m).reasons))
_lw = copy.deepcopy(legacy_m); _lw["capital"] = 8900.0
check("M. legacy weekly cap unchanged: equity 8900 vs 10000 start = −11% -> blocked",
      any("weekly loss" in r for r in gr.check_can_open_positions(_lw).reasons))
check("M. legacy state has no `managed` block, so build_corrective is never its path",
      "managed" not in legacy_m)

# --- ADDITIONAL MANDATORY: starting_managed_equity == 0.0 is honoured ------
z = v1_stuck(managed_eq=30000.0)
z["day"]["starting_managed_equity"] = 0.0
z["week"]["starting_managed_equity"] = 0.0
z["day"]["starting_capital"] = z["week"]["starting_capital"] = 10000.0
check("0.0: _period_start_equity returns 0.0 EXACTLY, no fall-through to starting_capital (10000)",
      gr._period_start_equity(z, z["day"]) == 0.0
      and gr._period_start_equity(z, z["week"]) == 0.0)
check("0.0: the `> 0` cap guards make a 0.0 baseline safe — no crash, no false loss cap",
      isinstance(gr.check_can_open_positions(z).reasons, list)
      and not any("loss cap" in r for r in gr.check_can_open_positions(z).reasons))
check("0.0: status_summary handles a 0.0 baseline without error and without 10000",
      "10000" not in json.dumps(gr.status_summary(z)))
zc = corrected(z, 30000.0)
check("0.0: build_corrective KEEPS an explicit 0.0 (is-not-None check, not `or`)",
      zc["day"]["starting_managed_equity"] == 0.0 and zc["week"]["starting_managed_equity"] == 0.0
      and zc["day"]["starting_capital"] == 0.0)

# --- corrective migration: idempotent, auditable, reversible -------------
c0 = v1_stuck(managed_eq=32000.0, day_sc=10000.0, week_sc=10000.0)
c1, a1 = mig.build_corrective(c0, managed_equity=32000.0, now="2026-09-10T12:00:00+05:30")
check("corr: model_version 1 -> 2",
      c0["managed"]["model_version"] == 1 and c1["managed"]["model_version"] == 2
      and c1["managed"]["model_version"] == jr.MANAGED_MODEL_VERSION)
check("corr: day/week starting_managed_equity set to managed equity (32000)",
      c1["day"]["starting_managed_equity"] == 32000.0
      and c1["week"]["starting_managed_equity"] == 32000.0)
check("corr: day/week starting_capital realigned 10000 -> 32000",
      c1["day"]["starting_capital"] == 32000.0 and c1["week"]["starting_capital"] == 32000.0)
check("corr: audit is action=corrective, from_model_version=1, per-period before/after",
      a1["action"] == "capital_model_corrective_migration" and a1["from_model_version"] == 1
      and a1["model_version"] == 2
      and a1["changes"]["week"]["from"]["starting_capital"] == 10000.0
      and a1["changes"]["week"]["to"]["starting_capital"] == 32000.0)
check("corr: managed equity / growth / peak_growth / symbols / realized P&L all untouched",
      c1["managed"]["portfolio_value"] == 32000.0
      and c1["managed"]["growth"] == c0["managed"]["growth"]
      and c1["managed"]["peak_growth"] == 0.0 and c1["managed"]["symbols"] == []
      and c1["realized_pnl_alltime"] == 0.0)
check("corr: after correction, status_summary reports NO daily/weekly loss cap",
      not any("loss cap" in r for r in gr.status_summary(c1)["blocking_reasons"]),
      gr.status_summary(c1)["blocking_reasons"])
c2, a2 = mig.build_corrective(c1, managed_equity=32000.0, now="2026-09-11T12:00:00+05:30")
check("corr: re-running is idempotent — baselines unchanged, changes == {}",
      c2["day"]["starting_managed_equity"] == 32000.0
      and c2["week"]["starting_managed_equity"] == 32000.0 and a2["changes"] == {})

# corrective CLI path — --simulate writes nothing; a write is idempotent on re-run
import io as _io, contextlib as _ctx  # noqa: E402

def _quiet_main(argv):
    with _ctx.redirect_stdout(_io.StringIO()):
        return mig.main(argv)

_cli = TMP / "cli_corrective.json"
_cli.write_text(json.dumps(v1_stuck(managed_eq=1234.0)))
_oms, _oma = mig.STATE_FILE, mig.AUDIT_LOG
mig.STATE_FILE, mig.AUDIT_LOG = _cli, TMP / "audit.jsonl"
try:
    rc_sim = _quiet_main(["--simulate"])
    check("CLI: --simulate corrective returns 0 and writes NOTHING (still model_version 1)",
          rc_sim == 0 and json.loads(_cli.read_text())["managed"]["model_version"] == 1)
    rc_w = _quiet_main([])
    _after = json.loads(_cli.read_text())
    check("CLI: corrective write -> model_version 2, starting_managed_equity backfilled (1234)",
          rc_w == 0 and _after["managed"]["model_version"] == 2
          and _after["day"]["starting_managed_equity"] == 1234.0
          and _after["week"]["starting_managed_equity"] == 1234.0)
    check("CLI: a .bak backup was written (reversible)",
          any(p.name.startswith("cli_corrective.json.pre-baseline-correction") for p in TMP.iterdir()))
    check("CLI: the audit log got one corrective record",
          (TMP / "audit.jsonl").exists()
          and json.loads((TMP / "audit.jsonl").read_text().splitlines()[-1])["action"]
          == "capital_model_corrective_migration")
    rc_again = _quiet_main([])
    check("CLI: re-running the corrective migration is a clean no-op (exit 0, still v2)",
          rc_again == 0 and json.loads(_cli.read_text())["managed"]["model_version"] == 2)
finally:
    mig.STATE_FILE, mig.AUDIT_LOG = _oms, _oma


import shutil  # noqa: E402
shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'=' * 52}")
print(f"  {PASSED} passed, {FAILED} failed")
print(f"{'=' * 52}\n")
sys.exit(1 if FAILED else 0)
