"""
Tests for Phase 1 Slice E: research/experiments/comparison.py — deterministic
cross-experiment evidence comparison, no execution path, no live-data path.

Run with:  python -m tests.test_research_comparison
"""

import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from research.store import Store  # noqa: E402
from research import memory as rm  # noqa: E402
from research.contracts import Contract  # noqa: E402
from research.experiments import comparison as cmp  # noqa: E402

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


TMP = Path(tempfile.mkdtemp(prefix="lq-test-comparison-"))
REG = TMP / "registry"
REG.mkdir(parents=True, exist_ok=True)

_seq = [0]


def fresh_store(name) -> Store:
    p = TMP / f"{name}.db"
    if p.exists():
        p.unlink()
    return Store.open(p)


def make_contract(cid, *, entry_rule='{"conditions": []}', exit_rule='{"stop_loss_pct": 5}',
                  universe="watchlist", start="2020-01-01", end="2020-06-01",
                  locked_at=None, registry_dir=REG):
    _seq[0] += 1
    lock_time = locked_at or f"2020-01-01T00:00:{_seq[0]:02d}"
    c = Contract(
        id=cid, title=f"Title for {cid}", hypothesis="claim text",
        null_hypothesis="no effect", universe=universe, signal="sig",
        entry_rule=entry_rule, exit_rule=exit_rule,
        splits={"discovery": ["2020-01-01", "2020-06-01"]},
        independence="daily bars, not iid", falsification="t_stat below threshold",
        abandon_condition="no edge after N trades", evaluation_start=start,
        evaluation_end=end, status="reported", locked_at=lock_time,
        locked_hash="test-hash",
    )
    c.save(registry_dir)
    return c


def make_verdict(n_trades, net_pnl, avg_net_pnl, t_stat):
    return {"n_trades": n_trades, "win_rate": None, "gross_pnl": net_pnl, "net_pnl": net_pnl,
            "total_costs": 0.0, "avg_net_pnl": avg_net_pnl, "expectancy_r": None,
            "t_stat": t_stat}


def link_and_score(store, hypothesis_id, contract, verdict):
    rm.record_hypothesis_proposal(store, claim="claim", source="test",
                                  hypothesis_id=hypothesis_id,
                                  extra={"contract_id": contract.id})
    rm.record_experiment_verdict(store, contract_id=contract.id,
                                 hypothesis_id=hypothesis_id, verdict=verdict)


# ---------------------------------------------------------------------------
print("\n--- classify_variant: the three independent axes ---")
# ---------------------------------------------------------------------------

c0 = cmp.classify_variant("EXP-Z", make_verdict(0, 0.0, None, None))
check("zero trades: label is no_trades, not a fabricated direction",
      c0["label"] == "no_trades" and c0["direction"] is None)

c1 = cmp.classify_variant("EXP-Z", make_verdict(5, 1000.0, 200.0, 5.0))
check("below MIN_TRADES_FOR_SIGNIFICANCE is insufficient_sample regardless of "
      "how large t_stat looks", c1["insufficient_sample"] is True
      and c1["label"] == "insufficient_sample")

c2 = cmp.classify_variant("EXP-Z", make_verdict(20, 10000.0, 500.0, 1.0))
check("n>=10, positive, but |t_stat| below threshold: not statistically significant",
      c2["statistically_significant"] is False and c2["label"] == "positive_not_significant")

c3 = cmp.classify_variant("EXP-Z", make_verdict(20, 200.0, 10.0, 5.0))
check("n>=10, positive, big t_stat, but avg_net_pnl negligible vs the research "
      "notional: significant but explicitly flagged as economically negligible",
      c3["statistically_significant"] is True and c3["economically_meaningful"] is False
      and c3["label"] == "positive_significant_but_negligible")

c4 = cmp.classify_variant("EXP-Z", make_verdict(20, 10000.0, 500.0, 5.0))
check("n>=10, positive, significant AND economically meaningful: positive_significant",
      c4["statistically_significant"] and c4["economically_meaningful"]
      and c4["label"] == "positive_significant")


# ---------------------------------------------------------------------------
print("\n--- first experiment: no prior evidence ---")
# ---------------------------------------------------------------------------

