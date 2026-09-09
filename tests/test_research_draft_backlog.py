"""
Tests for Phase 2 Slice Q: Research Draft Backlog (research/brain/draft_backlog.py).

A read-only review surface over the current DRAFT hypothesis queue —
assembled entirely by reusing already-established, already-tested modules
(hypothesis_intake.pending_drafts, experiments.evaluator, brain.similarity,
brain.research_areas, brain.discovery_provenance, brain.digest.
TESTED_STATUSES). This file does not re-derive or re-test any of THEIR
behavior (duplicate-fingerprint correctness is similarity.py's own test
file's job, area-tag semantics are research_areas.py's, provenance shape is
discovery_provenance.py's) — it tests only what this slice actually adds:
discovery, ordering, bounding, and read-only-ness of the assembled backlog.

Run with:  python -m tests.test_research_draft_backlog
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
from research.contracts import Contract, REGISTRY_DIR, registry as _registry  # noqa: E402
from research.brain import hypothesis_intake as hi  # noqa: E402
from research.brain import research_areas as ra  # noqa: E402
from research.brain import discovery_provenance as dp  # noqa: E402
from research import memory as rm  # noqa: E402
from research.brain import draft_backlog as db  # noqa: E402

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


TMP = Path(tempfile.mkdtemp(prefix="lq-test-draft-backlog-"))


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


def valid_proposal(**overrides) -> dict:
    """Same shape as every other Slice's own proposal fixture."""
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


def distinct_proposal(i: int, **overrides) -> dict:
    """A proposal whose rule spec (and hence _rule_fingerprint) is distinct
    from distinct_proposal(j) for any j != i."""
    p = valid_proposal(
        title=f"Idea {i}",
        hypothesis=f"Idea {i}: high-volume anomalies precede drift, threshold variant {i}.",
        entry_rule={"conditions": [{"metric": "volume_zscore", "op": ">", "value": 3.0 + i}]},
    )
    p.update(overrides)
    return p


def make_locked_sibling(cid, *, proposal: dict, status: str, registry_dir: Path) -> Contract:
    """A contract with the SAME rule specification as `proposal` (so it
    shares its _rule_fingerprint), constructed directly and saved with a
    non-draft status — the same make_contract-then-mutate-status pattern
    test_research_digest.py's own '13: previously tested' section already
    uses, rather than actually running an experiment (out of scope, and
    unnecessary — this module never interprets verdicts, only status)."""
    entry_rule_str = json.dumps(proposal["entry_rule"], sort_keys=True, separators=(",", ":"))
    exit_rule_str = json.dumps(proposal["exit_rule"], sort_keys=True, separators=(",", ":"))
    c = Contract(
        id=cid, title=f"sibling {cid}", hypothesis=f"sibling claim {cid}",
        null_hypothesis="no effect", universe=proposal["universe"], signal=proposal["signal"],
        entry_rule=entry_rule_str, exit_rule=exit_rule_str, splits=proposal["splits"],
        independence=proposal["independence"], falsification=proposal["falsification"],
        abandon_condition=proposal["abandon_condition"],
        evaluation_start=proposal["evaluation_start"], evaluation_end=proposal["evaluation_end"],
    )
    c.lock()
    c.status = status
    c.save(registry_dir)
    return c


def _registry_fingerprint(reg_dir: Path) -> str:
    """A byte-level fingerprint of every file under a registry directory —
    used by the read-only tests to prove nothing changed."""
    h = hashlib.sha256()
    if not reg_dir.exists():
        return h.hexdigest()
    for f in sorted(reg_dir.glob("**/*")):
        if f.is_file():
            h.update(f.name.encode())
            h.update(f.read_bytes())
    return h.hexdigest()


# ---------------------------------------------------------------------------
print("\n--- A: draft discovery — a recently created draft appears in the backlog ---")
# ---------------------------------------------------------------------------

store_a = fresh_store("a")
reg_a = fresh_registry("a")

result_a = hi.create_draft(store_a, valid_proposal(), registry_dir=reg_a)
drafts_a = db.list_drafts(store_a, registry_dir=reg_a)

check("A: the newly created draft appears in list_drafts()", len(drafts_a) == 1)
check("A: hypothesis_id matches", drafts_a[0]["hypothesis_id"] == result_a.hypothesis_id)
check("A: contract_id matches", drafts_a[0]["contract_id"] == result_a.contract.id)
check("A: claim/title text is present", drafts_a[0]["claim"] == "Volume-spike drift")
check("A: status is 'draft'", drafts_a[0]["status"] == "draft")
check("A: source is present (the create_draft default source)",
      drafts_a[0]["source"] == "research_cycle.claude")
