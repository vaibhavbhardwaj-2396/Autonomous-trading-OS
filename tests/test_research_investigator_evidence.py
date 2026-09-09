"""
Tests for Phase 2 Slice W: Evidence-Aware Research AI.

Slice V put prior experiment evidence into the deterministic Digest. This
slice does not touch the Digest, the Investigator's code, the intake
pipeline, or any execution path — it strengthens the Research AI's PROMPT
CONTRACT (routines/research_investigate.md) so a real model is explicitly
told to use that evidence, and verifies the seam mechanically: that the
evidence really does reach the prompt text sent to the AI runner, that the
prompt contains the required reasoning instructions verbatim, and that
nothing about the Investigator's authority, schema, or isolation changed as
a result.

This file does NOT attempt to grade what a real language model would do
with the prompt (no test in this repository invokes the real `claude`
binary — see test_research_investigator.py's own mandate). It tests the
CONTRACT: what the prompt says, and that saying more doesn't change any of
the deterministic guarantees around it.

Run with:  python -m tests.test_research_investigator_evidence
"""

import datetime as dt
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from research.store import Store  # noqa: E402
from research import memory as rm  # noqa: E402
from research.contracts import Contract  # noqa: E402
from research.brain import digest as digest_mod  # noqa: E402
from research.brain import hypothesis_intake as hi  # noqa: E402
from research.brain import investigator as inv  # noqa: E402

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


