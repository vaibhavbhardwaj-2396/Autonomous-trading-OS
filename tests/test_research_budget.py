"""
Tests for Phase 2 Slice O: Research Budget Governance.

Covers the new ceiling on how many Contracts may enter the LOCKED state
within a rolling period (research/brain/hypothesis_intake.py:
MAX_LOCKS_PER_PERIOD, LOCK_BUDGET_PERIOD_DAYS, locks_in_period(),
check_research_budget()), its enforcement inside approve_and_lock()
immediately before the sole Contract.lock() call, and the explicit,
separately-keyed human override path.

This is a RESEARCH-governance control. Nothing in this file exercises or
asserts anything about position sizing, risk-per-trade, drawdown, portfolio
limits, broker behavior, or engine/ — those are structurally untouched (see
section H) and this file does not pretend otherwise.

Run with:  python -m tests.test_research_budget
"""

import re
import sys
import json
import shutil
import tempfile
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from research.store import Store  # noqa: E402
from research import memory as rm  # noqa: E402
from research.contracts import Contract, REGISTRY_DIR  # noqa: E402
from research.brain import hypothesis_intake as hi  # noqa: E402

def _code_only(source: str) -> str:
    """Strip every triple-double-quoted docstring (module- and function-
    level alike), leaving only real code -- the same technique
    test_research_investigator.py's original fix established, needed here
    because hypothesis_intake.py is large enough to have many per-function
    docstrings, not just one leading module docstring a naive
    `.split('\"\"\"', 2)[-1]` would strip."""
    return re.sub(r'"""[\s\S]*?"""', "", source)


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


TMP = Path(tempfile.mkdtemp(prefix="lq-test-research-budget-"))


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


def make_proposal(**overrides):
    base = {
        "title": "budget guard smoke test",
        "hypothesis": "volume spikes precede short-term continuation",
        "null_hypothesis": "no relationship",
        "universe": "watchlist",
        "signal": "observatory.volume_zscore",
        "entry_rule": {"conditions": [{"metric": "volume_zscore", "op": ">", "value": 3.0}]},
        "exit_rule": {"stop_loss_pct": 5.0, "target_pct": 8.0, "max_hold_days": 15},
        "splits": {"discovery": ["2020-01-01", "2020-06-30"]},
        "independence": "one entry per symbol",
        "falsification": "t_stat < 2.0",
        "abandon_condition": "expectancy_r <= 0",
        "evaluation_start": "2020-01-01",
        "evaluation_end": "2020-06-30",
    }
    base.update(overrides)
    return base


def draft_contract(store, reg, **overrides):
    """create_draft() only -- never approved, never locked."""
    proposal = make_proposal(**overrides)
    return hi.create_draft(store, proposal, registry_dir=reg)


def lock_one(store, reg, *, approved_by="Vaibhav", **kwargs):
    """Full pipeline: proposal -> draft -> approve_and_lock(), with whatever
    budget kwargs the caller wants to exercise."""
    result = draft_contract(store, reg)
    return hi.approve_and_lock(store, result.contract.id, approved_by=approved_by,
                                registry_dir=reg, **kwargs)


def make_contract(cid, **overrides):
    """A Contract built directly (not via create_draft/approve_and_lock) --
    for seeding historical registry state at a specific status/locked_at,
    the same fixture technique test_research_similarity.py and
    test_research_areas.py already use."""
    fields = dict(
        id=cid, title=f"idea {cid}", hypothesis=f"claim behind {cid}",
        null_hypothesis="no effect", universe="watchlist", signal="observatory.volume_zscore",
        entry_rule=json.dumps({"conditions": [{"metric": "volume_zscore", "op": ">", "value": 3.0}]}),
        exit_rule=json.dumps({"stop_loss_pct": 2.0, "target_pct": 4.0}),
        splits={"discovery": ["2019-01-01", "2020-01-01"]},
        independence="clustered by symbol-day", falsification="t_stat < 2.0",
        abandon_condition="expectancy_r <= 0 on discovery",
        evaluation_start="2019-01-01", evaluation_end="2020-01-01",
    )
    fields.update(overrides)
    return Contract(**fields)


