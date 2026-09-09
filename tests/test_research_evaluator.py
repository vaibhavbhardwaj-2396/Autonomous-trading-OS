"""
Tests for Phase 1 Slice D: research/experiments/evaluator.py — pure
statistics over experiment_results, no engine.stats reuse, no live-data path.

Run with:  python -m tests.test_research_evaluator
"""

import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from research.store import Store  # noqa: E402
from research import memory as rm  # noqa: E402
from research.experiments import evaluator as ev  # noqa: E402

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


TMP = Path(tempfile.mkdtemp(prefix="lq-test-evaluator-"))


def fresh_store(name) -> Store:
    p = TMP / f"{name}.db"
    if p.exists():
        p.unlink()
    return Store.open(p)


def add_trade(store, contract_id, seq, net_pnl, gross_pnl=None, costs=1.0, r_multiple=None,
             entity="RELIANCE"):
    store.append_experiment_result(
        contract_id=contract_id, trade_seq=seq, entity=entity,
        entry_time="2020-01-01", exit_time="2020-01-05",
        entry_price=100.0, exit_price=100.0 + net_pnl / 10, quantity=10,
        gross_pnl=gross_pnl if gross_pnl is not None else net_pnl + costs,
        costs=costs, net_pnl=net_pnl, r_multiple=r_multiple,
    )


# ---------------------------------------------------------------------------
print("\n--- compute_verdict: empty, single, and mixed-outcome cases ---")
# ---------------------------------------------------------------------------

store = fresh_store("basic")

empty_verdict = ev.compute_verdict(store, "EXP-NOTHING")
check("an unknown/empty contract_id returns a clean zero verdict, not an error",
      empty_verdict["n_trades"] == 0 and empty_verdict["win_rate"] is None)

add_trade(store, "EXP-A", 1, net_pnl=100.0, r_multiple=1.0)
v1 = ev.compute_verdict(store, "EXP-A")
check("a single winning trade: n_trades=1", v1["n_trades"] == 1)
check("a single winning trade: win_rate=1.0", v1["win_rate"] == 1.0)
check("a single trade: t_stat is None (not computable from n=1, never fabricated)",
      v1["t_stat"] is None)
check("a single trade: expectancy_r equals its own r_multiple", v1["expectancy_r"] == 1.0)

add_trade(store, "EXP-A", 2, net_pnl=-50.0, r_multiple=-0.5)
add_trade(store, "EXP-A", 3, net_pnl=200.0, r_multiple=2.0)
add_trade(store, "EXP-A", 4, net_pnl=-30.0, r_multiple=-0.3)
v2 = ev.compute_verdict(store, "EXP-A")
check("mixed outcomes: n_trades=4", v2["n_trades"] == 4)
check("mixed outcomes: win_rate=0.5 (2 of 4 positive)", v2["win_rate"] == 0.5)
check("mixed outcomes: net_pnl sums exactly", v2["net_pnl"] == 100.0 - 50.0 + 200.0 - 30.0)
check("mixed outcomes: expectancy_r is the mean r_multiple",
      abs(v2["expectancy_r"] - ((1.0 - 0.5 + 2.0 - 0.3) / 4)) < 1e-9)
check("mixed outcomes: t_stat is a real number now that n>=2 with nonzero variance",
      isinstance(v2["t_stat"], float))

# a trade with no r_multiple (e.g. no stop declared) is excluded from
# expectancy_r / t_stat but still counted in n_trades / win_rate / pnl
store_none_r = fresh_store("none_r")
add_trade(store_none_r, "EXP-B", 1, net_pnl=50.0, r_multiple=None)
add_trade(store_none_r, "EXP-B", 2, net_pnl=-10.0, r_multiple=None)
v3 = ev.compute_verdict(store_none_r, "EXP-B")
check("trades with no r_multiple still count toward n_trades", v3["n_trades"] == 2)
check("trades with no r_multiple leave expectancy_r/t_stat as None, not zero",
      v3["expectancy_r"] is None and v3["t_stat"] is None)