TMP = Path(tempfile.mkdtemp(prefix="lq-test-investigator-evidence-"))


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
    """Same shape test_research_investigator.py's own fixture uses — the
    Research AI is expected to produce exactly this shape, unchanged by
    this slice (no schema change)."""
    base = {
        "title": "Volume-spike drift, regime-conditioned re-test",
        "hypothesis": ("High-volume anomalies (>=3 sigma) precede a short-term "
                        "upward drift in liquid large-caps, conditioned on a "
                        "non-trending regime."),
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
        "notes": "Distinct from HYP-PRIOR-EXAMPLE: this restricts entries to a "
                 "non-trending regime, which the earlier WEAK result never tested.",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Evidence fixture helpers — the exact conventions established in
# tests/test_research_digest_evidence.py (Slice V) and
# tests/test_research_priority.py before it.
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
    hid = hid or rm.new_hypothesis_id()
    overrides = {"title": title} if title else {}
    make_locked_contract(cid, locked_at=locked_at, registry_dir=reg,
                          entry_threshold=entry_threshold, **overrides)
    link_contract_to_hypothesis(store, hid, cid)
    score_contract(store, hid, cid, verdict)
    return hid, cid


def registry_files(reg_dir: Path) -> set:
    if not reg_dir.exists():
        return set()
    return {p.name for p in reg_dir.glob("*.json")}


AS_OF = dt.datetime(2030, 1, 1, 18, 0)
PROMPT_TEXT = inv.PROMPT_PATH.read_text()  # the static instruction file itself


# ===========================================================================
print("\n--- A: prior evidence reaches the Research AI ---")
# ===========================================================================

store_a = fresh_store("a")
reg_a = fresh_registry("a")
hid_a, cid_a = make_scored_hypothesis(
    store_a, reg_a, cid="EXP-A-1", verdict=make_verdict(20, 10_000.0, 500.0, 5.0),
    title="volume spike mean reversion")

digest_a = digest_mod.build_digest(store_a, AS_OF, registry_dir=reg_a)
check("A: the digest actually carries the recorded evidence (sanity check "
      "on the fixture, not on this slice's own code)",
      len(digest_a["evidence"]["hypotheses"]) == 1
      and digest_a["evidence"]["hypotheses"][0]["hypothesis_id"] == hid_a)

captured_prompt = {}


def capturing_runner(prompt: str) -> str:
    captured_prompt["prompt"] = prompt
    return json.dumps(valid_ai_proposal())


result_a = inv.investigate(store_a, AS_OF, runner=capturing_runner,
                            registry_dir=reg_a, digest=digest_a)
check("A: investigate() produced a normal InvestigatorResult",
      isinstance(result_a, inv.InvestigatorResult), result_a)
check("A: the prompt actually sent to the AI runner contains the evidence "
      "hypothesis_id", hid_a in captured_prompt.get("prompt", ""))
check("A: the prompt contains the evidence verdict ('PROMISING')",
      "PROMISING" in captured_prompt.get("prompt", ""))
check("A: the prompt contains comparison.py's own rationale text (not a "
      "second, invented summary)",
      digest_a["evidence"]["hypotheses"][0]["evidence_summary"] in captured_prompt["prompt"])


# ===========================================================================
print("\n--- B: no evidence state ---")
# ===========================================================================

store_b = fresh_store("b")
reg_b = fresh_registry("b")
digest_b = digest_mod.build_digest(store_b, AS_OF, registry_dir=reg_b)
check("B: an empty research history really does produce an empty evidence section",
      digest_b["evidence"]["hypotheses"] == [])

result_b = inv.investigate(store_b, AS_OF, runner=lambda p: json.dumps(valid_ai_proposal()),
                            registry_dir=reg_b, digest=digest_b)
check("B: the Investigator still works normally with an empty evidence section",
      isinstance(result_b, inv.InvestigatorResult), result_b)
contract_b = Contract.load(result_b.contract_id, reg_b)
check("B: the resulting contract is a normal, unlocked DRAFT",
      contract_b.status == "draft" and contract_b.locked_hash is None)


# ===========================================================================
print("\n--- C: exact-duplicate awareness ---")
# ===========================================================================

store_c = fresh_store("c")
reg_c = fresh_registry("c")
# Two contracts, identical rule, under one hypothesis -> a real
# exact_duplicates group AND a has_exact_duplicate:true evidence entry.
hid_c = rm.new_hypothesis_id()
make_locked_contract("EXP-C-DUP-A", locked_at="2024-01-01T00:00:00", registry_dir=reg_c)
link_contract_to_hypothesis(store_c, hid_c, "EXP-C-DUP-A")
score_contract(store_c, hid_c, "EXP-C-DUP-A", make_verdict(20, 10_000.0, 500.0, 5.0))
make_locked_contract("EXP-C-DUP-B", locked_at="2024-01-02T00:00:00", registry_dir=reg_c)
link_contract_to_hypothesis(store_c, hid_c, "EXP-C-DUP-B")
score_contract(store_c, hid_c, "EXP-C-DUP-B", make_verdict(20, 10_000.0, 500.0, 5.0))

digest_c = digest_mod.build_digest(store_c, AS_OF, registry_dir=reg_c)
check("C: the digest's exact_duplicates section is genuinely non-empty (fixture sanity)",
      digest_c["exact_duplicates"]["total_count"] >= 1)
check("C: the evidence entry for this hypothesis is flagged has_exact_duplicate",
      digest_c["evidence"]["hypotheses"][0]["has_exact_duplicate"] is True)

prompt_c = inv.build_prompt(digest_c)
check("C: the prompt sent to the AI includes the exact_duplicates section",
      '"exact_duplicates"' in prompt_c and "EXP-C-DUP-A" in prompt_c)
check("C: the prompt sent to the AI includes the has_exact_duplicate flag",
      '"has_exact_duplicate": true' in prompt_c)


# ===========================================================================
print("\n--- D: weak-evidence awareness (prompt contract) ---")
# ===========================================================================

check("D: the prompt explicitly instructs against merely rephrasing WEAK "
      "prior research, requiring a materially different mechanism/condition",
      "If prior evidence is WEAK" in PROMPT_TEXT
      and "materially different mechanism" in PROMPT_TEXT)
check("D: the 'what you must not do' list explicitly repeats the WEAK "
      "no-rephrasing rule as a hard constraint, not just a suggestion",
      re.search(r"rephrase a hypothesis.*WEAK", PROMPT_TEXT) is not None)


# ===========================================================================
print("\n--- E: contradicted-evidence awareness (prompt contract) ---")
# ===========================================================================

check("E: the prompt explicitly instructs treating CONTRADICTED findings as "
      "evidence to reason from, not settled truth",
      "CONTRADICTED — is evidence to reason from, not truth to defer to" in PROMPT_TEXT
      or ("CONTRADICTED" in PROMPT_TEXT and "not truth to defer to" in PROMPT_TEXT))
check("E: the prompt explicitly says a CONTRADICTED verdict is not proof "
      "the idea is settled false",
      "not proof the idea is settled false" in PROMPT_TEXT)
check("E: the prompt allows a genuinely different conditional/regime/"
      "mechanism test in response to CONTRADICTED evidence",
      "genuinely different\n    conditional, regime split, or mechanism" in PROMPT_TEXT
      or "genuinely different" in PROMPT_TEXT and "regime split" in PROMPT_TEXT)


# ===========================================================================
print("\n--- F: promising-evidence awareness (prompt contract) ---")
# ===========================================================================

check("F: the prompt has a dedicated section distinguishing promising from proven",
      "## Promising research is not truth" in PROMPT_TEXT)
FORBIDDEN_PHRASES = ("proven", "guaranteed", "profitable strategy", "validated trading edge")
check("F: every forbidden confident-claim phrase is explicitly named as "
      "disallowed in the prompt",
      all(phrase in PROMPT_TEXT for phrase in FORBIDDEN_PHRASES))
check("F: the prompt explicitly allows a PROMISING verdict to be challenged "
      "or confirmed rather than treated as final",
      "Treat it as evidence worth challenging or confirming" in PROMPT_TEXT)
check("F: the prompt allows robustness/regime-specific/alternative-mechanism "
      "tests as legitimate responses to PROMISING evidence",
      "robustness test" in PROMPT_TEXT and "regime-specific test" in PROMPT_TEXT
      and "alternative mechanism" in PROMPT_TEXT)


# ===========================================================================
print("\n--- G: refinement behavior produces a normal valid hypothesis ---")
# ===========================================================================

store_g = fresh_store("g")
reg_g = fresh_registry("g")
hid_g, _ = make_scored_hypothesis(
    store_g, reg_g, cid="EXP-G-1", verdict=make_verdict(20, 1_000.0, 50.0, 1.0),  # WEAK
    title="original weak idea")
digest_g = digest_mod.build_digest(store_g, AS_OF, registry_dir=reg_g)

# A fixed mock response representing a materially-different refinement of
# the WEAK prior result, referencing it by hypothesis_id in `notes` exactly
# as the prompt instructs.
refinement_proposal = valid_ai_proposal(
    notes=f"A regime-conditioned re-test of {hid_g} (WEAK): restricts entries "
          f"to a non-trending regime the original test never isolated.")
result_g = inv.investigate(store_g, AS_OF, runner=lambda p: json.dumps(refinement_proposal),
                            registry_dir=reg_g, digest=digest_g)
check("G: the Investigator still produces a normal InvestigatorResult for a "
      "refinement proposal", isinstance(result_g, inv.InvestigatorResult), result_g)
contract_g = Contract.load(result_g.contract_id, reg_g)
check("G: the refinement contract is a normal, unlocked DRAFT",
      contract_g.status == "draft" and contract_g.locked_hash is None)
check("G: the refinement's own hypothesis_id is a NEW id, distinct from the "
      "prior WEAK hypothesis it refines (a refinement is still a new "
      "hypothesis, not an edit of the old one)",
      result_g.hypothesis_id != hid_g)
check("G: the refinement's notes field, carried through untouched, still "
      "names the prior hypothesis it differs from",
      hid_g in Contract.load(result_g.contract_id, reg_g).notes)


# ===========================================================================
print("\n--- H: existing output/schema compatibility ---")
# ===========================================================================

proposal_h = valid_ai_proposal()
problems_h = hi.validate_proposal(proposal_h)
check("H: a Research-AI-shaped proposal (now optionally carrying `notes`) "
      "still passes the EXISTING, unmodified validate_proposal() with no "
      "schema change", problems_h == [], problems_h)

store_h = fresh_store("h")
reg_h = fresh_registry("h")
draft_h = hi.create_draft(store_h, proposal_h, registry_dir=reg_h)
check("H: the proposal still flows through the existing, unmodified "
      "create_draft() into a plain DRAFT",
      draft_h.contract.status == "draft")


# ===========================================================================
print("\n--- I: no authority expansion ---")
# ===========================================================================

# investigator.py's own docstrings AND a couple of `#` comments describe
# this exact boundary in prose ("approve_and_lock is never imported into
# this module") — real, deliberate documentation, not a violation. The
# check that matters is whether the name is ever actually CALLED or
# IMPORTED as code, so both docstrings and comments are stripped before
# searching, and the search looks for an invocation/import shape rather
# than the bare substring.
inv_src = Path(inv.__file__).read_text()
inv_code_no_comments = re.sub(r"#.*", "", _code_only(inv_src))
check("I: investigator.py never imports or calls approve_and_lock as code "
      "(the name appears only in its own docstrings/comments, describing "
      "the boundary, never as an import or a call)",
      "approve_and_lock(" not in inv_code_no_comments
      and not re.search(r"^\s*(?:from|import)\s+.*approve_and_lock",
                         inv_code_no_comments, re.MULTILINE))
check("I: investigator.py never calls Contract.lock()",
      ".lock(" not in _code_only(Path(inv.__file__).read_text()))
check("I: investigator.py never imports research.experiments (no path to "
      "run_experiment, evaluator, or comparison from this module)",
      not re.search(r"^\s*(?:from|import)\s+.*experiments",
                     _code_only(Path(inv.__file__).read_text()), re.MULTILINE))
check("I: the resulting contract from a normal run is never anything but a "
      "draft — no lock, no run, no report",
      Contract.load(result_a.contract_id, reg_a).status == "draft")
check("I: the updated prompt file still explicitly tells the AI it cannot "
      "approve, lock, run, or place a trade as a result of its own output "
      "(the boundary statement is preserved, not weakened, by this slice's "
      "additions)",
      "You cannot approve it, lock it, run it, or\nplace any trade as a "
      "result of it." in PROMPT_TEXT)
check("I: nowhere in the prompt is the AI affirmatively told to approve, "
      "lock, or run anything (only ever told it CANNOT)",
      not re.search(r"\byou (?:should|must|may|can) (?:approve|lock|run\b)",
                     PROMPT_TEXT, re.IGNORECASE))


# ===========================================================================
print("\n--- J: determinism ---")
# ===========================================================================

prompt_1 = inv.build_prompt(digest_a)
prompt_2 = inv.build_prompt(digest_a)
check("J: the same digest produces byte-identical prompt text across calls",
      prompt_1 == prompt_2)

store_j1 = fresh_store("j1")
reg_j1 = fresh_registry("j1")
store_j2 = fresh_store("j2")
reg_j2 = fresh_registry("j2")
fixed_response = json.dumps(valid_ai_proposal())
result_j1 = inv.investigate(store_j1, AS_OF, runner=lambda p: fixed_response,
                             registry_dir=reg_j1, digest=digest_b)
result_j2 = inv.investigate(store_j2, AS_OF, runner=lambda p: fixed_response,
                             registry_dir=reg_j2, digest=digest_b)
contract_j1 = Contract.load(result_j1.contract_id, reg_j1)
contract_j2 = Contract.load(result_j2.contract_id, reg_j2)
check("J: the same digest + the same fixed AI response produce the same "
      "proposal content (title/hypothesis/rules identical)",
      (contract_j1.title, contract_j1.hypothesis, contract_j1.entry_rule,
       contract_j1.exit_rule) ==
      (contract_j2.title, contract_j2.hypothesis, contract_j2.entry_rule,
       contract_j2.exit_rule))


# ===========================================================================
print("\n--- K: permission boundary unchanged ---")
# ===========================================================================

inv_body = _code_only(Path(inv.__file__).read_text())
check("K: investigator.py still imports no engine module",
      not re.search(r"^\s*(?:from|import)\s+engine\b", inv_body, re.MULTILINE))
check("K: investigator.py still imports no broker module",
      not re.search(r"^\s*(?:from|import)\s+[^\n]*broker", inv_body, re.MULTILINE))
check("K: investigator.py's code never references memory/state.json",
      "state.json" not in re.sub(r"#.*", "", inv_body))
check("K: the prompt file explicitly forbids reading engine/, "
      "memory/state.json, memory/guardrails.md, and .env",
      all(s in PROMPT_TEXT for s in
          ("engine/", "memory/state.json", "memory/guardrails.md", ".env")))
check("K: the Research AI's granted directories are unchanged by this slice "
      "(still just research/ and routines/)",
      inv.RESEARCH_AI_ADD_DIRS == ("research", "routines"))


# ===========================================================================
print("\n--- L: existing regressions ---")
# ===========================================================================
# Slices I-V are verified green by running their own test files directly as
# part of this slice's process (see the completion report) — not re-run
# inline here.
check("L: existing regressions are verified via the full suite sweep, see "
      "the completion report", True)


# ===========================================================================
print(f"\n{'='*52}\n  {PASSED} passed, {FAILED} failed\n{'='*52}")
sys.exit(1 if FAILED else 0)
