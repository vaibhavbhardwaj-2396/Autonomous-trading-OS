"""
Tests for Phase 2 Slice P: Overnight Autonomous Research Cadence
(research/overnight.py).

Covers the first unattended operational research loop: a bounded number of
Research AI attempts against a digest built once per run, each ending at
DRAFT (with Slice N provenance) or not at all — never approved, never
locked, never executed. No test in this file invokes the real `claude`
binary or a real Telegram send; every AI boundary is mocked via an
injected `runner` callable, and every notification is observed via
monkeypatching `research.overnight._notify`/`telegram_notify.send_message`,
per the same mandate every prior Research AI slice has kept.

Run with:  python -m tests.test_research_overnight
"""

import re
import sys
import json
import shutil
import inspect
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from research.store import Store  # noqa: E402
from research.contracts import Contract, REGISTRY_DIR  # noqa: E402
from research.brain import hypothesis_intake as hi  # noqa: E402
from research.brain import investigator as inv  # noqa: E402
from research.brain import discovery_provenance as dp  # noqa: E402
from research import overnight as on  # noqa: E402

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


TMP = Path(tempfile.mkdtemp(prefix="lq-test-overnight-"))

# Priority Task 0 — redirect the AI-failure diagnostic log to this run's own
# tmp dir; see tests/test_research_investigator.py's identical line for why
# this one redirect (investigator.investigate() resolves it as a bare
# global at call time) is enough for every investigate() call in this file.
inv.AI_FAILURE_LOG = TMP / "ai_failures.jsonl"


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
    valid_ai_proposal()."""
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


def distinct_proposal_responses(n: int) -> list:
    """n well-formed, mutually NON-duplicate AI responses (differing entry
    threshold, so their _rule_fingerprint()s differ) -- used wherever a
    test needs several attempts to each succeed as a genuine new draft
    rather than being caught by the duplicate_check."""
    out = []
    for i in range(n):
        p = valid_ai_proposal(
            title=f"Idea {i}",
            entry_rule={"conditions": [{"metric": "volume_zscore", "op": ">", "value": 3.0 + i}]},
        )
        out.append(json.dumps(p))
    return out


def sequential_runner(responses: list):
    """A runner returning each response in order, repeating the last one
    if called more times than len(responses) -- deterministic, no real AI
    call, matching every prior slice's mandate."""
    state = {"i": 0}

    def _runner(prompt):
        i = min(state["i"], len(responses) - 1)
        state["i"] += 1
        return responses[i]
    return _runner


def registry_files(reg_dir: Path) -> set:
    if not reg_dir.exists():
        return set()
    return {p.name for p in reg_dir.glob("*.json")}


# ---------------------------------------------------------------------------
print("\n--- A: one successful overnight run produces a valid DRAFT ---")
# ---------------------------------------------------------------------------

store_a = fresh_store("a")
reg_a = fresh_registry("a")
raw_a = json.dumps(valid_ai_proposal())

result_a = on.run_overnight_cycle(store_a, "2024-05-12", registry_dir=reg_a,
                                   runner=lambda p: raw_a, max_attempts=1)

check("A: exactly one draft was created", len(result_a.drafts_created) == 1,
      str(result_a))
check("A: the attempt is recorded as draft_created",
      result_a.attempts[0].outcome == "draft_created")
loaded_a = Contract.load(result_a.attempts[0].contract_id, reg_a)
check("A: the resulting Contract is status='draft'", loaded_a.status == "draft")
check("A: as_of on the result matches the digest's own as_of string",
      result_a.as_of.startswith("2024-05-12"))


# ---------------------------------------------------------------------------
print("\n--- B: no overnight code ever calls approve_and_lock() or Contract.lock() ---")
# ---------------------------------------------------------------------------

ON_SRC = (Path(__file__).parent.parent / "research" / "overnight.py").read_text()
ON_BODY = _code_only(ON_SRC)

