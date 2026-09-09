"""
Tests for Slice K — the validation/holdout single-use guard added to
derive_split_contract() (research/brain/hypothesis_intake.py).

THE GAP THIS CLOSES: before this slice, nothing stopped deriving several
validation (or holdout) siblings from the same locked parent contract and
keeping whichever result looked best — the Slice H architecture review's
flagged cherry-picking loophole. This file proves the closed version:

    (parent_contract_id, split_key)

may be derived AT MOST ONCE, in ANY status the resulting sibling ends up in
(draft, locked, running, reported, or abandoned) — enforced at the
DERIVATION layer, before a second sibling can ever become runnable, not by
refusing to re-run one that already exists (runner.py already did that,
unchanged by this slice) and not by flagging duplicates after the fact.

Also proves what this slice deliberately did NOT change: ordinary (non-
split-derived) contracts, the discovery-split prohibition, parent-linkage
and window semantics, content hashing, and runner.py's existing rerun
protection are all exercised here through their pre-existing, unmodified
behavior.

Run with:  python -m tests.test_validation_single_use
"""

import re
import sys
import shutil
import tempfile
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from research.store import Store  # noqa: E402
from research import memory as rm  # noqa: E402
from research.contracts import Contract, REGISTRY_DIR  # noqa: E402
from research.brain import hypothesis_intake as hi  # noqa: E402
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


TMP = Path(tempfile.mkdtemp(prefix="lq-test-validation-single-use-"))
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


def flat_series(n, base_close=100.0, base_vol=100_000, spike_at=None, spike_vol=5_000_000):
    closes = [base_close for _ in range(n)]
    vols = [base_vol + (i % 5) * 1000 for i in range(n)]
    if spike_at is not None:
        vols[spike_at] = spike_vol
    return closes, vols


def make_proposal(**overrides):
    base = {
        "title": "single-use guard smoke test",
        "hypothesis": "volume spikes precede short-term continuation",
        "null_hypothesis": "no relationship",
        "universe": "watchlist",
        "signal": "observatory.volume_zscore",
        "entry_rule": {"conditions": [{"metric": "volume_zscore", "op": ">", "value": 3.0}]},
        "exit_rule": {"stop_loss_pct": 5.0, "target_pct": 8.0, "max_hold_days": 15},
        "splits": {
            "discovery": ["2020-01-01", "2020-06-30"],
            "validation": ["2020-07-01", "2020-12-31"],
        },
        "independence": "one entry per symbol",
        "falsification": "t_stat < 2.0",
        "abandon_condition": "expectancy_r <= 0",
        "evaluation_start": "2020-01-01",
        "evaluation_end": "2020-06-30",
    }
    base.update(overrides)
    return base


def locked_contract(store, reg, **overrides):
    """Full pipeline, not a shortcut: proposal -> draft -> approve_and_lock."""
    proposal = make_proposal(**overrides)
    result = hi.create_draft(store, proposal, registry_dir=reg)
    locked = hi.approve_and_lock(store, result.contract.id, approved_by="Vaibhav", registry_dir=reg)
    return result.hypothesis_id, locked


# ---------------------------------------------------------------------------
print("\n--- A/B: first validation derivation succeeds, first run succeeds ---")
# ---------------------------------------------------------------------------

store1 = fresh_store("first")
reg1 = fresh_registry("first")
closes, vols = flat_series(365, spike_at=40)
seed_prices(store1, "RELIANCE", START, closes, vols)

hid1, parent1 = locked_contract(store1, reg1)

sib1 = hi.derive_split_contract(store1, parent1.id, "validation", hid1, registry_dir=reg1)
check("A: first derivation for (parent, validation) succeeds",
      sib1.contract.status == "draft")

locked_sib1 = hi.approve_and_lock(store1, sib1.contract.id, approved_by="Vaibhav", registry_dir=reg1)
run1 = runner.run_experiment(locked_sib1.id, store1, registry_dir=reg1)
check("B: first run of the validation sibling succeeds",
      run1["status"] == "reported")


# ---------------------------------------------------------------------------
print("\n--- C: second derivation for the same (parent, validation) is rejected ---")
# ---------------------------------------------------------------------------

try:
    hi.derive_split_contract(store1, parent1.id, "validation", hid1, registry_dir=reg1)
    check("C: a second derivation for the same (parent, validation) is rejected", False,
          "no exception raised")
except hi.IntakeRejected as e:
    check("C: a second derivation for the same (parent, validation) is rejected", True)
    check("C: the rejection names the earlier sibling's contract_id",
          any(locked_sib1.id in r for r in e.reasons), str(e.reasons))