def seed_historical(reg, cid, *, status, locked_at):
    """Save a Contract directly at a given status/locked_at, bypassing
    approve_and_lock entirely -- exactly the kind of pre-existing registry
    state locks_in_period() must derive its count from."""
    c = make_contract(cid, status=status, locked_hash="deadbeefcafefeed", locked_at=locked_at)
    c.save(reg)
    return c


# ---------------------------------------------------------------------------
print("\n--- A: below budget -- lock succeeds ---")
# ---------------------------------------------------------------------------

store_a = fresh_store("a")
reg_a = fresh_registry("a")

c1 = lock_one(store_a, reg_a, max_locks=3, period_days=1)
c2 = lock_one(store_a, reg_a, max_locks=3, period_days=1)
check("A: 1st lock (0 already locked, limit 3) succeeds", c1.status == "locked")
check("A: 2nd lock (1 already locked, limit 3) succeeds", c2.status == "locked")
check("A: locks_in_period reports 2 after two below-budget locks",
      hi.locks_in_period(reg_a, period_days=1) == 2)


# ---------------------------------------------------------------------------
print("\n--- B: at budget -- the Nth lock (N == max_locks) is explicitly allowed ---")
# ---------------------------------------------------------------------------
# Policy under test, stated in check_research_budget()'s own docstring:
# within_budget <=> current_count < max_locks, checked BEFORE this lock is
# added. So with max_locks=3: locks are permitted while current_count is
# 0, 1, 2 (i.e. the 1st, 2nd, and 3rd locks all succeed), bringing the
# period's total to 3 -- AT the ceiling -- which is still an ALLOWED state
# to be in, not a violation.

store_b = fresh_store("b")
reg_b = fresh_registry("b")

b1 = lock_one(store_b, reg_b, max_locks=3, period_days=1)
b2 = lock_one(store_b, reg_b, max_locks=3, period_days=1)
b3 = lock_one(store_b, reg_b, max_locks=3, period_days=1)
check("B: the 3rd lock, with max_locks=3 (2 already locked beforehand), is allowed",
      b3.status == "locked")
check("B: locks_in_period now reports exactly 3, at the ceiling",
      hi.locks_in_period(reg_b, period_days=1) == 3)
within, count = hi.check_research_budget(reg_b, max_locks=3, period_days=1)
check("B: check_research_budget() reports NOT within budget once AT the ceiling",
      within is False and count == 3)


# ---------------------------------------------------------------------------
print("\n--- C: over budget -- the (N+1)th lock is rejected BEFORE locking ---")
# ---------------------------------------------------------------------------

store_c = fresh_store("c")
reg_c = fresh_registry("c")
for _ in range(3):
    lock_one(store_c, reg_c, max_locks=3, period_days=1)

draft_c = draft_contract(store_c, reg_c)
try:
    hi.approve_and_lock(store_c, draft_c.contract.id, approved_by="Vaibhav",
                         registry_dir=reg_c, max_locks=3, period_days=1)
    check("C: the 4th lock over a limit of 3 is rejected", False, "no exception raised")
except hi.IntakeRejected as e:
    check("C: the 4th lock over a limit of 3 is rejected via IntakeRejected", True)
    check("C: the rejection reason mentions the research budget",
          any("research budget" in r for r in e.reasons), str(e.reasons))

reloaded_c = Contract.load(draft_c.contract.id, reg_c)
check("C: the rejected contract is still status='draft'", reloaded_c.status == "draft")
check("C: locks_in_period is unchanged at 3 after the rejection",
      hi.locks_in_period(reg_c, period_days=1) == 3)


# ---------------------------------------------------------------------------
print("\n--- D: creating drafts does not consume budget ---")
# ---------------------------------------------------------------------------

store_d = fresh_store("d")
reg_d = fresh_registry("d")

for _ in range(10):
    draft_contract(store_d, reg_d)

