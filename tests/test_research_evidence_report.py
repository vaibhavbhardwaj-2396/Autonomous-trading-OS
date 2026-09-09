"""
Tests for Phase 1 Slice F: research/reports/evidence_report.py — a
deterministic, read-only Markdown report over accumulated research
evidence. No execution path, no live-data path, no write path into
anything the live trading system governs.

Run with:  python -m tests.test_research_evidence_report
"""

import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from research.store import Store  # noqa: E402
from research import memory as rm  # noqa: E402
from research.contracts import Contract  # noqa: E402
from research.experiments import comparison as cmp  # noqa: E402
from research.reports import evidence_report as er  # noqa: E402

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


TMP = Path(tempfile.mkdtemp(prefix="lq-test-evidence-report-"))
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
                  locked_at=None, status="reported", registry_dir=REG):
    _seq[0] += 1
    lock_time = locked_at or f"2020-01-01T00:00:{_seq[0]:02d}"
    c = Contract(
        id=cid, title=f"Title for {cid}", hypothesis="claim text",
        null_hypothesis="no effect", universe=universe, signal="sig",
        entry_rule=entry_rule, exit_rule=exit_rule,
        splits={"discovery": ["2020-01-01", "2020-06-01"]},
        independence="daily bars, not iid", falsification="t_stat below threshold",
        abandon_condition="no edge after N trades", evaluation_start=start,
        evaluation_end=end, status=status, locked_at=lock_time,
        locked_hash="test-hash",
    )
    c.save(registry_dir)
    return c


def make_verdict(n_trades, net_pnl, avg_net_pnl, t_stat):
    return {"n_trades": n_trades, "win_rate": None, "gross_pnl": net_pnl, "net_pnl": net_pnl,
            "total_costs": 0.0, "avg_net_pnl": avg_net_pnl, "expectancy_r": None,
            "t_stat": t_stat}


def link_and_score(store, hypothesis_id, contract, verdict, claim="claim text"):
    rm.record_hypothesis_proposal(store, claim=claim, source="test",
                                  hypothesis_id=hypothesis_id,
                                  extra={"contract_id": contract.id})
    rm.record_experiment_verdict(store, contract_id=contract.id,
                                 hypothesis_id=hypothesis_id, verdict=verdict)


# ---------------------------------------------------------------------------
print("\n--- empty research set produces a valid report ---")
# ---------------------------------------------------------------------------

store_empty = fresh_store("empty")
report_empty = er.build_report(store_empty, registry_dir=REG)
check("an empty store produces n_hypotheses_with_evidence == 0",
      report_empty["n_hypotheses_with_evidence"] == 0)
check("an empty store's hypotheses list is empty, not missing or None",
      report_empty["hypotheses"] == [])
check("every verdict bucket is present and zero, not absent",
      all(report_empty["counts_by_verdict"].get(v) == 0 for v in cmp.VALID_VERDICTS))

md_empty = er.to_markdown(report_empty)
check("the empty report still renders valid, non-empty Markdown",
      isinstance(md_empty, str) and "Research Evidence Report" in md_empty)
check("the empty report says plainly that there is nothing to show",
      "No hypotheses have recorded evidence yet" in md_empty)


# ---------------------------------------------------------------------------
print("\n--- one hypothesis, one variant: basic shape ---")
# ---------------------------------------------------------------------------

store1 = fresh_store("basic")
hid1 = rm.new_hypothesis_id()
cA = make_contract("EXP-AAAAAA-A")
link_and_score(store1, hid1, cA, make_verdict(15, 7500.0, 500.0, 3.0), claim="volume spikes precede moves")

report1 = er.build_report(store1, registry_dir=REG)
check("one scored hypothesis produces exactly one report entry",
      report1["n_hypotheses_with_evidence"] == 1 and len(report1["hypotheses"]) == 1)
h1 = report1["hypotheses"][0]
check("the report entry carries the original claim text",
      h1["claim"] == "volume spikes precede moves")
check("the report entry's verdict matches comparison.py's own verdict for this contract",
      h1["verdict"] == cmp.evaluate_hypothesis_evidence(store1, cA.id, registry_dir=REG)["verdict"])
check("the report entry records the contract's current status",
      h1["contract_statuses"][cA.id] == "reported")
check("the report entry records when the experiment was last recorded",
      h1["latest_experiment_at"] is not None)

md1 = er.render(store1, registry_dir=REG)
check("the rendered report contains the hypothesis id",
      hid1 in md1)
check("the rendered report contains the contract id in a table row",
      cA.id in md1)


# ---------------------------------------------------------------------------
print("\n--- correct hypothesis grouping: multiple hypotheses never mix ---")
# ---------------------------------------------------------------------------

store2 = fresh_store("grouping")
hidX = rm.new_hypothesis_id()
hidY = rm.new_hypothesis_id()
cX = make_contract("EXP-BBBBBB-X")
cY = make_contract("EXP-CCCCCC-Y")
link_and_score(store2, hidX, cX, make_verdict(15, 5000.0, 400.0, 3.0))
link_and_score(store2, hidY, cY, make_verdict(15, -5000.0, -400.0, -3.0))

