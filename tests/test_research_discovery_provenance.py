"""
Tests for Phase 2 Slice N: Discovery Search Provenance.

Covers the new `research_discovery_search` dataset (research/memory.py:
record_discovery_search), its read side (research/brain/
discovery_provenance.py), and its integration into
research/brain/investigator.py's investigate() — the first link in the
eventual "digest snapshot -> discovery process -> hypothesis -> contracts ->
evidence" lineage: digest snapshot -> discovery process -> hypothesis_id,
and ONLY that link.

No test in this file invokes the real `claude` binary — every AI boundary
is mocked via an injected `runner` callable, same mandate as
test_research_investigator.py.

Run with:  python -m tests.test_research_discovery_provenance
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
from research.contracts import Contract  # noqa: E402
from research.brain import digest as dg  # noqa: E402
from research.brain import hypothesis_intake as hi  # noqa: E402
from research.brain import investigator as inv  # noqa: E402
from research.brain import discovery_provenance as dp  # noqa: E402

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


TMP = Path(tempfile.mkdtemp(prefix="lq-test-discovery-provenance-"))


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


def valid_ai_proposal(**overrides) -> dict:
    """Same fixture shape as test_research_investigator.py's
    valid_ai_proposal() — the Research AI is expected to produce exactly
    this shape."""
    base = {
        "title": "Volume-spike drift",
        "hypothesis": ("High-volume anomalies (>=3 sigma) precede a short-term "
                        "upward drift in liquid large-caps."),
        "null_hypothesis": "Volume anomalies have no relationship to subsequent returns.",
        "universe": "watchlist",
        "signal": "observatory.volume_zscore",
        "entry_rule": {"conditions": [{"metric": "volume_zscore", "op": ">", "value": 3.0}]},
        "exit_rule": {"stop_loss_pct": 2.0, "target_pct": 4.0, "max_hold_days": 10},
        "splits": {"discovery": ["2019-01-01", "2022-12-31"]},
        "independence": "one entry per symbol per rolling 10-day window",
        "falsification": "expectancy_r <= 0 on discovery, or t_stat < 2.0",
        "abandon_condition": "if discovery-split expectancy_r <= 0, abandon",
        "evaluation_start": "2019-01-01",
        "evaluation_end": "2022-12-31",
    }
    base.update(overrides)
    return base


def all_discovery_rows(store) -> list:
    return rm.query_research_log(store, rm.DATASET_DISCOVERY_SEARCH)


# ---------------------------------------------------------------------------
print("\n--- A: a successful Investigator run creates BOTH a hypothesis "
      "proposal AND discovery provenance ---")
# ---------------------------------------------------------------------------

store_a = fresh_store("a")
reg_a = fresh_registry("a")

raw_response = json.dumps(valid_ai_proposal())
result_a = inv.investigate(store_a, "2024-05-12", runner=lambda p: raw_response, registry_dir=reg_a)

check("A: investigate() still returns an InvestigatorResult",
      isinstance(result_a, inv.InvestigatorResult), str(result_a))

hyp_rows_a = [r for r in rm.query_research_log(store_a, rm.DATASET_HYPOTHESIS)
              if r["payload"].get("hypothesis_id") == result_a.hypothesis_id]
check("A: a hypothesis-proposal claim row exists for the resulting hypothesis_id",
      len(hyp_rows_a) == 1)

disc_rows_a = all_discovery_rows(store_a)
check("A: exactly one discovery-search provenance row was written",
      len(disc_rows_a) == 1)
check("A: the provenance row's dataset is research_discovery_search",
      disc_rows_a[0]["dataset"] == rm.DATASET_DISCOVERY_SEARCH)


# ---------------------------------------------------------------------------
print("\n--- B: the provenance row contains the EXACT resulting hypothesis_id ---")
# ---------------------------------------------------------------------------

check("B: the provenance payload's hypothesis_id matches investigate()'s result",
      disc_rows_a[0]["payload"]["hypothesis_id"] == result_a.hypothesis_id)


# ---------------------------------------------------------------------------
print("\n--- C: the provenance references the SAME as_of used to build the digest ---")
# ---------------------------------------------------------------------------

expected_digest = dg.build_digest(store_a, "2024-05-12", registry_dir=reg_a)
check("C: provenance as_of equals build_digest(store, as_of)'s own 'as_of' field",
      disc_rows_a[0]["payload"]["as_of"] == expected_digest["as_of"])

# and it must NOT be the raw, unnormalized "2024-05-12" string, unless that
# happens to already equal the normalized form -- assert equality with the
# canonical digest as_of specifically, not just any as_of-shaped string
check("C: the recorded as_of is the canonical digest as_of, not an independent recomputation",
      disc_rows_a[0]["payload"]["as_of"] == dg.build_digest(store_a, "2024-05-12", registry_dir=reg_a)["as_of"])


# ---------------------------------------------------------------------------
print("\n--- D: AI attribution — a stable implementation/prompt identifier ---")
# ---------------------------------------------------------------------------

check("D: discovery_type identifies this as a research_ai discovery",
      disc_rows_a[0]["payload"]["discovery_type"] == "research_ai" == inv.DISCOVERY_TYPE_RESEARCH_AI)
check("D: version equals investigator.py's own RESEARCH_AI_VERSION constant",
      disc_rows_a[0]["payload"]["version"] == inv.RESEARCH_AI_VERSION)
check("D: prompt_path identifies routines/research_investigate.md",
      disc_rows_a[0]["payload"].get("prompt_path") == "routines/research_investigate.md")

# same digest + same AI response + a second, independent run -> same version/
# discovery_type/prompt_path (only as_of/ids may coincide or differ)
store_d2 = fresh_store("d2")
reg_d2 = fresh_registry("d2")
result_d2 = inv.investigate(store_d2, "2024-05-12", runner=lambda p: raw_response, registry_dir=reg_d2)
row_d2 = all_discovery_rows(store_d2)[0]["payload"]
check("D: the attribution identifier is stable across independent runs",
      (row_d2["discovery_type"], row_d2["version"], row_d2["prompt_path"]) ==
      (disc_rows_a[0]["payload"]["discovery_type"], disc_rows_a[0]["payload"]["version"],
       disc_rows_a[0]["payload"]["prompt_path"]))


# ---------------------------------------------------------------------------
print("\n--- E: failed AI output creates NO misleading discovery provenance ---")
# ---------------------------------------------------------------------------

store_e = fresh_store("e")
reg_e = fresh_registry("e")

# E1: malformed / non-JSON output
try:
    inv.investigate(store_e, "2024-05-12", runner=lambda p: "not json at all", registry_dir=reg_e)
except inv.InvestigatorError:
    pass
check("E1: malformed AI output writes no discovery-search row",
      len(all_discovery_rows(store_e)) == 0)

# E2: timeout / runner exception
def _boom(prompt):
    raise TimeoutError("simulated timeout")

try:
    inv.investigate(store_e, "2024-05-12", runner=_boom, registry_dir=reg_e)
except inv.InvestigatorError:
    pass
check("E2: a runner timeout/exception writes no discovery-search row",
      len(all_discovery_rows(store_e)) == 0)

# E3: well-formed JSON but rejected by hypothesis_intake's existing validation
unsupported_metric = valid_ai_proposal(
    entry_rule={"conditions": [{"metric": "made_up_indicator", "op": ">", "value": 3.0}]})
try:
    inv.investigate(store_e, "2024-05-12",
                     runner=lambda p: json.dumps(unsupported_metric), registry_dir=reg_e)
except hi.IntakeRejected:
    pass
check("E3: a validation-rejected proposal writes no discovery-search row",
      len(all_discovery_rows(store_e)) == 0)

# E4: a no_proposal response is a normal outcome, not a failure -- and also
# must not create a discovery-search row (there is no hypothesis to link)
no_proposal_response = json.dumps({"no_proposal": True, "reason": "nothing interesting today"})
result_e4 = inv.investigate(store_e, "2024-05-12", runner=lambda p: no_proposal_response, registry_dir=reg_e)
check("E4: a no_proposal response returns NoProposal, not InvestigatorResult",
      isinstance(result_e4, inv.NoProposal))
check("E4: a no_proposal response writes no discovery-search row",
      len(all_discovery_rows(store_e)) == 0)

check("E: after every failure mode above, the discovery-search dataset is still completely empty",
      len(all_discovery_rows(store_e)) == 0)


# ---------------------------------------------------------------------------
print("\n--- F: multiple discovery runs create DISTINCT provenance records ---")
# ---------------------------------------------------------------------------

store_f = fresh_store("f")
reg_f = fresh_registry("f")

resp_1 = json.dumps(valid_ai_proposal(title="Idea one"))
resp_2 = json.dumps(valid_ai_proposal(title="Idea two"))

result_f1 = inv.investigate(store_f, "2024-05-12", runner=lambda p: resp_1, registry_dir=reg_f)
result_f2 = inv.investigate(store_f, "2024-05-12", runner=lambda p: resp_2, registry_dir=reg_f)

disc_rows_f = all_discovery_rows(store_f)
check("F: two independent runs produce two distinct hypothesis_ids",
      result_f1.hypothesis_id != result_f2.hypothesis_id)
check("F: two discovery-search rows now exist",
      len(disc_rows_f) == 2)
check("F: the two rows carry the two distinct hypothesis_ids, each exactly once",
      sorted(r["payload"]["hypothesis_id"] for r in disc_rows_f) ==
      sorted([result_f1.hypothesis_id, result_f2.hypothesis_id]))
check("F: the two rows are genuinely distinct database rows (different ids)",
      disc_rows_f[0]["id"] != disc_rows_f[1]["id"])


# ---------------------------------------------------------------------------
print("\n--- G: queryability — given a hypothesis_id, retrieve its provenance ---")
# ---------------------------------------------------------------------------

prov_f1 = dp.discovery_search_for_hypothesis(store_f, result_f1.hypothesis_id)
prov_f2 = dp.discovery_search_for_hypothesis(store_f, result_f2.hypothesis_id)
check("G: discovery_search_for_hypothesis() returns exactly one row for hypothesis 1",
      len(prov_f1) == 1)
check("G: discovery_search_for_hypothesis() returns exactly one row for hypothesis 2",
      len(prov_f2) == 1)
check("G: the returned row for hypothesis 1 actually IS about hypothesis 1",
      prov_f1[0]["hypothesis_id"] == result_f1.hypothesis_id)
check("G: querying a never-discovered hypothesis_id returns an empty list, not an error",
      dp.discovery_search_for_hypothesis(store_f, "HYP-does-not-exist") == [])
check("G: has_discovery_provenance() agrees with discovery_search_for_hypothesis()",
      dp.has_discovery_provenance(store_f, result_f1.hypothesis_id) is True
      and dp.has_discovery_provenance(store_f, "HYP-does-not-exist") is False)


# ---------------------------------------------------------------------------
print("\n--- H: read-only / isolation ---")
# ---------------------------------------------------------------------------

DP_SRC = (Path(__file__).parent.parent / "research" / "brain" / "discovery_provenance.py").read_text()
DP_BODY = DP_SRC.split('"""', 2)[-1]  # strip the leading module docstring

