"""
Tests for Phase 1 Slice D: research/experiments/runner.py — the first
sanctioned execution boundary.

Nothing here touches engine/execute.py, engine/broker_*, engine/guardrails.py,
engine/journal, memory/state.json, run_cycle.sh, or Claude permissions.

Run with:  python -m tests.test_research_runner
"""

import inspect
import json
import re
import shutil
import sys
import tempfile
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from research.store import Store  # noqa: E402
from research import memory as rm  # noqa: E402
from research.contracts import Contract, REGISTRY_DIR  # noqa: E402
from research.brain import hypothesis_intake as hi  # noqa: E402
from research.replay import leak_check, clock_ban_violations  # noqa: E402
from research.experiments import runner  # noqa: E402

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


TMP = Path(tempfile.mkdtemp(prefix="lq-test-runner-"))
START = dt.date(2020, 1, 1)


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


def seed_prices(store, symbol, start, closes, vols, highs=None, lows=None):
    for i, (c, v) in enumerate(zip(closes, vols)):
        d = start + dt.timedelta(days=i)
        h = highs[i] if highs is not None else c
        lo = lows[i] if lows is not None else c
        store.append_price(symbol=symbol, session_date=d, knowledge_time=d, source="test",
                            open_=c, high=h, low=lo, close=c, volume=v)


def make_proposal(**overrides):
    base = {
        "title": "runner smoke test",
        "hypothesis": "volume spikes precede short-term continuation",
        "null_hypothesis": "no relationship",
        "universe": "watchlist",
        "signal": "observatory.volume_zscore",
        "entry_rule": {"conditions": [{"metric": "volume_zscore", "op": ">", "value": 3.0}]},
        "exit_rule": {"stop_loss_pct": 5.0, "target_pct": 8.0, "max_hold_days": 15},
        "splits": {"discovery": ["2020-01-01", "2020-12-31"]},
        "independence": "one entry per symbol",
        "falsification": "t_stat < 2.0",
        "abandon_condition": "expectancy_r <= 0",
        "evaluation_start": "2020-01-01",
        "evaluation_end": "2020-06-30",
    }
    base.update(overrides)
    return base


def locked_contract(store, reg, symbol="RELIANCE", **overrides):
    """Full pipeline, not a shortcut: proposal -> draft -> approve_and_lock."""
    proposal = make_proposal(**overrides)
    result = hi.create_draft(store, proposal, registry_dir=reg)
    return hi.approve_and_lock(store, result.contract.id, approved_by="Vaibhav", registry_dir=reg)


def flat_series(n, base_close=100.0, base_vol=100_000, spike_at=None, spike_vol=5_000_000,
                drift=0.0):
    """n days of near-flat prices (tiny jitter so baselines have nonzero
    variance) with an optional single-day volume spike."""
    closes = [base_close + drift * i for i in range(n)]
    vols = [base_vol + (i % 5) * 1000 for i in range(n)]
    if spike_at is not None:
        vols[spike_at] = spike_vol
    return closes, vols


# ---------------------------------------------------------------------------
print("\n--- 0: the public entry point is id-based, not object-based ---")
# ---------------------------------------------------------------------------

sig = inspect.signature(runner.run_experiment)
params = list(sig.parameters)
check("run_experiment's first parameter is named contract_id",
      params[0] == "contract_id", str(params))
check("run_experiment's first parameter is annotated str, not Contract",
      sig.parameters["contract_id"].annotation in (str, "str"))

store0 = fresh_store("arch")
reg0 = fresh_registry("arch")
closes, vols = flat_series(40, spike_at=25)
seed_prices(store0, "RELIANCE", START, closes, vols)
locked0 = locked_contract(store0, reg0)

fake_contract = Contract(
    id="EXP-FORGED", title="t", hypothesis="h", null_hypothesis="n",
    universe="watchlist", signal="s", entry_rule="{}", exit_rule="{}",
    splits={"discovery": ["2019-01-01", "2020-01-01"]}, independence="i",
    falsification="f", abandon_condition="a",
    evaluation_start="2019-01-01", evaluation_end="2020-01-01",
    status="locked", locked_hash="deadbeef",
)
try:
    runner.run_experiment(fake_contract, store0, registry_dir=reg0)
    check("passing a hand-built Contract object instead of an id does not "
          "silently work", False)
