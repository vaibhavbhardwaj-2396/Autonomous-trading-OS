"""
tests/test_paper_eligibility.py — the explicit paper-eligibility boundary
(AA spec section 4): a StrategyVersion is never paper-tradeable just
because it is registered; approve/revoke are auditable events, not a
mutable flag; unknown/mismatched versions are refused (fail closed).

Run with:  python -m tests.test_paper_eligibility
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.paper_fixtures import PaperTestEnv, BuyEveryTimeStrategy, FIXED_NOW  # noqa: E402
from paper import eligibility as pelig  # noqa: E402
from strategies.core import create_strategy_version  # noqa: E402
from strategies import registry as sreg  # noqa: E402

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


NOW = FIXED_NOW.isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
print("\n--- registering a StrategyVersion does NOT make it paper-eligible ---")
# ---------------------------------------------------------------------------

with PaperTestEnv() as env:
    version_id = env.register_only(
        strategy_id="momentum", algorithm_id="elig_test_momentum_v1",
        strategy_cls=BuyEveryTimeStrategy)

    check("a freshly-registered (never approved) StrategyVersion is NOT paper-eligible",
          not pelig.is_paper_eligible(version_id, directory=env.eligibility_dir))
    check("list_paper_eligible() is empty when nothing has been approved",
          pelig.list_paper_eligible(directory=env.eligibility_dir) == [])


# ---------------------------------------------------------------------------
print("\n--- approve / revoke ---")
# ---------------------------------------------------------------------------

with PaperTestEnv() as env:
    version_id = env.register_only(
        strategy_id="momentum", algorithm_id="elig_test_momentum_v2",
        strategy_cls=BuyEveryTimeStrategy)

    event = pelig.mark_paper_eligible(
        version_id=version_id, strategy_id="momentum", actor="Vaibhav",
        reason="promising backtest", now=NOW, directory=env.eligibility_dir,
        registry_dir=env.registry_dir)
    check("mark_paper_eligible() records an APPROVE event with actor/reason",
          event["event"] == "APPROVE" and event["actor"] == "Vaibhav"
          and event["reason"] == "promising backtest")

    check("the version is now paper-eligible",
          pelig.is_paper_eligible(version_id, directory=env.eligibility_dir))
    eligible = pelig.list_paper_eligible(directory=env.eligibility_dir)
    check("list_paper_eligible() includes it, with its approval metadata",
          len(eligible) == 1 and eligible[0]["version_id"] == version_id
          and eligible[0]["actor"] == "Vaibhav")

    pelig.revoke_paper_eligibility(
        version_id=version_id, strategy_id="momentum", actor="Vaibhav",
        reason="backtest degraded", now=NOW, directory=env.eligibility_dir)
    check("after revoke, the version is no longer paper-eligible",
          not pelig.is_paper_eligible(version_id, directory=env.eligibility_dir))
    check("list_paper_eligible() no longer includes it",
          pelig.list_paper_eligible(directory=env.eligibility_dir) == [])

    events = pelig.list_events(directory=env.eligibility_dir)
    check("the full audit trail retains BOTH the APPROVE and the REVOKE event "
          "(nothing is deleted)", len(events) == 2
          and events[0]["event"] == "APPROVE" and events[1]["event"] == "REVOKE")

    re_approved = pelig.mark_paper_eligible(
        version_id=version_id, strategy_id="momentum", actor="Vaibhav",
        reason="re-approved after a strategy fix", now=NOW,
        directory=env.eligibility_dir, registry_dir=env.registry_dir)
    check("a version can be re-approved after revocation",
          pelig.is_paper_eligible(version_id, directory=env.eligibility_dir))
    check("the audit trail now has three events, in order",
          len(pelig.list_events(directory=env.eligibility_dir)) == 3)


# ---------------------------------------------------------------------------
print("\n--- fail-closed behaviour ---")
# ---------------------------------------------------------------------------

with PaperTestEnv() as env:
    try:
        pelig.mark_paper_eligible(
            version_id="not-a-real-version-id", strategy_id="momentum", actor="Vaibhav",
            reason="x", now=NOW, directory=env.eligibility_dir, registry_dir=env.registry_dir)
        check("approving an unknown version_id raises EligibilityError", False)
    except pelig.EligibilityError:
        check("approving an unknown version_id raises EligibilityError", True)

    version_id = env.register_only(
        strategy_id="momentum", algorithm_id="elig_test_momentum_v3",
        strategy_cls=BuyEveryTimeStrategy)
    try:
        pelig.mark_paper_eligible(
            version_id=version_id, strategy_id="WRONG-STRATEGY-ID", actor="Vaibhav",
            reason="x", now=NOW, directory=env.eligibility_dir, registry_dir=env.registry_dir)
        check("approving with a mismatched strategy_id raises EligibilityError", False)
    except pelig.EligibilityError:
        check("approving with a mismatched strategy_id raises EligibilityError", True)
    check("the mismatched-strategy_id attempt left no APPROVE event on file",
          not pelig.is_paper_eligible(version_id, directory=env.eligibility_dir))

    try:
        pelig.mark_paper_eligible(
            version_id=version_id, strategy_id="momentum", actor="   ",
            reason="x", now=NOW, directory=env.eligibility_dir, registry_dir=env.registry_dir)
        check("approving with a blank actor raises EligibilityError "
              "(a decision must be attributable)", False)
    except pelig.EligibilityError:
        check("approving with a blank actor raises EligibilityError "
              "(a decision must be attributable)", True)

    try:
        pelig.revoke_paper_eligibility(
            version_id="never-approved-id", strategy_id="momentum", actor="Vaibhav",
            reason="x", now=NOW, directory=env.eligibility_dir)
        check("revoking a version with no prior APPROVE event raises EligibilityError", False)
    except pelig.EligibilityError:
        check("revoking a version with no prior APPROVE event raises EligibilityError", True)


# ---------------------------------------------------------------------------
print("\n--- malformed log lines never hide the rest of the log ---")
# ---------------------------------------------------------------------------

with PaperTestEnv() as env:
    version_id = env.register_only(
        strategy_id="momentum", algorithm_id="elig_test_momentum_v4",
        strategy_cls=BuyEveryTimeStrategy)
    pelig.mark_paper_eligible(
        version_id=version_id, strategy_id="momentum", actor="Vaibhav", reason="x",
        now=NOW, directory=env.eligibility_dir, registry_dir=env.registry_dir)

    log_path = env.eligibility_dir / pelig.LOG_FILENAME
    with open(log_path, "a") as f:
        f.write("not valid json at all\n")
        f.write("\n")  # blank line

    check("a malformed line in the eligibility log is skipped, not fatal, and the "
          "valid APPROVE before it still resolves correctly",
          pelig.is_paper_eligible(version_id, directory=env.eligibility_dir))


print(f"\n{'=' * 52}")
print(f"  {PASSED} passed, {FAILED} failed")
print(f"{'=' * 52}\n")
sys.exit(1 if FAILED else 0)
