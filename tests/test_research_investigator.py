"""
Tests for Phase 2 Slice I: research/brain/investigator.py (Research AI v0).

Covers the slice's own mandate:
    1. a valid AI proposal becomes a valid DRAFT hypothesis
    2. invalid schema (malformed / non-JSON AI output) is rejected
    3. unsupported DSL (bad metric/operator) is rejected through the
       EXISTING hypothesis_intake validation — no second rule language
    4. a successful run can never produce a locked Contract
    5. isolation — no engine/broker import, no eval/exec/compile, no direct
       live-state reference, anywhere in investigator.py
    6. deterministic fixture behavior — same digest + same mocked AI
       response -> identical prompt, identical parsed proposal
    7. fail-closed behavior — timeout / malformed / empty AI response never
       creates a Contract or touches the registry

No test in this file invokes the real `claude` binary. Every AI boundary is
mocked via an injected `runner` callable, per the slice's own mandate.

Run with:  python -m tests.test_research_investigator
"""

import re
import sys
import json
import shutil
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from research.store import Store  # noqa: E402
from research.contracts import Contract  # noqa: E402
from research.brain import digest as dg  # noqa: E402
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


TMP = Path(tempfile.mkdtemp(prefix="lq-test-investigator-"))


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
    """A well-formed Research AI response — same shape
    test_research_hypothesis_intake.py's make_valid_proposal() uses, since
    the Research AI is expected to produce exactly this shape."""
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


def registry_files(reg_dir: Path) -> set:
    if not reg_dir.exists():
        return set()
    return {p.name for p in reg_dir.glob("*.json")}


# ---------------------------------------------------------------------------
print("\n--- 1: a valid AI proposal becomes a valid DRAFT hypothesis ---")
# ---------------------------------------------------------------------------

store = fresh_store("valid")
reg = fresh_registry("valid")

digest = dg.build_digest(store, "2024-05-12", registry_dir=reg)
raw_response = json.dumps(valid_ai_proposal())

result = inv.investigate(store, "2024-05-12", runner=lambda prompt: raw_response,
                          registry_dir=reg)

check("investigate() returns an InvestigatorResult",
      isinstance(result, inv.InvestigatorResult), str(result))
check("hypothesis_id is a system-minted HYP- id",
      result.hypothesis_id.startswith("HYP-"), result.hypothesis_id)
check("contract_id is a system-minted EXP- id",
      result.contract_id.startswith("EXP-"), result.contract_id)

loaded = Contract.load(result.contract_id, reg)
check("the resulting Contract is status='draft'", loaded.status == "draft", loaded.status)
check("the resulting Contract has no locked_hash", loaded.locked_hash is None)
check("the Contract carries the AI's title", loaded.title == "Volume-spike drift")

# ```json fenced form must also be accepted
store_f = fresh_store("fenced")
reg_f = fresh_registry("fenced")
fenced = f"```json\n{raw_response}\n```"
result_f = inv.investigate(store_f, "2024-05-12", runner=lambda p: fenced, registry_dir=reg_f)
check("a ```json-fenced AI response is accepted the same way",
      isinstance(result_f, inv.InvestigatorResult))


# ---------------------------------------------------------------------------
print("\n--- 2: invalid schema (malformed / non-JSON AI output) is rejected ---")
# ---------------------------------------------------------------------------

store2 = fresh_store("malformed")
reg2 = fresh_registry("malformed")

bad_cases = {
    "not json at all": "the volume spike looks interesting, I think we should test it",
    "a JSON array, not an object": "[1, 2, 3]",
    "a bare JSON number": "42",
    "truncated JSON": '{"title": "x", "hypothesis":',
}
for label, raw in bad_cases.items():
    try:
        inv.investigate(store2, "2024-05-12", runner=lambda p, r=raw: r, registry_dir=reg2)
        check(f"malformed output rejected: {label}", False, "no exception raised")
    except inv.InvestigatorError:
        check(f"malformed output rejected: {label}", True)
    except Exception as e:
        check(f"malformed output rejected: {label}", False,
              f"wrong exception type: {type(e).__name__}: {e}")

check("no registry file was written by any malformed-output attempt",
      registry_files(reg2) == set(), str(registry_files(reg2)))


# ---------------------------------------------------------------------------
print("\n--- 3: unsupported DSL is rejected through EXISTING hypothesis_intake validation ---")
# ---------------------------------------------------------------------------

store3 = fresh_store("baddsl")
reg3 = fresh_registry("baddsl")

unsupported_metric = valid_ai_proposal(
    entry_rule={"conditions": [{"metric": "made_up_indicator", "op": ">", "value": 3.0}]})
try:
    inv.investigate(store3, "2024-05-12",
                     runner=lambda p: json.dumps(unsupported_metric), registry_dir=reg3)
    check("unsupported metric rejected", False, "no exception raised")
