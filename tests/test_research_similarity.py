"""
Tests for Phase 2 Slice L: research/brain/similarity.py — global exact-
duplicate detection across the WHOLE Contract registry, independent of
hypothesis_id, plus its bounded surfacing in research/brain/digest.py.

This file never modifies research/experiments/comparison.py — it imports
_rule_fingerprint from it directly to prove similarity.py reuses the exact
same function rather than a reimplementation that could silently drift.

Run with:  python -m tests.test_research_similarity
"""

import re
import sys
import json
import shutil
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from research.store import Store  # noqa: E402
from research import memory as rm  # noqa: E402
from research.contracts import Contract, REGISTRY_DIR  # noqa: E402
from research.experiments import evaluator  # noqa: E402
from research.experiments.comparison import _rule_fingerprint  # noqa: E402
from research.brain import similarity as sim  # noqa: E402
from research.brain import digest as dg  # noqa: E402

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


TMP = Path(tempfile.mkdtemp(prefix="lq-test-similarity-"))


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


def make_contract(cid, **overrides):
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


def link(store, hid, contract_id):
    """Register the hypothesis_id <-> contract_id link the way
    hypothesis_intake.create_draft() normally would — required because
    Contract carries no hypothesis_id field of its own (see
    research/experiments/evaluator.py's resolve_hypothesis_id docstring)."""
    rm.record_hypothesis_proposal(
        store, claim=f"claim for {contract_id}", source="test-fixture",
        hypothesis_id=hid, extra={"contract_id": contract_id})


# ---------------------------------------------------------------------------
print("\n--- A: same rule under the SAME hypothesis is still detected, "
      "and comparison.py's own behavior is unaffected ---")
# ---------------------------------------------------------------------------

store_a = fresh_store("same_hyp")
reg_a = fresh_registry("same_hyp")

cA1 = make_contract("EXP-A-1")
cA1.lock()
cA1.save(reg_a)
link(store_a, "HYP-SAME", "EXP-A-1")

cA2 = make_contract("EXP-A-2")  # identical fingerprint-relevant fields
cA2.lock()
cA2.save(reg_a)
link(store_a, "HYP-SAME", "EXP-A-2")

check("similarity.py reuses comparison._rule_fingerprint(), not a copy",
      _rule_fingerprint(cA1) == _rule_fingerprint(cA2))

groups_a = sim.duplicate_groups(store_a, registry_dir=reg_a)
check("A: two same-hypothesis, same-rule contracts still form one group "
      "(sharing a hypothesis does not exempt them)",
      len(groups_a) == 1 and len(groups_a[0].members) == 2, str(groups_a))
check("A: both members correctly resolve to the SAME hypothesis_id",
      all(m.hypothesis_id == "HYP-SAME" for m in groups_a[0].members))


# ---------------------------------------------------------------------------
print("\n--- B: the exact same rule under DIFFERENT hypotheses is detected globally ---")
# ---------------------------------------------------------------------------

store_b = fresh_store("diff_hyp")
reg_b = fresh_registry("diff_hyp")

cB_A = make_contract("EXP-HYPA-1")
cB_A.lock()
cB_A.save(reg_b)
link(store_b, "HYP-A", "EXP-HYPA-1")

cB_B = make_contract("EXP-HYPB-1")  # identical fingerprint fields, different id/hypothesis
cB_B.lock()
cB_B.save(reg_b)
link(store_b, "HYP-B", "EXP-HYPB-1")

groups_b = sim.duplicate_groups(store_b, registry_dir=reg_b)
check("B: exactly one duplicate group is found across the two hypotheses",
      len(groups_b) == 1, str(groups_b))
member_hids = {m.hypothesis_id for m in groups_b[0].members}
check("B: the group's members resolve to their own, DIFFERENT hypothesis_ids",
      member_hids == {"HYP-A", "HYP-B"}, str(member_hids))
member_ids = {m.contract_id for m in groups_b[0].members}
check("B: the group contains exactly the two cross-hypothesis contract ids",
      member_ids == {"EXP-HYPA-1", "EXP-HYPB-1"}, str(member_ids))
check("B: is_duplicate() agrees for both members",
      sim.is_duplicate(store_b, "EXP-HYPA-1", registry_dir=reg_b)
      and sim.is_duplicate(store_b, "EXP-HYPB-1", registry_dir=reg_b))


# ---------------------------------------------------------------------------
print("\n--- C/D/E/F: any differing fingerprint dimension prevents a match ---")
# ---------------------------------------------------------------------------

store_cdef = fresh_store("dimensions")
reg_cdef = fresh_registry("dimensions")

base = make_contract("EXP-BASE")
base.lock()
base.save(reg_cdef)
link(store_cdef, "HYP-BASE", "EXP-BASE")