report2 = er.build_report(store2, registry_dir=REG)
check("two independent hypotheses produce two report entries",
      report2["n_hypotheses_with_evidence"] == 2)
by_hid = {h["hypothesis_id"]: h for h in report2["hypotheses"]}
check("hypothesis X's entry only lists X's own contract",
      [v["contract_id"] for v in by_hid[hidX]["variants"]] == [cX.id])
check("hypothesis Y's entry only lists Y's own contract",
      [v["contract_id"] for v in by_hid[hidY]["variants"]] == [cY.id])
check("hypotheses in the report are sorted by hypothesis_id, deterministically",
      [h["hypothesis_id"] for h in report2["hypotheses"]] == sorted([hidX, hidY]))


# ---------------------------------------------------------------------------
print("\n--- correct variant counts, including a duplicate contract ---")
# ---------------------------------------------------------------------------

store3 = fresh_store("counts")
hid3 = rm.new_hypothesis_id()
rule_a = '{"conditions": [{"metric":"close","op":">","value":1}]}'
rule_b = '{"conditions": [{"metric":"close","op":">","value":2}]}'
cD1 = make_contract("EXP-DDDDDD-D", entry_rule=rule_a, locked_at="2020-01-01T00:00:01")
cD2 = make_contract("EXP-DDDDDD-E", entry_rule=rule_b, locked_at="2020-01-01T00:00:02")
cD3 = make_contract("EXP-DDDDDD-F", entry_rule=rule_a, locked_at="2020-01-01T00:00:03")  # dup of D1
link_and_score(store3, hid3, cD1, make_verdict(15, 5000.0, 400.0, 3.0))
link_and_score(store3, hid3, cD2, make_verdict(12, -1000.0, -83.0, -0.5))
link_and_score(store3, hid3, cD3, make_verdict(15, 5000.0, 400.0, 3.0))

report3 = er.build_report(store3, registry_dir=REG)
h3 = report3["hypotheses"][0]
check("three scored contracts, two unique rule sets: n_variants_scored counts all three",
      h3["n_variants_scored"] == 3)
check("n_unique_rule_variants correctly excludes the duplicate",
      h3["n_unique_rule_variants"] == 2 and len(h3["variants"]) == 2)
check("the duplicate contract is listed separately, not silently dropped",
      len(h3["duplicates"]) == 1 and h3["duplicates"][0]["contract_id"] == cD3.id
      and h3["duplicates"][0]["duplicate_of"] == cD1.id)

md3 = er.render(store3, registry_dir=REG)
check("the rendered report surfaces the duplicate relationship in text",
      f"{cD3.id} — duplicate of {cD1.id}" in md3)


# ---------------------------------------------------------------------------
print("\n--- archived/contradicted/inconclusive research remains visible ---")
# ---------------------------------------------------------------------------

store4 = fresh_store("visibility")
hid_promising = rm.new_hypothesis_id()
hid_contradicted = rm.new_hypothesis_id()
hid_inconclusive = rm.new_hypothesis_id()
hid_abandoned = rm.new_hypothesis_id()

cP = make_contract("EXP-EEEEEE-P")
link_and_score(store4, hid_promising, cP, make_verdict(15, 6000.0, 400.0, 3.0))

cQ1 = make_contract("EXP-FFFFFF-Q", entry_rule='{"conditions": [{"metric":"close","op":">","value":1}]}')
cQ2 = make_contract("EXP-FFFFFF-R", entry_rule='{"conditions": [{"metric":"close","op":">","value":2}]}')
link_and_score(store4, hid_contradicted, cQ1, make_verdict(15, 6000.0, 400.0, 3.0))
link_and_score(store4, hid_contradicted, cQ2, make_verdict(15, -6000.0, -400.0, -3.0))

cS = make_contract("EXP-GGGGGG-S")
link_and_score(store4, hid_inconclusive, cS, make_verdict(3, 100.0, 33.0, 0.5))

cT = make_contract("EXP-HHHHHH-T", status="abandoned")
link_and_score(store4, hid_abandoned, cT, make_verdict(15, -6000.0, -400.0, -3.0))

report4 = er.build_report(store4, registry_dir=REG)
verdicts_seen = {h["hypothesis_id"]: h["verdict"] for h in report4["hypotheses"]}
check("all four hypotheses appear in the report regardless of outcome",
      report4["n_hypotheses_with_evidence"] == 4)
check("a PROMISING hypothesis is visible", verdicts_seen[hid_promising] == "PROMISING")
check("a CONTRADICTED hypothesis is visible, not hidden as a failure",
      verdicts_seen[hid_contradicted] == "CONTRADICTED")
check("an INCONCLUSIVE hypothesis is visible", verdicts_seen[hid_inconclusive] == "INCONCLUSIVE")
check("a hypothesis whose only contract is abandoned is still visible in the report "
      "(status shown, not the hypothesis itself dropped)",
      hid_abandoned in verdicts_seen)
check("counts_by_verdict tallies every bucket correctly",
      report4["counts_by_verdict"]["PROMISING"] == 1
      and report4["counts_by_verdict"]["CONTRADICTED"] == 1
      and report4["counts_by_verdict"]["INCONCLUSIVE"] == 1)