check("B: research/overnight.py never CALLS approve_and_lock",
      "approve_and_lock(" not in ON_BODY)
check("B: research/overnight.py never CALLS Contract.lock()",
      ".lock(" not in ON_BODY)
check("B: research/overnight.py never imports hypothesis_intake.approve_and_lock directly",
      not re.search(r"from .*hypothesis_intake import[^\n]*approve_and_lock", ON_SRC))

# behaviorally: a run with several successful, DISTINCT proposals never
# produces anything but drafts
store_b = fresh_store("b")
reg_b = fresh_registry("b")
runner_b = sequential_runner(distinct_proposal_responses(3))
result_b = on.run_overnight_cycle(store_b, "2024-05-12", registry_dir=reg_b,
                                   runner=runner_b, max_attempts=3)
check("B: all three attempts produced distinct drafts",
      len(result_b.drafts_created) == 3, str(result_b))
statuses_b = {c.status for c in (Contract.load(cid, reg_b) for cid in
                                  [a.contract_id for a in result_b.attempts if a.contract_id])}
check("B: every Contract created this run is status='draft', nothing else",
      statuses_b == {"draft"}, str(statuses_b))


# ---------------------------------------------------------------------------
print("\n--- C: no overnight code ever calls run_experiment() ---")
# ---------------------------------------------------------------------------

check("C: research/overnight.py never CALLS run_experiment",
      "run_experiment(" not in ON_BODY)
check("C: research/overnight.py does not import research.experiments.runner",
      not re.search(r"^\s*(import\s+.*experiments\.runner|from\s+.*experiments\.runner)",
                     ON_SRC, re.MULTILINE))
check("C: research/overnight.py does not import engine at all",
      not re.search(r"^\s*(import\s+engine|from\s+engine)", ON_SRC, re.MULTILINE))


# ---------------------------------------------------------------------------
print("\n--- D: the run never exceeds the configured proposal cap ---")
# ---------------------------------------------------------------------------

store_d = fresh_store("d")
reg_d = fresh_registry("d")
call_count = {"n": 0}
responses_d = distinct_proposal_responses(10)  # far more than the cap


def counting_runner(prompt):
    call_count["n"] += 1
    return responses_d[call_count["n"] - 1]


result_d = on.run_overnight_cycle(store_d, "2024-05-12", registry_dir=reg_d,
                                   runner=counting_runner, max_attempts=2)
check("D: the Research AI runner was invoked exactly max_attempts times, not more",
      call_count["n"] == 2, f"called {call_count['n']} times")
check("D: exactly 2 attempts are recorded, never more than the cap",
      len(result_d.attempts) == 2)
check("D: exactly 2 drafts were created (all distinct, none capped mid-success)",
      len(result_d.drafts_created) == 2)

# a smaller/larger explicit cap is honored too
store_d2 = fresh_store("d2")
reg_d2 = fresh_registry("d2")
result_d2 = on.run_overnight_cycle(store_d2, "2024-05-12", registry_dir=reg_d2,
                                    runner=sequential_runner(distinct_proposal_responses(5)),
                                    max_attempts=1)
check("D: max_attempts=1 makes exactly one attempt", len(result_d2.attempts) == 1)


# ---------------------------------------------------------------------------
print("\n--- E: malformed AI output fails safely, no fake hypothesis ---")
# ---------------------------------------------------------------------------

store_e = fresh_store("e")
reg_e = fresh_registry("e")
result_e = on.run_overnight_cycle(store_e, "2024-05-12", registry_dir=reg_e,
                                   runner=lambda p: "not json at all", max_attempts=1)
check("E: the attempt is recorded as ai_error", result_e.attempts[0].outcome == "ai_error")
check("E: no draft was created", result_e.drafts_created == [])
check("E: the registry directory has no contract files at all",
      registry_files(reg_e) == set())
check("E: no hypothesis-proposal claim row was written",
      len(store_e.view("2024-05-12").observations(
          "research_hypothesis_proposal", latest_only=False)) == 0)