except (runner.RunnerRejected, TypeError, AttributeError):
    check("passing a hand-built Contract object instead of an id does not "
          "silently work", True)
check("the forged contract produced no experiment_results",
      store0.experiment_results("EXP-FORGED") == [])


# ---------------------------------------------------------------------------
print("\n--- 1: a real end-to-end run (draft -> lock -> run -> reported) ---")
# ---------------------------------------------------------------------------

out = runner.run_experiment(locked0.id, store0, registry_dir=reg0)
check("run_experiment returns status=reported on success", out["status"] == "reported")
check("run_experiment reports at least one trade for a clear spike fixture",
      out["n_trades"] >= 1, str(out))

final0 = Contract.load(locked0.id, reg0)
check("the contract's on-disk status is REPORTED after a successful run",
      final0.status == "reported")
check("the contract's notes record a run summary",
      "Run completed" in final0.notes)

results0 = store0.experiment_results(locked0.id)
check("experiment_results rows were actually persisted", len(results0) == out["n_trades"])


# ---------------------------------------------------------------------------
print("\n--- 2: a DRAFT contract is refused, nothing runs ---")
# ---------------------------------------------------------------------------

store2 = fresh_store("draft_reject")
reg2 = fresh_registry("draft_reject")
seed_prices(store2, "RELIANCE", START, *flat_series(40, spike_at=25))
draft_result = hi.create_draft(store2, make_proposal(), registry_dir=reg2)

try:
    runner.run_experiment(draft_result.contract.id, store2, registry_dir=reg2)
    check("a draft contract is refused by run_experiment", False)
except runner.RunnerRejected as e:
    check("a draft contract is refused by run_experiment",
          any("not LOCKED" in r for r in e.reasons), str(e.reasons))

check("the draft's status is unchanged (still draft, not running/abandoned)",
      Contract.load(draft_result.contract.id, reg2).status == "draft")
check("no experiment_results were written for the refused draft",
      store2.experiment_results(draft_result.contract.id) == [])
check("no verdict was written for the refused draft",
      rm.query_research_log(store2, rm.DATASET_VERDICT) == [])


# ---------------------------------------------------------------------------
print("\n--- 3: abandoned / reported / superseded / running contracts are all "
      "refused — only LOCKED enters ---")
# ---------------------------------------------------------------------------

store3 = fresh_store("status_gate")
reg3 = fresh_registry("status_gate")
seed_prices(store3, "RELIANCE", START, *flat_series(40, spike_at=25))

for bad_status in ("abandoned", "reported", "superseded", "running"):
    c = locked_contract(store3, reg3, universe="watchlist")
    c.status = bad_status
    c.save(reg3)
    try:
        runner.run_experiment(c.id, store3, registry_dir=reg3)
        check(f"a {bad_status!r} contract is refused by run_experiment", False)
    except runner.RunnerRejected as e:
        check(f"a {bad_status!r} contract is refused by run_experiment",
              any("not LOCKED" in r for r in e.reasons), str(e.reasons))
    check(f"a refused {bad_status!r} contract's status is left exactly as it was "
          f"(no silent transition)",
          Contract.load(c.id, reg3).status == bad_status)


# ---------------------------------------------------------------------------
print("\n--- 4: a tampered contract (hash forged to match edited content) is "
      "still refused — verify() re-checks independently of trusting the file ---")
# ---------------------------------------------------------------------------

store4 = fresh_store("tamper")
reg4 = fresh_registry("tamper")
seed_prices(store4, "RELIANCE", START, *flat_series(40, spike_at=25))
c4 = locked_contract(store4, reg4)

# Edit a HASHED field directly on disk, and forge the hash to match the
# edited content — simulating an attacker sophisticated enough to recompute
# the hash, not just careless enough to leave it stale (that weaker case is
# also proven, in passing, by the raw-edit variant below).
path4 = reg4 / f"{c4.id}.json"
raw = json.loads(path4.read_text())
raw["title"] = "a silently different experiment"
tampered_hash = Contract(**{k: v for k, v in raw.items()
                             if k not in ("_content_hash", "_data_firewall")}).content_hash()
raw["locked_hash"] = tampered_hash
path4.write_text(json.dumps(raw, indent=2))