variants = {
    "C: different entry_rule": make_contract(
        "EXP-DIFF-ENTRY",
        entry_rule=json.dumps({"conditions": [{"metric": "volume_zscore", "op": ">", "value": 4.0}]})),
    "D: different exit_rule": make_contract(
        "EXP-DIFF-EXIT",
        exit_rule=json.dumps({"stop_loss_pct": 3.0, "target_pct": 6.0})),
    "E: different splits": make_contract(
        "EXP-DIFF-SPLITS",
        splits={"discovery": ["2019-01-01", "2020-01-01"], "validation": ["2020-01-02", "2020-06-01"]}),
    "F: different evaluation window": make_contract(
        "EXP-DIFF-WINDOW",
        evaluation_start="2019-06-01", evaluation_end="2020-06-01"),
}

for label, contract in variants.items():
    contract.lock()
    contract.save(reg_cdef)
    link(store_cdef, f"HYP-{contract.id}", contract.id)

groups_cdef = sim.duplicate_groups(store_cdef, registry_dir=reg_cdef)
check(f"{label[:1]}/../..: no differing variant is grouped with the base "
      f"contract or with each other",
      len(groups_cdef) == 0, str(groups_cdef))
for label, contract in variants.items():
    check(f"{label}: not flagged as a duplicate of the base contract",
          not sim.is_duplicate(store_cdef, contract.id, registry_dir=reg_cdef))


# ---------------------------------------------------------------------------
print("\n--- G: same fingerprint, three different contract ids, all detected together ---")
# ---------------------------------------------------------------------------

store_g = fresh_store("triple")
reg_g = fresh_registry("triple")
for i, cid in enumerate(("EXP-TRIP-1", "EXP-TRIP-2", "EXP-TRIP-3")):
    c = make_contract(cid)
    c.lock()
    c.save(reg_g)
    link(store_g, f"HYP-TRIP-{i}", cid)

groups_g = sim.duplicate_groups(store_g, registry_dir=reg_g)
check("G: all three same-fingerprint contracts land in ONE group",
      len(groups_g) == 1 and len(groups_g[0].members) == 3, str(groups_g))
check("G: the group's contract ids are exactly the three, each distinct",
      {m.contract_id for m in groups_g[0].members} == {"EXP-TRIP-1", "EXP-TRIP-2", "EXP-TRIP-3"})


# ---------------------------------------------------------------------------
print("\n--- H: draft contracts — verifying and preserving the existing inclusion policy ---")
# ---------------------------------------------------------------------------

store_h = fresh_store("draft_policy")
reg_h = fresh_registry("draft_policy")

from research.contracts import registry as contracts_registry  # noqa: E402

c_draft = make_contract("EXP-DRAFT")  # never locked — status stays "draft"
c_draft.save(reg_h)
link(store_h, "HYP-DRAFT", "EXP-DRAFT")

c_locked = make_contract("EXP-LOCKED")  # identical fingerprint fields
c_locked.lock()
c_locked.save(reg_h)
link(store_h, "HYP-LOCKED", "EXP-LOCKED")

check("sanity: research.contracts.registry() ALREADY includes drafts — "
      "this is the pre-existing, unmodified inclusion policy this module reuses",
      any(c.id == "EXP-DRAFT" and c.status == "draft"
          for c in contracts_registry(reg_h)))

groups_h = sim.duplicate_groups(store_h, registry_dir=reg_h)
check("H: a draft contract IS included in global duplicate detection, "
      "consistent with contracts.registry()'s own existing behavior — no "
      "new inclusion policy was invented for this slice",
      len(groups_h) == 1 and
      {m.contract_id for m in groups_h[0].members} == {"EXP-DRAFT", "EXP-LOCKED"},
      str(groups_h))
draft_member = next(m for m in groups_h[0].members if m.contract_id == "EXP-DRAFT")
check("H: the draft member's status is correctly reported as 'draft'",
      draft_member.status == "draft")


# ---------------------------------------------------------------------------
print("\n--- I: determinism ---")
# ---------------------------------------------------------------------------

groups_g_again = sim.duplicate_groups(store_g, registry_dir=reg_g)
check("I: two calls against an unchanged registry/store produce identical "
      "dataclass output", groups_g == groups_g_again)

dicts_1 = sim.duplicate_groups_as_dicts(store_g, registry_dir=reg_g)
dicts_2 = sim.duplicate_groups_as_dicts(store_g, registry_dir=reg_g)
check("I: JSON-ready dict form is also identical across calls", dicts_1 == dicts_2)
check("I: JSON-ready form is byte-for-byte identical once serialized",
      json.dumps(dicts_1, sort_keys=True) == json.dumps(dicts_2, sort_keys=True))

# Rebuild from a brand-new Store handle on the same file, to rule out
# determinism being an artifact of in-process caching.
store_g_reopened = Store.open(TMP / "triple.db")
groups_g_reopened = sim.duplicate_groups(store_g_reopened, registry_dir=reg_g)
check("I: determinism holds against a brand-new Store handle on the same file",
      groups_g == groups_g_reopened)