# no new registry file was written by the rejected attempt
reg1_files_before = {p.name for p in reg1.glob("*.json")}
try:
    hi.derive_split_contract(store1, parent1.id, "validation", hid1, registry_dir=reg1)
except hi.IntakeRejected:
    pass
reg1_files_after = {p.name for p in reg1.glob("*.json")}
check("C: a rejected second derivation writes no new registry file",
      reg1_files_before == reg1_files_after, str(reg1_files_after - reg1_files_before))


# ---------------------------------------------------------------------------
print("\n--- D: the existing sibling still cannot be rerun (pre-existing runner behavior) ---")
# ---------------------------------------------------------------------------

try:
    runner.run_experiment(locked_sib1.id, store1, registry_dir=reg1)
    check("D: a reported sibling cannot be rerun", False, "no exception raised")
except runner.RunnerRejected as e:
    check("D: a reported sibling cannot be rerun (unchanged runner.py behavior)", True)
    check("D: the rejection is the existing 'not LOCKED' reason, not a new Slice-K reason",
          any("not LOCKED" in r for r in e.reasons), str(e.reasons))


# ---------------------------------------------------------------------------
print("\n--- E: a DIFFERENT split (holdout) on the same parent is a distinct, allowed pair ---")
# ---------------------------------------------------------------------------

store_e = fresh_store("distinct_split")
reg_e = fresh_registry("distinct_split")
seed_prices(store_e, "RELIANCE", START, *flat_series(365, spike_at=40))

hid_e, parent_e = locked_contract(
    store_e, reg_e,
    splits={
        "discovery": ["2020-01-01", "2020-06-30"],
        "validation": ["2020-07-01", "2020-09-30"],
        "holdout": ["2020-10-01", "2020-12-31"],
    },
)

sib_val = hi.derive_split_contract(store_e, parent_e.id, "validation", hid_e, registry_dir=reg_e)
check("E: (parent, validation) derivation succeeds", sib_val.contract.status == "draft")

sib_hold = hi.derive_split_contract(store_e, parent_e.id, "holdout", hid_e, registry_dir=reg_e)
check("E: (parent, holdout) derivation ALSO succeeds — a distinct pair from "
      "(parent, validation), not blocked by it",
      sib_hold.contract.status == "draft")
check("E: the two siblings are genuinely different contracts",
      sib_val.contract.id != sib_hold.contract.id)

# but a second (parent, validation) is still rejected, and a second (parent, holdout) too
for split_key, first_id in (("validation", sib_val.contract.id), ("holdout", sib_hold.contract.id)):
    try:
        hi.derive_split_contract(store_e, parent_e.id, split_key, hid_e, registry_dir=reg_e)
        check(f"E: a second ({split_key}) derivation is still rejected", False,
              "no exception raised")
    except hi.IntakeRejected as e:
        check(f"E: a second ({split_key}) derivation is still rejected", True)
        check(f"E: it names the correct earlier sibling ({first_id})",
              any(first_id in r for r in e.reasons), str(e.reasons))


# ---------------------------------------------------------------------------
print("\n--- F: the SAME split on a DIFFERENT parent is a distinct, allowed pair ---")
# ---------------------------------------------------------------------------

store_f = fresh_store("distinct_parent")
reg_f = fresh_registry("distinct_parent")
seed_prices(store_f, "RELIANCE", START, *flat_series(365, spike_at=40))
seed_prices(store_f, "TCS", START, *flat_series(365, spike_at=40))

hid_fa, parent_fa = locked_contract(store_f, reg_f, title="parent A", universe="watchlist")
hid_fb, parent_fb = locked_contract(store_f, reg_f, title="parent B", universe="watchlist")
check("F: two independently drafted+locked parents got distinct ids",
      parent_fa.id != parent_fb.id, f"{parent_fa.id} vs {parent_fb.id}")

sib_fa = hi.derive_split_contract(store_f, parent_fa.id, "validation", hid_fa, registry_dir=reg_f)
check("F: (parent-A, validation) derivation succeeds", sib_fa.contract.status == "draft")

sib_fb = hi.derive_split_contract(store_f, parent_fb.id, "validation", hid_fb, registry_dir=reg_f)
check("F: (parent-B, validation) derivation ALSO succeeds — does not collide "
      "with (parent-A, validation)", sib_fb.contract.status == "draft")

try:
    hi.derive_split_contract(store_f, parent_fa.id, "validation", hid_fa, registry_dir=reg_f)
    check("F: a second (parent-A, validation) is still rejected", False, "no exception raised")