store1 = fresh_store("first")
hid1 = rm.new_hypothesis_id()
cA = make_contract("EXP-AAAAAA-A")
link_and_score(store1, hid1, cA, make_verdict(15, 7500.0, 500.0, 3.0))

summary1 = cmp.evaluate_hypothesis_evidence(store1, cA.id, registry_dir=REG)
check("a single, positive/significant/meaningful result with nothing else "
      "tested yet is PROMISING", summary1["verdict"] == "PROMISING", summary1)
check("n_variants_scored is 1 for a first experiment", summary1["n_variants_scored"] == 1)
check("not flagged as a duplicate when nothing preceded it",
      summary1["is_duplicate"] is False and summary1["duplicate_of"] is None)


# ---------------------------------------------------------------------------
print("\n--- multiple variants: structure and sort order ---")
# ---------------------------------------------------------------------------

store2 = fresh_store("multi")
hid2 = rm.new_hypothesis_id()
cB1 = make_contract("EXP-BBBBBB-B", entry_rule='{"conditions": [{"metric":"close","op":">","value":1}]}')
cB2 = make_contract("EXP-BBBBBB-C", entry_rule='{"conditions": [{"metric":"close","op":">","value":2}]}')
link_and_score(store2, hid2, cB1, make_verdict(15, 5000.0, 333.0, 3.0))
link_and_score(store2, hid2, cB2, make_verdict(12, -1000.0, -83.0, -0.5))

summary2 = cmp.evaluate_hypothesis_evidence(store2, cB1.id, registry_dir=REG)
check("two distinct rule sets under one hypothesis both appear as variants",
      summary2["n_unique_rule_variants"] == 2 and len(summary2["variants"]) == 2)
check("variants are sorted by contract_id for a stable, deterministic order",
      [v["contract_id"] for v in summary2["variants"]] == sorted(
          [v["contract_id"] for v in summary2["variants"]]))


# ---------------------------------------------------------------------------
print("\n--- consistent positive evidence across variants -> PROMISING ---")
# ---------------------------------------------------------------------------

store3 = fresh_store("pos")
hid3 = rm.new_hypothesis_id()
variants3 = []
for i, letter in enumerate("DEF"):
    c = make_contract(f"EXP-CCCCCC-{letter}",
                      entry_rule=f'{{"conditions": [{{"metric":"close","op":">","value":{i}}}]}}')
    link_and_score(store3, hid3, c, make_verdict(15 + i, 5000.0, 400.0 + i, 3.0 + i * 0.1))
    variants3.append(c)

summary3 = cmp.evaluate_hypothesis_evidence(store3, variants3[0].id, registry_dir=REG)
check("all-positive, all-significant, all-meaningful evidence -> PROMISING",
      summary3["verdict"] == "PROMISING", summary3["rationale"])
check("positive_count reflects every variant", summary3["positive_count"] == 3
      and summary3["negative_count"] == 0)


# ---------------------------------------------------------------------------
print("\n--- consistent negative evidence across variants -> WEAK, not PROMISING ---")
# ---------------------------------------------------------------------------

store4 = fresh_store("neg")
hid4 = rm.new_hypothesis_id()
variants4 = []
for i, letter in enumerate("GH"):
    c = make_contract(f"EXP-DDDDDD-{letter}",
                      entry_rule=f'{{"conditions": [{{"metric":"close","op":"<","value":{i}}}]}}')
    link_and_score(store4, hid4, c, make_verdict(15 + i, -5000.0, -400.0 - i, -3.0))
    variants4.append(c)

summary4 = cmp.evaluate_hypothesis_evidence(store4, variants4[0].id, registry_dir=REG)
check("consistent, significant negative evidence is WEAK, never PROMISING",
      summary4["verdict"] == "WEAK", summary4["rationale"])
check("negative_count reflects every variant", summary4["negative_count"] == 2
      and summary4["positive_count"] == 0)


# ---------------------------------------------------------------------------
print("\n--- conflicting evidence -> CONTRADICTED ---")
# ---------------------------------------------------------------------------

