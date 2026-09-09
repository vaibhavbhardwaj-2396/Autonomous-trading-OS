"""
Tests for Phase 2 Slice S: Research Priority Engine v0 (research/brain/priority.py).

The priority engine is pure ranking metadata — it decides RESEARCH
EXECUTION ORDER among already-locked, already-approved Contracts, never
trading priority, never a portfolio decision, and it touches no market
data, no AI, and no live state. This file tests the ranking rule itself
(confirmation, research-area balance, age, tie-break), its defensiveness
against missing/odd inputs, its actual wiring into
research.brain.scheduler.py, and its purity/isolation — it does not re-test
comparison.py's own PROMISING/WEAK/etc. verdict logic (that's
tests/test_research_comparison.py's job) or research_areas.py's own tagging
semantics (tests/test_research_areas.py's job).

Run with:  python -m tests.test_research_priority
"""

import re
import sys
import json
import shutil
import hashlib
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from research.store import Store  # noqa: E402
from research import memory as rm  # noqa: E402
from research.contracts import Contract, REGISTRY_DIR, registry as _registry  # noqa: E402
from research.brain import research_areas as ra  # noqa: E402
from research.brain import scheduler as sch  # noqa: E402
from research.brain import priority as prio  # noqa: E402

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


def _code_only(source: str) -> str:
    """Strip every triple-double-quoted docstring, leaving only real code —
    the convention established in test_research_investigator.py and reused
    by every isolation check since."""
    return re.sub(r'"""[\s\S]*?"""', "", source)


TMP = Path(tempfile.mkdtemp(prefix="lq-test-priority-"))


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


def make_locked_contract(cid, *, locked_at=None, status="locked", registry_dir, **overrides):
    """A locked Contract, constructed directly — the same make_contract-
    then-lock()-then-mutate-status pattern test_research_digest.py's own
    'previously tested' section and test_research_scheduler.py already
    use. No hypothesis-claim row is written by this helper on its own —
    callers that need one (for area tagging or split metadata) write it
    explicitly via research.memory, exactly as hypothesis_intake.
    create_draft()/derive_split_contract() would."""
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
    c = Contract(**fields)
    c.lock()
    if locked_at is not None:
        c.locked_at = locked_at
    c.status = status
    c.save(registry_dir)
    return c


def make_verdict(n_trades, net_pnl, avg_net_pnl, t_stat):
    """Same fabricated-verdict shape test_research_comparison.py's own
    make_verdict() uses — a verdict is just a dict evaluator.
    compute_verdict() would have produced; fabricating one directly (rather
    than running a real simulation) is the established, fast way this
    repository's own test suite proves evidence-classification behavior."""
    return {"n_trades": n_trades, "win_rate": None, "gross_pnl": net_pnl, "net_pnl": net_pnl,
            "total_costs": 0.0, "avg_net_pnl": avg_net_pnl, "expectancy_r": None,
            "t_stat": t_stat}


def link_contract_to_hypothesis(store, hypothesis_id, contract_id, *, extra=None):
    """The exact same DATASET_HYPOTHESIS row create_draft()/
    derive_split_contract() write — hypothesis_id -> contract_id, plus
    whatever split metadata `extra` carries."""
    rm.record_hypothesis_proposal(
        store, claim="claim", source="test", hypothesis_id=hypothesis_id,
        extra={"contract_id": contract_id, **(extra or {})})


def score_contract(store, hypothesis_id, contract_id, verdict):
    rm.record_experiment_verdict(
        store, contract_id=contract_id, hypothesis_id=hypothesis_id, verdict=verdict)


def make_promising_parent(store, reg, *, cid="EXP-PARENT", hid=None):
    """A contract that is already REPORTED with a fabricated PROMISING
    verdict — the exact scenario is_confirmation_experiment() is supposed
    to recognize as 'a previously promising result'. Reuses
    comparison.evaluate_hypothesis_evidence() itself (via
    is_confirmation_experiment) to classify it — this helper does not
    assert PROMISING itself; the first check below does, so a change to
    comparison.py's own thresholds would show up here honestly rather than
    being silently assumed."""
    hid = hid or rm.new_hypothesis_id()
    make_locked_contract(cid, status="reported", registry_dir=reg,
                          locked_at="2024-01-01T00:00:00")
    link_contract_to_hypothesis(store, hid, cid)
    score_contract(store, hid, cid, make_verdict(20, 10_000.0, 500.0, 5.0))
    return hid, cid


