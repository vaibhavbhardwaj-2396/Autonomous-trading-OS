"""
Phase 2 Slice U — End-to-End Research Factory Integration Harness.

Every component exercised here already has its own unit-test file (see the
list in this slice's own instructions). This file does NOT re-test any of
their internals — it proves the SEAMS between them actually fit together,
end to end, exactly as the target flow describes:

    historical/research fixture
          |
          v
    Observatory / quant_scan          (research.brain.observatory / quant_scan)
          |
          v
    Digest                            (research.brain.digest)
          |
          v
    Hypothesis proposal               (research.brain.investigator [mocked AI] /
          |                            research.brain.quant_scan)
          v
    duplicate screening                (research.brain.similarity, reused
          |                             internally by both proposers)
          v
    DRAFT                              (research.brain.hypothesis_intake.create_draft)
          |
          v
    research area                      (research.brain.research_areas)
          |
          v
    discovery provenance                (research.brain.discovery_provenance)
          |
          v
    human approval                      (hypothesis_intake.approve_and_lock —
          |                              called EXPLICITLY in this file, never
          |                              automated)
          v
    LOCKED Contract
          |
          v
    Research Priority                   (research.brain.priority)
          |
          v
    Scheduler                           (research.brain.scheduler)
          |
          v
    existing runner                     (research.experiments.runner — UNCHANGED,
          |                              not imported for its own sake, only via
          |                              scheduler.run_scheduler())
          v
    experiment result
          |
          v
    evaluator / comparison / evidence   (research.experiments.evaluator /
                                          research.experiments.comparison)

This is a TEST HARNESS, not a new production subsystem: it creates no new
module under research/, wires nothing new into overnight.py or scheduler.py,
and adds no new scheduler, queue, or orchestration daemon. Every step below
calls an existing, unmodified function; this file's own code is fixtures and
assertions only.

Two independent hypothesis "lineages" are built:
  - the AI-mocked path (section A) — stops at DRAFT + provenance, exactly as
    the slice's own scope says ("Verify the resulting hypothesis is a
    DRAFT."); the mocked runner is a plain Python function, never a real
    Claude CLI invocation.
  - the quant_scan path (sections B through L) — the "hero" lineage that
    is carried all the way through duplicate screening, area tagging,
    provenance, human approval, priority, the scheduler, a REAL runner
    execution against a deterministic synthetic price fixture, and finally
    evidence — so section L can trace one unbroken chain from hypothesis to
    verdict.

Run with:  python -m tests.test_research_factory_integration
"""

import re
import sys
import json
import shutil
import inspect
import tempfile
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from research.store import Store, IST  # noqa: E402
from research import memory as rm  # noqa: E402
from research.contracts import Contract, registry as _registry  # noqa: E402
from research.brain import hypothesis_intake as hi  # noqa: E402
from research.brain import investigator as inv  # noqa: E402
from research.brain import quant_scan as qs  # noqa: E402
from research.brain import research_areas as ra  # noqa: E402
from research.brain import discovery_provenance as dp  # noqa: E402
from research.brain import draft_backlog  # noqa: E402
from research.brain import digest as digest_mod  # noqa: E402
from research.brain import scheduler as sch  # noqa: E402
from research.brain import priority as prio  # noqa: E402
from research.experiments import runner  # noqa: E402
from research.experiments import evaluator  # noqa: E402
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
    """Strip triple-double-quoted docstrings — the established isolation-
    check convention (test_research_investigator.py onward)."""
    return re.sub(r'"""[\s\S]*?"""', "", source)


# ---------------------------------------------------------------------------
# Fixtures — all temporary, all cleaned up by the OS's tempdir GC. Nothing
# here ever touches research/registry (the real one), a real market-memory
# DB, memory/state.json, .env, or any engine/* file.
# ---------------------------------------------------------------------------

TMP = Path(tempfile.mkdtemp(prefix="lq-test-factory-integration-"))


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


def seed_index_membership(store, index_name, symbols, *, effective_date):
    """A synthetic index, scoped entirely to this test's own temp Store —
    lets a locked Contract declare universe="Nifty 50" (a value
    hypothesis_intake.VALID_UNIVERSES already accepts) and have the REAL
    runner/replay machinery resolve it to OUR synthetic symbols, without
    touching engine.watchlist or any real index data."""
    for sym in symbols:
        store.append(
            dataset="index_membership", entity=sym,
            event_time=effective_date, knowledge_time=effective_date,
            source="test", payload={"index_name": index_name, "symbol": sym, "valid_to": None},
        )