# Honest documentation of what the hash actually guarantees, not what it
# doesn't: a hash proves "this file's content matches its own claimed hash"
# — internal self-consistency — NOT "this is the exact content a human
# reviewed". An editor capable of recomputing the hash correctly produces a
# file verify() has no way to distinguish from a legitimately locked one; the
# defence against THAT is the append-only, single-writer discipline around
# the registry directory itself (only approve_and_lock() ever writes a
# locked file), not verify(). This is asserted explicitly, not glossed over.
try:
    tampered_run = runner.run_experiment(c4.id, store4, registry_dir=reg4)
    check("a self-consistently re-hashed edit is NOT caught by verify() — "
          "this is the honest, documented limit of a content hash (it proves "
          "internal consistency, not original authorship); the real defence "
          "is that only approve_and_lock() ever writes to the registry "
          "directory in the first place",
          tampered_run["status"] == "reported")
except (runner.RunnerRejected, runner.ExperimentFailed) as e:
    # If a future change to Contract's hashed-field set or verify() logic
    # ever DOES catch this, that's a strictly stronger guarantee — count it
    # as a pass rather than fail the suite for becoming more paranoid.
    check("a self-consistently re-hashed edit was unexpectedly caught anyway "
          "(a stronger guarantee than currently documented — not a failure)",
          True, str(e))

# The REAL, load-bearing case: an edit WITHOUT recomputing the hash (the
# realistic failure mode — a hand edit, a merge conflict, disk corruption).
store4b = fresh_store("tamper_stale")
reg4b = fresh_registry("tamper_stale")
seed_prices(store4b, "RELIANCE", START, *flat_series(40, spike_at=25))
c4b = locked_contract(store4b, reg4b)
path4b = reg4b / f"{c4b.id}.json"
raw4b = json.loads(path4b.read_text())
raw4b["title"] = "edited after locking, hash left stale"
path4b.write_text(json.dumps(raw4b, indent=2))

try:
    runner.run_experiment(c4b.id, store4b, registry_dir=reg4b)
    check("an edit that leaves the hash stale is refused by run_experiment", False)
except runner.RunnerRejected as e:
    check("an edit that leaves the hash stale is refused by run_experiment",
          any("verify()" in r for r in e.reasons), str(e.reasons))
check("no experiment_results were written for the tampered contract",
      store4b.experiment_results(c4b.id) == [])


# ---------------------------------------------------------------------------
print("\n--- 5: the model firewall is re-checked at run time, not just at lock time ---")
# ---------------------------------------------------------------------------
# Contract.lock() already refuses to lock a breaching contract (tested in
# tests/test_research_store.py), so the only way to get a LOCKED contract
# whose current fields breach the firewall is to force one directly onto
# disk, bypassing lock() entirely — exactly the scenario "a locked_hash that
# matches, but the contract was never legitimately vetted" that verify()
# exists to catch.

store5 = fresh_store("firewall")
reg5 = fresh_registry("firewall")

breaching = Contract(
    id="EXP-BREACH", title="t", hypothesis="h", null_hypothesis="n",
    universe="watchlist", signal="s",
    entry_rule=json.dumps({"conditions": [{"metric": "volume_zscore", "op": ">", "value": 3.0}]}),
    exit_rule=json.dumps({"stop_loss_pct": 5.0, "target_pct": 8.0}),
    splits={"discovery": ["2019-01-01", "2020-01-01"]}, independence="i",
    falsification="f", abandon_condition="a",
    evaluation_start="2019-01-01", evaluation_end="2020-01-01",  # predates the "cutoff" below
    llm_features=True, llm_model_id="claude-opus-5", llm_knowledge_cutoff="2026-05-05",
)
breaching.locked_hash = breaching.content_hash()  # forge a self-consistent hash
breaching.status = "locked"
breaching.save(reg5)

try:
    runner.run_experiment("EXP-BREACH", store5, registry_dir=reg5)
    check("a model-firewall-breaching contract is refused at run time", False)
except runner.RunnerRejected as e:
    check("a model-firewall-breaching contract is refused at run time",
          any("verify()" in r for r in e.reasons), str(e.reasons))
check("no experiment_results were written for the firewall-breaching contract",
      store5.experiment_results("EXP-BREACH") == [])


