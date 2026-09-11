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

import os
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
check("investigator.py's code never references the .env FILE "
      "(os.environ.get(...) for a named var, e.g. RESEARCH_AI_CLAUDE_BIN, is "
      "reading an env var, not opening the .env secrets file)",
      ".env" not in code.replace("os.environ", ""))

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
print("\n--- 9: Claude Code executable resolution (cron PATH fix) ---")
# ---------------------------------------------------------------------------
# cron's minimal PATH does not include ~/.local/bin, so a bare "claude" (a
# PATH lookup) fails under cron with [Errno 2] No such file or directory,
# even though the interactive shell resolves it fine. resolve_claude_binary()
# / _validate_claude_binary() replace that PATH lookup with a deterministic,
# configurable, validated absolute path.

_saved_env = os.environ.get(inv.ENV_CLAUDE_BIN)
os.environ.pop(inv.ENV_CLAUDE_BIN, None)
try:
    check("9.1: with no override, resolve_claude_binary() returns the configured default",
          inv.resolve_claude_binary() == inv.DEFAULT_CLAUDE_BIN, inv.resolve_claude_binary())
    check("9.1: the default is an absolute path (never a bare PATH-dependent name)",
          Path(inv.DEFAULT_CLAUDE_BIN).is_absolute())
    check("9.1: the default is this deployment's known Claude Code path",
          inv.DEFAULT_CLAUDE_BIN == "/root/.local/bin/claude")

    os.environ[inv.ENV_CLAUDE_BIN] = "/opt/custom/claude"
    check("9.2: RESEARCH_AI_CLAUDE_BIN overrides the default",
          inv.resolve_claude_binary() == "/opt/custom/claude")
    os.environ[inv.ENV_CLAUDE_BIN] = "  /opt/custom/claude  "
    check("9.2: surrounding whitespace in the env var is stripped",
          inv.resolve_claude_binary() == "/opt/custom/claude")
    os.environ[inv.ENV_CLAUDE_BIN] = ""
    check("9.2: a blank override falls back to the default (never an empty command)",
          inv.resolve_claude_binary() == inv.DEFAULT_CLAUDE_BIN)
finally:
    os.environ.pop(inv.ENV_CLAUDE_BIN, None)
    if _saved_env is not None:
        os.environ[inv.ENV_CLAUDE_BIN] = _saved_env

# 9.3: a missing executable is a clear, actionable InvestigatorError
_missing = str(TMP / "no-such-claude-binary")
try:
    inv._validate_claude_binary(_missing)
    _raised = None
except Exception as e:
    _raised = e
check("9.3: a missing executable raises InvestigatorError (never a bare OSError/[Errno 2])",
      isinstance(_raised, inv.InvestigatorError), str(_raised))
check("9.3: the error names the missing path and the env var that fixes it",
      _raised is not None and _missing in str(_raised) and inv.ENV_CLAUDE_BIN in str(_raised),
      str(_raised))

# 9.4: a relative / bare command name is refused outright — resolving it would
# silently reintroduce the exact PATH dependency this fix removes.
try:
    inv._validate_claude_binary("claude")
    _raised_rel = None
except Exception as e:
    _raised_rel = e
check("9.4: a non-absolute path is refused (this IS the cron bug being fixed)",
      isinstance(_raised_rel, inv.InvestigatorError)
      and "absolute" in str(_raised_rel).lower(), str(_raised_rel))

# 9.5: an existing, executable file validates cleanly (no exception)
_ok_bin = TMP / "fake-claude"
_ok_bin.write_text("#!/bin/sh\necho ok\n")
_ok_bin.chmod(0o755)
_validated_ok = True
try:
    inv._validate_claude_binary(str(_ok_bin))
except inv.InvestigatorError:
    _validated_ok = False
check("9.5: an absolute, existing, executable path validates without error", _validated_ok)

# 9.6: a non-executable file is refused with a distinct, actionable message
_noexec_bin = TMP / "not-executable-claude"
_noexec_bin.write_text("not a script")
_noexec_bin.chmod(0o644)
try:
    inv._validate_claude_binary(str(_noexec_bin))
    _raised_noexec = None