store_g_reopened.close()


# ---------------------------------------------------------------------------
print("\n--- J: read-only behavior — no mutation of registry, Store, or Contracts ---")
# ---------------------------------------------------------------------------

reg_files_before = {p.name: p.read_text() for p in reg_g.glob("*.json")}
store_rows_before = len(rm.query_research_log(store_g, rm.DATASET_HYPOTHESIS))

_ = sim.duplicate_groups(store_g, registry_dir=reg_g)
_ = sim.duplicate_groups_as_dicts(store_g, registry_dir=reg_g)
_ = sim.is_duplicate(store_g, "EXP-TRIP-1", registry_dir=reg_g)

reg_files_after = {p.name: p.read_text() for p in reg_g.glob("*.json")}
store_rows_after = len(rm.query_research_log(store_g, rm.DATASET_HYPOTHESIS))

check("J: no registry file was added, removed, or changed by any scan",
      reg_files_before == reg_files_after)
check("J: no new research-memory row was written by any scan",
      store_rows_before == store_rows_after)

SIM_SRC = (Path(__file__).parent.parent / "research" / "brain" / "similarity.py").read_text()
SIM_BODY = SIM_SRC.split('"""', 2)[-1]  # strip the leading module docstring
check("J: similarity.py's code never calls .save( on a Contract",
      ".save(" not in SIM_BODY)
check("J: similarity.py's code never calls .lock( on a Contract",
      ".lock(" not in SIM_BODY)
check("J: similarity.py's code never calls store.append",
      "store.append(" not in SIM_BODY and "store.save(" not in SIM_BODY)
check("J: similarity.py never constructs a new Contract",
      "Contract(" not in SIM_BODY)


# ---------------------------------------------------------------------------
print("\n--- K: digest integration ---")
# ---------------------------------------------------------------------------

d_k = dg.build_digest(store_g, "2030-01-01", registry_dir=reg_g)
check("K: the digest carries an 'exact_duplicates' section",
      "exact_duplicates" in d_k)
check("K: the section's groups match similarity.duplicate_groups_as_dicts()",
      d_k["exact_duplicates"]["groups"] == sim.duplicate_groups_as_dicts(store_g, registry_dir=reg_g))
check("K: shown_count/total_count are consistent and correct for an unbounded case",
      d_k["exact_duplicates"]["shown_count"] == 1
      and d_k["exact_duplicates"]["total_count"] == 1
      and d_k["exact_duplicates"]["truncated"] is False)

# bounding: force truncation with duplicate_limit=0
d_k_bounded = dg.build_digest(store_g, "2030-01-01", registry_dir=reg_g, duplicate_limit=0)
check("K: duplicate_limit truncates the shown groups to zero without losing "
      "the true total_count",
      d_k_bounded["exact_duplicates"]["shown_count"] == 0
      and d_k_bounded["exact_duplicates"]["total_count"] == 1
      and d_k_bounded["exact_duplicates"]["truncated"] is True)

# digest determinism, now including the new section
d_k_again = dg.build_digest(store_g, "2030-01-01", registry_dir=reg_g)
check("K: build_digest() including exact_duplicates is itself deterministic",
      d_k == d_k_again)
check("K: to_json() renders byte-for-byte identically across calls",
      dg.to_json(d_k) == dg.to_json(d_k_again))

# a digest with no duplicates anywhere reports a clean, empty section
store_empty = fresh_store("no_dupes")
reg_empty = fresh_registry("no_dupes")
c_solo = make_contract("EXP-SOLO")
c_solo.lock()
c_solo.save(reg_empty)
link(store_empty, "HYP-SOLO", "EXP-SOLO")
d_empty = dg.build_digest(store_empty, "2030-01-01", registry_dir=reg_empty)
check("K: a registry with no duplicates reports an empty exact_duplicates section, not an error",
      d_empty["exact_duplicates"] == {"groups": [], "shown_count": 0, "total_count": 0,
                                      "truncated": False})
store_empty.close()


# ---------------------------------------------------------------------------
print("\n--- isolation: similarity.py imports nothing from engine/ or broker ---")
# ---------------------------------------------------------------------------

imports = re.findall(r"^\s*(?:from|import)\s+([.\w]+)", SIM_SRC, re.MULTILINE)
check("similarity.py imports no engine module", not any(m.startswith("engine") for m in imports),
      str(imports))
check("similarity.py imports no broker module",
      not any("broker" in m for m in imports), str(imports))
check("similarity.py contains no eval/exec/compile call",
      not re.search(r"\beval\s*\(|\bexec\s*\(|(?<!re\.)\bcompile\s*\(", SIM_SRC))


for s in (store_a, store_b, store_cdef, store_g, store_h):
    s.close()

print(f"\n{'=' * 52}")
print(f"  {PASSED} passed, {FAILED} failed")
print(f"{'=' * 52}\n")
sys.exit(1 if FAILED else 0)
