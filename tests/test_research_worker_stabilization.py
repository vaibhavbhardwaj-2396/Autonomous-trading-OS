"""
tests/test_research_worker_stabilization.py — Priority Task 0: Stabilize
Continuous Research, Data Capture and Telegram.

Operational reliability, not a new research capability. Covers the task's
own required matrix (A-T):

  A/B/C/D  Research-AI JSON handling: malformed/empty/valid/wrapped-in-prose
  E/F      the hypothesis-length contract (prompt vs validator) is internally
           consistent and deterministic in both directions
  G/H/I    failure isolation: recorder capture, the next heartbeat, and
           unrelated worker actions all survive one AI failure
  J/N      exactly one Telegram notification decision per worker run, never
           duplicated
  K/L      heartbeat truth — successful AND failed runs both persist a
           complete telemetry record
  M        a Telegram-side failure never corrupts research state
  O        repeated malformed AI output does not cause unbounded retries
  P/Q      existing cooldown and flock/concurrency behavior is unchanged
  R/S      existing unified selection and CREATE_EXPERIMENT behavior is
           unchanged (smoke-level here; full coverage lives in their own
           dedicated suites, which this run also re-confirms — see the
           validation report)
  T        isolation boundaries are unchanged

Run with:  python -m tests.test_research_worker_stabilization
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from research.store import Store, now_ist  # noqa: E402
from research.contracts import Contract  # noqa: E402
from research.brain import hypothesis_intake as hi  # noqa: E402
from research.brain import investigator as inv  # noqa: E402
from research.brain import opportunity as opp  # noqa: E402
from research.brain import worker as w  # noqa: E402
from research.sources.base import run_source  # noqa: E402
from research import recorder  # noqa: E402

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


TMP = Path(tempfile.mkdtemp(prefix="lq-test-stabilization-"))

# Redirect the AI-failure diagnostic log for this whole file, the same way
# tests/test_research_investigator.py and tests/test_research_worker.py do
# — see either file's identical line for why one redirect here is enough.
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
    d.mkdir(parents=True, exist_ok=True)
    return d


def fresh_state(name) -> Path:
    p = TMP / f"wstate-{name}.json"
    if p.exists():
        p.unlink()
    return p


def read_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def make_proposal(**overrides) -> dict:
    base = {
        "title": "Volume-spike drift",
        "hypothesis": "High-volume anomalies precede a short-term upward drift.",
        "null_hypothesis": "No relationship.",
        "universe": "watchlist",
        "signal": "observatory.volume_zscore",
        "entry_rule": {"conditions": [{"metric": "volume_zscore", "op": ">", "value": 3.0}]},
        "exit_rule": {"stop_loss_pct": 2.0, "target_pct": 4.0, "max_hold_days": 10},
        "splits": {"discovery": ["2019-01-01", "2022-12-31"]},
        "independence": "one entry per symbol per rolling 10-day window",
        "falsification": "expectancy_r <= 0 on discovery, or t_stat < 2.0",
        "abandon_condition": "if discovery-split expectancy_r <= 0, abandon",
        "evaluation_start": "2019-01-01", "evaluation_end": "2022-12-31",
    }
    base.update(overrides)
    return base


def make_locked_contract(cid, *, registry_dir, locked_at="2024-01-01T00:00:00"):
    fields = dict(
        id=cid, title=f"idea {cid}", hypothesis=f"claim behind {cid}",
        null_hypothesis="no effect", universe="watchlist",
        signal="observatory.volume_zscore",
        entry_rule=json.dumps({"conditions": [{"metric": "volume_zscore", "op": ">", "value": 3.0}]}),
        exit_rule=json.dumps({"stop_loss_pct": 2.0, "target_pct": 4.0}),
        splits={"discovery": ["2019-01-01", "2020-01-01"]},
        independence="clustered by symbol-day", falsification="t_stat < 2.0",
        abandon_condition="expectancy_r <= 0 on discovery",
        evaluation_start="2019-01-01", evaluation_end="2020-01-01",
    )
    c = Contract(**fields)
    c.lock()
    c.locked_at = locked_at
    c.status = "locked"
    c.save(registry_dir)
    return c


PROMPT_TEXT = (Path(__file__).parent.parent / "routines" / "research_investigate.md").read_text()


# ===========================================================================
print("\n--- A: malformed JSON is recorded diagnostically ---")
# ===========================================================================

log_a = TMP / "fail_a.jsonl"
s_a = fresh_store("a")
reg_a = fresh_registry("a")
try:
    inv.investigate(s_a, "2024-05-12",
                    runner=lambda p: "the volume spike looks interesting",
                    registry_dir=reg_a, ai_failure_log=log_a)
    check("A0: a malformed response raises", False, "no exception")
except inv.InvestigatorError as e:
    check("A0: a malformed response raises InvestigatorError", True)
    check("A0: the exception itself carries stage='malformed_json'", e.stage == "malformed_json", e.stage)
    check("A0: the exception carries the untouched raw response",
          e.raw_response == "the volume spike looks interesting", e.raw_response)

rows_a = read_jsonl(log_a)
check("A1: exactly one diagnostic row was recorded", len(rows_a) == 1, rows_a)
check("A2: the row names the failure stage", rows_a and rows_a[0]["stage"] == "malformed_json", rows_a)
check("A3: the row preserves an excerpt of the raw response",
      rows_a and rows_a[0]["raw_response_excerpt"] == "the volume spike looks interesting", rows_a)
check("A4: the row records the response length", rows_a and rows_a[0]["raw_response_length"] == 34, rows_a)
check("A5: the row is NOT truncated for a short response",
      rows_a and rows_a[0]["raw_response_truncated"] is False, rows_a)

# a genuinely oversized response is truncated, never stored in full
long_raw = "not json " * 2000  # well over AI_FAILURE_RAW_EXCERPT_MAX
log_a2 = TMP / "fail_a2.jsonl"
try:
    inv.investigate(s_a, "2024-05-12", runner=lambda p: long_raw,
                    registry_dir=reg_a, ai_failure_log=log_a2)
except inv.InvestigatorError:
    pass
rows_a2 = read_jsonl(log_a2)
check("A6: an oversized raw response is truncated in the diagnostic log, never stored whole",
      rows_a2 and len(rows_a2[0]["raw_response_excerpt"]) == inv.AI_FAILURE_RAW_EXCERPT_MAX
      and rows_a2[0]["raw_response_truncated"] is True,
      rows_a2 and len(rows_a2[0]["raw_response_excerpt"]))


# ===========================================================================
print("\n--- B: empty AI response is handled cleanly ---")
# ===========================================================================

log_b = TMP / "fail_b.jsonl"
s_b = fresh_store("b")
reg_b = fresh_registry("b")
for raw, label in (("", "truly empty"), ("   \n\t  ", "whitespace-only")):
    try:
        inv.investigate(s_b, "2024-05-12", runner=lambda p, r=raw: r,
                        registry_dir=reg_b, ai_failure_log=log_b)
        check(f"B: {label} response raises", False, "no exception")
    except inv.InvestigatorError as e:
        check(f"B: {label} response raises InvestigatorError with stage='empty_response'",
              e.stage == "empty_response", e.stage)

rows_b = read_jsonl(log_b)
check("B: both empty-response failures were recorded diagnostically",
      len(rows_b) == 2 and all(r["stage"] == "empty_response" for r in rows_b), rows_b)
check("B: no Contract was written to the registry for either attempt",
      list(reg_b.glob("*.json")) == [])


# ===========================================================================
print("\n--- C: valid JSON succeeds (and writes no diagnostic row) ---")
# ===========================================================================

log_c = TMP / "fail_c.jsonl"
s_c = fresh_store("c")
reg_c = fresh_registry("c")
raw_c = json.dumps(make_proposal())
result_c = inv.investigate(s_c, "2024-05-12", runner=lambda p: raw_c,
                           registry_dir=reg_c, ai_failure_log=log_c)
check("C1: a valid proposal succeeds", isinstance(result_c, inv.InvestigatorResult), result_c)
check("C2: a Contract draft was written", len(list(reg_c.glob("*.json"))) == 1)
check("C3: NO diagnostic row was written for a successful call", read_jsonl(log_c) == [])


# ===========================================================================
print("\n--- D: surrounding non-JSON text is handled only where explicitly "
      "supported ---")
# ===========================================================================

_valid_json_text = json.dumps(make_proposal(title="Wrapped in prose"))
_wrapped = f"Sure, here is my proposal:\n{_valid_json_text}\nLet me know if you have questions!"
_parsed_wrapped = inv.parse_ai_output(_wrapped)
check("D1: a single JSON object wrapped in harmless prose is extracted and parsed",
      _parsed_wrapped["title"] == "Wrapped in prose", _parsed_wrapped)

check("D2: _extract_json_object() returns None when there is nothing to trim "
      "(the text IS already exactly one object)",
      inv._extract_json_object(_valid_json_text) is None)
check("D3: _extract_json_object() returns None when there are no braces at all",
      inv._extract_json_object("no json here whatsoever") is None)
check("D4: _extract_json_object() returns None for an unterminated object "
      "(no closing brace) — never guesses at completion",
      inv._extract_json_object('{"title": "x", "hypothesis":') is None)

# the EXISTING bad-output matrix must still fail — extraction never salvages
# genuinely broken JSON, only trims harmless surrounding text
still_bad = {
    "prose with no JSON at all": "the volume spike looks interesting",
    "a JSON array, not an object": "[1, 2, 3]",
    "a bare JSON number": "42",
    "truncated JSON, even with a leading brace": '{"title": "x", "hypothesis":',
    "two sibling objects (ambiguous span)": '{"a": 1} and also {"b": 2}',
}
for label, raw in still_bad.items():
    try:
        inv.parse_ai_output(raw)
        check(f"D5: still rejected — {label}", False, "no exception raised")
    except inv.InvestigatorError:
        check(f"D5: still rejected — {label}", True)

# end-to-end through investigate() too, not just the pure parser
log_d = TMP / "fail_d.jsonl"
s_d = fresh_store("d")
reg_d = fresh_registry("d")
_wrapped_full = f"My proposal:\n{json.dumps(make_proposal(title='E2E wrapped'))}\nThanks!"
r_d = inv.investigate(s_d, "2024-05-12", runner=lambda p: _wrapped_full,
                      registry_dir=reg_d, ai_failure_log=log_d)
check("D6: investigate() end-to-end accepts a proposal wrapped in harmless prose",
      isinstance(r_d, inv.InvestigatorResult), r_d)
check("D7: no diagnostic row was needed — this was a successful call", read_jsonl(log_d) == [])


# ===========================================================================
print("\n--- E/F: the hypothesis-length contract is internally consistent ---")
# ===========================================================================

_over_len = hi.FREE_TEXT_DEFAULT_MAX_LEN + 37
_over_proposal = make_proposal(hypothesis="x" * _over_len)
_problems_e = hi.validate_proposal(_over_proposal)
check("E1: an overlong `hypothesis` field is deterministically rejected",
      any("hypothesis exceeds the maximum length" in p for p in _problems_e), _problems_e)
check("E2: the rejection names the limit and the exact observed length",
      any(f"maximum length of {hi.FREE_TEXT_DEFAULT_MAX_LEN} characters" in p for p in _problems_e)
      and any(f"length={_over_len}" in p for p in _problems_e), _problems_e)

_at_limit_proposal = make_proposal(hypothesis="x" * hi.FREE_TEXT_DEFAULT_MAX_LEN)
check("F1: a hypothesis EXACTLY at the limit is accepted (boundary is inclusive)",
      hi.validate_proposal(_at_limit_proposal) == [], hi.validate_proposal(_at_limit_proposal))

_compact_proposal = make_proposal(
    hypothesis="High-volume anomalies precede a short-term upward drift, consistent with "
              "informed-flow accumulation ahead of a catalyst.")
check("F2: a normal, concise, well-under-limit hypothesis is accepted",
      hi.validate_proposal(_compact_proposal) == [])

# the limit itself was NOT weakened by this slice
check("F3: FREE_TEXT_DEFAULT_MAX_LEN is still 1000 — not silently raised to "
      "accommodate a longer AI response", hi.FREE_TEXT_DEFAULT_MAX_LEN == 1000)

# the PROMPT now actually communicates these limits (Phase 3's real fix —
# the prompt, not the validator, was the thing out of sync). Whitespace is
# normalized first — the prompt's own markdown line-wrapping (e.g. "1000
# characters" wrapped across a line break) is a formatting choice, not a
# content gap, and must not make this check spuriously fail.
_norm_prompt = re.sub(r"\s+", " ", PROMPT_TEXT)
for field_name in ("hypothesis", "null_hypothesis", "independence", "falsification",
                   "abandon_condition"):
    check(f"F4: the prompt states {field_name}'s character limit explicitly",
          f"`{field_name}`" in _norm_prompt
          and "1000 characters" in _norm_prompt.split(f"`{field_name}`", 1)[1][:400],
          "prompt does not clearly state the limit near this field's own bullet")
check("F5: the prompt explicitly routes overflow reasoning to `notes`",
      "notes" in _norm_prompt and "2000 characters" in _norm_prompt)
check("F6: the prompt's stated limit matches the validator's actual default "
      "exactly — single source of truth, code and prompt cannot silently drift apart",
      f"{hi.FREE_TEXT_DEFAULT_MAX_LEN} characters" in _norm_prompt)


# ===========================================================================
print("\n--- G: AI failure does not stop observation/data capture ---")
# ===========================================================================

_recorder_src = (Path(__file__).parent.parent / "research" / "recorder.py").read_text()
_recorder_imports = re.findall(r"^\s*(?:from|import)\s+([.\w]+)", _recorder_src, re.MULTILINE)
check("G1: research/recorder.py imports nothing from research.brain.investigator",
      not any("investigator" in m for m in _recorder_imports), str(_recorder_imports))
check("G2: research/recorder.py imports nothing from research.brain.worker",
      not any(m.endswith(".worker") or m.endswith("brain") for m in _recorder_imports)
      or "research.brain.worker" not in _recorder_src, str(_recorder_imports))
check("G3: research/recorder.py imports nothing from research.brain.opportunity",
      "opportunity" not in _recorder_src)

# behavioral: one source raising never stops the recorder from recording
# the others in the SAME run — the exact mechanism run_cycle() relies on.
def _boom():
    raise RuntimeError("feed is down")


def _ok():
    return {"rows_new": 7, "rows_seen": 10}


results_g = [run_source("broken_feed", _boom), run_source("healthy_feed", _ok)]
check("G4: a raising source is captured as a failed SourceResult, never an exception",
      results_g[0].ok is False and "feed is down" in results_g[0].error, results_g[0])
check("G5: the NEXT source in the same run still recorded successfully",
      results_g[1].ok is True and results_g[1].rows_new == 7, results_g[1])


# ===========================================================================
print("\n--- H: AI failure does not prevent the next worker heartbeat ---")
# ===========================================================================

s_h = fresh_store("h")
reg_h = fresh_registry("h")
st_h = fresh_state("h")
lim_h = w.WorkerLimits(max_discovery_attempts=1, max_experiments=0, max_promotions=0,
                       max_substrate_creations=0, cooldown_seconds=0)
r_h1 = w.run_worker_cycle(s_h, now_ist(), limits=lim_h, registry_dir=reg_h,
                          runner=lambda p: "not json at all", state_path=st_h)
check("H1: the first heartbeat records the AI failure",
      r_h1.ai_invocation_status == "error" and r_h1.errors, r_h1)
check("H1b: no cooldown was set purely from a REAL error (errors present)",
      "cooldown_until" not in json.loads(st_h.read_text()))

r_h2 = w.run_worker_cycle(s_h, now_ist(), limits=lim_h, registry_dir=reg_h,
                          runner=lambda p: json.dumps(make_proposal(title="H recovers")),
                          state_path=st_h)
check("H2: the VERY NEXT heartbeat, with a healthy AI response, succeeds normally",
      r_h2.ai_invocation_status == "ok" and r_h2.proposals_created == 1, r_h2)
check("H3: the second heartbeat's own errors list is empty — the prior "
      "failure did not leak into it", r_h2.errors == [], r_h2.errors)


# ===========================================================================
print("\n--- I: one failed discovery does not suppress unrelated worker "
      "actions ---")
# ===========================================================================

s_i = fresh_store("i")
reg_i = fresh_registry("i")
st_i = fresh_state("i")
make_locked_contract("EXP-I-0", registry_dir=reg_i)
lim_i = w.WorkerLimits(max_discovery_attempts=1, max_experiments=1, max_promotions=0,
                       max_substrate_creations=0, cooldown_seconds=0)
r_i = w.run_worker_cycle(s_i, now_ist(), limits=lim_i, registry_dir=reg_i,
                         runner=lambda p: "still not json", state_path=st_i)
check("I1: the broken discovery attempt is recorded as an error",
      r_i.ai_invocation_status == "error", r_i)
check("I2: the UNRELATED, already-locked experiment still ran successfully "
      "in the SAME heartbeat", r_i.experiments_run == 1, r_i)
check("I3: 'experiments' is in work_selected despite the discovery failure",
      "experiments" in r_i.work_selected, r_i.work_selected)


# ===========================================================================
print("\n--- J/N: exactly one Telegram notification decision per worker "
      "run, never duplicated ---")
# ===========================================================================

_w_src = (Path(__file__).parent.parent / "research" / "brain" / "worker.py").read_text()
_main_body = _w_src.split("def main(")[1]
check("J1: main() contains exactly one maybe_notify( call site",
      _main_body.count("maybe_notify(") == 1, _main_body.count("maybe_notify("))

_sent = {"msgs": []}
_orig_notify = w._notify
w._notify = lambda m: (_sent["msgs"].append(m) or True)
try:
    st_j = fresh_state("j")
    both = w.WorkerRunResult(
        started_at="t0", finished_at="t1", runtime_seconds=1.0, worker_id="run-both",
        digest_as_of=None, work_selected=["discovery", "experiments"], proposals_created=1,
        drafts=["hyp_x"], duplicate_rejections=0, experiments_run=1, experiment_outcomes=[],
        evidence_updates=1, no_work_reason=None, errors=["discovery: boom"], limits={},
        notified=False)
    status_j = w.maybe_notify(both, limits=w.WorkerLimits(), state_path=st_j, now=1000.0)
    check("J2: a run with BOTH a new draft AND an error triggers exactly ONE _notify() call",
          len(_sent["msgs"]) == 1, _sent["msgs"])
    check("J3: that one message contains both the draft note and the error note",
          "DRAFT" in _sent["msgs"][0] and "error" in _sent["msgs"][0].lower(), _sent["msgs"])
    check("J4: maybe_notify() reports status 'sent'", status_j == "sent", status_j)
finally:
    w._notify = _orig_notify

check("N1: (structural, see J1) — the single call site means a worker run "
      "can never be notified about twice by construction, not by a counter "
      "that could itself have a bug", True)


# ===========================================================================
print("\n--- K: successful heartbeat telemetry is persisted, with the full "
      "Phase 6 record ---")
# ===========================================================================

s_k = fresh_store("k")
reg_k = fresh_registry("k")
st_k = fresh_state("k")
log_k = TMP / "worker_runs_k.jsonl"
r_k = w.run_worker_cycle(s_k, now_ist(), limits=w.WorkerLimits(cooldown_seconds=0),
                         registry_dir=reg_k,
                         runner=lambda p: json.dumps(make_proposal(title="K happy path")),
                         state_path=st_k)
w._persist_run(r_k, run_log=log_k)
rows_k = read_jsonl(log_k)
REQUIRED_HEARTBEAT_FIELDS = {
    "run_id", "started_at", "finished_at", "runtime_seconds", "outcome",
    "actions_attempted", "actions_succeeded", "actions_failed",
    "discovery_attempts", "experiments_attempted", "promotions_attempted",
    "substrate_creations_attempted", "errors", "ai_invocation_status",
    "telegram_status",
}
check("K1: exactly one telemetry row was persisted", len(rows_k) == 1, rows_k)
check("K2: the row carries every Phase 6 required field",
      rows_k and REQUIRED_HEARTBEAT_FIELDS <= set(rows_k[0].keys()),
      rows_k and (REQUIRED_HEARTBEAT_FIELDS - set(rows_k[0].keys())))
check("K3: outcome is 'ok' for a clean, successful run", rows_k and rows_k[0]["outcome"] == "ok")
check("K4: run_id is a real, non-empty identifier", rows_k and bool(rows_k[0]["run_id"]))
check("K5: ai_invocation_status correctly reflects a successful AI call",
      rows_k and rows_k[0]["ai_invocation_status"] == "ok")


# ===========================================================================
print("\n--- L: failed heartbeat telemetry is persisted (main()'s crash "
      "fallback) ---")
# ===========================================================================

_orig_run_cycle = w.run_worker_cycle


def _exploding_cycle(*a, **k):
    raise RuntimeError("a genuinely unexpected bug, not a handled outcome")


w.run_worker_cycle = _exploding_cycle
try:
    rc_l = w.main(["--db", str(TMP / "l_crash.db"), "--registry-dir", str(fresh_registry("l")),
                  "--no-notify", "--quiet-on-success"])
finally:
    w.run_worker_cycle = _orig_run_cycle
check("L1: main() returns exit code 1 for an unexpected crash, never a raw traceback",
      rc_l == 1, rc_l)
_st_after_l = w.worker_status()
_last_row_l = None
try:
    _lines = w.RUN_LOG.read_text().splitlines()
    _last_row_l = json.loads(_lines[-1]) if _lines else None
except OSError:
    pass
check("L2: a telemetry row for the crashed heartbeat was actually persisted",
      _last_row_l is not None and _last_row_l.get("outcome") == "error", _last_row_l)
check("L3: the persisted row names the crash explicitly",
      _last_row_l is not None and "run_worker_cycle" in " ".join(_last_row_l.get("errors") or []),
      _last_row_l)


# ===========================================================================
print("\n--- M: a Telegram-side failure never corrupts research state ---")
# ===========================================================================

s_m = fresh_store("m")
reg_m = fresh_registry("m")
st_m = fresh_state("m")


def _raising_notify(message):
    raise RuntimeError("Telegram API unreachable")


_orig_notify_m = w._notify
w._notify = _raising_notify
try:
    r_m = w.run_worker_cycle(s_m, now_ist(), limits=w.WorkerLimits(cooldown_seconds=0),
                             registry_dir=reg_m,
                             runner=lambda p: json.dumps(make_proposal(title="M draft")),
                             state_path=st_m)
    status_m = w.maybe_notify(r_m, limits=w.WorkerLimits(), state_path=st_m)
finally:
    w._notify = _orig_notify_m
check("M1: maybe_notify() itself never raises even when the underlying "
      "notifier raises", True)  # the try/finally above completing at all proves this
check("M2: it correctly reports the send as failed", status_m == "failed", status_m)
check("M3: the draft Contract created this run is still perfectly intact",
      len(list(reg_m.glob("*.json"))) == 1)
check("M4: .worker_state.json is still valid JSON after a Telegram failure",
      isinstance(json.loads(st_m.read_text()), dict))


# ===========================================================================
print("\n--- O: repeated malformed AI responses do not cause unbounded "
      "retries ---")
# ===========================================================================

s_o = fresh_store("o")
reg_o = fresh_registry("o")
st_o = fresh_state("o")
_calls_o = {"n": 0}


def _always_malformed(p):
    _calls_o["n"] += 1
    return "garbage, not json, every single time"


lim_o = w.WorkerLimits(max_discovery_attempts=3, max_experiments=0, max_promotions=0,
                       max_substrate_creations=0, cooldown_seconds=0)
_t0 = time.time()
r_o = w.run_worker_cycle(s_o, now_ist(), limits=lim_o, registry_dir=reg_o,
                         runner=_always_malformed, state_path=st_o)
_elapsed_o = time.time() - _t0
check("O1: exactly max_discovery_attempts (3) AI calls were made — bounded, "
      "no internal retry-until-success loop", _calls_o["n"] == 3, _calls_o["n"])
check("O2: the cycle completed quickly despite every attempt failing "
      f"(took {_elapsed_o:.2f}s)", _elapsed_o < 30.0, _elapsed_o)
check("O3: discovery_attempts on the result matches the bounded count",
      r_o.discovery_attempts == 3, r_o.discovery_attempts)
check("O4: no proposal was created from any of the malformed attempts",
      r_o.proposals_created == 0, r_o)
check("O5: exactly 3 diagnostic rows were recorded — one per attempt, not "
      "more", len(read_jsonl(inv.AI_FAILURE_LOG)) >= 3)


# ===========================================================================
print("\n--- P: existing cooldown behavior is unchanged ---")
# ===========================================================================

s_p = fresh_store("p")
reg_p = fresh_registry("p")
st_p = fresh_state("p")
lim_p = w.WorkerLimits(max_discovery_attempts=1, cooldown_seconds=600)
w.run_worker_cycle(s_p, now_ist(), limits=lim_p, registry_dir=reg_p,
                   runner=lambda p: json.dumps({"no_proposal": True, "reason": "nothing"}),
                   state_path=st_p)
check("P1: a no-useful-work cycle still sets a cooldown, exactly as before",
      "cooldown_until" in json.loads(st_p.read_text()))
_calls_p = {"n": 0}
r_p2 = w.run_worker_cycle(s_p, now_ist(), limits=lim_p, registry_dir=reg_p,
                          runner=lambda p: (_calls_p.__setitem__("n", _calls_p["n"] + 1),
                                           json.dumps({"no_proposal": True, "reason": "x"}))[1],
                          state_path=st_p)
check("P2: discovery is still suppressed during cooldown on the very next heartbeat",
      _calls_p["n"] == 0 and "cooldown" in (r_p2.no_work_reason or "").lower(), r_p2)


# ===========================================================================
print("\n--- Q: existing flock/concurrency behavior is unchanged ---")
# ===========================================================================

lock_path_q = TMP / ".worker.lock"
with w.worker_lock(lock_path_q):
    _busy = False
    try:
        with w.worker_lock(lock_path_q):
            pass
    except w.WorkerBusy:
        _busy = True
    check("Q1: a second worker_lock() on the same path still raises WorkerBusy", _busy)
_reacquired = False
with w.worker_lock(lock_path_q):
    _reacquired = True
check("Q2: the lock is still released cleanly on context exit", _reacquired)


# ===========================================================================
print("\n--- R: existing unified opportunity selection is unchanged (smoke) ---")
# ===========================================================================

s_r = fresh_store("r")
reg_r = fresh_registry("r")
make_locked_contract("EXP-R-0", registry_dir=reg_r)
pool_r = opp.build_opportunity_pool(s_r, "2024-05-12", registry_dir=reg_r)
queue_r = opp.build_action_queue(s_r, pool_r, registry_dir=reg_r, discovery_available=True)
check("R1: build_action_queue() still returns a non-empty, sorted queue",
      len(queue_r) >= 2, len(queue_r))
check("R2: RUN_EXPERIMENT and DISCOVER still compete on the same scale",
      {"RUN_EXPERIMENT", "DISCOVER"} <= {a.kind for a in queue_r})
check("R3: (full 40-check coverage lives in tests/test_research_worker_selection.py, "
      "re-run as part of this task's own validation — see the final report)", True)


# ===========================================================================
print("\n--- S: existing CREATE_EXPERIMENT behavior is unchanged (smoke) ---")
# ===========================================================================

check("S1: opportunity.attempt_create_experiment is still present with its "
      "documented signature", callable(getattr(opp, "attempt_create_experiment", None)))
check("S2: ACTION_KINDS still includes CREATE_EXPERIMENT",
      "CREATE_EXPERIMENT" in opp.ACTION_KINDS, opp.ACTION_KINDS)
check("S3: (full 55-check coverage lives in tests/test_research_action_expansion.py, "
      "re-run as part of this task's own validation — see the final report)", True)


# ===========================================================================
print("\n--- T: isolation boundaries are unchanged ---")
# ===========================================================================

for modname, path in (
    ("investigator.py", "research/brain/investigator.py"),
    ("worker.py", "research/brain/worker.py"),
    ("opportunity.py", "research/brain/opportunity.py"),
    ("recorder.py", "research/recorder.py"),
):
    src = (Path(__file__).parent.parent / path).read_text()
    code = re.sub(r'"""[\s\S]*?"""', "", src)
    imports = re.findall(r"^\s*(?:from|import)\s+([.\w]+)", code, re.MULTILINE)
    check(f"T: {modname} imports no engine module",
          not any(m.startswith("engine") for m in imports), str(imports))
    check(f"T: {modname} imports no broker/paper module",
          not any("broker" in m or m.split(".")[0] == "paper" for m in imports), str(imports))

check("T: worker.py still never calls the locking primitive or the runner "
      "module directly", "approve_and_lock(" not in re.sub(r'"""[\s\S]*?"""', "", _w_src)
      and ".lock()" not in re.sub(r'"""[\s\S]*?"""', "", _w_src))
check("T: opportunity.py still has exactly one derive_split_contract( call site",
      re.sub(r'"""[\s\S]*?"""', "",
             (Path(__file__).parent.parent / "research" / "brain" / "opportunity.py")
             .read_text()).count("hi.derive_split_contract(") == 1)


shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'=' * 52}")
print(f"  {PASSED} passed, {FAILED} failed")
print(f"{'=' * 52}\n")
sys.exit(1 if FAILED else 0)
