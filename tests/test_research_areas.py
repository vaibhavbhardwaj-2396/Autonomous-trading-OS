"""
Tests for Phase 2 Slice M: research/brain/research_areas.py — a lightweight
hypothesis_id -> research_area map for research navigation/prioritization,
plus its bounded surfacing in research/brain/digest.py.

This is explicitly NOT duplicate detection (that's Slice L, similarity.py)
and NOT semantic/automatic classification — every area label here is
supplied explicitly by the caller of tag_hypothesis(), never inferred.

Run with:  python -m tests.test_research_areas
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
from research.brain import research_areas as ra  # noqa: E402
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


TMP = Path(tempfile.mkdtemp(prefix="lq-test-research-areas-"))


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


# ---------------------------------------------------------------------------
print("\n--- A: tag creation — a hypothesis can be associated with a research area ---")
# ---------------------------------------------------------------------------

store_a = fresh_store("a")
row_id = ra.tag_hypothesis(
    store_a, hypothesis_id="HYP-20260101-aaaaaaaa", research_area="momentum",
    source="test-fixture")
check("A: tag_hypothesis() returns a row id for a genuinely new tag", row_id is not None)

rows_a = rm.query_research_log(store_a, rm.DATASET_RESEARCH_AREA)
check("A: exactly one row was written to the research_area_tag dataset",
      len(rows_a) == 1)
check("A: the row's payload carries the hypothesis_id and research_area verbatim",
      rows_a[0]["payload"] == {"hypothesis_id": "HYP-20260101-aaaaaaaa", "research_area": "momentum"})

def _raises(fn) -> bool:
    try:
        fn()
        return False
    except ValueError:
        return True


check("A: tag_hypothesis() rejects an empty hypothesis_id",
      _raises(lambda: ra.tag_hypothesis(store_a, hypothesis_id="", research_area="momentum", source="t")))
check("A: tag_hypothesis() rejects a whitespace-only research_area",
      _raises(lambda: ra.tag_hypothesis(store_a, hypothesis_id="HYP-x", research_area="   ", source="t")))
check("A: tag_hypothesis() rejects a non-string hypothesis_id",
      _raises(lambda: ra.tag_hypothesis(store_a, hypothesis_id=None, research_area="momentum", source="t")))


# ---------------------------------------------------------------------------
print("\n--- B: retrieval — the association can be retrieved ---")
# ---------------------------------------------------------------------------

check("B: area_of() returns the tagged area for a tagged hypothesis",
      ra.area_of(store_a, "HYP-20260101-aaaaaaaa") == "momentum")
check("B: area_of() returns None for a hypothesis that was never tagged",
      ra.area_of(store_a, "HYP-never-tagged") is None)

# retagging: the most recently appended tag wins, without erasing the old row
ra.tag_hypothesis(store_a, hypothesis_id="HYP-20260101-aaaaaaaa",
                   research_area="volatility", source="test-fixture-retag")
check("B: retagging appends a second row rather than editing the first",
      len(rm.query_research_log(store_a, rm.DATASET_RESEARCH_AREA)) == 2)
check("B: area_of() reflects the MOST RECENT tag after a retag",
      ra.area_of(store_a, "HYP-20260101-aaaaaaaa") == "volatility")


# ---------------------------------------------------------------------------
print("\n--- C: multiple hypotheses can belong to the SAME area ---")
# ---------------------------------------------------------------------------

store_c = fresh_store("c")
ra.tag_hypothesis(store_c, hypothesis_id="HYP-C1", research_area="momentum", source="t")
ra.tag_hypothesis(store_c, hypothesis_id="HYP-C2", research_area="momentum", source="t")
ra.tag_hypothesis(store_c, hypothesis_id="HYP-C3", research_area="momentum", source="t")

groups_c = ra.groups(store_c)
check("C: exactly one area group exists", len(groups_c) == 1)
check("C: that group's name is 'momentum'", groups_c[0].name == "momentum")
check("C: all three hypotheses land in the same group",
      groups_c[0].hypothesis_ids == ("HYP-C1", "HYP-C2", "HYP-C3"))
check("C: to_dict() reports the correct hypothesis_count",
      groups_c[0].to_dict()["hypothesis_count"] == 3)


# ---------------------------------------------------------------------------
print("\n--- D: different hypotheses can belong to DIFFERENT areas ---")
# ---------------------------------------------------------------------------

store_d = fresh_store("d")
ra.tag_hypothesis(store_d, hypothesis_id="HYP-D1", research_area="momentum", source="t")
ra.tag_hypothesis(store_d, hypothesis_id="HYP-D2", research_area="volatility", source="t")
ra.tag_hypothesis(store_d, hypothesis_id="HYP-D3", research_area="event-driven", source="t")

groups_d = ra.groups(store_d)
check("D: three separate area groups exist", len(groups_d) == 3)
check("D: group names are exactly the three areas, alphabetically sorted",
      [g.name for g in groups_d] == ["event-driven", "momentum", "volatility"])
check("D: each group has exactly one member and it's the right one",
      {g.name: g.hypothesis_ids for g in groups_d} ==
      {"event-driven": ("HYP-D3",), "momentum": ("HYP-D1",), "volatility": ("HYP-D2",)})

# case-sensitivity is deliberate — not automatic taxonomy folding
store_d2 = fresh_store("d2")
ra.tag_hypothesis(store_d2, hypothesis_id="HYP-D4", research_area="Momentum", source="t")
ra.tag_hypothesis(store_d2, hypothesis_id="HYP-D5", research_area="momentum", source="t")
groups_d2 = ra.groups(store_d2)
check("D: 'Momentum' and 'momentum' are treated as distinct areas (no silent case-folding)",
      len(groups_d2) == 2 and {g.name for g in groups_d2} == {"Momentum", "momentum"})


# ---------------------------------------------------------------------------
print("\n--- E: deterministic grouping — same data produces identical groups ---")
# ---------------------------------------------------------------------------

store_e = fresh_store("e")
for hid, area in [("HYP-E3", "volatility"), ("HYP-E1", "momentum"),
                   ("HYP-E2", "momentum"), ("HYP-E4", "event-driven")]:
    ra.tag_hypothesis(store_e, hypothesis_id=hid, research_area=area, source="t")

groups_e_1 = ra.groups(store_e)
groups_e_2 = ra.groups(store_e)
check("E: two calls against an unchanged store produce identical dataclass output",
      groups_e_1 == groups_e_2)
check("E: JSON-ready dict form is also identical across calls",
      ra.groups_as_dicts(store_e) == ra.groups_as_dicts(store_e))
check("E: JSON-ready form is byte-for-byte identical once serialized",
      json.dumps(ra.groups_as_dicts(store_e), sort_keys=True) ==
      json.dumps(ra.groups_as_dicts(store_e), sort_keys=True))

store_e_reopened = Store.open(store_e.path)
check("E: determinism holds against a brand-new Store handle on the same file",
      ra.groups(store_e) == ra.groups(store_e_reopened))
store_e_reopened.close()


# ---------------------------------------------------------------------------
print("\n--- F: adding an area tag must not mutate any Contract ---")
# ---------------------------------------------------------------------------

store_f = fresh_store("f")
reg_f = fresh_registry("f")
c_f = make_contract("EXP-F-A").lock()
c_f.save(reg_f)

hash_before = c_f.content_hash()
status_before = c_f.status
file_before = (reg_f / "EXP-F-A.json").read_text()

ra.tag_hypothesis(store_f, hypothesis_id="HYP-F1", research_area="momentum", source="t")
ra.tag_hypothesis(store_f, hypothesis_id="HYP-F1", research_area="volatility", source="t-retag")

hash_after = c_f.content_hash()
status_after = c_f.status
file_after = (reg_f / "EXP-F-A.json").read_text()

check("F: Contract.content_hash() is unchanged after tagging/retagging",
      hash_before == hash_after)
check("F: Contract.status is unchanged after tagging/retagging",
      status_before == status_after == "locked")
check("F: the on-disk registry file is byte-for-byte unchanged",
      file_before == file_after)
check("F: Contract.verify() still passes (lock state intact)",
      c_f.verify() is None or c_f.verify() is True or True)  # verify() raises on failure; reaching here is the assertion
try:
    c_f.verify()
    verify_ok = True
except Exception:
    verify_ok = False
check("F: Contract.verify() raises nothing after tagging/retagging", verify_ok)

# research_areas.py never even imports Contract or touches the registry dir
RA_SRC = (Path(__file__).parent.parent / "research" / "brain" / "research_areas.py").read_text()
RA_BODY = RA_SRC.split('"""', 2)[-1]  # strip the leading module docstring
check("F: research_areas.py's code never imports Contract",
      "import Contract" not in RA_BODY and "from ..contracts" not in RA_BODY)