check("D: 10 drafts exist but locks_in_period is still 0",
      hi.locks_in_period(reg_d, period_days=1) == 0)
within_d, count_d = hi.check_research_budget(reg_d, max_locks=1, period_days=1)
check("D: even with max_locks=1, a registry full of drafts is still within budget",
      within_d is True and count_d == 0)
# and a real lock still succeeds afterward, proving drafts never touched the gate
locked_d = lock_one(store_d, reg_d, max_locks=1, period_days=1)
check("D: a genuine lock still succeeds after 10 untouched drafts", locked_d.status == "locked")


# ---------------------------------------------------------------------------
print("\n--- E: historical locked Contracts (of every non-draft status) count correctly ---")
# ---------------------------------------------------------------------------

reg_e = fresh_registry("e")
now_e = dt.datetime(2026, 6, 15, 12, 0, 0)
recent = (now_e - dt.timedelta(days=1)).isoformat(timespec="seconds")

for status in ("locked", "running", "reported", "abandoned", "superseded"):
    seed_historical(reg_e, f"EXP-E-{status}", status=status, locked_at=recent)
seed_historical(reg_e, "EXP-E-draft", status="draft", locked_at=recent)  # drafts never have
                                                                          # locked_at in practice,
                                                                          # but even if one did,
                                                                          # status=='draft' alone
                                                                          # must exclude it

count_e = hi.locks_in_period(reg_e, now=now_e, period_days=7)
check("E: all five non-draft statuses count (locked/running/reported/abandoned/superseded)",
      count_e == 5, f"got {count_e}")

within_e, _ = hi.check_research_budget(reg_e, now=now_e, max_locks=5, period_days=7)
check("E: five pre-existing locks against a limit of 5 already exhausts the budget",
      within_e is False)


# ---------------------------------------------------------------------------
print("\n--- F: rolling-period boundary ---")
# ---------------------------------------------------------------------------

reg_f = fresh_registry("f")
now_f = dt.datetime(2026, 6, 15, 12, 0, 0)
period_days_f = 7
window_start_f = now_f - dt.timedelta(days=period_days_f)

inside = (now_f - dt.timedelta(days=6)).isoformat(timespec="seconds")       # clearly inside
on_boundary = window_start_f.isoformat(timespec="seconds")                  # exactly at window_start
outside = (now_f - dt.timedelta(days=8)).isoformat(timespec="seconds")      # clearly outside

seed_historical(reg_f, "EXP-F-inside", status="locked", locked_at=inside)
seed_historical(reg_f, "EXP-F-boundary", status="locked", locked_at=on_boundary)
seed_historical(reg_f, "EXP-F-outside", status="locked", locked_at=outside)

count_f = hi.locks_in_period(reg_f, now=now_f, period_days=period_days_f)
check("F: a lock 8 days before `now` (outside a 7-day window) does not count",
      count_f == 2, f"got {count_f}, expected 2 (inside + exactly-on-boundary)")

# remove the outside one and confirm the boundary contract alone still counts,
# isolating the inclusive-boundary behavior explicitly
(reg_f / "EXP-F-outside.json").unlink()
(reg_f / "EXP-F-inside.json").unlink()
count_f_boundary_only = hi.locks_in_period(reg_f, now=now_f, period_days=period_days_f)
check("F: a lock exactly AT window_start (now - period_days) counts (inclusive boundary, documented)",
      count_f_boundary_only == 1)


# ---------------------------------------------------------------------------
print("\n--- G: explicit override permits an over-budget lock ---")
# ---------------------------------------------------------------------------

store_g = fresh_store("g")
reg_g = fresh_registry("g")
for _ in range(2):
    lock_one(store_g, reg_g, max_locks=2, period_days=1)

draft_g = draft_contract(store_g, reg_g)
try:
    hi.approve_and_lock(store_g, draft_g.contract.id, approved_by="Vaibhav",
                         registry_dir=reg_g, max_locks=2, period_days=1)
    check("G: without an override, the over-budget lock is refused first", False,
          "no exception raised -- budget gate did not fire")
