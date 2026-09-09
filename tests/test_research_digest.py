"""
Tests for Phase 1 Slice C: research/brain/digest.py.

Nothing here touches engine/, research/replay.py, research/schema.sql, or
Claude permissions.

Run with:  python -m tests.test_research_digest
"""

import sys
import json
import shutil
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from research.store import Store  # noqa: E402
from research import memory as rm  # noqa: E402
from research.contracts import Contract  # noqa: E402
from research.brain import digest as dg  # noqa: E402

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


TMP = Path(tempfile.mkdtemp(prefix="lq-test-digest-"))


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


def make_contract(cid, **overrides):
    fields = dict(
        id=cid, title=f"idea {cid}", hypothesis=f"claim behind {cid}",
        null_hypothesis="no effect", universe="watchlist", signal="observatory.volume_zscore",
        entry_rule=json.dumps({"conditions": [{"metric": "volume_zscore", "op": ">", "value": 3.0}]}),
        exit_rule=json.dumps({"stop_loss_pct": 2.0, "target_pct": 4.0}),
        splits={"discovery": ["2019-01-01", "2020-01-01"]},
        independence="clustered by symbol-day", falsification="t_stat < 2.0",
        abandon_condition="expectancy_r <= 0 on discovery",
        evaluation_start="2019-01-01", evaluation_end="2020-01-01",
    )
    fields.update(overrides)
    return Contract(**fields)


# ---------------------------------------------------------------------------
print("\n--- basic assembly: anomalies + registry + provenance ---")
# ---------------------------------------------------------------------------

store = fresh_store("basic")
reg = fresh_registry("basic")

rm.record_anomaly(store, entity="RELIANCE", metric="volume_zscore", value=3.4, baseline=1.0,
                   z_score=3.4, as_of="2024-05-10", source="observatory.volume_zscore",
                   extra={"source_dataset": "prices_eod", "n_observations": 20, "std": 0.5})
rm.record_anomaly(store, entity="INFY", metric="price_move_zscore", value=-0.12, baseline=0.0,
                   z_score=-3.8, as_of="2024-05-11", source="observatory.price_move_zscore",
                   extra={"source_dataset": "prices_eod", "n_observations": 20, "std": 0.03})

c1 = make_contract("EXP-D-1")
c1.lock()
c1.save(reg)

as_of = "2024-05-12"
d = dg.build_digest(store, as_of, registry_dir=reg)

check("digest has an as_of field", "as_of" in d)
check("digest carries provenance.includes and provenance.excludes",
      "includes" in d["provenance"] and "excludes" in d["provenance"])
check("provenance explicitly excludes engine/ and live trading state",
      any("engine" in x for x in d["provenance"]["excludes"])
      and any("state.json" in x for x in d["provenance"]["excludes"]))
check("both anomalies (as_of before the digest's as_of) are shown",
      d["anomalies"]["shown_count"] == 2, str(d["anomalies"]))
check("anomaly items carry the required metadata fields",
      set(d["anomalies"]["shown"][0]) == {
          "anomaly_id", "dataset", "entity", "metric", "value", "baseline", "z_score", "as_of"})
check("anomalies are sorted (RELIANCE before INFY, by event time)",
      [a["entity"] for a in d["anomalies"]["shown"]] == ["RELIANCE", "INFY"])
check("the locked contract appears in the registry section",
      d["contract_registry"]["count"] == 1 and d["contract_registry"]["contracts"][0]["id"] == "EXP-D-1")

# an anomaly recorded AFTER the digest's as_of must not leak in
rm.record_anomaly(store, entity="TCS", metric="volume_zscore", value=5.0, baseline=1.0,
                   z_score=5.0, as_of="2024-05-20", source="observatory.volume_zscore",
                   extra={"source_dataset": "prices_eod"})
d_same_as_of = dg.build_digest(store, as_of, registry_dir=reg)
check("an anomaly published after the digest's as_of does not appear",
      d_same_as_of["anomalies"]["shown_count"] == 2)


# ---------------------------------------------------------------------------
print("\n--- 13: previously tested / falsified contracts appear in the digest ---")
# ---------------------------------------------------------------------------

store2 = fresh_store("tested")
reg2 = fresh_registry("tested")

c_locked = make_contract("EXP-T-1")
c_locked.lock()
c_locked.save(reg2)

c_abandoned = make_contract("EXP-T-2", title="a dead end")
c_abandoned.lock()
c_abandoned.status = "abandoned"  # simulates the outcome of a completed (Slice D, not built) run
c_abandoned.save(reg2)

c_reported = make_contract("EXP-T-3", title="a finished, reported study")
c_reported.lock()
c_reported.status = "reported"
c_reported.save(reg2)

c_draft = make_contract("EXP-T-4", title="still being drafted")
c_draft.save(reg2)  # never locked

d2 = dg.build_digest(store2, "2030-01-01", registry_dir=reg2)

check("all four contracts appear in the full registry listing",
      d2["contract_registry"]["count"] == 4)