check("A: created_at is present and non-empty", bool(drafts_a[0]["created_at"]))
check("A: an untagged, unprovenanced draft reports no area and no provenance",
      drafts_a[0]["research_area"] is None and drafts_a[0]["discovery_provenance"] is None)


# ---------------------------------------------------------------------------
print("\n--- B: non-draft exclusion/inclusion semantics ---")
# ---------------------------------------------------------------------------
# Documented semantics (matches hypothesis_intake.pending_drafts() exactly,
# reused rather than re-derived): the backlog includes status=="draft"
# contracts ONLY. Once approve_and_lock() moves a contract past draft, it is
# no longer part of the to-review queue and drops out of list_drafts() —
# its history is still reachable via "related contract IDs"/"already
# tested" from anything that shares its hypothesis or its exact rule spec.

store_b = fresh_store("b")
reg_b = fresh_registry("b")

r_b1 = hi.create_draft(store_b, valid_proposal(), registry_dir=reg_b)
r_b2 = hi.create_draft(store_b, distinct_proposal(1), registry_dir=reg_b)
check("B: both freshly created drafts are visible before either is locked",
      {d["contract_id"] for d in db.list_drafts(store_b, registry_dir=reg_b)}
      == {r_b1.contract.id, r_b2.contract.id})

hi.approve_and_lock(store_b, r_b1.contract.id, approved_by="vaibhav", registry_dir=reg_b)
after_lock = db.list_drafts(store_b, registry_dir=reg_b)
check("B: a contract moved past 'draft' by approve_and_lock() no longer "
      "appears in the backlog", {d["contract_id"] for d in after_lock} == {r_b2.contract.id})
check("B: pending_drafts() and list_drafts() agree on which contracts are "
      "still pending (same underlying definition, reused not re-derived)",
      {c.id for c in hi.pending_drafts(reg_b)} == {d["contract_id"] for d in after_lock})


# ---------------------------------------------------------------------------
print("\n--- C: deterministic ordering ---")
# ---------------------------------------------------------------------------

store_c = fresh_store("c")
reg_c = fresh_registry("c")
created_c = [hi.create_draft(store_c, distinct_proposal(i), registry_dir=reg_c) for i in range(4)]

drafts_c1 = db.list_drafts(store_c, registry_dir=reg_c)
drafts_c2 = db.list_drafts(store_c, registry_dir=reg_c)
check("C: two calls against an unchanged store return identical order",
      [d["contract_id"] for d in drafts_c1] == [d["contract_id"] for d in drafts_c2])

# Independently recompute the expected order from the SAME data the backlog
# itself reports (created_at, hypothesis_id) — newest first, hypothesis_id
# ascending tie-break — rather than assuming a wall-clock gap exists between
# each create_draft() call (they may tie at whole-second resolution).
expected_order = sorted(
    drafts_c1, key=lambda d: (d["created_at"] or "", d["hypothesis_id"] or ""))
expected_order = list(reversed(expected_order))
# re-stabilize hypothesis_id ascending within any timestamp tie, exactly as
# list_drafts() itself does (two-stage stable sort) — plain reversal alone
# would also (wrongly) reverse the tie-break order.
expected_order.sort(key=lambda d: d["hypothesis_id"] or "")
expected_order.sort(key=lambda d: (d["created_at"] is not None, d["created_at"] or ""),
                     reverse=True)
check("C: newest-first with a stable hypothesis_id tie-break matches an "
      "independently recomputed expected order",
      [d["contract_id"] for d in drafts_c1] == [d["contract_id"] for d in expected_order])
check("C: every one of the 4 created drafts is present exactly once",
      sorted(d["contract_id"] for d in drafts_c1)
      == sorted(r.contract.id for r in created_c))


# ---------------------------------------------------------------------------
print("\n--- D: bounded output — limit is respected ---")
# ---------------------------------------------------------------------------

store_d = fresh_store("d")
reg_d = fresh_registry("d")
for i in range(5):
    hi.create_draft(store_d, distinct_proposal(i), registry_dir=reg_d)

limited = db.list_drafts(store_d, registry_dir=reg_d, limit=2)
check("D: list_drafts(limit=2) returns exactly 2 entries", len(limited) == 2)