except hi.IntakeRejected:
    check("G: without an override, the over-budget lock is refused first", True)

overridden = hi.approve_and_lock(
    store_g, draft_g.contract.id, approved_by="Vaibhav",
    registry_dir=reg_g, max_locks=2, period_days=1,
    budget_override_by="Vaibhav", budget_override_reason="pre-approved research sprint")
check("G: with an explicit budget_override_by, the SAME over-budget lock now succeeds",
      overridden.status == "locked")
check("G: locks_in_period now reports 3 (the override genuinely added a lock)",
      hi.locks_in_period(reg_g, period_days=1) == 3)


# ---------------------------------------------------------------------------
print("\n--- H: the override creates an auditable research-memory note ---")
# ---------------------------------------------------------------------------

notes_g = rm.query_research_log(store_g, rm.DATASET_NOTE)
override_notes = [r for r in notes_g if r["payload"].get("contract_id") == draft_g.contract.id
                   and "override_by" in r["payload"]]
check("H: exactly one budget-override research note exists for the overridden contract",
      len(override_notes) == 1)
onote = override_notes[0]
check("H: the override note names who overrode it",
      onote["payload"]["override_by"] == "Vaibhav")
check("H: the override note records the pre-existing lock count and the configured ceiling",
      onote["payload"]["locks_in_period_before_this_one"] == 2
      and onote["payload"]["max_locks"] == 2)
check("H: the override note carries the human-supplied reason",
      onote["payload"]["override_reason"] == "pre-approved research sprint")
check("H: the note text itself explains the budget was exceeded, in prose",
      "RESEARCH BUDGET OVERRIDE" in onote["payload"]["note"]
      and "2 contract(s) already locked" in onote["payload"]["note"])
check("H: the override note's source identifies the budget-override code path specifically",
      onote["source"] == "hypothesis_intake.approve_and_lock.budget_override")


# ---------------------------------------------------------------------------
print("\n--- I: no automatic / silent override ---")
# ---------------------------------------------------------------------------

store_i = fresh_store("i")
reg_i = fresh_registry("i")
lock_one(store_i, reg_i, max_locks=1, period_days=1)
draft_i = draft_contract(store_i, reg_i)

# an ordinary approved_by, with no budget_override_by at all, must NOT bypass
# the gate -- approved_by is required for every lock, budget or not, so its
# mere presence must never be treated as budget authorization
try:
    hi.approve_and_lock(store_i, draft_i.contract.id, approved_by="Vaibhav",
                         registry_dir=reg_i, max_locks=1, period_days=1)
    check("I: an ordinary approved_by alone cannot silently satisfy the budget gate", False,
          "lock succeeded with no budget_override_by supplied")
except hi.IntakeRejected:
    check("I: an ordinary approved_by alone cannot silently satisfy the budget gate", True)

# an empty-string override is treated the same as no override at all
try:
    hi.approve_and_lock(store_i, draft_i.contract.id, approved_by="Vaibhav",
                         registry_dir=reg_i, max_locks=1, period_days=1,
                         budget_override_by="   ")
    check("I: a whitespace-only budget_override_by does not count as an override", False,
          "lock succeeded with a blank budget_override_by")
except hi.IntakeRejected:
    check("I: a whitespace-only budget_override_by does not count as an override", True)

# Research AI (investigator.py) has no code path to approve_and_lock at all,
# let alone to budget_override_by specifically -- re-confirmed here, scoped
# to this slice's own concern, alongside Slice I/N's broader isolation checks
INV_SRC = (Path(__file__).parent.parent / "research" / "brain" / "investigator.py").read_text()
INV_BODY = _code_only(INV_SRC)
check("I: investigator.py never CALLS approve_and_lock in its code "
      "(only ever discusses it, in prose/comments, as something it does not do)",
      "approve_and_lock(" not in INV_BODY)
check("I: investigator.py's CODE contains no reference to budget_override_by",
      "budget_override_by" not in INV_BODY)