check("F: research_areas.py's code never calls .lock( or .save( on anything",
      ".lock(" not in RA_BODY and ".save(" not in RA_BODY)


# ---------------------------------------------------------------------------
print("\n--- G: digest integration ---")
# ---------------------------------------------------------------------------

store_g = fresh_store("g")
reg_g = fresh_registry("g")
ra.tag_hypothesis(store_g, hypothesis_id="HYP-G1", research_area="momentum", source="t")
ra.tag_hypothesis(store_g, hypothesis_id="HYP-G2", research_area="momentum", source="t")
ra.tag_hypothesis(store_g, hypothesis_id="HYP-G3", research_area="volatility", source="t")

d_g = dg.build_digest(store_g, "2030-01-01", registry_dir=reg_g)
check("G: the digest carries a 'research_areas' section",
      "research_areas" in d_g)
check("G: the section's areas match research_areas.groups()'s counts, sorted by name",
      d_g["research_areas"]["areas"] ==
      [{"name": g.name, "hypothesis_count": len(g.hypothesis_ids)} for g in ra.groups(store_g)])
check("G: shown_count/total_count/truncated are consistent for an unbounded case",
      d_g["research_areas"]["shown_count"] == 2
      and d_g["research_areas"]["total_count"] == 2
      and d_g["research_areas"]["truncated"] is False)
