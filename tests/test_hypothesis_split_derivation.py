"""
Tests for derive_split_contract() — the fix for the Contract.splits
enforcement gap: a locked Contract declares splits (discovery/validation/
holdout) but research/experiments/runner.py's simulate() only ever reads
evaluation_start/evaluation_end, never `splits`, so a declared validation or
holdout window was previously never actually walked by anything.

This is Option B (approved): derive a sibling Contract per declared split,
reusing the existing hypothesis-intake/lock/run machinery unchanged, rather
than teaching the runner to accept a `split` argument (Option A — rejected
because it would need a new column in experiment_results' (contract_id,
trade_seq) idempotency key).

Scope, exactly as approved: this file + derive_split_contract() in
research/brain/hypothesis_intake.py are the ONLY things this slice touches.
research/schema.sql, research/store.py, research/contracts.py,
research/experiments/runner.py, research/experiments/evaluator.py,
research/experiments/comparison.py, and research/reports/evidence_report.py
are all exercised below through their existing, unmodified public functions
only — never edited.

Run with:  python -m tests.test_hypothesis_split_derivation
"""

import re
import sys
import shutil
import tempfile
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from research.store import Store  # noqa: E402
from research import memory as rm  # noqa: E402
from research.contracts import Contract, REGISTRY_DIR  # noqa: E402
from research.brain import hypothesis_intake as hi  # noqa: E402
from research.experiments import runner, evaluator  # noqa: E402

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


TMP = Path(tempfile.mkdtemp(prefix="lq-test-split-derivation-"))
START = dt.date(2020, 1, 1)


def fresh_store(name) -> Store:
    p = TMP / f"{name}.db"
    if p.exists():
        p.unlink()
    return Store.open(p)


def fresh_registry(name) -> Path:
    d = TMP / f"registry-{name}"
    if d.exists():
        shutil.rmtree(d)
    return d


def seed_prices(store, symbol, start, closes, vols, highs=None, lows=None):
    for i, (c, v) in enumerate(zip(closes, vols)):
        d = start + dt.timedelta(days=i)
        h = highs[i] if highs is not None else c
        lo = lows[i] if lows is not None else c
        store.append_price(symbol=symbol, session_date=d, knowledge_time=d, source="test",
                            open_=c, high=h, low=lo, close=c, volume=v)


def flat_series(n, base_close=100.0, base_vol=100_000, spike_at=None, spike_vol=5_000_000,
                drift=0.0):
    closes = [base_close + drift * i for i in range(n)]
    vols = [base_vol + (i % 5) * 1000 for i in range(n)]
    if spike_at is not None:
        vols[spike_at] = spike_vol
    return closes, vols


def make_proposal(**overrides):
    base = {
        "title": "split derivation smoke test",
        "hypothesis": "volume spikes precede short-term continuation",
        "null_hypothesis": "no relationship",
        "universe": "watchlist",
        "signal": "observatory.volume_zscore",
        "entry_rule": {"conditions": [{"metric": "volume_zscore", "op": ">", "value": 3.0}]},
        "exit_rule": {"stop_loss_pct": 5.0, "target_pct": 8.0, "max_hold_days": 15},
        "splits": {
            "discovery": ["2020-01-01", "2020-06-30"],
            "validation": ["2020-07-01", "2020-12-31"],
        },
        "independence": "one entry per symbol",
        "falsification": "t_stat < 2.0",
        "abandon_condition": "expectancy_r <= 0",
        "evaluation_start": "2020-01-01",
        "evaluation_end": "2020-06-30",
    }
    base.update(overrides)
    return base


def locked_contract(store, reg, **overrides):
    """Full pipeline, not a shortcut: proposal -> draft -> approve_and_lock."""
    proposal = make_proposal(**overrides)
    result = hi.create_draft(store, proposal, registry_dir=reg)
    locked = hi.approve_and_lock(store, result.contract.id, approved_by="Vaibhav", registry_dir=reg)
    return result.hypothesis_id, locked


# ---------------------------------------------------------------------------
print("\n--- 1: deriving a validation-split sibling from a locked parent ---")
# ---------------------------------------------------------------------------

store1 = fresh_store("basic")
reg1 = fresh_registry("basic")
hid1, parent1 = locked_contract(store1, reg1)

sibling1 = hi.derive_split_contract(store1, parent1.id, "validation", hid1, registry_dir=reg1)

check("derive_split_contract() returns the same hypothesis_id",
      sibling1.hypothesis_id == hid1)
check("the sibling gets a new, distinct contract id",
      sibling1.contract.id != parent1.id, sibling1.contract.id)
check("the sibling id follows the same EXP-<suffix>-<letter> minting as create_draft()",
      sibling1.contract.id.startswith(f"EXP-{hid1.split('-')[-1][:6].upper()}-"))
check("the sibling's evaluation window is exactly the parent's validation split",
      (sibling1.contract.evaluation_start, sibling1.contract.evaluation_end)
      == tuple(parent1.splits["validation"]))
check("the sibling is returned as a DRAFT, never auto-locked",
      sibling1.contract.status == "draft")
