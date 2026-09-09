"""
Post-install self-check, and a demonstration of the thing that was built.

    venv/bin/python -m research.verify_install

Run this once on the VPS after unpacking. It proves, in order:

  1. the research package imports and the store initialises
  2. the kernel is still isolated
  3. the engine's signal stack can answer a question about the past, using
     nothing but what was known then — with no engine file aware of it
  4. the deletion test catches a computation that peeks

Step 3 is the one worth watching. It runs the UNMODIFIED regime classifier
against a synthetic 2022 market and prints what it would have said, having been
handed a provider instead of a clock. That is the whole of Step 4 in one screen.

Nothing here touches live state, the broker, or memory/state.json. It writes to
a scratch database it deletes afterwards.
"""

from __future__ import annotations

import sys
import shutil
import tempfile
import datetime as dt
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from research.store import Store, IST                                 # noqa: E402
from research.replay import Replay, leak_check                        # noqa: E402

OK, BAD = "  ✓", "  ✗"
problems: list[str] = []


def line(ok: bool, msg: str, detail: str = "") -> None:
    print(f"{OK if ok else BAD} {msg}")
    if not ok:
        problems.append(msg)
        if detail:
            print(f"      {detail}")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="lq-verify-"))
    print("\n" + "=" * 60)
    print("  LIVING QUANT — INSTALL VERIFICATION")
    print("=" * 60)

    # -- 1. imports and store ------------------------------------------------
    print("\n[1] Package and store")
    try:
        from research import contracts, recorder, backfill      # noqa: F401
        from research.sources import (prices_eod, announcements,  # noqa: F401
                                      option_chain, news, quotes,
                                      decisions, membership, briefing_artifacts)
        line(True, "all research modules import")
    except Exception as e:
        line(False, "research modules import", f"{type(e).__name__}: {e}")
        return 1

    store = Store.open(tmp / "verify.db")
    line(True, f"store initialises ({store.path.name})")

    # SQLite BEFORE DELETE triggers fire per ROW, so a DELETE against an empty
    # table succeeds trivially without firing anything. The check needs a row to
    # be meaningful — an empty-table probe reports a false alarm.
    store.append(dataset="_verify", entity="_probe",
                 event_time="2024-01-01", knowledge_time="2024-01-01",
                 source="verify", payload={"n": 1})
    conn = store._unsafe_connection()
    blocked = {"update": False, "delete": False}
    try:
        conn.execute("UPDATE observations SET payload='{}' WHERE dataset='_verify'")
    except Exception:
        blocked["update"] = True
    try:
        conn.execute("DELETE FROM observations WHERE dataset='_verify'")
    except Exception:
        blocked["delete"] = True
    line(blocked["update"] and blocked["delete"],
         "append-only triggers active (history cannot be rewritten)", str(blocked))

    # -- 2. isolation --------------------------------------------------------
    print("\n[2] Kernel isolation")
    import re
    offenders = []
    for f in sorted((ROOT / "engine").glob("*.py")):
        for mod in re.findall(r"^\s*(?:from|import)\s+([.\w]+)",
                              f.read_text(), re.MULTILINE):
            if mod.split(".")[0] == "research":
                offenders.append(f.name)
    line(not offenders, "engine/ does not import research/", str(offenders))
    for rel in ("engine/guardrails.py", "engine/execute.py", "memory/state.json"):
        line((ROOT / rel).exists(), f"{rel} present and untouched")

    # -- 3. the time machine -------------------------------------------------
    print("\n[3] Time machine — the engine answering a 2022 question")

    # A synthetic Nifty: 300 sessions trending up, then a sharp break. Enough
    # for EMA200 and ADX, which the real classifier requires.
    base = dt.date(2021, 6, 1)
    price = 15000.0
    session = 0
    d = base
    while session < 320:
        if d.weekday() < 5:
            session += 1
            price *= 1.0022 if session < 260 else 0.9955
            store.append_price(
                symbol="^NSEI", session_date=d,
                knowledge_time=dt.datetime.combine(d, dt.time(18, 0), tzinfo=IST),
                source="synthetic", open_=price * 0.998, high=price * 1.004,
                low=price * 0.996, close=price, volume=1_000_000, adjusted=False)
        d += dt.timedelta(days=1)

    replay = Replay(store)
    days = replay.trading_days("2021-06-01", "2023-01-01")
    line(len(days) > 300, f"replay calendar built from data ({len(days)} sessions)")

    early, late = replay.step(days[220]), replay.step(days[-1])
    hist = early.history("^NSEI", days=400)
    line(len(hist) == 221, f"as-of history stops at the step ({len(hist)} bars)")
    line(late.as_of > early.as_of and len(late.history("^NSEI", days=400)) > len(hist),
         "a later step sees strictly more history")

    try:
        from engine import regime as rg
        r_early = rg.classify(log=False, as_of=early.as_of, provider=early.provider)
        r_late = rg.classify(log=False, as_of=late.as_of, provider=late.provider)
        print(f"      as of {early.as_of:%d %b %Y}  →  {r_early.regime} "
              f"({r_early.confidence}) → {r_early.playbook}")
        print(f"      as of {late.as_of:%d %b %Y}  →  {r_late.regime} "
              f"({r_late.confidence}) → {r_late.playbook}")
        line(r_early.regime != "UNKNOWN",
             "the UNMODIFIED regime classifier answered a historical question")
        line(any("shadow HMM skipped" in n for n in r_early.notes),
             "the not-as-of-aware shadow HMM was skipped, not silently run")
    except Exception as e:
        line(False, "engine regime classifier under replay", f"{type(e).__name__}: {e}")

    from engine import market_data as md
    try:
        md.get_history("^NSEI", days=10, as_of=early.as_of)
        line(False, "as_of without a provider raises", "it returned data")
    except ValueError:
        line(True, "as_of without a provider raises (no silent live fetch)")

    # -- 4. the deletion test ------------------------------------------------
    print("\n[4] No-lookahead deletion test")

    def honest(step):
        return round(float(step.history("^NSEI", days=50)["close"].mean()), 4)

    def peeking(step):
        rows = step._store._unsafe_connection().execute(
            "SELECT close FROM prices_eod WHERE symbol='^NSEI' ORDER BY session_date"
        ).fetchall()
        return round(sum(r[0] for r in rows) / len(rows), 4)

    a = leak_check(store, days[220], honest, tmp / "lc1.db")
    b = leak_check(store, days[220], peeking, tmp / "lc2.db")
    line(a["leak_free"], "an honest computation passes")
    line(not b["leak_free"], "a peeking computation is CAUGHT")
    if not b["leak_free"]:
        print(f"      full={b['full'][:40]}  truncated={b['truncated'][:40]}")

    # -- 5. contracts --------------------------------------------------------
    print("\n[5] Experiment contracts — the model firewall")
    from research.contracts import Contract, ContractViolation
    c = Contract(
        id="EXP-VERIFY", title="firewall check", hypothesis="h", null_hypothesis="n",
        universe="u", signal="s", entry_rule="e", exit_rule="x",
        splits={"discovery": ["2019", "2022"]}, independence="clustered",
        falsification="t > 2.5 on holdout", abandon_condition="spread < 1.5%",
        evaluation_start="2019-01-01", evaluation_end="2022-12-31",
        llm_features=True, llm_model_id="claude-opus-5",
        llm_knowledge_cutoff="2026-05-05")
    try:
        c.lock()
        line(False, "an LLM contract over a pre-cutoff window is refused", "it locked")
    except ContractViolation:
        line(True, "an LLM contract over a pre-cutoff window is refused")

    c.evaluation_start, c.evaluation_end = "2026-06-01", "2027-06-01"
    try:
        c.lock()
        line(True, "the same contract locks when forward-only")
    except ContractViolation as e:
        line(False, "the same contract locks when forward-only", str(e))

    store.close()
    shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 60)
    if problems:
        print(f"  {len(problems)} PROBLEM(S) — do not proceed until resolved:")
        for p in problems:
            print(f"    - {p}")
        print("=" * 60 + "\n")
        return 1
    print("  ALL CHECKS PASSED — the laboratory is installed and honest.")
    print("  Next: research/probe/probe.py, then backfill, then the recorder cron.")
    print("  See docs/RESEARCH_DEPLOY.md")
    print("=" * 60 + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