except hi.IntakeRejected as e:
    check("unsupported metric rejected via hi.IntakeRejected (reused validation)", True)
    check("rejection reason names the unsupported metric",
          any("made_up_indicator" in r for r in e.reasons), str(e.reasons))
except Exception as e:
    check("unsupported metric rejected", False, f"wrong exception type: {type(e).__name__}")

unsupported_op = valid_ai_proposal(
    entry_rule={"conditions": [{"metric": "volume_zscore", "op": "~=", "value": 3.0}]})
try:
    inv.investigate(store3, "2024-05-12",
                     runner=lambda p: json.dumps(unsupported_op), registry_dir=reg3)
    check("unsupported operator rejected", False, "no exception raised")
except hi.IntakeRejected:
    check("unsupported operator rejected via hi.IntakeRejected (reused validation)", True)

missing_field = valid_ai_proposal()
del missing_field["falsification"]
try:
    inv.investigate(store3, "2024-05-12",
                     runner=lambda p: json.dumps(missing_field), registry_dir=reg3)
    check("missing required field rejected", False, "no exception raised")
except hi.IntakeRejected as e:
    check("missing required field rejected via hi.IntakeRejected", True)
    check("rejection names the missing field",
          any("falsification" in r for r in e.reasons), str(e.reasons))

check("no registry file was written by any DSL-rejected attempt",
      registry_files(reg3) == set(), str(registry_files(reg3)))

# a proposal must not be able to smuggle a contract_id (Audit fix #4, reused)
spoofed_id = valid_ai_proposal(contract_id="EXP-SPOOFED-A")
try:
    inv.investigate(store3, "2024-05-12",
                     runner=lambda p: json.dumps(spoofed_id), registry_dir=reg3)
    check("a proposal-supplied contract_id is rejected", False, "no exception raised")
except hi.IntakeRejected:
    check("a proposal-supplied contract_id is rejected (identity stays system-assigned)", True)


# ---------------------------------------------------------------------------
print("\n--- 4: a successful run can NEVER produce a locked Contract ---")
# ---------------------------------------------------------------------------

store4 = fresh_store("neverlocks")
reg4 = fresh_registry("neverlocks")
r4 = inv.investigate(store4, "2024-05-12", runner=lambda p: raw_response, registry_dir=reg4)
c4 = Contract.load(r4.contract_id, reg4)
check("the resulting contract is draft, not locked", c4.status == "draft")

src = (Path(__file__).parent.parent / "research" / "brain" / "investigator.py").read_text()


def _code_only(source: str) -> str:
    """Strip triple-double-quoted docstrings, where this module deliberately
    documents the safety boundary IN PROSE (naming approve_and_lock,
    engine.execute, etc. as things it does NOT do — see the module
    docstring). What must be structurally absent is a real code reference,
    not a doc mention of the very thing being ruled out — the same
    distinction test_kernel_isolation.py draws by matching only actual
    import statements rather than any mention of a module's name."""
    return re.sub(r'"""[\s\S]*?"""', "", source)


code = _code_only(src)
check("investigator.py never CALLS approve_and_lock anywhere in its code "
      "(only ever discusses it, in prose, as something it does not do)",
      "approve_and_lock(" not in code, code)
check("investigator.py never calls Contract.lock() in its code",
      ".lock(" not in code)
check("investigator.py source contains no direct instantiation of Contract",
      "Contract(" not in code)


# ---------------------------------------------------------------------------
print("\n--- 5: isolation — no engine/broker import, no dynamic execution ---")
# ---------------------------------------------------------------------------

imports = re.findall(r"^\s*(?:from|import)\s+([.\w]+)", src, re.MULTILINE)
check("investigator.py imports no engine module", not any(m.startswith("engine") for m in imports),
      str(imports))
check("investigator.py imports no broker module",
      not any("broker" in m for m in imports), str(imports))
check("investigator.py's code never CALLS into engine.guardrails",
      "engine.guardrails" not in code)
check("investigator.py's code never CALLS into engine.execute",
      "engine.execute" not in code)
check("investigator.py's code never CALLS into engine.journal",
      "engine.journal" not in code)
check("investigator.py's code never references memory/state.json",
      "state.json" not in code)
check("investigator.py's code never references .env",
      ".env" not in code)

_EVAL_EXEC_RE = re.compile(r"\beval\s*\(|\bexec\s*\(|(?<!re\.)\bcompile\s*\(")
check("investigator.py contains no eval/exec/compile call",
      not _EVAL_EXEC_RE.search(src), str(_EVAL_EXEC_RE.findall(src)))
check("investigator.py contains no __import__ or getattr/setattr-based dispatch",
      "__import__" not in src and "getattr(" not in src and "setattr(" not in src)

check("investigator.py's only research-memory-adjacent read is build_digest()",
      "query_research_log" not in code and "rm.query" not in code)


# ---------------------------------------------------------------------------
print("\n--- 6: deterministic fixture behavior ---")
# ---------------------------------------------------------------------------