check("trades with no r_multiple still contribute to net_pnl/win_rate",
      v3["net_pnl"] == 40.0 and v3["win_rate"] == 0.5)

# zero-variance r_multiples: t_stat must not divide by zero
store_flat = fresh_store("flat_r")
add_trade(store_flat, "EXP-C", 1, net_pnl=10.0, r_multiple=1.0)
add_trade(store_flat, "EXP-C", 2, net_pnl=10.0, r_multiple=1.0)
add_trade(store_flat, "EXP-C", 3, net_pnl=10.0, r_multiple=1.0)
v4 = ev.compute_verdict(store_flat, "EXP-C")
check("identical r_multiples (zero variance) give t_stat=None, never a "
      "division-by-zero crash or an infinite value",
      v4["t_stat"] is None)


# ---------------------------------------------------------------------------
print("\n--- resolve_hypothesis_id ---")
# ---------------------------------------------------------------------------

store5 = fresh_store("resolve")
hid, _ = rm.record_hypothesis_proposal(
    store5, claim="a claim", source="test", extra={"contract_id": "EXP-LINKED"})
check("resolve_hypothesis_id finds the hypothesis a contract's claim row named",
      ev.resolve_hypothesis_id(store5, "EXP-LINKED") == hid)
check("resolve_hypothesis_id returns None for a contract_id nothing claims",
      ev.resolve_hypothesis_id(store5, "EXP-NEVER-PROPOSED") is None)

# two contracts under the same hypothesis resolve to the SAME hypothesis_id,
# each keyed correctly by its own contract_id
rm.record_hypothesis_proposal(store5, claim="variant", source="test",
                              hypothesis_id=hid, extra={"contract_id": "EXP-LINKED-B"})
check("a second contract sharing the same hypothesis resolves to the same id",
      ev.resolve_hypothesis_id(store5, "EXP-LINKED-B") == hid)


# ---------------------------------------------------------------------------
print("\n--- record_verdict: computes AND writes, in one call ---")
# ---------------------------------------------------------------------------

store6 = fresh_store("record")
rm.record_hypothesis_proposal(store6, claim="claim", source="test",
                              hypothesis_id="HYP-20200101-aaaaaaaa",
                              extra={"contract_id": "EXP-RECORD"})
add_trade(store6, "EXP-RECORD", 1, net_pnl=42.0, r_multiple=0.8)

returned = ev.record_verdict(store6, "EXP-RECORD")
logged = rm.query_research_log(store6, rm.DATASET_VERDICT)
check("record_verdict writes exactly one verdict row", len(logged) == 1)
check("the returned verdict matches what was actually written",
      logged[0]["payload"]["n_trades"] == returned["n_trades"]
      and logged[0]["payload"]["net_pnl"] == returned["net_pnl"])
check("the written verdict is linked to the resolved hypothesis_id",
      logged[0]["payload"]["hypothesis_id"] == "HYP-20200101-aaaaaaaa")
check("the written verdict is linked to the right contract_id",
      logged[0]["payload"]["contract_id"] == "EXP-RECORD")


# ---------------------------------------------------------------------------
print("\n--- Isolation: evaluator.py touches nothing live ---")
# ---------------------------------------------------------------------------

src = (Path(__file__).parent.parent / "research" / "experiments" / "evaluator.py").read_text()
imports = re.findall(r"^\s*(?:from|import)\s+([.\w]+)", src, re.MULTILINE)
check("evaluator.py imports no engine module whatsoever",
      not any(m.split(".")[0] == "engine" for m in imports), str(imports))
check("evaluator.py contains no eval/exec/compile call",
      not re.search(r"\beval\s*\(|\bexec\s*\(|(?<!re\.)\bcompile\s*\(", src))
check("evaluator.py never opens memory/trades.jsonl or any live file",
      "trades.jsonl" not in src.split('"""', 2)[-1] if src.count('"""') >= 2 else True)


for s in (store, store_none_r, store_flat, store5, store6):
    s.close()

print(f"\n{'=' * 52}")
print(f"  {PASSED} passed, {FAILED} failed")
print(f"{'=' * 52}\n")
sys.exit(1 if FAILED else 0)