store5 = fresh_store("conflict")
hid5 = rm.new_hypothesis_id()
cI = make_contract("EXP-EEEEEE-I", entry_rule='{"conditions": [{"metric":"close","op":">","value":1}]}')
cJ = make_contract("EXP-EEEEEE-J", entry_rule='{"conditions": [{"metric":"close","op":">","value":2}]}')
link_and_score(store5, hid5, cI, make_verdict(15, 6000.0, 400.0, 3.0))
link_and_score(store5, hid5, cJ, make_verdict(15, -6000.0, -400.0, -3.0))

summary5 = cmp.evaluate_hypothesis_evidence(store5, cI.id, registry_dir=REG)
check("one significant positive + one significant negative variant -> CONTRADICTED",
      summary5["verdict"] == "CONTRADICTED", summary5["rationale"])
check("CONTRADICTED is reported identically regardless of which variant asked",
      cmp.evaluate_hypothesis_evidence(store5, cJ.id, registry_dir=REG)["verdict"]
      == "CONTRADICTED")


# ---------------------------------------------------------------------------
print("\n--- insufficient sample size -> INCONCLUSIVE ---")
# ---------------------------------------------------------------------------

store6 = fresh_store("insufficient")
hid6 = rm.new_hypothesis_id()
cK = make_contract("EXP-FFFFFF-K")
link_and_score(store6, hid6, cK, make_verdict(4, 400.0, 100.0, 1.5))

summary6 = cmp.evaluate_hypothesis_evidence(store6, cK.id, registry_dir=REG)
check("a sample below MIN_TRADES_FOR_SIGNIFICANCE, alone, is INCONCLUSIVE "
      "rather than PROMISING/WEAK/CONTRADICTED", summary6["verdict"] == "INCONCLUSIVE",
      summary6["rationale"])


# ---------------------------------------------------------------------------
print("\n--- economically positive but statistically weak -> not PROMISING ---")
# ---------------------------------------------------------------------------

store7 = fresh_store("stat_weak")
hid7 = rm.new_hypothesis_id()
cL = make_contract("EXP-GGGGGG-L")
link_and_score(store7, hid7, cL, make_verdict(20, 10000.0, 500.0, 1.0))

summary7 = cmp.evaluate_hypothesis_evidence(store7, cL.id, registry_dir=REG)
check("a large, positive average P&L with a weak t-stat does not clear the "
      "PROMISING bar (statistical significance is a separate axis from "
      "economic size)", summary7["verdict"] != "PROMISING", summary7["rationale"])


# ---------------------------------------------------------------------------
print("\n--- statistically strong but economically negligible -> not PROMISING ---")
# ---------------------------------------------------------------------------

store8 = fresh_store("econ_weak")
hid8 = rm.new_hypothesis_id()
cM = make_contract("EXP-HHHHHH-M")
link_and_score(store8, hid8, cM, make_verdict(20, 200.0, 10.0, 6.0))

summary8 = cmp.evaluate_hypothesis_evidence(store8, cM.id, registry_dir=REG)
check("a huge t-stat on a practically irrelevant average P&L does not clear "
      "the PROMISING bar either", summary8["verdict"] != "PROMISING", summary8["rationale"])
check("the underlying variant is still correctly flagged significant, just "
      "not economically meaningful",
      summary8["variants"][0]["statistically_significant"] is True
      and summary8["variants"][0]["economically_meaningful"] is False)


# ---------------------------------------------------------------------------
print("\n--- duplicate / repeated experiment handling ---")
# ---------------------------------------------------------------------------

store9 = fresh_store("dup")
hid9 = rm.new_hypothesis_id()
same_rule = '{"conditions": [{"metric":"close","op":">","value":42}]}'
cN = make_contract("EXP-IIIIII-N", entry_rule=same_rule, locked_at="2020-01-01T00:00:01")
cO = make_contract("EXP-IIIIII-O", entry_rule=same_rule, locked_at="2020-01-01T00:00:02")
link_and_score(store9, hid9, cN, make_verdict(15, 5000.0, 400.0, 3.0))
link_and_score(store9, hid9, cO, make_verdict(15, 5000.0, 400.0, 3.0))

summary_orig = cmp.evaluate_hypothesis_evidence(store9, cN.id, registry_dir=REG)
summary_dup = cmp.evaluate_hypothesis_evidence(store9, cO.id, registry_dir=REG)
check("the earlier-locked contract of an identical rule set is NOT flagged redundant",
      summary_orig["is_duplicate"] is False and summary_orig["verdict"] != "REDUNDANT")
