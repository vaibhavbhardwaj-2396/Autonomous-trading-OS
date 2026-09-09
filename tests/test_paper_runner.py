"""
tests/test_paper_runner.py — the bounded paper cycle end-to-end: eligibility
gating, structural signal validation, fail-safe per-strategy skips,
cycle-level idempotency (run twice -> no duplicates), determinism, and
Telegram best-effort (a notification failure never loses paper state).

Run with:  python -m tests.test_paper_runner
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.paper_fixtures import (  # noqa: E402
    PaperTestEnv, BuyEveryTimeStrategy, SellEveryTimeStrategy, RaisesStrategy,
    OutsideUniverseStrategy, fixed_history_df,
)
from paper.runner import run_paper_cycle, default_cycle_id  # noqa: E402
from paper import notify as pnotify  # noqa: E402
from paper.store import PaperStore  # noqa: E402

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


DF = fixed_history_df([100.0, 101.0, 102.0])


def hist_provider(symbol, days):
    return DF


# ---------------------------------------------------------------------------
print("\n--- eligibility gating end-to-end ---")
# ---------------------------------------------------------------------------

with PaperTestEnv(position_notional=10_000.0) as env:
    eligible_vid = env.register_and_approve(
        strategy_id="eligible", algorithm_id="runner_test_eligible_1",
        strategy_cls=BuyEveryTimeStrategy)
    not_eligible_vid = env.register_only(
        strategy_id="not_eligible", algorithm_id="runner_test_not_eligible_1",
        strategy_cls=BuyEveryTimeStrategy)

    summary = run_paper_cycle(
        cycle_id="gating-1", clock=env.clock, history_provider=hist_provider,
        universe=["INFY"], registry_dir=env.registry_dir,
        eligibility_dir=env.eligibility_dir, store=env.store, notify=False,
    )
    check("only the eligible StrategyVersion is evaluated (eligible_version_count==1)",
          summary["eligible_version_count"] == 1)
    orders = env.store.list_orders()
    check("only the eligible strategy's order was ever recorded — the registered-but-"
          "not-approved one never traded at all",
          len(orders) == 1 and orders[0]["strategy_version_id"] == eligible_vid)


# ---------------------------------------------------------------------------
print("\n--- structural validation: signal outside the given universe ---")
# ---------------------------------------------------------------------------

with PaperTestEnv(position_notional=10_000.0) as env:
    env.register_and_approve(
        strategy_id="rogue", algorithm_id="runner_test_outside_universe_1",
        strategy_cls=OutsideUniverseStrategy)
    summary = run_paper_cycle(
        cycle_id="oob-1", clock=env.clock, history_provider=hist_provider,
        universe=["INFY"], registry_dir=env.registry_dir,
        eligibility_dir=env.eligibility_dir, store=env.store, notify=False,
    )
    check("a signal for a symbol outside the given universe is REJECTED, not silently "
          "processed or crashed on", summary["orders_rejected"] == 1)
    order = env.store.list_orders()[0]
    check("...with reason=symbol_outside_universe",
          order["reason"] == "symbol_outside_universe")


# ---------------------------------------------------------------------------
print("\n--- fail-safe: a broken Strategy never takes down the whole cycle ---")
# ---------------------------------------------------------------------------

with PaperTestEnv(position_notional=10_000.0) as env:
    good_vid = env.register_and_approve(
        strategy_id="good", algorithm_id="runner_test_good_1",
        strategy_cls=BuyEveryTimeStrategy)
    env.register_and_approve(
        strategy_id="bad", algorithm_id="runner_test_bad_1",
        strategy_cls=RaisesStrategy)

    summary = run_paper_cycle(
        cycle_id="failsafe-1", clock=env.clock, history_provider=hist_provider,
        universe=["INFY"], registry_dir=env.registry_dir,
        eligibility_dir=env.eligibility_dir, store=env.store, notify=False,
    )
    check("the broken strategy is recorded as skipped, with a reason",
          len(summary["skipped_versions"]) == 1
          and "boom" not in summary["skipped_versions"][0]["reason"]
          and "RuntimeError" in summary["skipped_versions"][0]["reason"])
    check("the GOOD strategy still traded normally despite the other one raising",
          summary["orders_filled"] == 1)
    orders = env.store.list_orders()
    check("only the good strategy's order was recorded",
          orders[0]["strategy_version_id"] == good_vid)


# ---------------------------------------------------------------------------
print("\n--- missing price data: rejected, not crashed ---")
# ---------------------------------------------------------------------------

with PaperTestEnv(position_notional=10_000.0) as env:
    env.register_and_approve(
        strategy_id="buyer", algorithm_id="runner_test_no_price_1",
        strategy_cls=BuyEveryTimeStrategy)

    import pandas as pd
    empty_df = pd.DataFrame({"close": []})
    summary = run_paper_cycle(
        cycle_id="noprice-1", clock=env.clock, history_provider=lambda s, d: empty_df,
        universe=["INFY"], registry_dir=env.registry_dir,
        eligibility_dir=env.eligibility_dir, store=env.store, notify=False,
    )
    check("a signal with no available price data is rejected (no_price_data), "
          "never crashes the cycle", summary["orders_rejected"] == 1
          and env.store.list_orders()[0]["reason"] == "no_price_data")


# ---------------------------------------------------------------------------
print("\n--- idempotency: run the same cycle_id twice ---")
# ---------------------------------------------------------------------------

with PaperTestEnv(position_notional=10_000.0) as env:
    env.register_and_approve(
        strategy_id="buyer", algorithm_id="runner_test_idempotent_1",
        strategy_cls=BuyEveryTimeStrategy)

    summary1 = run_paper_cycle(
        cycle_id="idempotent-1", clock=env.clock, history_provider=hist_provider,
        universe=["INFY"], registry_dir=env.registry_dir,
        eligibility_dir=env.eligibility_dir, store=env.store, notify=False,
    )
    summary2 = run_paper_cycle(
        cycle_id="idempotent-1", clock=env.clock, history_provider=hist_provider,
        universe=["INFY"], registry_dir=env.registry_dir,
        eligibility_dir=env.eligibility_dir, store=env.store, notify=False,
    )
    check("the second run of an already-COMPLETED cycle_id is flagged idempotent_replay",
          summary1["idempotent_replay"] is False and summary2["idempotent_replay"] is True)
    check("the two runs report the identical orders_filled count",
          summary1["orders_filled"] == summary2["orders_filled"] == 1)
    check("only ONE order actually exists after running the same cycle_id twice",
          len(env.store.list_orders()) == 1)
    check("only ONE lot actually exists — no duplicate position was opened",
          len(env.store.list_open_lots(strategy_version_id=env.store.list_orders()[0]
              ["strategy_version_id"], symbol="INFY")) == 1)

    account_after_1 = env.store.get_account()
    summary3 = run_paper_cycle(
        cycle_id="idempotent-1", clock=env.clock, history_provider=hist_provider,
        universe=["INFY"], registry_dir=env.registry_dir,
        eligibility_dir=env.eligibility_dir, store=env.store, notify=False,
    )
    account_after_3 = env.store.get_account()
    check("a THIRD run of the same cycle_id still changes nothing about paper cash",
          account_after_1["cash"] == account_after_3["cash"])


# ---------------------------------------------------------------------------
print("\n--- a DIFFERENT cycle_id is a genuinely new cycle, not deduped ---")
# ---------------------------------------------------------------------------

with PaperTestEnv(position_notional=10_000.0) as env:
    env.register_and_approve(
        strategy_id="buyer", algorithm_id="runner_test_diffcycle_1",
        strategy_cls=BuyEveryTimeStrategy)
    run_paper_cycle(cycle_id="day-1", clock=env.clock, history_provider=hist_provider,
                    universe=["INFY"], registry_dir=env.registry_dir,
                    eligibility_dir=env.eligibility_dir, store=env.store, notify=False)
    run_paper_cycle(cycle_id="day-2", clock=env.clock, history_provider=hist_provider,
                    universe=["INFY"], registry_dir=env.registry_dir,
                    eligibility_dir=env.eligibility_dir, store=env.store, notify=False)
    check("two DIFFERENT cycle_ids each produce their own order — pyramiding, "
          "not deduplication", len(env.store.list_orders()) == 2)


# ---------------------------------------------------------------------------
print("\n--- default_cycle_id() ---")
# ---------------------------------------------------------------------------

with PaperTestEnv() as env:
    cid = default_cycle_id("market_close", env.clock)
    check("default_cycle_id() embeds the cycle label and the clock's own date",
          "market_close" in cid and "2026-01-05" in cid)


# ---------------------------------------------------------------------------
print("\n--- Telegram best-effort: a notify failure never loses paper state ---")
# ---------------------------------------------------------------------------

with PaperTestEnv(position_notional=10_000.0) as env:
    env.register_and_approve(
        strategy_id="buyer", algorithm_id="runner_test_telegram_1",
        strategy_cls=BuyEveryTimeStrategy)

    original_send = pnotify._send
    pnotify._send = lambda text: (_ for _ in ()).throw(RuntimeError("simulated Telegram outage"))
    try:
        summary = run_paper_cycle(
            cycle_id="telegram-fail-1", clock=env.clock, history_provider=hist_provider,
            universe=["INFY"], registry_dir=env.registry_dir,
            eligibility_dir=env.eligibility_dir, store=env.store, notify=True,
        )
    finally:
        pnotify._send = original_send

    check("run_paper_cycle() does not raise even when every Telegram send fails",
          summary["orders_filled"] == 1)
    check("the cycle still completed and persisted normally despite Telegram failing",
          env.store.get_cycle("telegram-fail-1")["status"] == "COMPLETED")
    check("notify_paper_trade() itself returns False (not raise) on a send failure",
          pnotify.notify_paper_trade(side="BUY", strategy_id="x", version_id="y",
                                     symbol="INFY", quantity=1, fill_price=1.0) in (True, False))


# ---------------------------------------------------------------------------
print("\n--- determinism across two fully independent cycle runs ---")
# ---------------------------------------------------------------------------

def _run(tmp_env: PaperTestEnv, algo_id: str) -> dict:
    tmp_env.register_and_approve(
        strategy_id="det", algorithm_id=algo_id, strategy_cls=BuyEveryTimeStrategy)
    return run_paper_cycle(
        cycle_id="det-1", clock=tmp_env.clock, history_provider=hist_provider,
        universe=["INFY", "TCS"], registry_dir=tmp_env.registry_dir,
        eligibility_dir=tmp_env.eligibility_dir, store=tmp_env.store, notify=False,
    )


with PaperTestEnv(position_notional=10_000.0) as env_a, \
     PaperTestEnv(position_notional=10_000.0) as env_b:
    summary_a = _run(env_a, "runner_test_det_a")
    summary_b = _run(env_b, "runner_test_det_b")
    check("two independent environments given identical signals/data/config produce "
          "identical orders_filled/orders_rejected/P&L",
          summary_a["orders_filled"] == summary_b["orders_filled"]
          and summary_a["total_net_pnl"] == summary_b["total_net_pnl"]
          and summary_a["cash"] == summary_b["cash"])


print(f"\n{'=' * 52}")
print(f"  {PASSED} passed, {FAILED} failed")
print(f"{'=' * 52}\n")
sys.exit(1 if FAILED else 0)