except Exception as e:
    _raised_noexec = e
check("9.6: a non-executable file is refused (chmod +x guidance)",
      _raised_noexec is not None and "not executable" in str(_raised_noexec).lower(),
      str(_raised_noexec))

# 9.7: end to end through investigate() with the REAL _default_runner (still
# never touching the real claude binary — the configured path is broken on
# purpose) — the failure surfaces as InvestigatorError, not [Errno 2], and
# nothing is written.
_saved_env2 = os.environ.get(inv.ENV_CLAUDE_BIN)
os.environ[inv.ENV_CLAUDE_BIN] = str(TMP / "still-missing-claude")
store9 = fresh_store("claudebin")
reg9 = fresh_registry("claudebin")
_threw, _e2e_msg = False, ""
try:
    inv.investigate(store9, "2024-05-12", runner=inv._default_runner, registry_dir=reg9)
except inv.InvestigatorError as e:
    _threw, _e2e_msg = True, str(e)
finally:
    os.environ.pop(inv.ENV_CLAUDE_BIN, None)
    if _saved_env2 is not None:
        os.environ[inv.ENV_CLAUDE_BIN] = _saved_env2
    store9.close()
check("9.7: investigate() with the real _default_runner + a broken "
      "RESEARCH_AI_CLAUDE_BIN raises InvestigatorError end to end (not a bare [Errno 2])",
      _threw and "Errno 2" not in _e2e_msg and inv.ENV_CLAUDE_BIN in _e2e_msg, _e2e_msg)
check("9.7: nothing was written to the registry when resolution fails",
      registry_files(reg9) == set())

# 9.8: isolation + no hard-coded observed value — the fix adds no new import
# and no fixed market/account rupee amount.
_isrc = re.sub(r'"""[\s\S]*?"""', "",
              (Path(__file__).parent.parent / "research" / "brain" / "investigator.py").read_text())
_iimports = re.findall(r"^\s*(?:from|import)\s+([.\w]+)", _isrc, re.MULTILINE)
check("9.8: investigator.py still imports no engine / broker / paper module",
      not any(m.split(".")[0] in ("engine", "paper") for m in _iimports), str(_iimports))
check("9.8: no observed live market/account amount is hard-coded by this fix",
      not any(t in _isrc for t in ("32.31", "63339", "63307", "570447", "570000")))


# ---------------------------------------------------------------------------
print("\n--- 10: the `signal` prompt/contract alignment (200-char field) ---")
# ---------------------------------------------------------------------------
# The bug this closes: routines/research_investigate.md told the Research AI
# "signal - a short string describing the idea in your own words" with no
# numeric limit, while hypothesis_intake.FREE_TEXT_MAX_LEN caps signal at 200
# (the same tier as `title`) — Claude produced a sentence-length signal that
# then failed intake. The fix is prompt-only; the 200-char contract itself is
# unchanged (proven directly against hi.FREE_TEXT_MAX_LEN below, never a
# second hard-coded "200").

PROMPT_TEXT = inv.PROMPT_PATH.read_text()
_signal_bullet_match = re.search(r"- `signal`.*?(?=\n- `|\n\n)", PROMPT_TEXT, re.DOTALL)
check("10.1: the prompt has a `signal` bullet in its 'What to produce' section",
      _signal_bullet_match is not None)
_signal_bullet = _signal_bullet_match.group(0) if _signal_bullet_match else ""

check("10.1: the prompt explicitly states the signal character limit",
      "200 characters" in _signal_bullet, _signal_bullet)
check("10.1: the prompt includes a concrete example identifier",
      "observatory." in _signal_bullet and "`" in _signal_bullet, _signal_bullet)
_example_match = re.search(r"`(observatory\.[\w.]+)`", _signal_bullet)
check("10.1: the example given is itself a valid, well-under-200-character identifier",
      _example_match is not None and 0 < len(_example_match.group(1)) <= 200, _signal_bullet)