check("H: discovery_provenance.py imports no engine module",
      not re.search(r"^\s*(import\s+engine|from\s+engine)", DP_SRC, re.MULTILINE))
check("H: discovery_provenance.py imports no broker module",
      "broker" not in DP_BODY.lower())
check("H: discovery_provenance.py never imports Contract or research.contracts",
      "import Contract" not in DP_BODY and "from ..contracts" not in DP_BODY)
check("H: discovery_provenance.py never calls .lock( or .save( on anything",
      ".lock(" not in DP_BODY and ".save(" not in DP_BODY)
check("H: discovery_provenance.py never calls store.append( directly",
      "store.append(" not in DP_BODY)
check("H: discovery_provenance.py never runs an experiment (no run_experiment reference)",
      "run_experiment" not in DP_BODY)
check("H: discovery_provenance.py contains no eval/exec/compile call",
      not re.search(r"\b(eval|exec|compile)\s*\(", DP_BODY))

# investigator.py's own provenance write is the ONLY new write it gained —
# confirm it still never approves/locks anything
INV_SRC = (Path(__file__).parent.parent / "research" / "brain" / "investigator.py").read_text()
INV_BODY = INV_SRC.split('"""', 2)[-1]
check("H: investigator.py still never calls approve_and_lock or Contract.lock",
      "approve_and_lock(" not in INV_BODY and ".lock(" not in INV_BODY)