store6 = fresh_store("deterministic")
reg6 = fresh_registry("deterministic")
d6 = dg.build_digest(store6, "2024-05-12", registry_dir=reg6)

prompt_a = inv.build_prompt(d6)
prompt_b = inv.build_prompt(d6)
check("build_prompt() is byte-for-byte identical given the same digest",
      prompt_a == prompt_b)

fixed_raw = json.dumps(valid_ai_proposal(), sort_keys=True)
proposal_a = inv.parse_ai_output(fixed_raw)
proposal_b = inv.parse_ai_output(fixed_raw)
check("parse_ai_output() is identical given the same raw text",
      proposal_a == proposal_b)

check("build_context() is a pure passthrough of build_digest()",
      inv.build_context(store6, "2024-05-12") == dg.build_digest(store6, "2024-05-12"))

# same digest + same mocked AI response, run twice against two fresh stores ->
# identical resulting Contracts modulo the system-minted id/hypothesis_id
# (which are legitimately fresh-per-run by design — see hypothesis_intake.py's
# own "Audit fix #4" note on identity being system-controlled, not proposer-
# controlled or derivable from content alone).
store6b = fresh_store("deterministic_b")
reg6b = fresh_registry("deterministic_b")
r6a = inv.investigate(store6, "2024-05-12", runner=lambda p: fixed_raw, registry_dir=reg6)
r6b = inv.investigate(store6b, "2024-05-12", runner=lambda p: fixed_raw, registry_dir=reg6b)
c6a, c6b = Contract.load(r6a.contract_id, reg6), Contract.load(r6b.contract_id, reg6b)
same_content_fields = ("title", "hypothesis", "null_hypothesis", "universe", "signal",
                       "entry_rule", "exit_rule", "splits", "independence",
                       "falsification", "abandon_condition",
                       "evaluation_start", "evaluation_end")
check("two independent runs against the same digest + AI response produce "
      "content-identical drafts (id/hypothesis_id excluded, by design)",
      all(getattr(c6a, f) == getattr(c6b, f) for f in same_content_fields))


# ---------------------------------------------------------------------------
print("\n--- 7: fail-closed behavior — timeout / malformed / empty response ---")
# ---------------------------------------------------------------------------

store7 = fresh_store("failclosed")
reg7 = fresh_registry("failclosed")


def _raises_timeout(prompt):
    raise inv.InvestigatorError("Research AI process timed out after 300s")


try:
    inv.investigate(store7, "2024-05-12", runner=_raises_timeout, registry_dir=reg7)
    check("a timing-out runner fails the whole call", False, "no exception raised")
except inv.InvestigatorError:
    check("a timing-out runner fails the whole call, no Contract created", True)

try:
    inv.investigate(store7, "2024-05-12", runner=lambda p: "", registry_dir=reg7)
    check("an empty AI response fails closed", False, "no exception raised")
except inv.InvestigatorError:
    check("an empty AI response fails closed", True)

try:
    inv.investigate(store7, "2024-05-12", runner=lambda p: "   \n  ", registry_dir=reg7)
    check("a whitespace-only AI response fails closed", False, "no exception raised")
except inv.InvestigatorError:
    check("a whitespace-only AI response fails closed", True)


def _broken_runner(prompt):
    raise RuntimeError("subprocess crashed unexpectedly")


try:
    inv.investigate(store7, "2024-05-12", runner=_broken_runner, registry_dir=reg7)
    check("an arbitrary runner exception fails closed as InvestigatorError", False,
          "no exception raised")
except inv.InvestigatorError:
    check("an arbitrary runner exception fails closed as InvestigatorError", True)
except Exception as e:
    check("an arbitrary runner exception fails closed as InvestigatorError", False,
          f"leaked raw exception type instead: {type(e).__name__}")

check("no registry file exists after any fail-closed attempt in this section",
      registry_files(reg7) == set(), str(registry_files(reg7)))


# ---------------------------------------------------------------------------
print("\n--- 8: the no_proposal escape hatch is a normal outcome, not an error ---")
# ---------------------------------------------------------------------------

store8 = fresh_store("noproposal")
reg8 = fresh_registry("noproposal")
no_prop_raw = json.dumps({"no_proposal": True, "reason": "nothing in the digest stood out"})
r8 = inv.investigate(store8, "2024-05-12", runner=lambda p: no_prop_raw, registry_dir=reg8)
check("a no_proposal response returns a NoProposal, not an InvestigatorResult",
      isinstance(r8, inv.NoProposal), str(r8))
check("the NoProposal carries the AI's stated reason",
      r8.reason == "nothing in the digest stood out")
check("a no_proposal response writes nothing to the registry",
      registry_files(reg8) == set())


# ---------------------------------------------------------------------------
for s in (store, store_f, store2, store3, store4, store6, store6b, store7, store8):
    s.close()

print(f"\n{'=' * 52}")
print(f"  {PASSED} passed, {FAILED} failed")
print(f"{'=' * 52}\n")
sys.exit(1 if FAILED else 0)