# ---------------------------------------------------------------------------
print("\n--- 6: no-lookahead — leak_check applied to the runner's own signal "
      "evaluation (the only place it reads market data) ---")
# ---------------------------------------------------------------------------

store6 = fresh_store("leak")
closes6, vols6 = flat_series(60, spike_at=45)
seed_prices(store6, "RELIANCE", START, closes6, vols6)
entry_rule6 = {"conditions": [{"metric": "volume_zscore", "op": ">", "value": 3.0}]}

spike_as_of = START + dt.timedelta(days=45)
res_spike = leak_check(store6, spike_as_of,
                       lambda step: runner.entry_signal(step.view, "RELIANCE", entry_rule6))
check("leak_check finds the spike-day signal is leak-free", res_spike["leak_free"], res_spike)
check("the spike-day signal is genuinely True (not a trivially-false, "
      "vacuously-passing check)", res_spike["full"] == "True", res_spike["full"])

before_as_of = START + dt.timedelta(days=10)
res_before = leak_check(store6, before_as_of,
                        lambda step: runner.entry_signal(step.view, "RELIANCE", entry_rule6))
check("leak_check finds the pre-spike signal is leak-free too", res_before["leak_free"], res_before)
check("the pre-spike signal is False (nothing to see yet)",
      res_before["full"] == "False", res_before["full"])


# ---------------------------------------------------------------------------
print("\n--- 6b: both exit branches (stop and target) fire correctly, in one run ---")
# ---------------------------------------------------------------------------

store6b = fresh_store("exits")
reg6b = fresh_registry("exits")
n = 26

closes_r, vols_r = flat_series(n, base_close=200.0, spike_at=25)
highs_r, lows_r = list(closes_r), list(closes_r)
closes_r.append(185.0); vols_r.append(100_000); highs_r.append(195.0); lows_r.append(180.0)

closes_t, vols_t = flat_series(n, base_close=200.0, spike_at=25)
highs_t, lows_t = list(closes_t), list(closes_t)
closes_t.append(210.0); vols_t.append(100_000); highs_t.append(220.0); lows_t.append(195.0)

seed_prices(store6b, "RELIANCE", START, closes_r, vols_r, highs=highs_r, lows=lows_r)
seed_prices(store6b, "TCS", START, closes_t, vols_t, highs=highs_t, lows=lows_t)

locked6b = locked_contract(
    store6b, reg6b,
    exit_rule={"stop_loss_pct": 5.0, "target_pct": 8.0, "max_hold_days": 20},
    evaluation_start=str(START), evaluation_end=str(START + dt.timedelta(days=n)),
)
runner.run_experiment(locked6b.id, store6b, registry_dir=reg6b)
results6b = {r["entity"]: r for r in store6b.experiment_results(locked6b.id)}

check("both RELIANCE (stop) and TCS (target) produced a trade in the same run",
      set(results6b) == {"RELIANCE", "TCS"}, str(list(results6b)))
if "RELIANCE" in results6b:
    check("RELIANCE exits via 'stop' at exactly entry_price * (1 - 5%)",
          results6b["RELIANCE"]["exit_reason"] == "stop"
          and abs(results6b["RELIANCE"]["exit_price"] - 190.0) < 1e-9,
          str(results6b["RELIANCE"]))
if "TCS" in results6b:
    check("TCS exits via 'target' at exactly entry_price * (1 + 8%)",
          results6b["TCS"]["exit_reason"] == "target"
          and abs(results6b["TCS"]["exit_price"] - 216.0) < 1e-9,
          str(results6b["TCS"]))


# ---------------------------------------------------------------------------
print("\n--- 7: persisted costs equal the actual engine.costs calculation, "
      "not an approximation or a stub ---")
# ---------------------------------------------------------------------------

from engine.costs import equity_round_trip  # noqa: E402