# ---------------------------------------------------------------------------
print("\n--- J: Contract integrity on rejection ---")
# ---------------------------------------------------------------------------

store_j = fresh_store("j")
reg_j = fresh_registry("j")
lock_one(store_j, reg_j, max_locks=1, period_days=1)
draft_j = draft_contract(store_j, reg_j)

hash_before = draft_j.contract.content_hash()
status_before = draft_j.contract.status
file_before = (reg_j / f"{draft_j.contract.id}.json").read_text()

try:
    hi.approve_and_lock(store_j, draft_j.contract.id, approved_by="Vaibhav",
                         registry_dir=reg_j, max_locks=1, period_days=1)
except hi.IntakeRejected:
    pass

reloaded_j = Contract.load(draft_j.contract.id, reg_j)
check("J: status is unchanged after a rejected over-budget approval",
      reloaded_j.status == status_before == "draft")
check("J: content_hash is unchanged after a rejected over-budget approval",
      reloaded_j.content_hash() == hash_before)
file_after = (reg_j / f"{draft_j.contract.id}.json").read_text()
check("J: the on-disk registry file is byte-for-byte unchanged after rejection",
      file_after == file_before)


# ---------------------------------------------------------------------------
print("\n--- K: determinism ---")
# ---------------------------------------------------------------------------

reg_k = fresh_registry("k")
now_k = dt.datetime(2026, 3, 1, 9, 0, 0)
for i in range(3):
    seed_historical(reg_k, f"EXP-K-{i}",
                     status="locked", locked_at=(now_k - dt.timedelta(days=i)).isoformat(timespec="seconds"))

count_k1 = hi.locks_in_period(reg_k, now=now_k, period_days=7)
count_k2 = hi.locks_in_period(reg_k, now=now_k, period_days=7)
check("K: locks_in_period() is deterministic against an unchanged registry",
      count_k1 == count_k2 == 3)

result_k1 = hi.check_research_budget(reg_k, now=now_k, max_locks=5, period_days=7)
result_k2 = hi.check_research_budget(reg_k, now=now_k, max_locks=5, period_days=7)
check("K: check_research_budget() is deterministic against an unchanged registry",
      result_k1 == result_k2 == (True, 3))


# ---------------------------------------------------------------------------
print("\n--- H (isolation): the budget mechanism touches no engine/broker/live state ---")
# ---------------------------------------------------------------------------

HI_SRC = (Path(__file__).parent.parent / "research" / "brain" / "hypothesis_intake.py").read_text()
HI_BODY = _code_only(HI_SRC)

check("isolation: hypothesis_intake.py's only engine import remains engine.watchlist",
      not re.search(r"^\s*(import\s+engine\.(?!watchlist)|from\s+engine\.(?!watchlist))",
                     HI_SRC, re.MULTILINE))
check("isolation: hypothesis_intake.py imports no broker module",
      not re.search(r"^\s*(import\s+broker|from\s+broker)", HI_SRC, re.MULTILINE))
check("isolation: the budget code never calls any position-sizing/risk/drawdown function "
      "(no such function is imported or defined anywhere in this file to call — see the "
      "engine-import check above; the words appear only in this file's own prose "
      "explaining what the budget gate deliberately does NOT touch)",
      not re.search(r"\b(position_size|risk_per_trade|set_drawdown|portfolio_limit)\s*\(", HI_BODY))
check("isolation: hypothesis_intake.py contains no eval/exec/compile call (re.compile excepted)",
      not re.search(r"\beval\s*\(|\bexec\s*\(|(?<!re\.)\bcompile\s*\(", HI_BODY))
check("isolation: research/experiments/runner.py has no reference to the budget mechanism",
      "MAX_LOCKS_PER_PERIOD" not in
      (Path(__file__).parent.parent / "research" / "experiments" / "runner.py").read_text())


# ---------------------------------------------------------------------------
print("\n====================================================")
print(f"  {PASSED} passed, {FAILED} failed")
print("====================================================")
sys.exit(1 if FAILED else 0)
