"""
Tests for Phase 2 Slice T: Mathematical Discovery Engine v0
(research/brain/quant_scan.py).

This is a hypothesis GENERATOR, not a strategy generator and not a second
execution path: it turns recorded price history into, at most, ONE
structured hypothesis proposal per call, through the exact same
validate_proposal()/create_draft() front door research/brain/investigator.py
(the AI path) already uses. This file does not re-test hypothesis_intake.py's
own validation rules (tests/test_research_hypothesis_intake.py does that) or
comparison.py's own evidence math (tests/test_research_comparison.py does
that) — it tests quant_scan.py's own scan/aggregate/filter/propose pipeline,
its leakage-safety, its determinism, and its structural isolation from
execution, locking, and live trading.

Run with:  python -m tests.test_research_quant_scan
"""

import re
import sys
import shutil
import tempfile
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from research.store import Store  # noqa: E402
from research.contracts import Contract, registry as _registry  # noqa: E402
from research.brain import hypothesis_intake as hi  # noqa: E402
from research.brain import discovery_provenance as dp  # noqa: E402
from research.brain import quant_scan as qs  # noqa: E402

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
    by every isolation check since (test_research_priority.py, etc.)."""
    return re.sub(r'"""[\s\S]*?"""', "", source)


TMP = Path(tempfile.mkdtemp(prefix="lq-test-quant-scan-"))
START = dt.date(2024, 1, 1)


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


def seed_symbol(
    store, symbol, n_days, spike_indices, *,
    spike_vol=900_000, base_vol=100_000, spike_return=0.01, drift=0.0,
    offset=0, knowledge_offset_days=None,
):
    """Deterministic synthetic price/volume history: `n_days` consecutive
    sessions starting at START, ordinary volume with small deterministic
    (non-zero-variance) noise, and a volume spike (`spike_vol`) plus a
    `spike_return` close-to-close jump on every index in `spike_indices`.
    `offset` lets several symbols share the same spike day-offsets without
    producing byte-identical noise series. `knowledge_offset_days`, if
    given, delays when each row becomes knowable (days after session_date)
    — used only by the leakage/as_of test."""
    spike_set = set(spike_indices)
    close = 100.0
    for i in range(n_days):
        d = START + dt.timedelta(days=i)
        kd = d if not knowledge_offset_days else d + dt.timedelta(days=knowledge_offset_days(i))
        vol = spike_vol if i in spike_set else base_vol + ((i + offset) % 5) * 100
        store.append_price(
            symbol=symbol, session_date=d, knowledge_time=kd, source="test",
            open_=close, high=close, low=close, close=close, volume=vol,
        )
        if i in spike_set:
            close = close * (1 + spike_return)
        else:
            close = close * (1 + drift + 0.00005 * (((i + offset) % 3) - 1))


def seed_flat(store, symbol, n_days, *, offset=0):
    """No anomalies anywhere: constant volume (tiny deterministic jitter,
    never enough to clear even the lower 3.0 threshold) and a flat close."""
    close = 100.0
    for i in range(n_days):
        d = START + dt.timedelta(days=i)
        vol = 100_000 + ((i + offset) % 3) * 10  # jitter far too small for z >= 3
        store.append_price(
            symbol=symbol, session_date=d, knowledge_time=d, source="test",
            open_=close, high=close, low=close, close=close, volume=vol,
        )
        close = close * (1 + 0.00002 * (((i + offset) % 2) * 2 - 1))


# A clean, well-separated spike schedule: spikes 30 sessions apart so each
# spike's trailing WINDOW_DAYS(=20)-session baseline is never contaminated
# by an earlier spike still sitting inside that window.
N_DAYS = 180
SPIKE_INDICES = list(range(25, N_DAYS - 10, 30))  # [25, 55, 85, 115, 145] -> 5 per symbol
KNOWN_SYMBOLS = ("AAA", "BBB", "CCC")


def seed_known_relationship(store):
    for i, sym in enumerate(KNOWN_SYMBOLS):
        seed_symbol(store, sym, N_DAYS, SPIKE_INDICES, offset=i)


AS_OF = dt.datetime.combine(START + dt.timedelta(days=N_DAYS), dt.time(18, 0))


# ---------------------------------------------------------------------------
print("\n--- A: known fixture produces the expected candidate ---")
# ---------------------------------------------------------------------------

store_a = fresh_store("a")
seed_known_relationship(store_a)
cand_a = qs.find_candidate(store_a, AS_OF, KNOWN_SYMBOLS)

check("a candidate is found for the known relationship", cand_a is not None)
if cand_a is not None:
    check("selected threshold is the first (lowest) in the declared grid",
          cand_a.threshold == 3.0, cand_a.threshold)
    check("selected horizon is the first (shortest) in the declared grid",
          cand_a.horizon == 1, cand_a.horizon)
    check("n matches the 5 spikes x 3 symbols actually seeded",
          cand_a.stats.n == 15, cand_a.stats.n)
    check("mean forward return matches the seeded 1% spike_return",
          abs(cand_a.stats.mean_return - 0.01) < 1e-6, cand_a.stats.mean_return)
    check("hit rate is 100% (every seeded spike was followed by a positive return)",
          cand_a.stats.hit_rate == 1.0, cand_a.stats.hit_rate)
    check("symbols_examined is the sorted, deduplicated input universe",
          cand_a.symbols_examined == tuple(sorted(KNOWN_SYMBOLS)))

# ---------------------------------------------------------------------------
print("\n--- B: null fixture produces no candidate ---")
# ---------------------------------------------------------------------------

store_b = fresh_store("b")
for i, sym in enumerate(KNOWN_SYMBOLS):
    seed_flat(store_b, sym, N_DAYS, offset=i)

cand_b = qs.find_candidate(store_b, AS_OF, KNOWN_SYMBOLS)
check("no candidate is found when nothing anomalous ever happened", cand_b is None)

result_b = qs.run_scan(store_b, AS_OF, KNOWN_SYMBOLS, registry_dir=fresh_registry("b"))
check("run_scan returns NoCandidate for the null fixture",
      isinstance(result_b, qs.NoCandidate), result_b)

# ---------------------------------------------------------------------------
print("\n--- C: determinism ---")
# ---------------------------------------------------------------------------

cand_a2 = qs.find_candidate(store_a, AS_OF, KNOWN_SYMBOLS)
check("two calls against the identical store/as_of/symbols are identical",
      cand_a == cand_a2)

cand_a_set = qs.find_candidate(store_a, AS_OF, set(KNOWN_SYMBOLS))
check("passing symbols as a set (unordered) does not change the result",
      cand_a == cand_a_set)

cand_a_dupe_input = qs.find_candidate(
    store_a, AS_OF, list(KNOWN_SYMBOLS) + list(KNOWN_SYMBOLS))
check("duplicate symbols in the input do not change the result",
      cand_a == cand_a_dupe_input)

# ---------------------------------------------------------------------------
print("\n--- D: bounded search — only the declared grid is ever evaluated ---")
# ---------------------------------------------------------------------------

check("THRESHOLDS is the declared, fixed 2-value grid",
      qs.THRESHOLDS == (3.0, 4.0), qs.THRESHOLDS)
check("HORIZONS is the declared, fixed 3-value grid",
      qs.HORIZONS == (1, 3, 5), qs.HORIZONS)
check("the full grid is exactly 6 combinations",
      len(qs.THRESHOLDS) * len(qs.HORIZONS) == 6)

_seen_combos = []
_real_aggregate = qs._aggregate


def _counting_aggregate(by_symbol, threshold, horizon):
    _seen_combos.append((threshold, horizon))
    return _real_aggregate(by_symbol, threshold, horizon)


qs._aggregate = _counting_aggregate
try:
    # store_b's null fixture never clears the filter at any combo, so
    # find_candidate is forced to walk the ENTIRE grid before giving up —
    # exactly what's needed to observe every combination it ever tries.
    qs.find_candidate(store_b, AS_OF, KNOWN_SYMBOLS)
finally:
    qs._aggregate = _real_aggregate

expected_combos = [(t, h) for t in qs.THRESHOLDS for h in qs.HORIZONS]
check("exactly the 6 declared (threshold, horizon) combinations were evaluated, "
      "in the declared fixed order, nothing more and nothing else",
      _seen_combos == expected_combos, _seen_combos)

# ---------------------------------------------------------------------------
print("\n--- E: minimum sample filter ---")
# ---------------------------------------------------------------------------

check("_passes_filter rejects a strong-effect result with too few samples",
      not qs._passes_filter(qs.RelationshipStats(
          threshold=3.0, horizon=1, n=qs.MIN_SAMPLES_FOR_CANDIDATE - 1,
          mean_return=0.05, hit_rate=1.0, stdev=0.0, t_stat=None,
          first_observed="2024-01-01", last_observed="2024-01-01")))
check("_passes_filter accepts the same effect once n reaches the minimum",
      qs._passes_filter(qs.RelationshipStats(
          threshold=3.0, horizon=1, n=qs.MIN_SAMPLES_FOR_CANDIDATE,
          mean_return=0.05, hit_rate=1.0, stdev=0.0, t_stat=None,
          first_observed="2024-01-01", last_observed="2024-01-01")))

# End-to-end: one symbol, only 2 clean spikes (well under the minimum),
# strong 5% effect on each — must still produce no candidate.
store_e = fresh_store("e")
seed_symbol(store_e, "LONE", N_DAYS, [25, 85], spike_return=0.05)
cand_e = qs.find_candidate(store_e, AS_OF, ["LONE"])
check("too few real anomaly instances (below MIN_SAMPLES_FOR_CANDIDATE) "
      "produces no candidate even with a large effect size", cand_e is None)

# ---------------------------------------------------------------------------
print("\n--- F: effect-magnitude filter ---")
# ---------------------------------------------------------------------------

check("_passes_filter rejects a well-sampled but negligible effect",
      not qs._passes_filter(qs.RelationshipStats(
          threshold=3.0, horizon=1, n=50,
          mean_return=qs.MIN_EFFECT_MAGNITUDE / 2, hit_rate=0.5, stdev=0.01,
          t_stat=1.0, first_observed="2024-01-01", last_observed="2024-06-01")))
check("_passes_filter accepts the same sample size once the effect reaches the minimum",
      qs._passes_filter(qs.RelationshipStats(
          threshold=3.0, horizon=1, n=50,
          mean_return=qs.MIN_EFFECT_MAGNITUDE, hit_rate=0.5, stdev=0.01,
          t_stat=1.0, first_observed="2024-01-01", last_observed="2024-06-01")))

# End-to-end: plenty of clean spikes, but the forward return is deliberately
# far too small (0.05%) to clear MIN_EFFECT_MAGNITUDE (0.3%).
store_f = fresh_store("f")
for i, sym in enumerate(KNOWN_SYMBOLS):
    seed_symbol(store_f, sym, N_DAYS, SPIKE_INDICES, spike_return=0.0005, offset=i)
cand_f = qs.find_candidate(store_f, AS_OF, KNOWN_SYMBOLS)
check("plenty of samples but a below-bar effect size produces no candidate",
      cand_f is None)

# ---------------------------------------------------------------------------
print("\n--- G: hypothesis structure — passes existing validation unmodified ---")
# ---------------------------------------------------------------------------

proposal_g = qs.build_proposal(cand_a)
problems_g = hi.validate_proposal(proposal_g)
check("a produced proposal has zero problems under hi.validate_proposal()",
      problems_g == [], problems_g)
check("the proposal never supplies contract_id (system-minted only)",
      "contract_id" not in proposal_g)
check("the proposal's only unknown-top-level-key risk is absent: every key is "
      "in hypothesis_intake's own allowed set",
      set(proposal_g) <= (hi.REQUIRED_FIELDS | hi.OPTIONAL_FIELDS))
check("the entry_rule metric is on hypothesis_intake's supported-metric whitelist",
      proposal_g["entry_rule"]["conditions"][0]["metric"] in hi.SUPPORTED_METRICS)
check("the entry_rule operator is on hypothesis_intake's supported-operator whitelist",
      proposal_g["entry_rule"]["conditions"][0]["op"] in hi.SUPPORTED_OPERATORS)
check("the hypothesis text explicitly frames this as a hypothesis needing "
      "independent testing, not a proven strategy",
      "hypothesis" in proposal_g["hypothesis"].lower()
      and "independently specified" in proposal_g["hypothesis"])
check("the hypothesis text never claims a proven or profitable strategy",
      "profitable strategy" not in proposal_g["hypothesis"].lower()
      and "discovered alpha" not in proposal_g["hypothesis"].lower())

# ---------------------------------------------------------------------------
print("\n--- H: one-proposal cap ---")
# ---------------------------------------------------------------------------

reg_h = fresh_registry("h")
store_h = fresh_store("h")
seed_known_relationship(store_h)

result_h1 = qs.run_scan(store_h, AS_OF, KNOWN_SYMBOLS, registry_dir=reg_h)
check("the first scan run on a fresh registry produces exactly one draft",
      isinstance(result_h1, qs.ScanResult))
check("exactly one draft exists in the registry after one scan run",
      len(hi.pending_drafts(reg_h)) == 1, len(hi.pending_drafts(reg_h)))

result_h2 = qs.run_scan(store_h, AS_OF, KNOWN_SYMBOLS, registry_dir=reg_h)
check("a second scan run against the same data does not produce a second draft "
      "(the candidate now exactly duplicates the first draft's Contract)",
      isinstance(result_h2, qs.DuplicateCandidate), result_h2)
check("the registry still holds exactly one draft after the second run",
      len(hi.pending_drafts(reg_h)) == 1, len(hi.pending_drafts(reg_h)))
check("DuplicateCandidate correctly names the earlier draft's contract_id",
      result_h2.existing_contract_id == result_h1.contract_id)

# ---------------------------------------------------------------------------
print("\n--- I: existing governance — reuses validate_proposal/create_draft/duplicate detection ---")
# ---------------------------------------------------------------------------

reg_i = fresh_registry("i")
store_i = fresh_store("i")
seed_known_relationship(store_i)

result_i = qs.run_scan(store_i, AS_OF, KNOWN_SYMBOLS, registry_dir=reg_i)
check("run_scan produces a genuine ScanResult through the normal path",
      isinstance(result_i, qs.ScanResult))
if isinstance(result_i, qs.ScanResult):
    loaded = Contract.load(result_i.contract_id, reg_i)
    check("the resulting Contract is a DRAFT, never locked",
          loaded.status == "draft", loaded.status)
    check("the draft's locked_hash is unset (create_draft() never locks)",
          loaded.locked_hash is None)

# The SAME hi.create_draft() this module calls must still reject a malformed
# proposal exactly as it would for a human/AI proposal — demonstrating no
# second, weaker validation path was introduced for this module's own use.
bad_proposal = dict(qs.build_proposal(cand_a))
del bad_proposal["null_hypothesis"]  # remove a required field
raised = False
try:
    hi.create_draft(store_i, bad_proposal, registry_dir=reg_i)
except hi.IntakeRejected:
    raised = True
check("a malformed quant_scan proposal is rejected by the SAME "
      "hi.create_draft()/hi.validate_proposal() every other proposer uses",
      raised)

# Duplicate detection: pre-register a Contract with the exact fingerprint
# quant_scan's own candidate would produce, THEN scan — must be recognized
# as a duplicate of the PRE-EXISTING contract, not create a second one.
reg_i2 = fresh_registry("i2")
store_i2 = fresh_store("i2")
seed_known_relationship(store_i2)
pre_cand = qs.find_candidate(store_i2, AS_OF, KNOWN_SYMBOLS)
pre_proposal = qs.build_proposal(pre_cand)
pre_result = hi.create_draft(store_i2, pre_proposal, registry_dir=reg_i2)

result_i2 = qs.run_scan(store_i2, AS_OF, KNOWN_SYMBOLS, registry_dir=reg_i2)
check("a candidate matching an ALREADY-EXISTING contract's exact fingerprint "
      "is recognized as a duplicate via the existing global fingerprint rule",
      isinstance(result_i2, qs.DuplicateCandidate)
      and result_i2.existing_contract_id == pre_result.contract.id,
      result_i2)
check("no second contract was created for the duplicate candidate",
      len(_registry(reg_i2)) == 1, len(_registry(reg_i2)))

# ---------------------------------------------------------------------------
print("\n--- J: no Contract locking ---")
# ---------------------------------------------------------------------------

SRC = Path("research/brain/quant_scan.py").read_text()
BODY = _code_only(SRC)

check("quant_scan.py's code (outside docstrings) never calls .lock(",
      ".lock(" not in BODY)
check("quant_scan.py's code (outside docstrings) never calls approve_and_lock(",
      "approve_and_lock(" not in BODY)
check("quant_scan.py never imports approve_and_lock at all",
      not re.search(r"^\s*(?:from|import)\s+[^\n]*approve_and_lock", SRC, re.MULTILINE))
check("quant_scan.py's own module namespace has no approve_and_lock name",
      not hasattr(qs, "approve_and_lock"))

# ---------------------------------------------------------------------------
print("\n--- K: no experiment execution ---")
# ---------------------------------------------------------------------------

check("quant_scan.py's code never calls run_experiment(",
      "run_experiment(" not in BODY)
check("quant_scan.py's code never defines or calls simulate(",
      "simulate(" not in BODY)
check("quant_scan.py never imports research.experiments.runner at all",
      not re.search(r"^\s*(?:from|import)\s+[^\n]*experiments\.runner", SRC, re.MULTILINE)
      and not re.search(r"^\s*from\s+\.\.experiments\s+import\s+runner", SRC, re.MULTILINE))
check("quant_scan.py's own module namespace has no run_experiment/simulate name",
      not hasattr(qs, "run_experiment") and not hasattr(qs, "simulate"))

# ---------------------------------------------------------------------------
print("\n--- L: no AI ---")
# ---------------------------------------------------------------------------

check("quant_scan.py never imports subprocess",
      not re.search(r"^\s*(?:import|from)\s+subprocess\b", SRC, re.MULTILINE))
check("quant_scan.py never references a claude/anthropic CLI or SDK call",
      "claude" not in BODY.lower() and "anthropic" not in BODY.lower())
check("quant_scan.py never imports a network client (requests/httpx/urllib)",
      not re.search(r"^\s*(?:import|from)\s+(requests|httpx|urllib)\b", SRC, re.MULTILINE))
check("quant_scan.py's code contains no eval/exec/compile call",
      not re.search(r"\beval\s*\(|\bexec\s*\(|\bcompile\s*\(", BODY))
check("find_candidate/run_scan take no injectable AI-runner callback "
      "(unlike investigator.investigate()'s `runner` parameter — there is "
      "nothing here to inject because nothing here ever calls out)",
      "runner" not in qs.run_scan.__code__.co_varnames[:qs.run_scan.__code__.co_argcount])

# ---------------------------------------------------------------------------
print("\n--- M: no live engine ---")
# ---------------------------------------------------------------------------

check("quant_scan.py never imports anything from engine/",
      not re.search(r"^\s*(?:from|import)\s+engine\b", SRC, re.MULTILINE))
check("quant_scan.py's code never references memory/state.json",
      "state.json" not in BODY)
check("quant_scan.py never imports a broker module",
      not re.search(r"^\s*(?:from|import)\s+[^\n]*broker", SRC, re.MULTILINE))
check("quant_scan.py never imports engine.market_data",
      "engine.market_data" not in SRC)
check("quant_scan.py never imports engine.execute",
      "engine.execute" not in SRC)

# ---------------------------------------------------------------------------
print("\n--- N: leakage protection ---")
# ---------------------------------------------------------------------------

# N1 — pure-function proof: _zscore_at(sessions, i) reads ONLY sessions[0..i].
# Build one series with a clean, isolated spike; compute its z-score against
# the FULL series, then again against the series with every row AFTER i
# physically deleted. Both must agree exactly.
sessions_full = []
close = 100.0
for i in range(60):
    vol = 900_000 if i == 40 else 100_000 + (i % 5) * 100
    sessions_full.append({
        "session_date": (START + dt.timedelta(days=i)).isoformat(),
        "close": close, "volume": vol,
    })
    close = close * (1.01 if i == 40 else 1.0001)

z_full = qs._zscore_at(sessions_full, 40)
z_truncated = qs._zscore_at(sessions_full[:41], 40)  # every row after index 40 removed
check("_zscore_at at index i is unaffected by rows physically removed after i "
      "(the leakage guarantee, proven empirically, not just asserted)",
      z_full is not None and z_truncated is not None and z_full.z == z_truncated.z,
      (z_full, z_truncated))

# Mutating a row strictly AFTER i must also have zero effect.
sessions_mutated = [dict(s) for s in sessions_full]
sessions_mutated[41]["volume"] = 5_000_000  # a huge, obviously-different future value
sessions_mutated[55]["close"] = 1.0
z_mutated_future = qs._zscore_at(sessions_mutated, 40)
check("_zscore_at at index i is unaffected by mutating rows strictly after i",
      z_mutated_future is not None and z_mutated_future.z == z_full.z)

# Mutating the baseline (strictly BEFORE i) or the current session itself
# (at i) DOES matter — confirms the test above isn't vacuous.
sessions_mutated_baseline = [dict(s) for s in sessions_full]
sessions_mutated_baseline[25]["volume"] = 5_000_000  # inside the trailing
                                                       # WINDOW_DAYS(=20) baseline for i=40
z_mutated_baseline = qs._zscore_at(sessions_mutated_baseline, 40)
check("(sanity) mutating the BASELINE (strictly before i) does change the result "
      "— proving _zscore_at genuinely reads that data, not a vacuous no-op",
      z_mutated_baseline is not None and z_mutated_baseline.z != z_full.z)

# N2 — end-to-end AsOfView proof: the same symbol, scanned through two views
# at different `as_of` instants, must produce the identical z-score for a
# session both views can see, and the later-only forward return must simply
# be ABSENT (never invented) from the earlier view rather than differing.
store_n = fresh_store("n")
spike_day = START + dt.timedelta(days=40)
seed_symbol(store_n, "LEAK", 60, [40], knowledge_offset_days=lambda i: 0)

as_of_early = dt.datetime.combine(spike_day, dt.time(23, 59))  # spike day itself visible;
                                                                 # forward days are not
as_of_late = dt.datetime.combine(START + dt.timedelta(days=59), dt.time(23, 59))

view_early = store_n.view(as_of_early)
view_late = store_n.view(as_of_late)

z_early = qs._zscore_at(view_early.prices("LEAK", days=None), 40)
z_late = qs._zscore_at(view_late.prices("LEAK", days=None), 40)
check("the z-score at the spike session is identical whether or not the "
      "store's AsOfView is gated to hide every later session",
      z_early is not None and z_late is not None and z_early.z == z_late.z,
      (z_early, z_late))

instances_early = qs._scan_symbol(view_early, "LEAK")
instances_late = qs._scan_symbol(view_late, "LEAK")
check("with future sessions hidden by as_of, the spike day yields NO instance "
      "at all (forward return genuinely unknown — never invented)",
      not any(inst.session_date == spike_day.isoformat() for inst in instances_early))
check("with the full history visible, the same spike day DOES yield an "
      "instance with a computable forward return",
      any(inst.session_date == spike_day.isoformat() for inst in instances_late))

# ---------------------------------------------------------------------------
print("\n--- O: provenance ---")
# ---------------------------------------------------------------------------

reg_o = fresh_registry("o")
store_o = fresh_store("o")
seed_known_relationship(store_o)
result_o = qs.run_scan(store_o, AS_OF, KNOWN_SYMBOLS, registry_dir=reg_o)

check("run_scan on a known relationship produces a genuine ScanResult",
      isinstance(result_o, qs.ScanResult))
if isinstance(result_o, qs.ScanResult):
    prov = dp.discovery_search_for_hypothesis(store_o, result_o.hypothesis_id)
    check("exactly one discovery-search provenance row is recorded",
          len(prov) == 1, len(prov))
    if prov:
        row = prov[0]
        check("provenance discovery_type is 'mathematical'",
              row["discovery_type"] == "mathematical", row.get("discovery_type"))
        check("provenance version matches this module's QUANT_SCAN_VERSION",
              row["version"] == qs.QUANT_SCAN_VERSION, row.get("version"))
        check("provenance records the reference time (as_of)",
              isinstance(row.get("as_of"), str) and bool(row["as_of"]))
        check("provenance records the tested threshold grid",
              row.get("thresholds_tested") == list(qs.THRESHOLDS), row.get("thresholds_tested"))
        check("provenance records the tested horizon grid",
              row.get("horizons_tested") == list(qs.HORIZONS), row.get("horizons_tested"))
        check("provenance records which combination was actually selected",
              row.get("selected_threshold") == result_o.candidate.threshold
              and row.get("selected_horizon") == result_o.candidate.horizon)
        check("provenance is keyed to the resulting hypothesis_id",
              row.get("hypothesis_id") == result_o.hypothesis_id)
    check("has_discovery_provenance() agrees",
          dp.has_discovery_provenance(store_o, result_o.hypothesis_id))

# A rejected/no-candidate run must record NOTHING — mirroring investigator.py's
# own "provenance only after a genuine DRAFT" rule exactly.
reg_o2 = fresh_registry("o2")
store_o2 = fresh_store("o2")
for i, sym in enumerate(KNOWN_SYMBOLS):
    seed_flat(store_o2, sym, N_DAYS, offset=i)
qs.run_scan(store_o2, AS_OF, KNOWN_SYMBOLS, registry_dir=reg_o2)
all_discovery_rows = store_o2.view(AS_OF).observations(
    "research_discovery_search", latest_only=False)
check("a NoCandidate run writes no discovery-search provenance row at all",
      len(all_discovery_rows) == 0, len(all_discovery_rows))


# ---------------------------------------------------------------------------
print(f"\n{'=' * 52}\n  {PASSED} passed, {FAILED} failed\n{'=' * 52}")
sys.exit(1 if FAILED else 0)