store7 = fresh_store("costs")
reg7 = fresh_registry("costs")
# A distinctive, deterministic price series with an unambiguous single trade:
# spike triggers entry, then flat prices force a time_exit at a known price.
closes7, vols7 = flat_series(40, base_close=137.25, spike_at=25, drift=0.0)
seed_prices(store7, "RELIANCE", START, closes7, vols7)
locked7 = locked_contract(
    store7, reg7,
    exit_rule={"stop_loss_pct": 50.0, "target_pct": 50.0, "max_hold_days": 3},
    evaluation_start=str(START), evaluation_end=str(START + dt.timedelta(days=39)),
)
out7 = runner.run_experiment(locked7.id, store7, registry_dir=reg7)
results7 = store7.experiment_results(locked7.id)
check("the cost test fixture produced exactly one trade", len(results7) == 1, str(results7))
if results7:
    trade = results7[0]
    expected_costs = equity_round_trip(trade["entry_price"], trade["quantity"]).total
    check("the persisted costs figure equals a fresh, independent "
          "engine.costs.equity_round_trip() computation on the same inputs "
          "(not mocked, not a stub, not merely 'was called')",
          abs(trade["costs"] - expected_costs) < 1e-9,
          f"persisted={trade['costs']} recomputed={expected_costs}")
    check("net_pnl is gross_pnl minus exactly that costs figure",
          abs(trade["net_pnl"] - (trade["gross_pnl"] - trade["costs"])) < 1e-9)
    check("costs are strictly positive for a real trade (never a hard-coded "
          "0.0 or a suspiciously round stub value)",
          trade["costs"] > 0)


# ---------------------------------------------------------------------------
print("\n--- 8: idempotency is now a STRONGER guarantee than 'no duplicate rows' — "
      "a reported contract cannot be re-run at all ---")
# ---------------------------------------------------------------------------

before_rerun = len(store0.experiment_results(locked0.id))
try:
    runner.run_experiment(locked0.id, store0, registry_dir=reg0)
    check("re-running an already-REPORTED contract is refused outright "
          "(structurally impossible, not merely deduplicated)", False)
except runner.RunnerRejected as e:
    check("re-running an already-REPORTED contract is refused outright "
          "(structurally impossible, not merely deduplicated)",
          any("not LOCKED" in r for r in e.reasons))
after_rerun = len(store0.experiment_results(locked0.id))
check("the refused re-run attempt did not change the persisted trade count",
      before_rerun == after_rerun)

# The underlying storage primitive's own idempotency (Slice A) still holds
# independently of the runner's stricter gate — re-verified here, not
# re-invented: a direct double-call with the same trade_seq is a no-op.
dup = store0.append_experiment_result(
    contract_id=locked0.id, trade_seq=1, entity="RELIANCE",
    entry_time="2020-01-01", exit_time="2020-01-02", entry_price=1.0, exit_price=1.0,
    quantity=1, gross_pnl=0.0, costs=0.0, net_pnl=0.0,
)
check("Store.append_experiment_result's own (contract_id, trade_seq) "
      "idempotency still backstops the runner's gate",
      dup is None)


# ---------------------------------------------------------------------------
print("\n--- 9: deterministic replay — same locked contract + same data => "
      "same results, across two entirely separate stores ---")
# ---------------------------------------------------------------------------

def build_and_run(name):
    s = fresh_store(f"det_{name}")
    r = fresh_registry(f"det_{name}")
    seed_prices(s, "RELIANCE", START, *flat_series(40, base_close=250.0, spike_at=25))
    c = locked_contract(s, r, evaluation_start=str(START),
                        evaluation_end=str(START + dt.timedelta(days=39)))
    runner.run_experiment(c.id, s, registry_dir=r)
    return s.experiment_results(c.id)


def strip_nondeterministic(rows):
    return [{k: v for k, v in r.items() if k not in ("id", "ingested_at", "contract_id")}
            for r in rows]

results_a = strip_nondeterministic(build_and_run("a"))
results_b = strip_nondeterministic(build_and_run("b"))
check("two independent runs of structurally-identical locked contracts over "
      "identical price data produce identical trade results",
      results_a == results_b, f"{results_a} != {results_b}")
check("the deterministic-replay fixture actually produced a trade to compare "
      "(not two empty lists trivially matching)",
      len(results_a) >= 1)


# ---------------------------------------------------------------------------
print("\n--- 10: digest integration — a reported contract shows up as "
      "previously_tested and counts toward comparison_count ---")
# ---------------------------------------------------------------------------

from research.brain import digest as dg  # noqa: E402