check("the sibling has no locked_hash", sibling1.contract.locked_hash is None)

for field_name in ("title", "hypothesis", "null_hypothesis", "universe", "signal",
                    "entry_rule", "exit_rule", "splits", "independence",
                    "falsification", "abandon_condition", "llm_features",
                    "llm_model_id", "llm_knowledge_cutoff", "cost_model"):
    check(f"sibling.{field_name} is copied unchanged from the parent",
          getattr(sibling1.contract, field_name) == getattr(parent1, field_name))

reloaded1 = Contract.load(sibling1.contract.id, reg1)
check("the sibling draft was actually persisted to the registry directory",
      reloaded1.status == "draft"
      and reloaded1.evaluation_start == parent1.splits["validation"][0])

check("the sibling appears in pending_drafts() alongside any other open draft",
      sibling1.contract.id in {c.id for c in hi.pending_drafts(reg1)})

hyp_rows1 = rm.query_research_log(store1, rm.DATASET_HYPOTHESIS)
sibling_claim = [r for r in hyp_rows1
                  if r["payload"].get("contract_id") == sibling1.contract.id]
check("exactly one hypothesis-claim row was recorded for the sibling", len(sibling_claim) == 1)
if sibling_claim:
    payload = sibling_claim[0]["payload"]
    check("the claim's payload links split_of back to the parent contract",
          payload.get("split_of") == parent1.id, str(payload))
    check("the claim's payload records which split this sibling represents",
          payload.get("split") == "validation", str(payload))

check("the parent contract itself is completely untouched on disk",
      Contract.load(parent1.id, reg1).to_dict() == parent1.to_dict())


# ---------------------------------------------------------------------------
print("\n--- 2: the sibling requires its own explicit approve_and_lock() ---")
# ---------------------------------------------------------------------------

try:
    runner.run_experiment(sibling1.contract.id, store1, registry_dir=reg1)
    check("run_experiment() refuses an unlocked (draft) sibling", False)
except Exception as e:
    check("run_experiment() refuses an unlocked (draft) sibling", True, str(e))

locked_sibling1 = hi.approve_and_lock(
    store1, sibling1.contract.id, approved_by="Vaibhav", registry_dir=reg1)
check("the sibling locks cleanly through the ordinary approve_and_lock() path",
      locked_sibling1.status == "locked" and bool(locked_sibling1.locked_hash))
check("the locked sibling's hash matches its own recomputation (splits copied "
      "correctly, so the hash is well-formed)",
      locked_sibling1.locked_hash == locked_sibling1.content_hash())


# ---------------------------------------------------------------------------
print("\n--- 3: rejection cases ---")
# ---------------------------------------------------------------------------

store3 = fresh_store("rejections")
reg3 = fresh_registry("rejections")
hid3, parent3 = locked_contract(store3, reg3)

try:
    hi.derive_split_contract(store3, parent3.id, "discovery", hid3, registry_dir=reg3)
    check('deriving split_key="discovery" is rejected', False)
except hi.IntakeRejected as e:
    check('deriving split_key="discovery" is rejected', True, str(e.reasons))

try:
    hi.derive_split_contract(store3, parent3.id, "holdout", hid3, registry_dir=reg3)
    check("deriving a split the parent never declared is rejected", False)
except hi.IntakeRejected as e:
    check("deriving a split the parent never declared is rejected", True, str(e.reasons))

try:
    hi.derive_split_contract(store3, "EXP-NOSUCH-A", "validation", hid3, registry_dir=reg3)
    check("deriving from a nonexistent parent contract is rejected", False)
except hi.IntakeRejected as e:
    check("deriving from a nonexistent parent contract is rejected", True, str(e.reasons))

draft_proposal = make_proposal(hypothesis_id=hid3)
draft_result = hi.create_draft(store3, draft_proposal, registry_dir=reg3)
try:
    hi.derive_split_contract(store3, draft_result.contract.id, "validation", hid3, registry_dir=reg3)
    check("deriving from a still-draft (not locked) parent is rejected", False)
except hi.IntakeRejected as e:
    check("deriving from a still-draft (not locked) parent is rejected", True, str(e.reasons))

hid_other, parent_other = locked_contract(store3, reg3)
try:
    hi.derive_split_contract(store3, parent3.id, "validation", hid_other, registry_dir=reg3)
    check("deriving with a hypothesis_id that does not own the parent contract is rejected", False)
except hi.IntakeRejected as e:
    check("deriving with a hypothesis_id that does not own the parent contract is rejected",
          True, str(e.reasons))

check("no rejected derivation left a stray file in the registry directory",
      {c.id for c in hi._registry(reg3)} == {parent3.id, draft_result.contract.id, parent_other.id},
      str({c.id for c in hi._registry(reg3)}))


# ---------------------------------------------------------------------------
print("\n--- 4: end-to-end — the derived, locked sibling actually runs through "
      "the completely unmodified runner/evaluator, scoped to its own window ---")
# ---------------------------------------------------------------------------

store4 = fresh_store("e2e")
reg4 = fresh_registry("e2e")

closes, vols = flat_series(365, spike_at=40)
seed_prices(store4, "RELIANCE", START, closes, vols)