check("H: investigator.py still imports no engine module",
      not re.search(r"^\s*(import\s+engine|from\s+engine)", INV_SRC, re.MULTILINE))


# ---------------------------------------------------------------------------
print("\n--- I: Contract integrity — provenance must not touch the resulting Contract ---")
# ---------------------------------------------------------------------------

store_i = fresh_store("i")
reg_i = fresh_registry("i")

result_i = inv.investigate(store_i, "2024-05-12", runner=lambda p: raw_response, registry_dir=reg_i)
contract_before = Contract.load(result_i.contract_id, reg_i)
hash_before = contract_before.content_hash()
status_before = contract_before.status
file_before = (reg_i / f"{result_i.contract_id}.json").read_text()

check("I: the resulting Contract is status='draft', never locked, right after investigate()",
      status_before == "draft")

# query provenance repeatedly (a read) -- must never mutate the Contract
_ = dp.discovery_search_for_hypothesis(store_i, result_i.hypothesis_id)
_ = dp.discovery_search_for_hypothesis(store_i, result_i.hypothesis_id)
_ = dp.has_discovery_provenance(store_i, result_i.hypothesis_id)

contract_after = Contract.load(result_i.contract_id, reg_i)
hash_after = contract_after.content_hash()
status_after = contract_after.status
file_after = (reg_i / f"{result_i.contract_id}.json").read_text()

check("I: Contract.content_hash() is unchanged after recording/reading provenance",
      hash_before == hash_after)
check("I: Contract.status is unchanged (still 'draft', never became 'locked')",
      status_before == status_after == "draft")
check("I: the on-disk registry file is byte-for-byte unchanged",
      file_before == file_after)


# ---------------------------------------------------------------------------
print("\n====================================================")
print(f"  {PASSED} passed, {FAILED} failed")
print("====================================================")
sys.exit(1 if FAILED else 0)