tested_ids = {c["id"] for c in d2["previously_tested"]["contracts"]}
check("abandoned and reported contracts appear under previously_tested",
      tested_ids == {"EXP-T-2", "EXP-T-3"}, str(tested_ids))
check("a still-locked (not yet evaluated) contract does NOT appear as previously tested",
      "EXP-T-1" not in tested_ids)
check("a draft contract does NOT appear as previously tested",
      "EXP-T-4" not in tested_ids)
check("previously_tested entries carry enough detail to avoid re-proposing the idea "
      "(falsification / abandon_condition / entry-exit rules present)",
      all({"falsification", "abandon_condition", "entry_rule", "exit_rule"} <= set(c)
          for c in d2["previously_tested"]["contracts"]))


# ---------------------------------------------------------------------------
print("\n--- 14: multiple-comparison context appears in the digest ---")
# ---------------------------------------------------------------------------

check("comparison_count counts locked + abandoned + reported, excludes draft",
      d2["contract_registry"]["comparison_count"] == 3,
      f"got {d2['contract_registry']['comparison_count']}")

store3 = fresh_store("mc_empty")
reg3 = fresh_registry("mc_empty")
d3 = dg.build_digest(store3, "2024-01-01", registry_dir=reg3)
check("an empty registry reports comparison_count 0, not an error",
      d3["contract_registry"]["comparison_count"] == 0)
check("an empty registry reports zero anomalies cleanly",
      d3["anomalies"]["shown_count"] == 0 and d3["anomalies"]["total_count"] == 0)


# ---------------------------------------------------------------------------
print("\n--- 12: digest is deterministic ---")
# ---------------------------------------------------------------------------

d_a = dg.build_digest(store2, "2030-01-01", registry_dir=reg2)
d_b = dg.build_digest(store2, "2030-01-01", registry_dir=reg2)
check("two builds of the same store/as_of produce an identical dict", d_a == d_b)

json_a = dg.to_json(d_a)
json_b = dg.to_json(d_b)
check("the rendered JSON text is byte-for-byte identical across builds", json_a == json_b)

# Rebuild from scratch (fresh Store handle, fresh registry read) to make sure
# determinism isn't an artifact of in-process caching.
store2b = Store.open(TMP / "tested.db")
d_c = dg.build_digest(store2b, "2030-01-01", registry_dir=reg2)
check("determinism holds even against a brand-new Store handle on the same file",
      d_a == d_c)
store2b.close()


# ---------------------------------------------------------------------------
print("\n--- bounding: anomaly_limit truncates deterministically ---")
# ---------------------------------------------------------------------------

store4 = fresh_store("bounded")
reg4 = fresh_registry("bounded")
for i in range(10):
    rm.record_anomaly(store4, entity=f"SYM{i}", metric="volume_zscore", value=float(i),
                       baseline=0.0, z_score=3.0 + i, as_of=f"2024-01-{i + 1:02d}",
                       source="observatory.volume_zscore", extra={"source_dataset": "prices_eod"})
d4 = dg.build_digest(store4, "2024-02-01", anomaly_limit=3, registry_dir=reg4)
check("anomaly_limit caps the shown list", d4["anomalies"]["shown_count"] == 3)
check("total_count still reports the true total, not the capped count",
      d4["anomalies"]["total_count"] == 10)
check("truncated flag is set when the cap actually cut something", d4["anomalies"]["truncated"] is True)
check("the most recent anomalies are kept, not the oldest, when truncating",
      [a["entity"] for a in d4["anomalies"]["shown"]] == ["SYM7", "SYM8", "SYM9"])

d4_again = dg.build_digest(store4, "2024-02-01", anomaly_limit=3, registry_dir=reg4)
check("truncation is itself deterministic across repeated calls", d4 == d4_again)


# ---------------------------------------------------------------------------
print("\n--- Isolation: digest.py imports nothing from engine/ ---")
# ---------------------------------------------------------------------------

import re  # noqa: E402

src = (Path(__file__).parent.parent / "research" / "brain" / "digest.py").read_text()
imports = re.findall(r"^\s*(?:from|import)\s+([.\w]+)", src, re.MULTILINE)
check("digest.py imports no engine module", not any(m.startswith("engine") for m in imports), str(imports))
# The docstring legitimately *documents* what it excludes (engine/, state.json,
# guardrails) as part of stating the provenance boundary; the import check
# above is what actually proves nothing engine-side is reachable. Here we
# additionally confirm the module performs no file writes of its own at all —
# a digest builder that only reads should never need to.
check("digest.py contains no file-write calls (no open(...,'w'), no Path.write_*)",
      not re.search(r"open\([^)]*['\"]w", src) and ".write_text(" not in src
      and ".write(" not in src)


for s in (store, store2, store3, store4):
    s.close()

print(f"\n{'=' * 52}")
print(f"  {PASSED} passed, {FAILED} failed")
print(f"{'=' * 52}\n")
sys.exit(1 if FAILED else 0)