def _registry_fingerprint(reg_dir: Path) -> str:
    h = hashlib.sha256()
    if not reg_dir.exists():
        return h.hexdigest()
    for f in sorted(reg_dir.glob("**/*")):
        if f.is_file():
            h.update(f.name.encode())
            h.update(f.read_bytes())
    return h.hexdigest()


# ---------------------------------------------------------------------------
print("\n--- A: deterministic ordering ---")
# ---------------------------------------------------------------------------

store_a = fresh_store("a")
reg_a = fresh_registry("a")
for i in range(4):
    make_locked_contract(f"EXP-A-{i}", locked_at=f"2024-01-0{i+1}T00:00:00", registry_dir=reg_a)

contracts_a = _registry(reg_a)
order1 = [c.id for c in prio.rank_experiments(store_a, contracts_a, registry_dir=reg_a)]
order2 = [c.id for c in prio.rank_experiments(store_a, contracts_a, registry_dir=reg_a)]
check("A: two calls with identical inputs return an identical ranking",
      order1 == order2)
check("A: every contract appears exactly once", sorted(order1) == sorted(c.id for c in contracts_a))


# ---------------------------------------------------------------------------
print("\n--- B: validation/confirmation priority ---")
# ---------------------------------------------------------------------------

store_b = fresh_store("b")
reg_b = fresh_registry("b")

hid_b, parent_b = make_promising_parent(store_b, reg_b)
check("B: the fabricated parent is genuinely classified PROMISING by the "
      "REAL, unmodified comparison.evaluate_hypothesis_evidence() — not "
      "assumed", prio.is_confirmation_experiment.__module__ == "research.brain.priority")

# the confirmation sibling: same hypothesis, split_of/split metadata,
# eligible (still locked, not yet run)
make_locked_contract("EXP-B-CONFIRM", locked_at="2024-02-01T00:00:00", registry_dir=reg_b)
link_contract_to_hypothesis(store_b, hid_b, "EXP-B-CONFIRM",
                             extra={"split_of": parent_b, "split": "validation"})

# an ordinary, unrelated, fresh exploratory contract — same locked_at, so
# nothing but the confirmation dimension can explain any ordering difference
make_locked_contract("EXP-B-PLAIN", locked_at="2024-02-01T00:00:00", registry_dir=reg_b)
link_contract_to_hypothesis(store_b, rm.new_hypothesis_id(), "EXP-B-PLAIN")

check("B: is_confirmation_experiment() recognizes the validation sibling "
      "of a PROMISING parent", prio.is_confirmation_experiment(store_b, "EXP-B-CONFIRM",
                                                                registry_dir=reg_b) is True)
check("B: an ordinary contract is never treated as a confirmation experiment",
      prio.is_confirmation_experiment(store_b, "EXP-B-PLAIN", registry_dir=reg_b) is False)

eligible_b = [c for c in _registry(reg_b) if c.status == "locked"]  # excludes the reported parent
ranked_b = [c.id for c in prio.rank_experiments(store_b, eligible_b, registry_dir=reg_b)]
check("B: a valid confirmation experiment outranks an otherwise-comparable "
      "ordinary exploratory experiment", ranked_b == ["EXP-B-CONFIRM", "EXP-B-PLAIN"], ranked_b)

# B2 — a validation sibling of a parent that is NOT promising (never
# scored) must NOT receive confirmation priority.
store_b2 = fresh_store("b2")
reg_b2 = fresh_registry("b2")
make_locked_contract("EXP-B2-PARENT", status="locked", registry_dir=reg_b2)  # never run/scored
hid_b2 = rm.new_hypothesis_id()
link_contract_to_hypothesis(store_b2, hid_b2, "EXP-B2-PARENT")
make_locked_contract("EXP-B2-CHILD", registry_dir=reg_b2)
link_contract_to_hypothesis(store_b2, hid_b2, "EXP-B2-CHILD",
                             extra={"split_of": "EXP-B2-PARENT", "split": "validation"})
check("B2: a validation sibling of a parent with NO recorded verdict yet "
      "is not (yet) a confirmation experiment",
      prio.is_confirmation_experiment(store_b2, "EXP-B2-CHILD", registry_dir=reg_b2) is False)