check("G: the momentum area reports hypothesis_count 2 and volatility 1",
      {a["name"]: a["hypothesis_count"] for a in d_g["research_areas"]["areas"]} ==
      {"momentum": 2, "volatility": 1})

# bounding: force truncation with area_limit=0
d_g_bounded = dg.build_digest(store_g, "2030-01-01", registry_dir=reg_g, area_limit=0)
check("G: area_limit=0 truncates the shown areas to zero without losing the true total_count",
      d_g_bounded["research_areas"]["shown_count"] == 0
      and d_g_bounded["research_areas"]["total_count"] == 2
      and d_g_bounded["research_areas"]["truncated"] is True)

d_g_again = dg.build_digest(store_g, "2030-01-01", registry_dir=reg_g)
check("G: build_digest() including research_areas is itself deterministic",
      d_g == d_g_again)
check("G: to_json() renders byte-for-byte identically across calls",
      dg.to_json(d_g) == dg.to_json(d_g_again))


# ---------------------------------------------------------------------------
print("\n--- H: isolation — no engine/broker import, no execution, no locking ---")
# ---------------------------------------------------------------------------

check("H: research_areas.py imports no engine module",
      not re.search(r"^\s*(import\s+engine|from\s+engine)", RA_SRC, re.MULTILINE))
check("H: research_areas.py imports no broker module",
      "broker" not in RA_BODY.lower())
check("H: research_areas.py contains no eval/exec/compile call",
      not re.search(r"\b(eval|exec|compile)\s*\(", RA_BODY))
check("H: research_areas.py never calls run_experiment or approve_and_lock",
      "run_experiment" not in RA_BODY and "approve_and_lock" not in RA_BODY)
check("H: research_areas.py never touches memory/state.json",
      "state.json" not in RA_BODY)


# ---------------------------------------------------------------------------
print("\n--- I: empty state — sensible, deterministic digest with no tags ---")
# ---------------------------------------------------------------------------

store_i = fresh_store("i")
reg_i = fresh_registry("i")
d_i = dg.build_digest(store_i, "2030-01-01", registry_dir=reg_i)
check("I: an untagged research memory reports an empty areas list, not an error",
      d_i["research_areas"]["areas"] == [])
check("I: shown_count/total_count are both 0 and truncated is False",
      d_i["research_areas"]["shown_count"] == 0
      and d_i["research_areas"]["total_count"] == 0
      and d_i["research_areas"]["truncated"] is False)
check("I: groups() on an untagged store returns an empty list, not None or an error",
      ra.groups(store_i) == [])
check("I: area_of() on an untagged store returns None",
      ra.area_of(store_i, "HYP-anything") is None)

d_i_again = dg.build_digest(store_i, "2030-01-01", registry_dir=reg_i)
check("I: the empty-state digest is itself deterministic across calls",
      d_i == d_i_again)


# ---------------------------------------------------------------------------
print("\n====================================================")
print(f"  {PASSED} passed, {FAILED} failed")
print("====================================================")
sys.exit(1 if FAILED else 0)
