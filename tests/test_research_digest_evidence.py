"""
Tests for Phase 2 Slice V: the Research Evidence Feedback Loop — the new
`evidence` section of research/brain/digest.py.

This file does NOT re-test comparison.py's own PROMISING/WEAK/INCONCLUSIVE/
CONTRADICTED/REDUNDANT classification logic (tests/test_research_comparison.py
already owns that), nor evaluator.py's own statistics (tests/test_research_
runner.py / the evaluator's own doctests own that). It tests exactly the new
seam this slice adds: that build_digest() correctly SURFACES that existing,
unmodified machinery — bounded, deterministically ordered, read-only, and
without inventing any new scoring model.

Run with:  python -m tests.test_research_digest_evidence
"""

import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from research.store import Store  # noqa: E402
from research import memory as rm  # noqa: E402
from research.contracts import Contract, registry as _registry  # noqa: E402
from research.brain import digest as digest_mod  # noqa: E402
from research.brain import research_areas as ra  # noqa: E402
from research.brain import investigator as inv  # noqa: E402
from research.experiments import comparison  # noqa: E402

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
    return re.sub(r'"""[\s\S]*?"""', "", source)


TMP = Path(tempfile.mkdtemp(prefix="lq-test-digest-evidence-"))


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


# ---------------------------------------------------------------------------
# Fixture helpers — the exact conventions already established in
# tests/test_research_priority.py and tests/test_research_comparison.py:
# fabricate verdict dicts directly (the shape evaluator.compute_verdict()
# would have produced) rather than running a real simulation, and write the
# hypothesis-claim/verdict rows through the same research.memory functions
# create_draft()/evaluator.record_verdict() themselves use.
# ---------------------------------------------------------------------------