check("the later-locked contract with the identical rule set is REDUNDANT",
      summary_dup["verdict"] == "REDUNDANT" and summary_dup["duplicate_of"] == cN.id)
check("a duplicate does not inflate the unique-variant count",
      summary_orig["n_unique_rule_variants"] == 1 and summary_dup["n_unique_rule_variants"] == 1)
check("n_variants_scored still counts both underlying runs even though only "
      "one counts as independent evidence",
      summary_orig["n_variants_scored"] == 2)


# ---------------------------------------------------------------------------
print("\n--- deterministic comparison output ---")
# ---------------------------------------------------------------------------

store10 = fresh_store("determinism")
hid10 = rm.new_hypothesis_id()
cP = make_contract("EXP-JJJJJJ-P")
cQ = make_contract("EXP-JJJJJJ-Q", entry_rule='{"conditions": [{"metric":"close","op":">","value":9}]}')
link_and_score(store10, hid10, cP, make_verdict(15, 5000.0, 400.0, 3.0))
link_and_score(store10, hid10, cQ, make_verdict(15, 4000.0, 300.0, 2.5))

run_a = cmp.evaluate_hypothesis_evidence(store10, cP.id, registry_dir=REG)
run_b = cmp.evaluate_hypothesis_evidence(store10, cP.id, registry_dir=REG)
check("calling evaluate_hypothesis_evidence twice against an unchanged store "
      "produces an identical result", run_a == run_b)


# ---------------------------------------------------------------------------
print("\n--- correct hypothesis linkage ---")
# ---------------------------------------------------------------------------

store11 = fresh_store("linkage")
hid11 = rm.new_hypothesis_id()
cR = make_contract("EXP-KKKKKK-R")
link_and_score(store11, hid11, cR, make_verdict(15, 5000.0, 400.0, 3.0))

summary11 = cmp.evaluate_hypothesis_evidence(store11, cR.id, registry_dir=REG)
check("the evidence summary is linked to the correct hypothesis_id",
      summary11["hypothesis_id"] == hid11)
check("the evidence summary names the contract_id it was generated for",
      summary11["contract_id"] == cR.id)


# ---------------------------------------------------------------------------
print("\n--- unrelated hypotheses are never mixed ---")
# ---------------------------------------------------------------------------

store12 = fresh_store("unrelated")
hidX = rm.new_hypothesis_id()
hidY = rm.new_hypothesis_id()
cS = make_contract("EXP-LLLLLL-S")
cT = make_contract("EXP-MMMMMM-T")
link_and_score(store12, hidX, cS, make_verdict(15, 5000.0, 400.0, 3.0))
link_and_score(store12, hidY, cT, make_verdict(15, -5000.0, -400.0, -3.0))

summary_s = cmp.evaluate_hypothesis_evidence(store12, cS.id, registry_dir=REG)
summary_t = cmp.evaluate_hypothesis_evidence(store12, cT.id, registry_dir=REG)
check("hypothesis X's evidence summary only ever sees hypothesis X's contract",
      [v["contract_id"] for v in summary_s["variants"]] == [cS.id])
check("hypothesis Y's evidence summary only ever sees hypothesis Y's contract",
      [v["contract_id"] for v in summary_t["variants"]] == [cT.id])
check("a strongly negative unrelated hypothesis does not drag X's PROMISING "
      "verdict toward CONTRADICTED", summary_s["verdict"] == "PROMISING")


# ---------------------------------------------------------------------------
print("\n--- rejection cases ---")
# ---------------------------------------------------------------------------

store13 = fresh_store("rejects")
cU = make_contract("EXP-NNNNNN-U")  # never linked to a hypothesis, never scored

raised_unlinked = False
try:
    cmp.evaluate_hypothesis_evidence(store13, cU.id, registry_dir=REG)
except cmp.ComparisonRejected:
    raised_unlinked = True
check("a contract never drafted through hypothesis_intake is rejected, not "
      "silently given an empty evidence summary", raised_unlinked)