except hi.IntakeRejected:
    check("F: a second (parent-A, validation) is still rejected", True)

try:
    hi.derive_split_contract(store_f, parent_fb.id, "validation", hid_fb, registry_dir=reg_f)
    check("F: a second (parent-B, validation) is still rejected", False, "no exception raised")
except hi.IntakeRejected:
    check("F: a second (parent-B, validation) is still rejected", True)


# ---------------------------------------------------------------------------
print("\n--- G: ordinary discovery contracts are completely unaffected ---")
# ---------------------------------------------------------------------------

store_g = fresh_store("discovery_unaffected")
reg_g = fresh_registry("discovery_unaffected")

# An ordinary contract that is never split-derived at all: create_draft() +
# approve_and_lock() behave exactly as before this slice — this function's
# code path never even runs.
hid_g, parent_g = locked_contract(store_g, reg_g)
check("G: an ordinary locked contract is unaffected by this slice",
      parent_g.status == "locked" and parent_g.locked_hash is not None)

# derive_split_contract() still refuses split_key="discovery" outright, for
# the SAME pre-existing reason as before — not because of the new single-use
# guard (the discovery prohibition is checked and raises before the guard is
# ever reached).
try:
    hi.derive_split_contract(store_g, parent_g.id, "discovery", hid_g, registry_dir=reg_g)
    check("G: split_key='discovery' is still refused outright", False, "no exception raised")
except hi.IntakeRejected as e:
    check("G: split_key='discovery' is still refused outright (pre-existing rule)", True)
    check("G: for the pre-existing reason, not a single-use rejection",
          any("must not be" in r and "discovery" in r for r in e.reasons)
          and not any("already been" in r for r in e.reasons), str(e.reasons))

# A second, independent hypothesis/parent with its OWN validation split is
# entirely unaffected by store1's single-use state from section A-C above —
# no global "one validation ever" limitation was introduced.
hid_g2, parent_g2 = locked_contract(store_g, reg_g, title="a second, independent parent")
sib_g2 = hi.derive_split_contract(store_g, parent_g2.id, "validation", hid_g2, registry_dir=reg_g)
check("G: a fresh parent's first validation derivation succeeds normally — "
      "no global one-run-ever limitation exists",
      sib_g2.contract.status == "draft")


# ---------------------------------------------------------------------------
print("\n--- H: an ABANDONED validation attempt still consumes the single-use slot ---")
# ---------------------------------------------------------------------------
# Documented policy decision (see derive_split_contract()'s docstring,
# "SINGLE-USE VALIDATION/HOLDOUT SPLITS"): an abandoned first sibling is NOT
# forgiven. This mirrors research.contracts.comparison_count()'s existing
# treatment of abandoned contracts in the wider multiple-comparisons ledger
# (they still count), and closes the concrete retry loop that forgiving it
# would open, since "abandoned" today is set by exactly one code path
# (run_experiment()'s except-block on any exception) — a technical failure,
# not necessarily an unfavorable result.

store_h = fresh_store("abandoned")
reg_h = fresh_registry("abandoned")
hid_h, parent_h = locked_contract(store_h, reg_h)

sib_h = hi.derive_split_contract(store_h, parent_h.id, "validation", hid_h, registry_dir=reg_h)
locked_sib_h = hi.approve_and_lock(store_h, sib_h.contract.id, approved_by="Vaibhav", registry_dir=reg_h)

# Simulate the outcome of a completed-but-failed run without needing to
# manufacture a genuine runner exception — the same technique
# tests/test_research_digest.py already uses for an abandoned fixture
# ("simulates the outcome of a completed run"). The single-use guard reads
# only the hypothesis-claim log (already written by derive_split_contract()
# above), never the sibling's live status, so this is a faithful test of the
# actual policy under test, not a shortcut around it.
locked_sib_h.status = "abandoned"
locked_sib_h.save(reg_h)

try:
    hi.derive_split_contract(store_h, parent_h.id, "validation", hid_h, registry_dir=reg_h)
    check("H: a second derivation after an ABANDONED first attempt is still rejected",
          False, "no exception raised — abandoned was silently forgiven")
except hi.IntakeRejected as e:
    check("H: a second derivation after an ABANDONED first attempt is still rejected", True)
    check("H: the rejection names the abandoned sibling",
          any(locked_sib_h.id in r for r in e.reasons), str(e.reasons))


# ---------------------------------------------------------------------------
print("\n--- malformed / unrelated split metadata does not bypass or falsely trigger the rule ---")
# ---------------------------------------------------------------------------