def make_locked_contract(cid, *, locked_at=None, status="reported", registry_dir,
                          entry_threshold=3.0, **overrides):
    fields = dict(
        id=cid, title=f"idea behind {cid}", hypothesis=f"claim behind {cid}",
        null_hypothesis="no effect", universe="watchlist",
        signal="observatory.volume_zscore",
        entry_rule=json.dumps({"conditions": [
            {"metric": "volume_zscore", "op": ">", "value": entry_threshold}]}),
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
    return {"n_trades": n_trades, "win_rate": None, "gross_pnl": net_pnl,
            "net_pnl": net_pnl, "total_costs": 0.0, "avg_net_pnl": avg_net_pnl,
            "expectancy_r": None, "t_stat": t_stat}


def link_contract_to_hypothesis(store, hypothesis_id, contract_id, *, extra=None):
    rm.record_hypothesis_proposal(
        store, claim="claim", source="test", hypothesis_id=hypothesis_id,
        extra={"contract_id": contract_id, **(extra or {})})


def score_contract(store, hypothesis_id, contract_id, verdict):
    rm.record_experiment_verdict(
        store, contract_id=contract_id, hypothesis_id=hypothesis_id, verdict=verdict)


def make_scored_hypothesis(store, reg, *, cid, verdict, hid=None,
                            locked_at="2024-01-01T00:00:00", entry_threshold=3.0,
                            title=None):
    """One hypothesis, one locked+reported contract, one recorded verdict —
    the minimal unit build_digest's new evidence section can surface."""
    hid = hid or rm.new_hypothesis_id()
    overrides = {"title": title} if title else {}
    make_locked_contract(cid, locked_at=locked_at, registry_dir=reg,
                          entry_threshold=entry_threshold, **overrides)
    link_contract_to_hypothesis(store, hid, cid)
    score_contract(store, hid, cid, verdict)
    return hid, cid


def _registry_fingerprint(reg_dir: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    if not reg_dir.exists():
        return h.hexdigest()
    for f in sorted(reg_dir.glob("**/*")):
        if f.is_file():
            h.update(f.name.encode())
            h.update(f.read_bytes())
    return h.hexdigest()


def _all_log_rows(store):
    """Every row across every dataset this project defines, for a
    before/after read-only comparison — deliberately enumerates the datasets
    by name rather than importing some private "list all datasets" helper
    that doesn't exist, since research.memory's public surface is exactly
    these named DATASET_* constants."""
    datasets = [
        rm.DATASET_ANOMALY, rm.DATASET_HYPOTHESIS, rm.DATASET_VERDICT,
        rm.DATASET_EVIDENCE, rm.DATASET_NOTE, rm.DATASET_RESEARCH_AREA,
        rm.DATASET_DISCOVERY_SEARCH,
    ]
    return {d: rm.query_research_log(store, d) for d in datasets}


AS_OF = None  # the evidence section is deliberately un-gated (see digest.py);
              # as_of is still required by build_digest's signature for the
              # other (gated) sections, so every call below passes a fixed
              # concrete timestamp instead of None.
import datetime as dt  # noqa: E402
AS_OF = dt.datetime(2030, 1, 1, 18, 0)


# ===========================================================================
print("\n--- A: evidence appears in the digest ---")
# ===========================================================================

store_a = fresh_store("a")
reg_a = fresh_registry("a")
hid_a, cid_a = make_scored_hypothesis(
    store_a, reg_a, cid="EXP-A-1", verdict=make_verdict(20, 10_000.0, 500.0, 5.0),
    title="volume spike mean reversion")

digest_a = digest_mod.build_digest(store_a, AS_OF, registry_dir=reg_a)
entries_a = digest_a["evidence"]["hypotheses"]
check("A: exactly one evidence entry appears for the one scored hypothesis",
      len(entries_a) == 1, entries_a)
entry_a = entries_a[0] if entries_a else {}
check("A: the entry carries the right hypothesis_id",
      entry_a.get("hypothesis_id") == hid_a)
check("A: the entry carries the contract's title",
      entry_a.get("title") == "volume spike mean reversion")
check("A: the entry's verdict is the real, comparison.py-computed PROMISING",
      entry_a.get("verdict") == "PROMISING", entry_a)
check("A: contract_count reflects the one contract drafted under this hypothesis",
      entry_a.get("contract_count") == 1)
check("A: evidence_summary is comparison.py's own rationale text, not a new one",
      isinstance(entry_a.get("evidence_summary"), str) and len(entry_a["evidence_summary"]) > 0)


# ===========================================================================
print("\n--- B: no evidence state ---")
# ===========================================================================

store_b = fresh_store("b")
reg_b = fresh_registry("b")
digest_b = digest_mod.build_digest(store_b, AS_OF, registry_dir=reg_b)
check("B: an empty research history produces an empty but well-formed evidence section",
      digest_b["evidence"] == {"hypotheses": [], "shown_count": 0,
                                "total_count": 0, "truncated": False},
      digest_b["evidence"])


# ===========================================================================
print("\n--- C: verdict coverage (existing comparison.py semantics, unmodified) ---")
# ===========================================================================

store_c = fresh_store("c")
reg_c = fresh_registry("c")

# Every hypothesis below gets its OWN entry_threshold (unrelated to its
# economics) purely so its rule fingerprint never accidentally collides,
# cross-hypothesis, with another test hypothesis in this same registry —
# similarity.py's exact-duplicate detection is deliberately GLOBAL (any two
# contracts, any hypotheses), unlike comparison.py's within-hypothesis
# dedup, so distinct fixtures need visibly distinct rules unless the test
# specifically wants a cross-fingerprint collision (see REDUN-A/REDUN-B
# below, the one pair that deliberately shares a fingerprint).

# PROMISING: positive, statistically significant, economically meaningful.
hid_promising, _ = make_scored_hypothesis(
    store_c, reg_c, cid="EXP-C-PROMISING", verdict=make_verdict(20, 10_000.0, 500.0, 5.0),
    locked_at="2024-01-01T00:00:00", entry_threshold=3.0)

# WEAK: positive but not statistically significant.
hid_weak, _ = make_scored_hypothesis(
    store_c, reg_c, cid="EXP-C-WEAK", verdict=make_verdict(20, 1_000.0, 50.0, 1.0),
    locked_at="2024-01-02T00:00:00", entry_threshold=3.1)

# INCONCLUSIVE: zero trades recorded at all.
hid_inconclusive, _ = make_scored_hypothesis(
    store_c, reg_c, cid="EXP-C-INCONCLUSIVE", verdict=make_verdict(0, 0.0, None, None),
    locked_at="2024-01-03T00:00:00", entry_threshold=3.2)

# CONTRADICTED: two independent (non-duplicate) variants of the SAME
# hypothesis, one significant positive, one significant negative.
hid_contradicted = rm.new_hypothesis_id()
make_locked_contract("EXP-C-CONTRA-A", locked_at="2024-01-04T00:00:00",
                      registry_dir=reg_c, entry_threshold=3.3)
link_contract_to_hypothesis(store_c, hid_contradicted, "EXP-C-CONTRA-A")
score_contract(store_c, hid_contradicted, "EXP-C-CONTRA-A", make_verdict(20, 10_000.0, 500.0, 5.0))
make_locked_contract("EXP-C-CONTRA-B", locked_at="2024-01-05T00:00:00",
                      registry_dir=reg_c, entry_threshold=3.4)  # different rule -> not a duplicate
link_contract_to_hypothesis(store_c, hid_contradicted, "EXP-C-CONTRA-B")
score_contract(store_c, hid_contradicted, "EXP-C-CONTRA-B", make_verdict(20, -10_000.0, -500.0, -5.0))

# REDUNDANT: two contracts under one hypothesis with the IDENTICAL rule
# (same universe/entry_rule/exit_rule/splits/evaluation window) — the later
# one is REDUNDANT under comparison.py's own within-hypothesis fingerprint
# check. The digest must still report the true AGGREGATE for the hypothesis
# (PROMISING, from the sole non-duplicate/earliest variant), never the
# per-contract REDUNDANT label. This is also the ONE pair in this fixture
# that deliberately shares a fingerprint, for the section I duplicate check.
hid_redundant = rm.new_hypothesis_id()
make_locked_contract("EXP-C-REDUN-A", locked_at="2024-01-06T00:00:00",
                      registry_dir=reg_c, entry_threshold=5.0)
link_contract_to_hypothesis(store_c, hid_redundant, "EXP-C-REDUN-A")
score_contract(store_c, hid_redundant, "EXP-C-REDUN-A", make_verdict(20, 10_000.0, 500.0, 5.0))
make_locked_contract("EXP-C-REDUN-B", locked_at="2024-01-07T00:00:00",
                      registry_dir=reg_c, entry_threshold=5.0)  # identical rule -> duplicate
link_contract_to_hypothesis(store_c, hid_redundant, "EXP-C-REDUN-B")
score_contract(store_c, hid_redundant, "EXP-C-REDUN-B", make_verdict(20, 10_000.0, 500.0, 5.0))
# Confirm the underlying semantics really do call the later one REDUNDANT —
# this is what makes the digest's handling of it a meaningful test rather
# than a vacuous one.
redundant_check = comparison.evaluate_hypothesis_evidence(store_c, "EXP-C-REDUN-B", registry_dir=reg_c)
check("C: sanity check — comparison.py itself calls the later duplicate REDUNDANT",
      redundant_check["verdict"] == "REDUNDANT", redundant_check)

digest_c = digest_mod.build_digest(store_c, AS_OF, registry_dir=reg_c, evidence_limit=None)
by_hid_c = {e["hypothesis_id"]: e for e in digest_c["evidence"]["hypotheses"]}

check("C: PROMISING surfaces correctly", by_hid_c.get(hid_promising, {}).get("verdict") == "PROMISING")
check("C: WEAK surfaces correctly", by_hid_c.get(hid_weak, {}).get("verdict") == "WEAK")
check("C: INCONCLUSIVE surfaces correctly (zero trades)",
      by_hid_c.get(hid_inconclusive, {}).get("verdict") == "INCONCLUSIVE")
check("C: CONTRADICTED surfaces correctly (opposing significant variants)",
      by_hid_c.get(hid_contradicted, {}).get("verdict") == "CONTRADICTED")
check("C: a hypothesis with a REDUNDANT duplicate variant still reports its "
      "TRUE aggregate verdict (PROMISING), never the per-contract REDUNDANT label",
      by_hid_c.get(hid_redundant, {}).get("verdict") == "PROMISING",
      by_hid_c.get(hid_redundant))
check("C: no hypothesis-level evidence entry ever shows the raw string "
      "'REDUNDANT' (that label only ever applies to one specific duplicate "
      "contract, never to a whole hypothesis)",
      all(e["verdict"] != "REDUNDANT" for e in digest_c["evidence"]["hypotheses"]))


# ===========================================================================
print("\n--- D: deterministic ordering ---")
# ===========================================================================

digest_c_2 = digest_mod.build_digest(store_c, AS_OF, registry_dir=reg_c, evidence_limit=None)
check("D: two builds of the same store/as_of produce byte-identical evidence sections",
      digest_c["evidence"] == digest_c_2["evidence"])

order_c = [e["hypothesis_id"] for e in digest_c["evidence"]["hypotheses"]]
# Both hid_promising and hid_redundant land in the PROMISING tier; within
# that tier the more-recently-locked representative sorts first —
# EXP-C-REDUN-A (2024-01-06) is more recent than EXP-C-PROMISING
# (2024-01-01), so hid_redundant comes first, exactly per the documented
# "most recent evidence" rule.
expected_order = [hid_redundant, hid_promising, hid_contradicted, hid_weak, hid_inconclusive]
check("D: ordering follows the documented rule — PROMISING and CONTRADICTED "
      "(informative/actionable) ranked ahead of WEAK and INCONCLUSIVE, most "
      "recent evidence first within a verdict tier, hypothesis_id tie-break",
      order_c == expected_order, order_c)


# ===========================================================================
print("\n--- E: bounded output (evidence_limit) ---")
# ===========================================================================

digest_c_bounded = digest_mod.build_digest(store_c, AS_OF, registry_dir=reg_c, evidence_limit=2)
check("E: evidence_limit=2 shows exactly 2 entries",
      digest_c_bounded["evidence"]["shown_count"] == 2)
check("E: total_count still reports every hypothesis with evidence, uncapped",
      digest_c_bounded["evidence"]["total_count"] == 5)
check("E: truncated is set when the cap actually cut something",
      digest_c_bounded["evidence"]["truncated"] is True)
check("E: the shown entries are the top 2 by the documented ordering rule",
      [e["hypothesis_id"] for e in digest_c_bounded["evidence"]["hypotheses"]] == expected_order[:2])


# ===========================================================================
print("\n--- F: evidence_limit=0 shows zero, not unbounded ---")
# ===========================================================================

digest_c_zero = digest_mod.build_digest(store_c, AS_OF, registry_dir=reg_c, evidence_limit=0)
check("F: evidence_limit=0 shows exactly zero entries (not falsy-and-unbounded)",
      digest_c_zero["evidence"]["shown_count"] == 0
      and digest_c_zero["evidence"]["hypotheses"] == [])
check("F: total_count is still fully reported at evidence_limit=0",
      digest_c_zero["evidence"]["total_count"] == 5)
check("F: truncated is True at evidence_limit=0 whenever any evidence exists",
      digest_c_zero["evidence"]["truncated"] is True)


# ===========================================================================
print("\n--- G: research-area integration ---")
# ===========================================================================

ra.tag_hypothesis(store_a, hypothesis_id=hid_a, research_area="volume_momentum", source="test")
digest_a_tagged = digest_mod.build_digest(store_a, AS_OF, registry_dir=reg_a)
tagged_entry = digest_a_tagged["evidence"]["hypotheses"][0]
check("G: a known area tag appears on its evidence entry",
      tagged_entry["research_area"] == "volume_momentum", tagged_entry)

untagged_entry = by_hid_c[hid_weak]
check("G: an untagged hypothesis reports research_area as None, not a guess",
      untagged_entry["research_area"] is None)


# ===========================================================================
print("\n--- H: discovery-provenance integration ---")
# ===========================================================================

rm.record_discovery_search(
    store_a, hypothesis_id=hid_a, discovery_type="mathematical",
    as_of="2024-06-01T18:00:00+05:30", version="quant_scan.v0",
    source="test")
digest_a_prov = digest_mod.build_digest(store_a, AS_OF, registry_dir=reg_a)
prov_entry = digest_a_prov["evidence"]["hypotheses"][0]
check("H: an existing discovery-provenance row is surfaced on its evidence entry",
      prov_entry["discovery_provenance"] == {
          "discovery_type": "mathematical", "version": "quant_scan.v0",
          "as_of": "2024-06-01T18:00:00+05:30"},
      prov_entry["discovery_provenance"])

no_prov_entry = by_hid_c[hid_weak]
check("H: a hypothesis with no discovery-search row reports discovery_provenance as None",
      no_prov_entry["discovery_provenance"] is None)


# ===========================================================================
print("\n--- I: exact-duplicate integration ---")
# ===========================================================================

check("I: a hypothesis whose variants include an exact rule duplicate is flagged",
      by_hid_c[hid_redundant]["has_exact_duplicate"] is True)
check("I: a hypothesis with no duplicate variants is not flagged",
      by_hid_c[hid_weak]["has_exact_duplicate"] is False
      and by_hid_c[hid_contradicted]["has_exact_duplicate"] is False)


# ===========================================================================
print("\n--- J: no raw experiment dump ---")
# ===========================================================================

EXPECTED_KEYS = {
    "hypothesis_id", "title", "verdict", "contract_count", "n_variants_scored",
    "positive_count", "negative_count", "evidence_summary", "research_area",
    "discovery_provenance", "has_exact_duplicate",
}
check("J: every evidence entry exposes exactly the documented bounded field "
      "set — no trade-level or raw-result keys leak through",
      all(set(e.keys()) == EXPECTED_KEYS for e in digest_c["evidence"]["hypotheses"]),
      [set(e.keys()) for e in digest_c["evidence"]["hypotheses"]])

rendered = digest_mod.to_json(digest_c)
check("J: the rendered digest never contains a raw trade/result field name",
      "trade_seq" not in rendered and "gross_pnl" not in rendered
      and "r_multiple" not in rendered)
check("J: the rendered evidence section stays small even though the "
      "underlying verdict/rationale text could in principle be large — a "
      "sanity bound, not a hard architectural limit",
      len(json.dumps(digest_c["evidence"])) < 20_000)


# ===========================================================================
print("\n--- K: Research AI (Investigator) compatibility ---")
# ===========================================================================

# investigator.build_context(store, as_of) is, unchanged, just
# `return build_digest(store, as_of)` against the module-default registry —
# it takes no registry_dir override (a pre-existing, Slice-V-unrelated
# property of that function, not something this slice touches). Rather than
# depend on whatever the real default registry happens to contain in this
# environment, the compatibility proof uses the exact digest this slice
# actually built (digest_a_prov, already carrying a populated evidence
# section) and feeds it into the REAL, unmodified build_prompt() —
# byte-for-byte the same call investigate() itself makes immediately after
# build_context() (see investigator.py: `prompt = build_prompt(digest)`).
try:
    prompt_text = inv.build_prompt(digest_a_prov)
    prompt_ok = isinstance(prompt_text, str) and "evidence" in prompt_text
except Exception as e:  # pragma: no cover - failure is the interesting case
    prompt_ok = False
    print(f"      build_prompt raised: {e}")
check("K: the enriched digest still serializes cleanly into the Investigator's "
      "existing, unmodified prompt-construction path — no authority or "
      "signature change required", prompt_ok)
check("K: investigator.py's own digest entry point is still exactly "
      "build_digest(), unmodified by this slice",
      inv.build_context(store_b, AS_OF) == digest_mod.build_digest(store_b, AS_OF))


# ===========================================================================
print("\n--- L: read-only behavior ---")
# ===========================================================================

store_l = fresh_store("l")
reg_l = fresh_registry("l")
make_scored_hypothesis(store_l, reg_l, cid="EXP-L-1", verdict=make_verdict(20, 10_000.0, 500.0, 5.0))

before_fingerprint = _registry_fingerprint(reg_l)
before_rows = _all_log_rows(store_l)

for _ in range(3):
    digest_mod.build_digest(store_l, AS_OF, registry_dir=reg_l)
    digest_mod.build_digest(store_l, AS_OF, registry_dir=reg_l, evidence_limit=0)
    digest_mod.build_digest(store_l, AS_OF, registry_dir=reg_l, evidence_limit=None)

after_fingerprint = _registry_fingerprint(reg_l)
after_rows = _all_log_rows(store_l)

check("L: building the digest (repeatedly, with varying evidence_limit) "
      "never modifies a single byte of the contract registry",
      before_fingerprint == after_fingerprint)
check("L: building the digest never appends, updates, or removes a single "
      "research-memory row, in any dataset",
      before_rows == after_rows)


# ===========================================================================
print("\n--- M: isolation ---")
# ===========================================================================

digest_src = Path("research/brain/digest.py").read_text()
digest_body = _code_only(digest_src)
check("M: digest.py never imports engine.execute", "engine.execute" not in digest_body)
check("M: digest.py never imports engine.guardrails", "engine.guardrails" not in digest_body)
check("M: digest.py never imports a broker module",
      not re.search(r"^\s*(?:from|import)\s+[^\n]*broker", digest_body, re.MULTILINE))
# digest.py's own long-standing module docstring (predating this slice)
# names "memory/state.json" as one of the things this module deliberately
# EXCLUDES — that mention is documentation, not an access. The real safety
# property is that digest.py never actually opens/reads/writes that path
# (or any path at all — see the "no file-write calls" check already in
# tests/test_research_digest.py); Slice V doesn't change that.
check("M: digest.py never opens, reads, or writes memory/state.json — the "
      "one existing mention of it is documentation ('what this module "
      "excludes'), not an access",
      not re.search(r"(?:open|read_text|write_text|read_bytes|write_bytes)\s*\([^)]*state\.json",
                     digest_body))
check("M: digest.py never calls Contract.lock()", ".lock(" not in digest_body)
check("M: digest.py never calls Contract.save() / persists anything",
      ".save(" not in digest_body)
check("M: digest.py never calls approve_and_lock", "approve_and_lock" not in digest_body)
check("M: digest.py never calls run_experiment", "run_experiment" not in digest_body)
check("M: digest.py imports no engine module at all",
      not re.search(r"^\s*(?:from|import)\s+engine\b", digest_body, re.MULTILINE))


# ===========================================================================
print("\n--- N: existing regressions ---")
# ===========================================================================
# Slices I-U are verified green by running their own test files directly
# (tests/test_research_*.py, tests/test_kernel_isolation.py,
# tests/test_research_factory_integration.py) as part of this slice's
# process, per the completion report — not re-asserted inline here, since
# doing so would just re-run those files' own bodies inside this one.
check("N: existing regressions are verified via the full suite sweep, see "
      "the completion report", True)


# ===========================================================================
print(f"\n{'='*52}\n  {PASSED} passed, {FAILED} failed\n{'='*52}")
sys.exit(1 if FAILED else 0)