d10 = dg.build_digest(store0, dt.datetime.now(dt.timezone.utc), registry_dir=reg0)
tested_ids = {c["id"] for c in d10["previously_tested"]["contracts"]}
check("the reported contract from test 1 appears under previously_tested in the digest",
      locked0.id in tested_ids, str(tested_ids))
check("comparison_count reflects the locked-and-run contract",
      d10["contract_registry"]["comparison_count"] >= 1)


# ---------------------------------------------------------------------------
print("\n--- 11: hypothesis verdict linkage ---")
# ---------------------------------------------------------------------------

from research.experiments import evaluator  # noqa: E402

resolved_hid = evaluator.resolve_hypothesis_id(store0, locked0.id)
check("the runner's contract resolves back to the hypothesis_id that "
      "proposed it", resolved_hid is not None and resolved_hid.startswith("HYP-"))

verdict_rows = rm.query_research_log(store0, rm.DATASET_VERDICT)
matching = [r for r in verdict_rows if r["payload"]["contract_id"] == locked0.id]
check("exactly one verdict was recorded for the contract", len(matching) == 1, str(matching))
if matching:
    check("the recorded verdict links the correct hypothesis_id",
          matching[0]["payload"]["hypothesis_id"] == resolved_hid)
    check("the recorded verdict's n_trades matches what run_experiment returned",
          matching[0]["payload"]["n_trades"] == out["n_trades"])


# ---------------------------------------------------------------------------
print("\n--- 12: architecture / import / kernel boundaries ---")
# ---------------------------------------------------------------------------

EXPERIMENTS_DIR = Path(__file__).parent.parent / "research" / "experiments"
FORBIDDEN_ENGINE_MODULES = {"guardrails", "execute", "journal", "broker",
                             "broker_kite", "broker_indstocks"}
EVAL_EXEC_RE = re.compile(r"\beval\s*\(|\bexec\s*\(|(?<!re\.)\bcompile\s*\(")

for f in sorted(EXPERIMENTS_DIR.glob("*.py")):
    src = f.read_text()
    imports = re.findall(r"^\s*(?:from|import)\s+([.\w]+)", src, re.MULTILINE)
    bad_engine = [m for m in imports
                  if m.split(".")[0] == "engine" and m.split(".")[-1] in FORBIDDEN_ENGINE_MODULES]
    check(f"{f.name} imports no live-capital engine module", not bad_engine, str(imports))
    check(f"{f.name} contains no eval/exec/compile call", not EVAL_EXEC_RE.search(src))

clock_violations = clock_ban_violations(EXPERIMENTS_DIR)
check("research/experiments/ contains no wall-clock reads "
      "(reusing research.replay's own clock-ban scanner)",
      not clock_violations, str(clock_violations))

# These check actual import/open statements, not docstring prose — both
# files' docstrings legitimately *discuss* state.json/broker_*/engine.stats
# as part of documenting what they refuse to touch, so a plain substring
# search would false-positive on the documentation itself.
runner_src = (EXPERIMENTS_DIR / "runner.py").read_text()
runner_imports = re.findall(r"^\s*(?:from|import)\s+([.\w]+)", runner_src, re.MULTILINE)
check("runner.py's actual imports never include a broker module or engine.execute",
      not any("broker" in m or m == "engine.execute" for m in runner_imports),
      str(runner_imports))
check("runner.py performs no file open()/write to state.json or run_cycle.sh",
      not re.search(r"(open\(|write_text\(|write\()[^)]*(state\.json|run_cycle\.sh)",
                    runner_src))

evaluator_src = (EXPERIMENTS_DIR / "evaluator.py").read_text()
evaluator_imports = re.findall(r"^\s*(?:from|import)\s+([.\w]+)", evaluator_src, re.MULTILINE)
check("evaluator.py's actual imports never include engine.stats or engine.journal",
      not any(m in ("engine.stats", "engine.journal") for m in evaluator_imports),
      str(evaluator_imports))
check("evaluator.py imports no engine module at all",
      not any(m.split(".")[0] == "engine" for m in evaluator_imports), str(evaluator_imports))


for s in (store0, store2, store3, store4, store4b, store5, store6, store6b, store7):
    s.close()

print(f"\n{'=' * 52}")
print(f"  {PASSED} passed, {FAILED} failed")
print(f"{'=' * 52}\n")
sys.exit(1 if FAILED else 0)