# ---------------------------------------------------------------------------
print("\n--- F: a no_proposal AI response is handled cleanly ---")
# ---------------------------------------------------------------------------

store_f = fresh_store("f")
reg_f = fresh_registry("f")
no_proposal_raw = json.dumps({"no_proposal": True, "reason": "nothing interesting tonight"})
result_f = on.run_overnight_cycle(store_f, "2024-05-12", registry_dir=reg_f,
                                   runner=lambda p: no_proposal_raw, max_attempts=1)
check("F: the attempt is recorded as no_proposal", result_f.attempts[0].outcome == "no_proposal")
check("F: the recorded detail carries the AI's stated reason",
      "nothing interesting tonight" in result_f.attempts[0].detail)
check("F: no draft was created", result_f.drafts_created == [])
check("F: the registry directory has no contract files at all",
      registry_files(reg_f) == set())


# ---------------------------------------------------------------------------
print("\n--- G: a validation-rejected proposal never becomes a draft ---")
# ---------------------------------------------------------------------------

store_g = fresh_store("g")
reg_g = fresh_registry("g")
bad_metric = valid_ai_proposal(
    entry_rule={"conditions": [{"metric": "made_up_indicator", "op": ">", "value": 3.0}]})
result_g = on.run_overnight_cycle(store_g, "2024-05-12", registry_dir=reg_g,
                                   runner=lambda p: json.dumps(bad_metric), max_attempts=1)
check("G: the attempt is recorded as rejected", result_g.attempts[0].outcome == "rejected")
check("G: the rejection detail names the unsupported metric",
      "made_up_indicator" in result_g.attempts[0].detail)
check("G: no draft was created", result_g.drafts_created == [])
check("G: the registry directory has no contract files at all",
      registry_files(reg_g) == set())


# ---------------------------------------------------------------------------
print("\n--- H: a successful draft carries Slice N discovery provenance ---")
# ---------------------------------------------------------------------------

hyp_id_a = result_a.drafts_created[0]
prov_a = dp.discovery_search_for_hypothesis(store_a, hyp_id_a)
check("H: exactly one discovery-provenance row exists for the drafted hypothesis",
      len(prov_a) == 1)
check("H: the provenance row's discovery_type is research_ai",
      prov_a[0]["discovery_type"] == inv.DISCOVERY_TYPE_RESEARCH_AI)
check("H: the provenance row's version matches investigator.py's RESEARCH_AI_VERSION",
      prov_a[0]["version"] == inv.RESEARCH_AI_VERSION)
check("H: the provenance row's as_of matches the overnight run's as_of",
      prov_a[0]["as_of"] == result_a.as_of)


# ---------------------------------------------------------------------------
print("\n--- I: notification behavior ---")
# ---------------------------------------------------------------------------

summary_a = on._summarize_for_notification(result_a, registry_dir=reg_a)
check("I: the notification summary mentions the attempt count",
      "1 attempt" in summary_a)
check("I: the notification summary mentions exactly 1 DRAFT created",
      "1 DRAFT hypothesis" in summary_a)
check("I: the notification summary names the created hypothesis_id",
      hyp_id_a in summary_a)
check("I: the notification summary includes the draft's own claim/title text",
      "Volume-spike drift" in summary_a)
check("I: the notification explicitly frames DRAFTs as not validated/not locked/not tradeable",
      "not validated" in summary_a.lower() and "not locked" in summary_a.lower())
for forbidden in ("confidence", "profit", "edge found", "place order", "buy ", "sell "):
    check(f"I: the notification never claims {forbidden!r}",
          forbidden not in summary_a.lower())

# a run with nothing but failures still produces a sane, non-crashing summary
summary_e = on._summarize_for_notification(result_e, registry_dir=reg_e)
check("I: a failed-attempt-only run still produces a notification mentioning 0 drafts",
      "0 DRAFT hypothesis" in summary_e)