check("10.1: the prompt tells the AI this is NOT a sentence-length explanation",
      "sentence" in _signal_bullet.lower() or "explanation" in _signal_bullet.lower(),
      _signal_bullet)
check("10.1: the prompt routes reasoning to `hypothesis` / `notes` instead",
      "`hypothesis`" in _signal_bullet and "`notes`" in _signal_bullet, _signal_bullet)

# 10.2 — single source of truth: the prompt's stated number must be the SAME
# number as the actual validation limit, read from the code, never a second
# literal "200" independently maintained in the test.
check("10.2: the prompt's stated limit matches hi.FREE_TEXT_MAX_LEN['signal'] exactly "
      "(single source of truth — code and prompt cannot silently drift apart)",
      f"{hi.FREE_TEXT_MAX_LEN['signal']} characters" in _signal_bullet, _signal_bullet)
check("10.2: the 200-char CONTRACT itself is unchanged by this fix",
      hi.FREE_TEXT_MAX_LEN["signal"] == 200 and hi.FREE_TEXT_MAX_LEN["title"] == 200)

# 10.4 — parse_ai_output() round-trips an arbitrary dict unchanged: no key
# renaming, merging, or truncation happens in the parser, so a `signal`
# rejection can only ever be about the VALUE the AI produced, never about
# where the parser routed it.
_arbitrary = {
    "alpha": 1, "beta": [1, 2, {"nested": "value"}], "gamma": None, "delta": True,
    "signal": "observatory.volume_zscore", "epsilon": {"x": {"y": [1, "z"]}},
}
check("10.4: parse_ai_output() returns an arbitrary dict byte-for-byte unchanged",
      inv.parse_ai_output(json.dumps(_arbitrary)) == _arbitrary)
_fenced_arbitrary = f"```json\n{json.dumps(_arbitrary)}\n```"
check("10.4: the same holds through a ```json fence (only the fence is stripped)",
      inv.parse_ai_output(_fenced_arbitrary) == _arbitrary)
_parse_src = re.sub(r'"""[\s\S]*?"""', "",
                    (Path(__file__).parent.parent / "research" / "brain" /
                     "investigator.py").read_text())
_parse_body = _parse_src.split("def parse_ai_output")[1].split("\ndef ")[0]
check("10.4: parse_ai_output()'s body never names a specific proposal field "
      "(signal/title/hypothesis/...) — it is a generic JSON parse, field-blind by "
      "construction, so it structurally cannot route one field's content into another",
      not any(f'"{f}"' in _parse_body or f"'{f}'" in _parse_body
              for f in ("signal", "title", "hypothesis", "null_hypothesis", "notes")),
      _parse_body)

# 10.5 — end to end: an otherwise-valid proposal with only signal oversized
# is rejected as IntakeRejected, nothing is written, and the worker (not just
# investigate() in isolation) stays bounded and continues past it.
store10 = fresh_store("oversized_signal")
reg10 = fresh_registry("oversized_signal")
_oversized = json.dumps(valid_ai_proposal(signal="s" * 250))
_raised, _reason = None, ""
try:
    inv.investigate(store10, "2024-05-12", runner=lambda p: _oversized, registry_dir=reg10)
except hi.IntakeRejected as e:
    _raised, _reason = e, "; ".join(e.reasons)
check("10.5: investigate() raises hi.IntakeRejected for an otherwise-valid proposal "
      "with only signal oversized", _raised is not None, str(_raised))
check("10.5: the rejection names the field, the limit, and the observed length (250)",
      "signal" in _reason and "200 characters" in _reason and "length=250" in _reason,
      _reason)
check("10.5: nothing was written to the registry", registry_files(reg10) == set())
store10.close()


# ---------------------------------------------------------------------------
for s in (store, store_f, store2, store3, store4, store6, store6b, store7, store8):
    s.close()

print(f"\n{'=' * 52}")
print(f"  {PASSED} passed, {FAILED} failed")
print(f"{'=' * 52}\n")
sys.exit(1 if FAILED else 0)