hid13 = rm.new_hypothesis_id()
cV = make_contract("EXP-OOOOOO-V")
rm.record_hypothesis_proposal(store13, claim="claim", source="test",
                              hypothesis_id=hid13, extra={"contract_id": cV.id})
# linked, but never scored (no verdict recorded)
raised_unscored = False
try:
    cmp.evaluate_hypothesis_evidence(store13, cV.id, registry_dir=REG)
except cmp.ComparisonRejected:
    raised_unscored = True
check("a contract linked to a hypothesis but never scored is rejected, not "
      "silently given an empty evidence summary", raised_unscored)


# ---------------------------------------------------------------------------
print("\n--- record_evidence: computes AND writes, in one call ---")
# ---------------------------------------------------------------------------

store14 = fresh_store("record")
hid14 = rm.new_hypothesis_id()
cW = make_contract("EXP-PPPPPP-W")
link_and_score(store14, hid14, cW, make_verdict(15, 5000.0, 400.0, 3.0))

returned = cmp.record_evidence(store14, cW.id, registry_dir=REG)
logged = rm.query_research_log(store14, rm.DATASET_EVIDENCE)
check("record_evidence writes exactly one evidence row", len(logged) == 1)
check("the returned summary matches what was actually written",
      logged[0]["payload"]["verdict"] == returned["verdict"]
      and logged[0]["payload"]["contract_id"] == cW.id)
check("the written evidence row is linked to the resolved hypothesis_id",
      logged[0]["payload"]["hypothesis_id"] == hid14)

before = cW.__class__.load(cW.id, REG)
cmp.record_evidence(store14, cW.id, registry_dir=REG)
after = cW.__class__.load(cW.id, REG)
check("comparing/recording evidence never mutates the contract's own registry "
      "file (status, notes, locked_hash all unchanged)",
      before.status == after.status and before.notes == after.notes
      and before.locked_hash == after.locked_hash)


# ---------------------------------------------------------------------------
print("\n--- Isolation: comparison.py touches nothing live, opens no "
      "alternative execution path ---")
# ---------------------------------------------------------------------------

src = (Path(__file__).parent.parent / "research" / "experiments" / "comparison.py").read_text()
imports = re.findall(r"^\s*(?:from|import)\s+([.\w]+)", src, re.MULTILINE)
check("comparison.py imports no top-level engine module",
      not any(m.split(".")[0] == "engine" for m in imports), str(imports))
check("comparison.py contains no eval/exec/compile call",
      not re.search(r"\beval\s*\(|\bexec\s*\(|(?<!re\.)\bcompile\s*\(", src))

# The module docstring AND an error message inside evaluate_hypothesis_
# evidence legitimately mention run_experiment/simulate/load_runnable_
# contract as prose (what this module does NOT do, and where to go run an
# unscored contract) — the same false-positive trap hit in Slice C/D's
# isolation tests, this time against a string literal rather than a
# docstring. The structurally sound check is stronger than a text search
# anyway: comparison.py's only import from runner.py is the notional
# constant, so run_experiment/load_runnable_contract/simulate are never
# names bound in this module's namespace — calling any of them as a bare
# name would raise NameError, not execute a trade.
import_line = next(l for l in src.splitlines() if l.strip().startswith("from .runner import"))
check("comparison.py imports ONLY the research-notional constant from "
      "runner.py — run_experiment/load_runnable_contract/simulate are never "
      "bound names in this module, so calling any of them is structurally "
      "impossible (NameError), not just avoided by convention",
      import_line.strip() == "from .runner import RESEARCH_POSITION_NOTIONAL",
      import_line)
check("comparison.py never calls Contract.save / .save( — it only reads "
      "contracts, never mutates the registry", ".save(" not in src)
check("comparison.py never opens memory/trades.jsonl or any live file",
      "trades.jsonl" not in src.split('"""', 2)[-1] if src.count('"""') >= 2 else True)


for s in (store1, store2, store3, store4, store5, store6, store7, store8,
          store9, store10, store11, store12, store13, store14):
    s.close()

print(f"\n{'=' * 52}")
print(f"  {PASSED} passed, {FAILED} failed")
print(f"{'=' * 52}\n")
sys.exit(1 if FAILED else 0)