# _notify itself is guarded against a broken send path, same as recorder.py's
sent = {"messages": []}


class _FakeTelegram:
    @staticmethod
    def send_message(text):
        sent["messages"].append(text)


sys.modules["telegram_notify"] = _FakeTelegram()
on._notify("test message one")
check("I: _notify() delivers through the existing telegram_notify.send_message path",
      sent["messages"] == ["test message one"])


class _BrokenTelegram:
    @staticmethod
    def send_message(text):
        raise RuntimeError("network is down")


sys.modules["telegram_notify"] = _BrokenTelegram()
try:
    on._notify("this must not raise")
    check("I: _notify() swallows a broken notification path without raising", True)
except Exception as e:
    check("I: _notify() swallows a broken notification path without raising", False, str(e))
del sys.modules["telegram_notify"]


# ---------------------------------------------------------------------------
print("\n--- J: failure isolation ---")
# ---------------------------------------------------------------------------

# J1: a failed notification does not corrupt research state
reg_files_before_notify = {p.name: p.read_text() for p in reg_a.glob("*.json")}
sys.modules["telegram_notify"] = _BrokenTelegram()
on._notify(on._summarize_for_notification(result_a, registry_dir=reg_a))
del sys.modules["telegram_notify"]
reg_files_after_notify = {p.name: p.read_text() for p in reg_a.glob("*.json")}
check("J1: registry files are byte-for-byte unchanged after a failed notification attempt",
      reg_files_before_notify == reg_files_after_notify)

# J2: one failed attempt does not prevent a later bounded attempt from succeeding
store_j = fresh_store("j")
reg_j = fresh_registry("j")
responses_j = distinct_proposal_responses(2)


def flaky_then_ok(prompt):
    call = flaky_then_ok.n
    flaky_then_ok.n += 1
    if call == 0:
        raise TimeoutError("simulated Research AI timeout on the first attempt")
    return responses_j[call - 1]


flaky_then_ok.n = 0

result_j = on.run_overnight_cycle(store_j, "2024-05-12", registry_dir=reg_j,
                                   runner=flaky_then_ok, max_attempts=2)
check("J2: the first (failing) attempt is recorded as ai_error",
      result_j.attempts[0].outcome == "ai_error")
check("J2: the second attempt still ran and succeeded despite the first failing",
      result_j.attempts[1].outcome == "draft_created")
check("J2: exactly one draft exists from the run overall", len(result_j.drafts_created) == 1)


# ---------------------------------------------------------------------------
print("\n--- K: determinism of digest input ---")
# ---------------------------------------------------------------------------

store_k = fresh_store("k")
reg_k = fresh_registry("k")
prompts_seen = []
responses_k = distinct_proposal_responses(2)


def prompt_capturing_runner(prompt):
    prompts_seen.append(prompt)
    call = len(prompts_seen) - 1
    return responses_k[call]


build_digest_calls = {"n": 0}
_real_build_digest = on.build_digest


def _counting_build_digest(*a, **kw):
    build_digest_calls["n"] += 1
    return _real_build_digest(*a, **kw)


on.build_digest = _counting_build_digest
try:
    result_k = on.run_overnight_cycle(store_k, "2024-05-12", registry_dir=reg_k,
                                       runner=prompt_capturing_runner, max_attempts=2)
finally:
    on.build_digest = _real_build_digest

check("K: build_digest() is called exactly once for the whole run, regardless of attempts",
      build_digest_calls["n"] == 1, f"called {build_digest_calls['n']} times")

# both attempts succeeded (creating a new draft in between), yet the DIGEST
# section embedded in the prompt must be byte-identical across both attempts
digest_json_1 = prompts_seen[0].split("```json\n", 1)[1].rsplit("\n```", 1)[0]
digest_json_2 = prompts_seen[1].split("```json\n", 1)[1].rsplit("\n```", 1)[0]
check("K: two attempts in the same run embed the IDENTICAL digest JSON in their prompts, "
      "even though the first attempt's own draft changed the registry in between",
      digest_json_1 == digest_json_2)