# B3 — a validation sibling of a parent that was scored but WEAK (not
# PROMISING) must also not receive confirmation priority.
store_b3 = fresh_store("b3")
reg_b3 = fresh_registry("b3")
make_locked_contract("EXP-B3-PARENT", status="reported", registry_dir=reg_b3)
hid_b3 = rm.new_hypothesis_id()
link_contract_to_hypothesis(store_b3, hid_b3, "EXP-B3-PARENT")
score_contract(store_b3, hid_b3, "EXP-B3-PARENT", make_verdict(20, -500.0, -25.0, 3.0))  # WEAK/negative
make_locked_contract("EXP-B3-CHILD", registry_dir=reg_b3)
link_contract_to_hypothesis(store_b3, hid_b3, "EXP-B3-CHILD",
                             extra={"split_of": "EXP-B3-PARENT", "split": "validation"})
check("B3: a validation sibling of a non-PROMISING (e.g. WEAK/negative) "
      "parent is not a confirmation experiment",
      prio.is_confirmation_experiment(store_b3, "EXP-B3-CHILD", registry_dir=reg_b3) is False)


# ---------------------------------------------------------------------------
print("\n--- C: research-area balance ---")
# ---------------------------------------------------------------------------

store_c = fresh_store("c")
reg_c = fresh_registry("c")

# Area 'momentum': 3 pending, all older than area 'volatility's one pending
# contract — a pure age-only rule would run all 3 momentum contracts before
# volatility ever gets a turn. The round-robin rule must not do that.
momentum_ids = []
for i in range(3):
    cid = f"EXP-C-MOM-{i}"
    make_locked_contract(cid, locked_at=f"2024-01-0{i+1}T00:00:00", registry_dir=reg_c)
    hid = rm.new_hypothesis_id()
    link_contract_to_hypothesis(store_c, hid, cid)
    ra.tag_hypothesis(store_c, hypothesis_id=hid, research_area="momentum", source="test")
    momentum_ids.append(cid)

make_locked_contract("EXP-C-VOL-0", locked_at="2024-01-10T00:00:00", registry_dir=reg_c)
hid_vol = rm.new_hypothesis_id()
link_contract_to_hypothesis(store_c, hid_vol, "EXP-C-VOL-0")
ra.tag_hypothesis(store_c, hypothesis_id=hid_vol, research_area="volatility", source="test")

ranked_c = [c.id for c in prio.rank_experiments(store_c, _registry(reg_c), registry_dir=reg_c)]
check("C: the single, newer, underrepresented-area contract still ranks "
      "ahead of the heavily-represented area's 2nd and 3rd contracts "
      "(round-robin, not pure age)",
      ranked_c.index("EXP-C-VOL-0") < ranked_c.index(momentum_ids[1])
      and ranked_c.index("EXP-C-VOL-0") < ranked_c.index(momentum_ids[2]))
check("C: within the heavily-represented area, the OLDEST momentum "
      "contract still comes first among momentum's own members",
      ranked_c.index(momentum_ids[0]) < ranked_c.index(momentum_ids[1])
      < ranked_c.index(momentum_ids[2]))
check("C: a bounded selection of 2 (Area A: 100-style backlog vs Area B: "
      "2-style backlog, scaled down here) picks one from EACH area rather "
      "than exhausting the larger area first",
      set(ranked_c[:2]) == {momentum_ids[0], "EXP-C-VOL-0"})


# ---------------------------------------------------------------------------
print("\n--- D: age (all else equal) ---")
# ---------------------------------------------------------------------------

store_d = fresh_store("d")
reg_d = fresh_registry("d")
make_locked_contract("EXP-D-NEW", locked_at="2024-06-01T00:00:00", registry_dir=reg_d)
make_locked_contract("EXP-D-OLD", locked_at="2024-01-01T00:00:00", registry_dir=reg_d)

ranked_d = [c.id for c in prio.rank_experiments(store_d, _registry(reg_d), registry_dir=reg_d)]
check("D: with confirmation and area equal (both untagged, neither a "
      "confirmation), the older locked_at ranks first",
      ranked_d == ["EXP-D-OLD", "EXP-D-NEW"])


# ---------------------------------------------------------------------------
print("\n--- E: stable contract_id tie-break ---")
# ---------------------------------------------------------------------------

store_e = fresh_store("e")
reg_e = fresh_registry("e")
make_locked_contract("EXP-E-B", locked_at="2024-01-01T00:00:00", registry_dir=reg_e)
make_locked_contract("EXP-E-A", locked_at="2024-01-01T00:00:00", registry_dir=reg_e)  # exact tie

ranked_e = [c.id for c in prio.rank_experiments(store_e, _registry(reg_e), registry_dir=reg_e)]
check("E: an exact tie on every other dimension breaks on contract_id ascending",
      ranked_e == ["EXP-E-A", "EXP-E-B"])


# ---------------------------------------------------------------------------
print("\n--- F: missing area does not fail ---")
# ---------------------------------------------------------------------------