backlog_d = db.build_backlog(store_d, registry_dir=reg_d, limit=2)
check("D: build_backlog reports shown_count=2", backlog_d["shown_count"] == 2)
check("D: build_backlog reports the true total_count=5", backlog_d["total_count"] == 5)
check("D: build_backlog reports truncated=True", backlog_d["truncated"] is True)

unbounded_d = db.build_backlog(store_d, registry_dir=reg_d, limit=None)
check("D: limit=None returns every draft, untruncated",
      unbounded_d["shown_count"] == 5 and unbounded_d["truncated"] is False)


# ---------------------------------------------------------------------------
print("\n--- E: limit=0 produces zero entries, not unbounded ---")
# ---------------------------------------------------------------------------

zero_d = db.build_backlog(store_d, registry_dir=reg_d, limit=0)
check("E: limit=0 shows zero drafts (not treated as falsy-and-therefore-unbounded)",
      zero_d["shown_count"] == 0 and zero_d["drafts"] == [])
check("E: limit=0 still reports the true total_count", zero_d["total_count"] == 5)
check("E: limit=0 reports truncated=True (something real was hidden)",
      zero_d["truncated"] is True)
check("E: list_drafts(limit=0) directly also returns an empty list",
      db.list_drafts(store_d, registry_dir=reg_d, limit=0) == [])


# ---------------------------------------------------------------------------
print("\n--- F: research area appears where available ---")
# ---------------------------------------------------------------------------

store_f = fresh_store("f")
reg_f = fresh_registry("f")
r_f1 = hi.create_draft(store_f, valid_proposal(), registry_dir=reg_f)
r_f2 = hi.create_draft(store_f, distinct_proposal(1), registry_dir=reg_f)
ra.tag_hypothesis(store_f, hypothesis_id=r_f1.hypothesis_id, research_area="momentum",
                   source="test")

drafts_f = {d["contract_id"]: d for d in db.list_drafts(store_f, registry_dir=reg_f)}
check("F: the tagged draft carries its research area",
      drafts_f[r_f1.contract.id]["research_area"] == "momentum")
check("F: the untagged draft carries no research area",
      drafts_f[r_f2.contract.id]["research_area"] is None)

# retagging into a new area — most recent tag wins, same as research_areas.py
ra.tag_hypothesis(store_f, hypothesis_id=r_f1.hypothesis_id, research_area="volatility",
                   source="test")
drafts_f2 = {d["contract_id"]: d for d in db.list_drafts(store_f, registry_dir=reg_f)}
check("F: retagging is reflected as the current area (last write wins)",
      drafts_f2[r_f1.contract.id]["research_area"] == "volatility")


# ---------------------------------------------------------------------------
print("\n--- G: Research AI provenance appears where available ---")
# ---------------------------------------------------------------------------

store_g = fresh_store("g")
reg_g = fresh_registry("g")
r_g1 = hi.create_draft(store_g, valid_proposal(), registry_dir=reg_g,
                        default_source="research_investigator.ai")
r_g2 = hi.create_draft(store_g, distinct_proposal(1), registry_dir=reg_g)

rm.record_discovery_search(
    store_g, hypothesis_id=r_g1.hypothesis_id, discovery_type="research_ai",
    as_of="2024-05-12", version="investigator.v1", source="research_investigator.ai",
    extra={"prompt_path": "research/brain/prompts/investigator.md"})

drafts_g = {d["contract_id"]: d for d in db.list_drafts(store_g, registry_dir=reg_g)}
prov_g1 = drafts_g[r_g1.contract.id]["discovery_provenance"]
check("G: a Research-AI-sourced draft carries discovery provenance",
      prov_g1 is not None)
check("G: provenance discovery_type is exposed", prov_g1["discovery_type"] == "research_ai")
check("G: provenance version is exposed", prov_g1["version"] == "investigator.v1")
check("G: provenance as_of is exposed", prov_g1["as_of"] == "2024-05-12")
check("G: provenance prompt reference is exposed",
      prov_g1["prompt_path"] == "research/brain/prompts/investigator.md")
check("G: a draft with no discovery-search row carries no provenance",
      drafts_g[r_g2.contract.id]["discovery_provenance"] is None)
check("G: the drafted contract's own recorded 'source' field also reflects "
      "where it came from", drafts_g[r_g1.contract.id]["source"] == "research_investigator.ai")


# ---------------------------------------------------------------------------
print("\n--- H: exact duplicate status reuses global similarity behavior ---")
# ---------------------------------------------------------------------------