store_m = fresh_store("malformed_metadata")
reg_m = fresh_registry("malformed_metadata")
hid_m, parent_m = locked_contract(store_m, reg_m)
hid_m2, parent_m2 = locked_contract(store_m, reg_m, title="an unrelated parent")

# Rows that pre-date Slice K (no split_of/split at all — e.g. the parent's
# own original creation claim) must not be mistaken for a match.
own_claim_rows = rm.query_research_log(store_m, rm.DATASET_HYPOTHESIS)
check("sanity: the parent's own creation claim has no split_of/split fields",
      all("split_of" not in r["payload"] for r in own_claim_rows
          if r["payload"].get("contract_id") == parent_m.id))

# A claim naming a DIFFERENT split_key for the same parent must not block
# "validation".
rm.record_hypothesis_proposal(
    store_m, claim="unrelated", source="test-fixture", hypothesis_id=hid_m,
    extra={"contract_id": "EXP-FAKE-Z", "split_of": parent_m.id, "split": "holdout"})

# A claim naming the CORRECT split_key but a DIFFERENT parent must not block
# "validation" on parent_m either.
rm.record_hypothesis_proposal(
    store_m, claim="unrelated", source="test-fixture", hypothesis_id=hid_m,
    extra={"contract_id": "EXP-FAKE-Y", "split_of": parent_m2.id, "split": "validation"})

# A malformed/incomplete claim (split_of present, split missing entirely)
# must not raise or false-match either.
rm.record_hypothesis_proposal(
    store_m, claim="unrelated", source="test-fixture", hypothesis_id=hid_m,
    extra={"contract_id": "EXP-FAKE-X", "split_of": parent_m.id})

sib_m = hi.derive_split_contract(store_m, parent_m.id, "validation", hid_m, registry_dir=reg_m)
check("unrelated/malformed split metadata does not block a legitimate "
      "first derivation", sib_m.contract.status == "draft")

# Now the REAL match exists — a second attempt must be rejected, and must
# name the real sibling, not one of the fake rows above.
try:
    hi.derive_split_contract(store_m, parent_m.id, "validation", hid_m, registry_dir=reg_m)
    check("a genuine duplicate is still caught after the malformed rows", False,
          "no exception raised")
except hi.IntakeRejected as e:
    check("a genuine duplicate is still caught after the malformed rows", True)
    check("it names the real sibling, not one of the fake/unrelated rows",
          any(sib_m.contract.id in r for r in e.reasons)
          and not any("EXP-FAKE" in r for r in e.reasons), str(e.reasons))


# ---------------------------------------------------------------------------
print("\n--- isolation / scope guard ---")
# ---------------------------------------------------------------------------

INTAKE_SRC = (Path(__file__).parent.parent / "research" / "brain"
              / "hypothesis_intake.py").read_text()
INTAKE_BODY = INTAKE_SRC.split('"""', 2)[-1]  # strip the leading module docstring

check("hypothesis_intake.py still contains no eval/exec/compile call",
      not re.search(r"\beval\s*\(|\bexec\s*\(|(?<!re\.)\bcompile\s*\(", INTAKE_BODY))

intake_imports = re.findall(r"^\s*(?:from|import)\s+([.\w]+)", INTAKE_SRC, re.MULTILINE)
check("hypothesis_intake.py's only engine import remains engine.watchlist — "
      "Slice K introduced no new import",
      {m for m in intake_imports if m.split(".")[0] == "engine"} <= {"engine.watchlist"},
      str(intake_imports))

check("the single-use guard adds no new store/schema access — it reads only "
      "the hypothesis-claim rows already fetched via _hypothesis_claims()",
      "store.append(" not in INTAKE_BODY.split("def derive_split_contract")[1].split("\ndef ")[0])

RUNNER_SRC = (Path(__file__).parent.parent / "research" / "experiments"
              / "runner.py").read_text()
check("runner.py's pre-existing rerun protection (status must be exactly "
      "'locked') is present and unmodified by this slice",
      'contract.status != "locked"' in RUNNER_SRC)
check("runner.py contains no reference to the new single-use mechanism — "
      "this slice's fix lives entirely at the derivation layer, not the "
      "execution layer",
      "_prior_split_derivation" not in RUNNER_SRC and "split_of" not in RUNNER_SRC)


for s in (store1, store_e, store_f, store_g, store_h, store_m):
    s.close()

print(f"\n{'=' * 52}")
print(f"  {PASSED} passed, {FAILED} failed")
print(f"{'=' * 52}\n")
sys.exit(1 if FAILED else 0)