check("K: both attempts in this run did in fact succeed (proving the registry really did change)",
      len(result_k.drafts_created) == 2)


# ---------------------------------------------------------------------------
print("\n--- L: _persist_run() writes a verifiable JSONL run log ---")
# ---------------------------------------------------------------------------

store_l = fresh_store("persist")
reg_l = fresh_registry("persist")
run_log_l = TMP / "overnight_runs_test.jsonl"

result_l1 = on.run_overnight_cycle(
    store_l, "2024-05-12", registry_dir=reg_l,
    runner=sequential_runner(distinct_proposal_responses(2)), max_attempts=2)
on._persist_run(result_l1, run_log=run_log_l)

check("L: _persist_run() with an explicit run_log writes to that path, "
      "not the real repository log",
      run_log_l.exists() and run_log_l.resolve() != on.RUN_LOG.resolve())

rows_l = [json.loads(line) for line in run_log_l.read_text().splitlines() if line.strip()]
check("L: exactly one JSONL row was written for the one run", len(rows_l) == 1)

row_l = rows_l[0]
check("L: the row records a start time and an end time",
      bool(row_l.get("started")) and bool(row_l.get("finished")))
check("L: the row records the run's as_of", str(row_l.get("as_of", "")).startswith("2024-05-12"))
check("L: the row records how many attempts were made",
      len(row_l.get("attempts", [])) == 2)
check("L: the row records which hypothesis IDs were created as drafts",
      row_l.get("drafts_created") == list(result_l1.drafts_created) and
      len(row_l["drafts_created"]) == 2)
check("L: each attempt row inside the log names its own outcome",
      all(a.get("outcome") == "draft_created" for a in row_l["attempts"]))

# a second run appends rather than overwriting
result_l2 = on.run_overnight_cycle(
    store_l, "2024-05-13", registry_dir=reg_l,
    runner=lambda p: "not json at all", max_attempts=1)
on._persist_run(result_l2, run_log=run_log_l)
rows_l2 = [json.loads(line) for line in run_log_l.read_text().splitlines() if line.strip()]
check("L: a second call to _persist_run() appends a second row rather than "
      "overwriting the first", len(rows_l2) == 2)
check("L: the appended row reflects the second run's own (failed) outcome",
      rows_l2[1]["attempts"][0]["outcome"] == "ai_error" and
      rows_l2[1]["drafts_created"] == [])

# main() exposes the override via --run-log, mirroring --db/--registry-dir
MAIN_SRC = inspect.getsource(on.main)
check("L: main() exposes a --run-log CLI argument for the same override",
      "--run-log" in MAIN_SRC)


# ---------------------------------------------------------------------------
print("\n--- duplicate detection (no duplicate research) ---")
# ---------------------------------------------------------------------------

store_dup = fresh_store("dup")
reg_dup = fresh_registry("dup")
same_raw = json.dumps(valid_ai_proposal())
result_dup = on.run_overnight_cycle(store_dup, "2024-05-12", registry_dir=reg_dup,
                                     runner=lambda p: same_raw, max_attempts=3)
check("duplicate: the first identical attempt creates a draft",
      result_dup.attempts[0].outcome == "draft_created")
check("duplicate: subsequent identical attempts are caught as duplicates, not new drafts",
      result_dup.attempts[1].outcome == "duplicate" and result_dup.attempts[2].outcome == "duplicate")
check("duplicate: only ONE draft exists in the registry despite 3 identical attempts",
      len(result_dup.drafts_created) == 1 and len(registry_files(reg_dup)) == 1)
check("duplicate: the duplicate detail names the existing contract_id",
      result_dup.attempts[0].contract_id in result_dup.attempts[1].detail)


# ---------------------------------------------------------------------------
print("\n====================================================")
print(f"  {PASSED} passed, {FAILED} failed")
print("====================================================")
sys.exit(1 if FAILED else 0)