md4 = er.render(store4, registry_dir=REG)
for hid in (hid_promising, hid_contradicted, hid_inconclusive, hid_abandoned):
    check(f"rendered report contains section for {hid}", hid in md4)


# ---------------------------------------------------------------------------
print("\n--- deterministic output ---")
# ---------------------------------------------------------------------------

store5 = fresh_store("determinism")
hid5 = rm.new_hypothesis_id()
cU = make_contract("EXP-IIIIII-U")
cV = make_contract("EXP-IIIIII-V", entry_rule='{"conditions": [{"metric":"close","op":">","value":9}]}')
link_and_score(store5, hid5, cU, make_verdict(15, 5000.0, 400.0, 3.0))
link_and_score(store5, hid5, cV, make_verdict(15, 4000.0, 300.0, 2.5))

md_a = er.render(store5, registry_dir=REG)
md_b = er.render(store5, registry_dir=REG)
check("rendering the same store twice produces byte-identical Markdown",
      md_a == md_b)

report_a = er.build_report(store5, registry_dir=REG)
report_b = er.build_report(store5, registry_dir=REG)
check("build_report is also dict-equal across repeated calls", report_a == report_b)


# ---------------------------------------------------------------------------
print("\n--- report does not mutate contracts or strategy state ---")
# ---------------------------------------------------------------------------

store6 = fresh_store("no_mutation")
hid6 = rm.new_hypothesis_id()
cW = make_contract("EXP-JJJJJJ-W")
link_and_score(store6, hid6, cW, make_verdict(15, 5000.0, 400.0, 3.0))

before = Contract.load(cW.id, REG)
er.render(store6, registry_dir=REG)
after = Contract.load(cW.id, REG)
check("rendering a report never mutates a contract's status, notes, or hash",
      before.status == after.status and before.notes == after.notes
      and before.locked_hash == after.locked_hash)

strategy_path = Path(__file__).parent.parent / "memory" / "strategy.md"
strategy_before = strategy_path.read_text() if strategy_path.exists() else None
out_path = TMP / "reports" / "evidence_report.md"
written = er.write_report(store6, out_path, registry_dir=REG)
strategy_after = strategy_path.read_text() if strategy_path.exists() else None
check("write_report writes only the path it was given",
      written == out_path and out_path.exists())
check("the live memory/strategy.md file is completely untouched by generating a report",
      strategy_before == strategy_after)


# ---------------------------------------------------------------------------
print("\n--- Isolation: no live reads, no live writes, no execution path ---")
# ---------------------------------------------------------------------------

src = (Path(__file__).parent.parent / "research" / "reports" / "evidence_report.py").read_text()
imports = re.findall(r"^\s*(?:from|import)\s+([.\w]+)", src, re.MULTILINE)

check("evidence_report.py imports no engine module of any kind (not even "
      "engine.costs/engine.watchlist, which Slice D/E needed and this "
      "module does not)", not any(m.split(".")[0] == "engine" for m in imports),
      str(imports))

FORBIDDEN = {"guardrails", "execute", "journal", "broker", "broker_kite", "broker_indstocks"}
check("no forbidden engine submodule (journal, execute, guardrails, "
      "broker/broker_kite/broker_indstocks) is imported",
      not any(any(f in m for f in FORBIDDEN) for m in imports), str(imports))

check("evidence_report.py contains no eval/exec/compile call",
      not re.search(r"\beval\s*\(|\bexec\s*\(|(?<!re\.)\bcompile\s*\(", src))

# The module docstring legitimately discusses Contract.save()/strategy.md as
# things this file must NOT do/touch, and to_markdown()'s own rendered
# disclaimer text legitimately names "memory/strategy.md" as user-facing
# copy explaining the governance boundary — the same false-positive trap
# hit repeatedly in Slice C/D/E's isolation tests. Check the actual code,
# not prose or rendered-copy string literals, for real file operations.
body = src.split('"""', 2)[-1] if src.count('"""') >= 2 else src
check("evidence_report.py never calls Contract.save() / .save( in its "
      "actual code — every Contract.load() here is read-only",
      ".save(" not in body)

strategy_lines = [l for l in body.splitlines() if "strategy.md" in l]
check("every mention of strategy.md in the actual code is display copy in "
      "the rendered disclaimer text, never part of a file open/write call",
      all("open(" not in l and "write_text(" not in l and "Path(" not in l
          for l in strategy_lines),
      strategy_lines)
check("evidence_report.py never opens memory/trades.jsonl or any live file",
      "trades.jsonl" not in body)
check("evidence_report.py's actual code never writes to any path outside "
      "the one explicitly passed to write_report (no hardcoded file path "
      "anywhere in the module)",
      not re.search(r"open\(|write_text\(", body.replace("path.write_text(", "")))


for s in (store_empty, store1, store2, store3, store4, store5, store6):
    s.close()

print(f"\n{'=' * 52}")
print(f"  {PASSED} passed, {FAILED} failed")
print(f"{'=' * 52}\n")
sys.exit(1 if FAILED else 0)