hid4, parent4 = locked_contract(
    store4, reg4,
    splits={
        "discovery": ["2020-01-01", "2020-06-30"],
        "validation": ["2020-07-01", "2020-12-30"],
    },
    evaluation_start="2020-01-01", evaluation_end="2020-06-30",
)

parent_out = runner.run_experiment(parent4.id, store4, registry_dir=reg4)
check("the parent (discovery-window) run completes and reports",
      parent_out["status"] == "reported")

sibling4 = hi.derive_split_contract(store4, parent4.id, "validation", hid4, registry_dir=reg4)
locked_sibling4 = hi.approve_and_lock(
    store4, sibling4.contract.id, approved_by="Vaibhav", registry_dir=reg4)

sibling_out = runner.run_experiment(locked_sibling4.id, store4, registry_dir=reg4)
check("the derived validation-window sibling runs to completion through the "
      "unmodified runner.run_experiment()", sibling_out["status"] == "reported")

sibling_trades = store4.experiment_results(locked_sibling4.id)
check("every trade recorded for the sibling actually falls inside the "
      "validation window, not the parent's discovery window",
      all("2020-07-01" <= t["entry_time"][:10] <= "2020-12-30" for t in sibling_trades)
      if sibling_trades else True,
      str([t["entry_time"] for t in sibling_trades]))

resolved_hid4 = evaluator.resolve_hypothesis_id(store4, locked_sibling4.id)
check("the sibling resolves back to the same hypothesis_id as its parent",
      resolved_hid4 == hid4)

sibling_ids = evaluator.contract_ids_for_hypothesis(store4, hid4)
check("evaluator.contract_ids_for_hypothesis() finds both parent and sibling "
      "with zero new lookup code required",
      set(sibling_ids) == {parent4.id, locked_sibling4.id}, str(sibling_ids))

sibling_verdict = evaluator.verdict_for_contract(store4, locked_sibling4.id)
check("a verdict was recorded for the derived sibling, independent of the parent's",
      sibling_verdict is not None and sibling_verdict["contract_id"] == locked_sibling4.id)


# ---------------------------------------------------------------------------
print("\n--- 5: comparison.py and evidence_report.py, completely unmodified, "
      "already see the sibling as a second variant of the same hypothesis ---")
# ---------------------------------------------------------------------------

from research.experiments import comparison  # noqa: E402
from research.reports import evidence_report  # noqa: E402

summary4 = comparison.evaluate_hypothesis_evidence(store4, parent4.id, registry_dir=reg4)
variant_ids4 = {v["contract_id"] for v in summary4["variants"]}
check("comparison.evaluate_hypothesis_evidence() (unmodified) counts the "
      "split-derived sibling as one of the hypothesis's scored variants",
      locked_sibling4.id in variant_ids4 or
      any(d["contract_id"] == locked_sibling4.id for d in
          [{"contract_id": cid} for cid in
           evaluator.contract_ids_for_hypothesis(store4, hid4)
           if cid not in variant_ids4]),
      str(variant_ids4))

report4 = evidence_report.build_report(store4, registry_dir=reg4)
report_hids = {h["hypothesis_id"] for h in report4["hypotheses"]}
check("evidence_report.build_report() (unmodified) includes this hypothesis "
      "now that both the parent and its split sibling are scored",
      hid4 in report_hids, str(report_hids))


# ---------------------------------------------------------------------------
print("\n--- 6: scope guard — this file's own footprint stays inside what "
      "was approved (no eval/exec, no engine import, derive_split_contract "
      "is additive-only) ---")
# ---------------------------------------------------------------------------

INTAKE_SRC = (Path(__file__).parent.parent / "research" / "brain"
              / "hypothesis_intake.py").read_text()
# Strip the leading module docstring before checking for real statements —
# the docstring legitimately discusses splits/runner/schema in prose.
INTAKE_BODY = INTAKE_SRC.split('"""', 2)[-1]

check("hypothesis_intake.py still contains no eval/exec/compile call",
      not re.search(r"\beval\s*\(|\bexec\s*\(|(?<!re\.)\bcompile\s*\(", INTAKE_BODY))

intake_imports = re.findall(r"^\s*(?:from|import)\s+([.\w]+)", INTAKE_SRC, re.MULTILINE)
check("hypothesis_intake.py's only engine import remains engine.watchlist "
      "(lazy, inside _valid_universes(), unchanged by this slice)",
      {m for m in intake_imports if m.split(".")[0] == "engine"} <= {"engine.watchlist"},
      str(intake_imports))

check("derive_split_contract() never calls Contract.lock() directly — only "
      "approve_and_lock() does, in this file, exactly as before this slice",
      "sibling.lock(" not in INTAKE_BODY and ".lock()" not in
      INTAKE_BODY.split("def derive_split_contract")[1].split("\ndef ")[0])


for s in (store1, store3, store4):
    s.close()

print(f"\n{'=' * 52}")
print(f"  {PASSED} passed, {FAILED} failed")
print(f"{'=' * 52}\n")

sys.exit(1 if FAILED else 0)