store_h = fresh_store("h")
reg_h = fresh_registry("h")
p_h = valid_proposal()
r_h1 = hi.create_draft(store_h, p_h, registry_dir=reg_h)
r_h2 = hi.create_draft(store_h, dict(p_h, title="same spec, different claim",
                                      hypothesis="a differently-worded version of the same idea"),
                        registry_dir=reg_h)
r_h3 = hi.create_draft(store_h, distinct_proposal(9), registry_dir=reg_h)  # not a duplicate

drafts_h = {d["contract_id"]: d for d in db.list_drafts(store_h, registry_dir=reg_h)}
check("H: two drafts with the identical rule spec are BOTH flagged exact_duplicate",
      drafts_h[r_h1.contract.id]["exact_duplicate"] is True
      and drafts_h[r_h2.contract.id]["exact_duplicate"] is True)
check("H: each duplicate names the OTHER contract_id, not itself",
      drafts_h[r_h1.contract.id]["duplicate_of_contract_ids"] == [r_h2.contract.id]
      and drafts_h[r_h2.contract.id]["duplicate_of_contract_ids"] == [r_h1.contract.id])
check("H: a draft with a unique rule spec is NOT flagged as an exact duplicate",
      drafts_h[r_h3.contract.id]["exact_duplicate"] is False
      and drafts_h[r_h3.contract.id]["duplicate_of_contract_ids"] == [])
check("H: neither duplicate is reported as already tested yet (no tested "
      "sibling exists)", drafts_h[r_h1.contract.id]["already_tested"] is False)

# now add a THIRD, already-tested (status='reported') contract with the SAME
# rule spec — reusing similarity.py's own registry-wide, any-status scan.
tested_sibling = make_locked_sibling("EXP-TESTED-1", proposal=p_h, status="reported",
                                     registry_dir=reg_h)
drafts_h2 = {d["contract_id"]: d for d in db.list_drafts(store_h, registry_dir=reg_h)}
check("H: once an already-tested sibling with the same exact spec exists, "
      "the still-draft duplicates are reported as already_tested=True",
      drafts_h2[r_h1.contract.id]["already_tested"] is True
      and drafts_h2[r_h2.contract.id]["already_tested"] is True)
check("H: the tested sibling's contract_id shows up among the duplicate set",
      tested_sibling.id in drafts_h2[r_h1.contract.id]["duplicate_of_contract_ids"])
check("H: an unrelated draft's already_tested status is unaffected",
      drafts_h2[r_h3.contract.id]["already_tested"] is False)


# ---------------------------------------------------------------------------
print("\n--- I: related contract IDs are presented without duplicating full definitions ---")
# ---------------------------------------------------------------------------

store_i = fresh_store("i")
reg_i = fresh_registry("i")

# two variants of the SAME hypothesis (siblings by hypothesis_id, distinct
# rule specs so they are not ALSO exact-duplicates of each other)
p_i = valid_proposal()
r_i1 = hi.create_draft(store_i, p_i, registry_dir=reg_i)
r_i2 = hi.create_draft(
    store_i, dict(p_i, hypothesis_id=r_i1.hypothesis_id,
                  entry_rule={"conditions": [{"metric": "volume_zscore", "op": ">", "value": 5.0}]}),
    registry_dir=reg_i)

drafts_i = {d["contract_id"]: d for d in db.list_drafts(store_i, registry_dir=reg_i)}
check("I: a sibling variant of the same hypothesis is named in related_contract_ids",
      r_i2.contract.id in drafts_i[r_i1.contract.id]["related_contract_ids"]
      and r_i1.contract.id in drafts_i[r_i2.contract.id]["related_contract_ids"])
check("I: related_contract_ids never includes the draft's own contract_id",
      r_i1.contract.id not in drafts_i[r_i1.contract.id]["related_contract_ids"])
check("I: related contracts are presented as bare ID strings, never as "
      "expanded Contract objects/dicts",
      all(isinstance(x, str) for x in drafts_i[r_i1.contract.id]["related_contract_ids"]))
check("I: the per-draft entry does not embed a full Contract definition "
      "(no entry_rule/exit_rule/splits keys at the top level)",
      not ({"entry_rule", "exit_rule", "splits"} & set(drafts_i[r_i1.contract.id].keys())))

md_i = db.render(store_i, registry_dir=reg_i)
check("I: the rendered Markdown report cites related contracts by ID, "
      "not by re-printing their full rule definitions",
      r_i2.contract.id in md_i and "entry_rule" not in md_i)