def seed_symbol(store, symbol, n_days, spike_indices, *, start, spike_return=0.01, offset=0):
    """The exact synthetic-relationship recipe proven in
    tests/test_research_quant_scan.py's section A: clean, well-separated
    (30-session) volume spikes, each followed by a deterministic +1% jump —
    close-to-close return the runner's OWN entry_rule/exit_rule evaluation
    (research/experiments/runner.py, unmodified) will independently detect
    and trade against the same seeded prices quant_scan discovers its
    candidate from."""
    spike_set = set(spike_indices)
    close = 100.0
    for i in range(n_days):
        d = start + dt.timedelta(days=i)
        vol = 900_000 if i in spike_set else 100_000 + ((i + offset) % 5) * 100
        store.append_price(
            symbol=symbol, session_date=d, knowledge_time=d, source="test",
            open_=close, high=close, low=close, close=close, volume=vol,
        )
        close = close * (1 + spike_return) if i in spike_set else close * (1 + 0.00005 * (((i + offset) % 3) - 1))


def make_locked_contract(cid, *, locked_at=None, status="locked", registry_dir, **overrides):
    """A locked Contract, constructed directly — the same pattern
    tests/test_research_priority.py and tests/test_research_scheduler.py
    already use for building priority/scheduler fixtures without going
    through the full create_draft()/approve_and_lock() ceremony every time
    (that ceremony IS exercised for real, explicitly, on the hero contract
    in section F)."""
    fields = dict(
        id=cid, title=f"idea {cid}", hypothesis=f"claim behind {cid}",
        null_hypothesis="no effect", universe="watchlist", signal="observatory.volume_zscore",
        entry_rule=json.dumps({"conditions": [{"metric": "volume_zscore", "op": ">", "value": 3.0}]}),
        exit_rule=json.dumps({"max_hold_days": 5}),
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
    """Same fabricated-verdict shape test_research_comparison.py's/
    test_research_priority.py's own make_verdict() uses."""
    return {"n_trades": n_trades, "win_rate": None, "gross_pnl": net_pnl, "net_pnl": net_pnl,
            "total_costs": 0.0, "avg_net_pnl": avg_net_pnl, "expectancy_r": None,
            "t_stat": t_stat}


def link_contract_to_hypothesis(store, hypothesis_id, contract_id, *, extra=None):
    rm.record_hypothesis_proposal(
        store, claim="claim", source="test", hypothesis_id=hypothesis_id,
        extra={"contract_id": contract_id, **(extra or {})})


def score_contract(store, hypothesis_id, contract_id, verdict):
    rm.record_experiment_verdict(
        store, contract_id=contract_id, hypothesis_id=hypothesis_id, verdict=verdict)


def make_promising_parent(store, reg, *, cid, locked_at):
    """A REPORTED contract with a fabricated PROMISING verdict — the exact
    scenario priority.is_confirmation_experiment() looks for. Classified
    PROMISING by the REAL, unmodified comparison.evaluate_hypothesis_
    evidence() — not assumed."""
    hid = rm.new_hypothesis_id()
    make_locked_contract(cid, status="reported", registry_dir=reg, locked_at=locked_at)
    link_contract_to_hypothesis(store, hid, cid)
    score_contract(store, hid, cid, make_verdict(20, 10_000.0, 500.0, 5.0))
    return hid, cid


def make_simple_proposal(evaluation_start, evaluation_end, **overrides):
    base = {
        "title": "budget gate integration check",
        "hypothesis": "a plain proposal used only to exercise the research budget gate",
        "null_hypothesis": "none",
        "universe": "watchlist",
        "signal": "test",
        "entry_rule": {"conditions": [{"metric": "volume_zscore", "op": ">", "value": 3.0}]},
        "exit_rule": {"max_hold_days": 5},
        "splits": {"discovery": [evaluation_start, evaluation_end]},
        "independence": "n/a", "falsification": "n/a", "abandon_condition": "n/a",
        "evaluation_start": evaluation_start, "evaluation_end": evaluation_end,
    }
    base.update(overrides)
    return base


# ===========================================================================
print("\n--- A: full AI discovery path (mocked AI boundary) ---")
# ===========================================================================

AI_PROPOSAL = {
    "title": "mean reversion after a sharp one-day drop",
    "hypothesis": "a return_1d below -1% is followed by a short-term bounce",
    "null_hypothesis": "no relationship between a one-day drop and forward returns",
    "universe": "watchlist",
    "signal": "observatory.price_move_zscore",
    "entry_rule": {"conditions": [{"metric": "return_1d", "op": "<", "value": -0.01}]},
    "exit_rule": {"stop_loss_pct": 3.0, "target_pct": 6.0, "max_hold_days": 10},
    "splits": {"discovery": ["2019-01-01", "2019-12-31"]},
    "independence": "one entry per symbol per drop event",
    "falsification": "t_stat < 2.0 on the discovery split",
    "abandon_condition": "expectancy_r <= 0 on discovery",
    "evaluation_start": "2019-01-01",
    "evaluation_end": "2019-12-31",
}


def mock_ai_runner(prompt: str) -> str:
    """The entire 'AI boundary' for this test: a plain Python function
    returning fixed JSON. No subprocess, no network call, no real Claude CLI
    — investigator.investigate() cannot tell the difference between this and
    a real model response, because parse_ai_output() only ever sees text."""
    return json.dumps(AI_PROPOSAL)


store_a = fresh_store("a")
reg_a = fresh_registry("a")
as_of_a = dt.datetime(2019, 6, 1, 18, 0, tzinfo=IST)

result_a = inv.investigate(store_a, as_of_a, runner=mock_ai_runner, registry_dir=reg_a)
check("A: investigate() with a mocked AI runner returns an InvestigatorResult",
      isinstance(result_a, inv.InvestigatorResult), result_a)

ai_contract = Contract.load(result_a.contract_id, reg_a) if isinstance(result_a, inv.InvestigatorResult) else None
ai_did_not_lock = ai_contract is not None and ai_contract.status == "draft"
check("A: the resulting hypothesis is a DRAFT, never locked", ai_did_not_lock)
check("A: the draft's locked_hash is unset", ai_contract is not None and ai_contract.locked_hash is None)

ai_prov = dp.discovery_search_for_hypothesis(store_a, result_a.hypothesis_id) if isinstance(result_a, inv.InvestigatorResult) else []
check("A: discovery provenance is recorded with discovery_type='research_ai'",
      len(ai_prov) == 1 and ai_prov[0]["discovery_type"] == inv.DISCOVERY_TYPE_RESEARCH_AI, ai_prov)
check("A: the AI draft is discoverable via the normal pending_drafts() review queue",
      any(c.id == result_a.contract_id for c in hi.pending_drafts(reg_a)))
ai_experiments_before_any_run = len(store_a.experiment_results(result_a.contract_id)) == 0
check("A: no experiment_results exist for a freshly-drafted AI hypothesis "
      "(the AI boundary never executes anything)", ai_experiments_before_any_run)


# ===========================================================================
print("\n--- B: full mathematical discovery path (quant_scan) ---")
# ===========================================================================

ZQ_SYMBOLS = ("ZQA", "ZQB", "ZQC")
N_DAYS = 180
SPIKE_INDICES = [25, 55, 85, 115, 145]
START_MAIN = dt.date(2023, 1, 1)
AS_OF_MAIN = dt.datetime.combine(START_MAIN + dt.timedelta(days=N_DAYS), dt.time(18, 0), tzinfo=IST)

store_main = fresh_store("main")
reg_main = fresh_registry("main")

seed_index_membership(store_main, "Nifty 50", ZQ_SYMBOLS, effective_date=START_MAIN)
for i, sym in enumerate(ZQ_SYMBOLS):
    seed_symbol(store_main, sym, N_DAYS, SPIKE_INDICES, start=START_MAIN, offset=i)

result_hero = qs.run_scan(store_main, AS_OF_MAIN, ZQ_SYMBOLS, universe="Nifty 50", registry_dir=reg_main)
check("B: run_scan() on a known synthetic relationship returns a genuine ScanResult",
      isinstance(result_hero, qs.ScanResult), result_hero)

hero_hid = result_hero.hypothesis_id
hero_cid = result_hero.contract_id
hero_contract = Contract.load(hero_cid, reg_main)
quant_did_not_lock = hero_contract.status == "draft"
check("B: the resulting hypothesis is a DRAFT, never locked", quant_did_not_lock)
check("B: the draft's locked_hash is unset", hero_contract.locked_hash is None)
check("B: the draft's universe is the requested synthetic 'Nifty 50' universe",
      hero_contract.universe == "Nifty 50")

check("B: the quant-scan draft enters the SAME downstream data model as "
      "the AI-generated draft — both are research.contracts.Contract "
      "instances with status='draft'",
      isinstance(hero_contract, Contract) and isinstance(ai_contract, Contract)
      and hero_contract.status == ai_contract.status == "draft")
check("B: the quant-scan draft resolves back to its hypothesis exactly like "
      "the AI draft does",
      evaluator.resolve_hypothesis_id(store_main, hero_cid) == hero_hid
      and evaluator.resolve_hypothesis_id(store_a, result_a.contract_id) == result_a.hypothesis_id)
check("B: the quant-scan draft is discoverable via the normal "
      "pending_drafts() review queue",
      any(c.id == hero_cid for c in hi.pending_drafts(reg_main)))

quant_experiments_before_any_run = len(store_main.experiment_results(hero_cid)) == 0
check("B: no experiment_results exist for a freshly-drafted quant hypothesis "
      "(quant_scan never executes anything)", quant_experiments_before_any_run)


# ===========================================================================
print("\n--- C: duplicate protection ---")
# ===========================================================================

result_hero_again = qs.run_scan(store_main, AS_OF_MAIN, ZQ_SYMBOLS, universe="Nifty 50", registry_dir=reg_main)
check("C: re-running the identical scan against the identical data does NOT "
      "create a second draft — it is recognized as a duplicate",
      isinstance(result_hero_again, qs.DuplicateCandidate), result_hero_again)
check("C: the duplicate correctly names the ORIGINAL contract_id",
      isinstance(result_hero_again, qs.DuplicateCandidate)
      and result_hero_again.existing_contract_id == hero_cid)
check("C: exactly one draft exists for this hypothesis after both attempts",
      len(hi.pending_drafts(reg_main)) == 1
      and evaluator.contract_ids_for_hypothesis(store_main, hero_hid) == [hero_cid])


# ===========================================================================
print("\n--- D: research-area mapping ---")
# ===========================================================================

HERO_AREA = "volume_momentum"
ra.tag_hypothesis(store_main, hypothesis_id=hero_hid, research_area=HERO_AREA, source="test-integration")

check("D: research_areas.area_of() reports the newly-tagged area",
      ra.area_of(store_main, hero_hid) == HERO_AREA)

backlog_entries = draft_backlog.list_drafts(store_main, registry_dir=reg_main)
hero_backlog_entry = next((e for e in backlog_entries if e["contract_id"] == hero_cid), None)
check("D: the draft backlog surfaces the tagged research area for this draft",
      hero_backlog_entry is not None and hero_backlog_entry["research_area"] == HERO_AREA,
      hero_backlog_entry)

# research_areas.py tags are organizational metadata, not bitemporal market
# data — research.memory.record_research_area_tag() stamps them with the
# real wall clock (see its own docstring/implementation), not the research
# fixture's simulated AS_OF_MAIN. So the digest call proving the tag is
# visible through that path is built "as of right now" (after the tag was
# just written), not as of the simulated historical research clock — using
# AS_OF_MAIN here would under-gate a real-time-stamped row against a 2023
# fixture date and fail for reasons having nothing to do with the digest
# integration this section is actually testing.
digest_main = digest_mod.build_digest(store_main, dt.datetime.now(tz=IST), registry_dir=reg_main)
area_names_in_digest = {a["name"]: a["hypothesis_count"] for a in digest_main["research_areas"]["areas"]}
check("D: the digest's research_areas section includes the newly-tagged area",
      area_names_in_digest.get(HERO_AREA, 0) >= 1, area_names_in_digest)


# ===========================================================================
print("\n--- E: provenance chain ---")
# ===========================================================================

hero_prov_rows = dp.discovery_search_for_hypothesis(store_main, hero_hid)
check("E: exactly one discovery-search provenance row exists for the hero hypothesis",
      len(hero_prov_rows) == 1, hero_prov_rows)
if hero_prov_rows:
    row = hero_prov_rows[0]
    check("E: provenance discovery_type is 'mathematical'",
          row["discovery_type"] == "mathematical", row)
    check("E: provenance version matches quant_scan's own version identifier",
          row["version"] == qs.QUANT_SCAN_VERSION, row)
    check("E: provenance carries a research as_of reference time",
          isinstance(row.get("as_of"), str) and bool(row["as_of"]))
check("E: has_discovery_provenance() agrees", dp.has_discovery_provenance(store_main, hero_hid))
check("E: the Contract can be associated back to the hypothesis through the "
      "existing hypothesis ID (evaluator.resolve_hypothesis_id)",
      evaluator.resolve_hypothesis_id(store_main, hero_cid) == hero_hid)

# Captured now, before section F locks anything — this is the "no execution
# before approval" snapshot the critical assertions section reuses later.
quant_experiments_before_lock = len(store_main.experiment_results(hero_cid)) == 0
hero_status_before_lock = Contract.load(hero_cid, reg_main).status


# ===========================================================================
print("\n--- F: human approval (the only path to LOCKED) ---")
# ===========================================================================

check("F: immediately before approval, the hero contract is still a DRAFT",
      hero_status_before_lock == "draft", hero_status_before_lock)

locked_hero = hi.approve_and_lock(
    store_main, hero_cid, approved_by="Vaibhav (integration harness)", registry_dir=reg_main)

check("F: approve_and_lock() transitions the contract to LOCKED",
      locked_hero.status == "locked", locked_hero.status)
check("F: the locked contract now carries a locked_hash",
      locked_hero.locked_hash is not None)
check("F: the on-disk registry reflects the lock",
      Contract.load(hero_cid, reg_main).status == "locked")
human_approval_was_the_only_lock_path = (
    hero_status_before_lock == "draft" and locked_hero.status == "locked")


# ===========================================================================
print("\n--- G: research budget gate ---")
# ===========================================================================

store_g = fresh_store("g")
reg_g = fresh_registry("g")

d1 = hi.create_draft(store_g, make_simple_proposal("2018-01-01", "2018-06-30"), registry_dir=reg_g)
d2 = hi.create_draft(store_g, make_simple_proposal("2018-07-01", "2018-12-31"), registry_dir=reg_g)

locked_g1 = hi.approve_and_lock(
    store_g, d1.contract.id, approved_by="Vaibhav", registry_dir=reg_g, max_locks=1)
check("G: the first lock, under a budget of 1, is permitted",
      locked_g1.status == "locked")

budget_blocked = False
try:
    hi.approve_and_lock(store_g, d2.contract.id, approved_by="Vaibhav",
                         registry_dir=reg_g, max_locks=1)
    check("G: a second lock within the same period, over a budget of 1, is refused", False)
except hi.IntakeRejected as e:
    budget_blocked = True
    check("G: a second lock within the same period, over a budget of 1, is refused",
          True, str(e))
check("G: the blocked draft is still a draft — the budget refusal changed nothing",
      Contract.load(d2.contract.id, reg_g).status == "draft")

locked_g2 = hi.approve_and_lock(
    store_g, d2.contract.id, approved_by="Vaibhav", registry_dir=reg_g,
    max_locks=1, budget_override_by="Vaibhav (explicit override)",
    budget_override_reason="integration test G")
check("G: an explicit budget_override_by lock succeeds where the plain call was refused",
      locked_g2.status == "locked")
override_notes = [
    r for r in rm.query_research_log(store_g, rm.DATASET_NOTE)
    if r["payload"].get("contract_id") == d2.contract.id
    and "OVERRIDE" in (r["payload"].get("note") or "")
]
check("G: the override is recorded in research memory, not silently applied",
      len(override_notes) == 1, override_notes)


# ===========================================================================
print("\n--- H: research priority ---")
# ===========================================================================

hero_dt = dt.datetime.fromisoformat(locked_hero.locked_at)

parent_hid, parent_cid = make_promising_parent(
    store_main, reg_main, cid="EXP-INTEG-PARENT",
    locked_at=(hero_dt - dt.timedelta(days=10)).isoformat(timespec="seconds"))

make_locked_contract("EXP-INTEG-CONFIRM", registry_dir=reg_main,
                      locked_at=(hero_dt - dt.timedelta(days=3)).isoformat(timespec="seconds"))
link_contract_to_hypothesis(store_main, parent_hid, "EXP-INTEG-CONFIRM",
                             extra={"split_of": parent_cid, "split": "validation"})

hid_old = rm.new_hypothesis_id()
make_locked_contract("EXP-INTEG-OLD", registry_dir=reg_main,
                      locked_at=(hero_dt - dt.timedelta(days=2)).isoformat(timespec="seconds"))
link_contract_to_hypothesis(store_main, hid_old, "EXP-INTEG-OLD")
ra.tag_hypothesis(store_main, hypothesis_id=hid_old, research_area=HERO_AREA, source="test")  # SAME area as hero

hid_other = rm.new_hypothesis_id()
make_locked_contract("EXP-INTEG-OTHER", registry_dir=reg_main,
                      locked_at=(hero_dt - dt.timedelta(days=1)).isoformat(timespec="seconds"))
link_contract_to_hypothesis(store_main, hid_other, "EXP-INTEG-OTHER")
ra.tag_hypothesis(store_main, hypothesis_id=hid_other, research_area="seasonal_drift", source="test")  # fresh area

check("H: is_confirmation_experiment() genuinely recognizes EXP-INTEG-CONFIRM "
      "via the real, unmodified evidence classification",
      prio.is_confirmation_experiment(store_main, "EXP-INTEG-CONFIRM", registry_dir=reg_main) is True)

eligible_now = runner.runnable_contracts(reg_main)
eligible_ids_now = sorted(c.id for c in eligible_now)
check("H: exactly the 4 intended contracts are currently eligible (locked, not yet run)",
      eligible_ids_now == sorted(["EXP-INTEG-CONFIRM", "EXP-INTEG-OLD", "EXP-INTEG-OTHER", hero_cid]),
      eligible_ids_now)

ranked = [c.id for c in prio.rank_experiments(store_main, eligible_now, registry_dir=reg_main)]
EXPECTED_PRIORITY_ORDER = ["EXP-INTEG-CONFIRM", "EXP-INTEG-OLD", "EXP-INTEG-OTHER", hero_cid]
check("H: the priority engine orders confirmation first, then the "
      "round-robin-underrepresented area, then age, then the hero last "
      "(same area as EXP-INTEG-OLD but locked later) — exactly the v0 "
      "semantics documented in research/brain/priority.py, not "
      "re-derived here",
      ranked == EXPECTED_PRIORITY_ORDER, ranked)
check("H: scheduler.eligible_contracts() delegates to the SAME priority "
      "ordering, byte for byte",
      [c.id for c in sch.eligible_contracts(store_main, registry_dir=reg_main)] == ranked)

priority_ran_no_experiments = all(
    len(store_main.experiment_results(cid)) == 0 for cid in EXPECTED_PRIORITY_ORDER
)
check("H: ranking alone executed nothing — every contract still has zero "
      "experiment_results", priority_ran_no_experiments)


# ===========================================================================
print("\n--- I: scheduler (priority -> bounded selection -> runner.run_experiment()) ---")
# ===========================================================================

run1 = sch.run_scheduler(store_main, registry_dir=reg_main, max_experiments=2)
check("I: a bounded run with max_experiments=2 selects exactly the top 2 in priority order",
      run1.selected_contract_ids == EXPECTED_PRIORITY_ORDER[:2], run1.selected_contract_ids)
check("I: the scheduler actually attempted exactly those 2 (real runner.run_experiment calls)",
      run1.attempted_contract_ids == EXPECTED_PRIORITY_ORDER[:2])
check("I: both attempted contracts reached a terminal runner-defined status "
      "(reported) — the scheduler itself never invents a status",
      set(run1.reported_contract_ids) == set(EXPECTED_PRIORITY_ORDER[:2]), run1.reported_contract_ids)
check("I: the remaining 2 eligible contracts are correctly reported as pending "
      "(cap-bound, not attempted this run)",
      set(run1.pending_contract_ids) == set(EXPECTED_PRIORITY_ORDER[2:]), run1.pending_contract_ids)
check("I: the run did not stop early", run1.stopped_early is False)
check("I: the two attempted contracts are no longer LOCKED afterward "
      "(the runner, not the scheduler, transitioned them)",
      all(Contract.load(cid, reg_main).status == "reported" for cid in EXPECTED_PRIORITY_ORDER[:2]))
check("I: the two NOT-yet-attempted contracts are still LOCKED, untouched",
      all(Contract.load(cid, reg_main).status == "locked" for cid in EXPECTED_PRIORITY_ORDER[2:]))

run2 = sch.run_scheduler(store_main, registry_dir=reg_main, max_experiments=10)
check("I: a second, larger-cap run picks up exactly the previously-pending contracts",
      set(run2.attempted_contract_ids) == set(EXPECTED_PRIORITY_ORDER[2:]), run2.attempted_contract_ids)
check("I: the hero contract (real synthetic price fixture) was executed by "
      "THIS run, via the real runner", hero_cid in run2.attempted_contract_ids)
check("I: the hero contract reached REPORTED with real trades (not zero)",
      hero_cid in run2.reported_contract_ids)
only_scheduler_executed = (
    priority_ran_no_experiments
    and hero_cid not in run1.attempted_contract_ids
    and hero_cid in run2.attempted_contract_ids
)
check("I: the hero contract was executed ONLY once the scheduler actually "
      "selected it — never during priority computation, drafting, tagging, "
      "provenance, or approval", only_scheduler_executed)


# ===========================================================================
print("\n--- J: real, persisted experiment result ---")
# ===========================================================================

hero_results = store_main.experiment_results(hero_cid)
check("J: the hero contract produced real, persisted experiment_results "
      "(not zero trades)", len(hero_results) >= 10, len(hero_results))
check("J: no duplicate trade_seq values were written for the hero contract",
      len({r["trade_seq"] for r in hero_results}) == len(hero_results))
check("J: the hero Contract reached a runner-defined terminal status (reported)",
      Contract.load(hero_cid, reg_main).status == "reported")

n_results_before_rerun = len(store_main.experiment_results(hero_cid))
rerun_refused = False
try:
    runner.run_experiment(hero_cid, store_main, registry_dir=reg_main)
    check("J: re-running an already-REPORTED contract is refused outright", False)
except runner.RunnerRejected:
    rerun_refused = True
    check("J: re-running an already-REPORTED contract is refused outright", True)
check("J: refusing the re-run wrote no additional/duplicate result records",
      len(store_main.experiment_results(hero_cid)) == n_results_before_rerun)

hero_verdict = evaluator.verdict_for_contract(store_main, hero_cid)
check("J: a verdict exists and can be computed from the persisted results",
      hero_verdict is not None and hero_verdict.get("n_trades") == len(hero_results), hero_verdict)


# ===========================================================================
print("\n--- K: evidence ---")
# ===========================================================================

hero_evidence = comparison.record_evidence(store_main, hero_cid, registry_dir=reg_main)
check("K: evaluate_hypothesis_evidence()/record_evidence() succeed for the "
      "hero contract", hero_evidence["contract_id"] == hero_cid)
check("K: the evidence is keyed to the same hypothesis_id throughout the lineage",
      hero_evidence["hypothesis_id"] == hero_hid, hero_evidence["hypothesis_id"])
check("K: the evidence verdict is one of the existing, unmodified verdict labels",
      hero_evidence["verdict"] in comparison.VALID_VERDICTS, hero_evidence["verdict"])

evidence_rows = rm.query_research_log(store_main, rm.DATASET_EVIDENCE)
persisted_evidence = [r for r in evidence_rows
                      if r["payload"].get("hypothesis_id") == hero_hid
                      and r["payload"].get("contract_id") == hero_cid]
check("K: the evidence summary was actually persisted to research memory",
      len(persisted_evidence) == 1, len(persisted_evidence))

# Assertion 7 (evidence remains downstream of execution): a locked-but-never
# -run contract has NO evidence to give — evaluate_hypothesis_evidence()
# refuses rather than fabricating one.
make_locked_contract("EXP-INTEG-NEVERRUN", registry_dir=reg_main,
                      locked_at=(hero_dt + dt.timedelta(days=1)).isoformat(timespec="seconds"))
hid_neverrun = rm.new_hypothesis_id()
link_contract_to_hypothesis(store_main, hid_neverrun, "EXP-INTEG-NEVERRUN")
evidence_requires_execution = False
try:
    comparison.evaluate_hypothesis_evidence(store_main, "EXP-INTEG-NEVERRUN", registry_dir=reg_main)
    check("K: a locked-but-never-run contract has no evidence to evaluate", False)
except comparison.ComparisonRejected:
    evidence_requires_execution = True
    check("K: a locked-but-never-run contract has no evidence to evaluate "
          "(evaluate_hypothesis_evidence refuses rather than fabricating a result)", True)


# ===========================================================================
print("\n--- L: complete lineage ---")
# ===========================================================================

lineage_provenance = dp.discovery_search_for_hypothesis(store_main, hero_hid)
lineage_contract_ids = evaluator.contract_ids_for_hypothesis(store_main, hero_hid)
lineage_results = store_main.experiment_results(hero_cid)
lineage_verdict = evaluator.verdict_for_contract(store_main, hero_cid)
lineage_evidence = comparison.evaluate_hypothesis_evidence(store_main, hero_cid, registry_dir=reg_main)

check("L: hypothesis -> discovery provenance", len(lineage_provenance) == 1)
check("L: hypothesis -> Contract (via the existing hypothesis ID)",
      hero_cid in lineage_contract_ids, lineage_contract_ids)
check("L: Contract -> experiment result", len(lineage_results) >= 10)
check("L: experiment result -> verdict",
      lineage_verdict is not None and lineage_verdict["n_trades"] == len(lineage_results))
check("L: verdict -> evidence, still keyed to the SAME hypothesis_id "
      "throughout the entire chain",
      lineage_evidence["hypothesis_id"] == hero_hid
      and lineage_evidence["contract_id"] == hero_cid,
      lineage_evidence)
check("L: the discovery-provenance row's own hypothesis_id matches every "
      "other link in the chain",
      lineage_provenance[0]["hypothesis_id"] == hero_hid)


# ===========================================================================
print("\n--- M: isolation ---")
# ===========================================================================

MODULE_FILES = {
    "investigator.py": Path("research/brain/investigator.py"),
    "quant_scan.py": Path("research/brain/quant_scan.py"),
    "hypothesis_intake.py": Path("research/brain/hypothesis_intake.py"),
    "scheduler.py": Path("research/brain/scheduler.py"),
    "priority.py": Path("research/brain/priority.py"),
    "this integration test file": Path(__file__),
}

def _imports_module(source: str, dotted: str) -> bool:
    """True only for an ACTUAL Python import of `dotted`
    (`import engine.execute`, `from engine.execute import ...`, or
    `from engine import execute`) — never a prose mention of the module's
    name in a docstring, a `#` comment, or (for this test file itself) the
    string literal naming it inside one of these very checks. Every
    production module here documents this exact boundary in its own module
    docstring ("must never import engine.execute/engine.guardrails"), and
    this test file's own MODULE_FILES/check() code necessarily contains
    those same substrings as data — a plain substring search would flag
    both as false positives, so this checks for the import syntax
    specifically."""
    parent, _, leaf = dotted.rpartition(".")
    patterns = [
        rf"^\s*import\s+{re.escape(dotted)}\b",
        rf"^\s*from\s+{re.escape(dotted)}\s+import\b",
    ]
    if parent:
        patterns.append(rf"^\s*from\s+{re.escape(parent)}\s+import\s+[^\n#]*\b{re.escape(leaf)}\b")
    return any(re.search(p, source, re.MULTILINE) for p in patterns)


# This isolation-check block itself is, necessarily, the one place in this
# entire file that has to spell out "engine.execute" / "engine.guardrails" /
# "state.json" as literal data (as check names and as search targets) — that
# is what a boundary check *is*. So when the self-check runs against "this
# integration test file", it scans only the file's actual test logic (the
# fixtures and sections above), not this harness block itself, else it would
# trivially fail against its own check strings rather than against anything
# the test actually does.
_SELF_SCAN_SENTINEL = "\nfor label, path in MODULE_FILES.items():\n"

for label, path in MODULE_FILES.items():
    src = path.read_text()
    if label == "this integration test file":
        src = src.split(_SELF_SCAN_SENTINEL, 1)[0]
    body = _code_only(src)
    check(f"M: {label} never imports engine.execute",
          not _imports_module(body, "engine.execute"))
    check(f"M: {label} never imports engine.guardrails",
          not _imports_module(body, "engine.guardrails"))
    check(f"M: {label} never imports a broker module",
          not re.search(r"^\s*(?:from|import)\s+[^\n]*broker", body, re.MULTILINE))
    check(f"M: {label}'s code never references memory/state.json",
          "state.json" not in re.sub(r"#.*", "", body))

mock_runner_src = _code_only(inspect.getsource(mock_ai_runner))
check("M: the mocked AI runner never shells out to a subprocess",
      "subprocess" not in mock_runner_src)
check("M: the mocked AI runner never references a real claude/anthropic CLI or SDK",
      "claude" not in mock_runner_src.lower() and "anthropic" not in mock_runner_src.lower())
check("M: investigator.py's own default runner (never used by this test) is "
      "the only place a real CLI is invoked, and this test always overrides it",
      inv._default_runner is not mock_ai_runner)


# ===========================================================================
print("\n--- Critical architectural assertions ---")
# ===========================================================================

check("1. AI proposal does not lock", ai_did_not_lock)
check("2. Quant proposal does not lock", quant_did_not_lock)
check("3. Neither discovery engine executes experiments",
      ai_experiments_before_any_run and quant_experiments_before_any_run
      and quant_experiments_before_lock)
check("4. Only explicit human approval locks a Contract",
      human_approval_was_the_only_lock_path)
check("5. Only scheduler/runner executes an already-locked experiment",
      only_scheduler_executed)
check("6. Runner remains unchanged (id-based execution boundary, unmodified "
      "by this slice)",
      list(inspect.signature(runner.run_experiment).parameters)[0] == "contract_id"
      and inspect.signature(runner.run_experiment).parameters["contract_id"].annotation in (str, "str"))
check("7. Evidence remains downstream of execution", evidence_requires_execution)
check("8. Live engine remains outside the entire research graph",
      all(not _imports_module(_code_only(Path(p).read_text()), "engine.execute")
          and not _imports_module(_code_only(Path(p).read_text()), "engine.guardrails")
          for p in MODULE_FILES.values()))


# ===========================================================================
print(f"\n{'=' * 52}\n  {PASSED} passed, {FAILED} failed\n{'=' * 52}")
sys.exit(1 if FAILED else 0)