store_f = fresh_store("f")
reg_f = fresh_registry("f")
make_locked_contract("EXP-F-UNTAGGED", registry_dir=reg_f)  # no hypothesis link at all
hid_f = rm.new_hypothesis_id()
make_locked_contract("EXP-F-LINKED-UNTAGGED", registry_dir=reg_f)
link_contract_to_hypothesis(store_f, hid_f, "EXP-F-LINKED-UNTAGGED")  # linked, but never tagged

check("F: research_area_for() falls back to UNASSIGNED_AREA for a contract "
      "with no resolvable hypothesis at all, without raising",
      prio.research_area_for(store_f, "EXP-F-UNTAGGED") == prio.UNASSIGNED_AREA)
check("F: research_area_for() falls back to UNASSIGNED_AREA for a linked "
      "but never-tagged hypothesis, without raising",
      prio.research_area_for(store_f, "EXP-F-LINKED-UNTAGGED") == prio.UNASSIGNED_AREA)

try:
    ranked_f = prio.rank_experiments(store_f, _registry(reg_f), registry_dir=reg_f)
    check("F: ranking a registry with no research-area tags at all does not raise",
          {c.id for c in ranked_f} == {"EXP-F-UNTAGGED", "EXP-F-LINKED-UNTAGGED"})
except Exception as e:
    check("F: ranking a registry with no research-area tags at all does not raise",
          False, f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
print("\n--- G: missing/odd timestamps are handled defensively ---")
# ---------------------------------------------------------------------------

store_g = fresh_store("g")
reg_g = fresh_registry("g")
c_normal = make_locked_contract("EXP-G-NORMAL", locked_at="2024-01-01T00:00:00", registry_dir=reg_g)
c_missing = make_locked_contract("EXP-G-MISSING", registry_dir=reg_g)
c_missing.locked_at = None  # force the defensive branch directly (should not occur via .lock())
c_odd = make_locked_contract("EXP-G-ODD", registry_dir=reg_g)
c_odd.locked_at = "not-a-real-timestamp"

try:
    ranked_g = prio.rank_experiments(store_g, [c_normal, c_missing, c_odd], registry_dir=reg_g)
    ok = True
except Exception as e:
    ok = False
    ranked_g = []
check("G: a contract with locked_at=None does not raise; ranking still "
      "completes", ok)
check("G: a missing locked_at sorts AFTER every contract with a real "
      "timestamp, never silently first",
      [c.id for c in ranked_g].index("EXP-G-MISSING")
      > [c.id for c in ranked_g].index("EXP-G-NORMAL"))
check("G: a garbage (non-ISO) locked_at string is treated as an opaque, "
      "still-comparable string rather than raising a parse error",
      "EXP-G-ODD" in [c.id for c in ranked_g])


# ---------------------------------------------------------------------------
print("\n--- H: scheduler integration ---")
# ---------------------------------------------------------------------------

store_h = fresh_store("h")
reg_h = fresh_registry("h")
# newer-but-underrepresented-area vs older-but-heavily-represented-area,
# same setup shape as section C, to prove the SCHEDULER (not just the
# priority module in isolation) now follows priority rather than the old
# age-only ordering.
mom_ids_h = []
for i in range(3):
    cid = f"EXP-H-MOM-{i}"
    make_locked_contract(cid, locked_at=f"2024-01-0{i+1}T00:00:00", registry_dir=reg_h)
    hid = rm.new_hypothesis_id()
    link_contract_to_hypothesis(store_h, hid, cid)
    ra.tag_hypothesis(store_h, hypothesis_id=hid, research_area="momentum", source="test")
    mom_ids_h.append(cid)
make_locked_contract("EXP-H-VOL-0", locked_at="2024-01-10T00:00:00", registry_dir=reg_h)
hid_h = rm.new_hypothesis_id()
link_contract_to_hypothesis(store_h, hid_h, "EXP-H-VOL-0")
ra.tag_hypothesis(store_h, hypothesis_id=hid_h, research_area="volatility", source="test")

sched_eligible_h = [c.id for c in sch.eligible_contracts(store_h, registry_dir=reg_h)]
direct_priority_h = [c.id for c in prio.rank_experiments(
    store_h, sch.runner.runnable_contracts(reg_h), registry_dir=reg_h)]
check("H: scheduler.eligible_contracts() matches priority.rank_experiments() "
      "exactly, byte for byte (the scheduler delegates, it does not "
      "reimplement)", sched_eligible_h == direct_priority_h)
check("H: the scheduler's own ordering is the round-robin priority order, "
      "not the old age-only order (the underrepresented area is not "
      "starved)", sched_eligible_h.index("EXP-H-VOL-0") < sched_eligible_h.index(mom_ids_h[1]))

result_h = sch.run_scheduler(store_h, registry_dir=reg_h, max_experiments=2)
check("H: a bounded scheduler run with max_experiments=2 actually selects "
      "one contract from EACH area, not two from the larger one",
      {c for c in result_h.selected_contract_ids} == {mom_ids_h[0], "EXP-H-VOL-0"})


# ---------------------------------------------------------------------------
print("\n--- I: no execution duplication ---")
# ---------------------------------------------------------------------------

PRIO_SRC = Path("research/brain/priority.py").read_text()
PRIO_BODY = _code_only(PRIO_SRC)

check("I: priority.py never calls run_experiment(", "run_experiment(" not in PRIO_BODY)
check("I: priority.py never defines or calls simulate(", "simulate(" not in PRIO_BODY)
check("I: priority.py never imports research.experiments.runner at all",
      not re.search(r"^\s*(?:from|import)\s+.*\brunner\b", PRIO_SRC, re.MULTILINE))
check("I: priority.py never imports a broker module",
      not re.search(r"^\s*(?:from|import)\s+broker\w*", PRIO_SRC, re.MULTILINE))


# ---------------------------------------------------------------------------
print("\n--- J: no live impact ---")
# ---------------------------------------------------------------------------

check("J: priority.py never imports an engine module",
      not re.search(r"^\s*(?:from|import)\s+engine\b", PRIO_SRC, re.MULTILINE))
check("J: priority.py's code (outside prose/docstrings) never references "
      "memory/state.json", "state.json" not in PRIO_BODY)
check("J: priority.py never imports hypothesis_intake (no path to "
      "create_draft/approve_and_lock)",
      not re.search(r"^\s*(?:from|import)\s+.*hypothesis_intake", PRIO_SRC, re.MULTILINE))
check("J: priority.py never calls .lock( or .save( on a Contract",
      ".lock(" not in PRIO_BODY and ".save(" not in PRIO_BODY)
check("J: priority.py never calls store.append( directly",
      "store.append(" not in PRIO_BODY)


# ---------------------------------------------------------------------------
print("\n--- K: pure behavior ---")
# ---------------------------------------------------------------------------

store_k = fresh_store("k")
reg_k = fresh_registry("k")
for i in range(4):
    cid = f"EXP-K-{i}"
    make_locked_contract(cid, locked_at=f"2024-05-0{i+1}T00:00:00", registry_dir=reg_k)
    hid = rm.new_hypothesis_id()
    link_contract_to_hypothesis(store_k, hid, cid)
    if i % 2 == 0:
        ra.tag_hypothesis(store_k, hypothesis_id=hid, research_area="momentum", source="test")

before_fp = _registry_fingerprint(reg_k)
before_row_counts = {
    ds: len(rm.query_research_log(store_k, ds))
    for ds in (rm.DATASET_HYPOTHESIS, rm.DATASET_RESEARCH_AREA, rm.DATASET_VERDICT,
               rm.DATASET_EVIDENCE, rm.DATASET_NOTE)
}

contracts_k = _registry(reg_k)
result1 = [c.id for c in prio.rank_experiments(store_k, contracts_k, registry_dir=reg_k)]
result2 = [c.id for c in prio.rank_experiments(store_k, contracts_k, registry_dir=reg_k)]
explain1 = prio.explain(store_k, contracts_k, registry_dir=reg_k)
explain2 = prio.explain(store_k, contracts_k, registry_dir=reg_k)

after_fp = _registry_fingerprint(reg_k)
after_row_counts = {
    ds: len(rm.query_research_log(store_k, ds))
    for ds in (rm.DATASET_HYPOTHESIS, rm.DATASET_RESEARCH_AREA, rm.DATASET_VERDICT,
               rm.DATASET_EVIDENCE, rm.DATASET_NOTE)
}

check("K: rank_experiments() is called-twice-identical", result1 == result2)
check("K: explain() is called-twice-identical", explain1 == explain2)
check("K: registry files are byte-for-byte unchanged after ranking",
      before_fp == after_fp)
check("K: no research-memory row counts changed from ranking alone",
      before_row_counts == after_row_counts)


# ---------------------------------------------------------------------------
print("\n====================================================")
print(f"  {PASSED} passed, {FAILED} failed")
print("====================================================")
sys.exit(1 if FAILED else 0)