# ---------------------------------------------------------------------------
print("\n--- J: read-only — running the backlog changes nothing ---")
# ---------------------------------------------------------------------------

store_j = fresh_store("j")
reg_j = fresh_registry("j")
for i in range(3):
    hi.create_draft(store_j, distinct_proposal(i), registry_dir=reg_j)
ra.tag_hypothesis(store_j, hypothesis_id="HYP-99990101-deadbeef",
                   research_area="unrelated", source="test")  # unrelated row, for row-count parity

before_registry_fp = _registry_fingerprint(reg_j)
before_registry_ids = sorted(c.id for c in _registry(reg_j))
before_row_counts = {
    ds: len(rm.query_research_log(store_j, ds))
    for ds in (rm.DATASET_HYPOTHESIS, rm.DATASET_RESEARCH_AREA, rm.DATASET_DISCOVERY_SEARCH,
               rm.DATASET_NOTE)
}

_ = db.build_backlog(store_j, registry_dir=reg_j)
_ = db.render(store_j, registry_dir=reg_j)
_ = db.list_drafts(store_j, registry_dir=reg_j, limit=1)

after_registry_fp = _registry_fingerprint(reg_j)
after_registry_ids = sorted(c.id for c in _registry(reg_j))
after_row_counts = {
    ds: len(rm.query_research_log(store_j, ds))
    for ds in (rm.DATASET_HYPOTHESIS, rm.DATASET_RESEARCH_AREA, rm.DATASET_DISCOVERY_SEARCH,
               rm.DATASET_NOTE)
}

check("J: registry files are byte-for-byte unchanged after building and "
      "rendering the backlog", before_registry_fp == after_registry_fp)
check("J: the set of contract ids in the registry is unchanged",
      before_registry_ids == after_registry_ids)
check("J: no Contract's status changed (still all drafts)",
      all(c.status == "draft" for c in _registry(reg_j)))
check("J: no research-memory row counts changed in any dataset (no new "
      "rows were appended by reading the backlog)",
      before_row_counts == after_row_counts)


# ---------------------------------------------------------------------------
print("\n--- K: isolation ---")
# ---------------------------------------------------------------------------

DB_SRC = Path("research/brain/draft_backlog.py").read_text()
DB_BODY = _code_only(DB_SRC)

check("K: draft_backlog.py never imports an engine module",
      not re.search(r"^\s*(?:from|import)\s+engine\b", DB_SRC, re.MULTILINE))
check("K: draft_backlog.py never imports a broker module",
      not re.search(r"^\s*(?:from|import)\s+broker\w*", DB_SRC, re.MULTILINE))
check("K: draft_backlog.py never CALLS approve_and_lock(",
      "approve_and_lock(" not in DB_BODY, DB_BODY)
check("K: draft_backlog.py never calls Contract.lock() (or any .lock() call)",
      ".lock(" not in DB_BODY)
check("K: draft_backlog.py never calls run_experiment(",
      "run_experiment(" not in DB_BODY)
check("K: draft_backlog.py never calls Contract.save() (no writes to the registry)",
      ".save(" not in DB_BODY)
check("K: draft_backlog.py never calls store.append( directly (writes only "
      "ever go through research.memory's typed wrappers, and this module "
      "calls none of those either — 'lines.append(' calls in to_markdown() "
      "are plain Python list appends, unrelated to Store writes)",
      "store.append(" not in DB_BODY)
check("K: draft_backlog.py never calls any of research.memory's write "
      "wrappers (record_hypothesis_proposal / record_research_area_tag / "
      "record_discovery_search / record_research_note / record_anomaly / "
      "record_experiment_verdict / record_evidence_summary)",
      not re.search(r"\brm\.record_\w+\(", DB_BODY))
EVAL_EXEC_RE = re.compile(r"\beval\s*\(|\bexec\s*\(|(?<!re\.)\bcompile\s*\(")
check("K: draft_backlog.py contains no eval/exec/compile call",
      not EVAL_EXEC_RE.search(DB_BODY), DB_BODY)
check("K: draft_backlog.py never imports research.contracts.registry's "
      "write-capable Contract.save/Contract.lock surface beyond reading "
      "fields (no direct 'Contract(' construction in this module)",
      "Contract(" not in DB_BODY)


# ---------------------------------------------------------------------------
print("\n====================================================")
print(f"  {PASSED} passed, {FAILED} failed")
print("====================================================")
sys.exit(1 if FAILED else 0)
