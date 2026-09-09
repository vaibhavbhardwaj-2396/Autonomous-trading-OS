"""
Tests for Phase 2 Slice J: the .claude/settings.json permission-CONFIGURATION
boundary for the Research AI.

IMPORTANT DISTINCTION, stated up front because it is easy to blur:

    tests/test_research_investigator.py (Slice I) proves PYTHON-LEVEL CODE
    isolation — research/brain/investigator.py's own import graph and code
    never reaches engine/, any broker module, or live trading state,
    regardless of what any Claude Code process around it is or isn't
    permitted to touch on disk.

    THIS FILE proves something narrower and different: that
    .claude/settings.json — the ONE permission profile any Claude Code
    process invoked from this project root loads, whether it's the live
    agent (run_cycle.sh) or the Research AI (research/brain/investigator.py's
    _default_runner, which runs with cwd=PROJECT_ROOT for exactly this
    reason) — declares the deny rules docs/RESEARCH_DEPLOY.md §5 calls for.

This is a STATIC assertion about a JSON config file. It is not a runtime test
of the Claude Code CLI's sandbox enforcement, and no test in this file
invokes the real `claude` binary. A passing check here means "the rule that
should cause the CLI to refuse this edit is present and correctly formed" —
it does NOT mean "the CLI was observed to refuse the edit." Whether the CLI
actually enforces `Edit(...)` deny rules at runtime is unverified by this
suite and would require a real CLI invocation, which is out of scope here
exactly as it was for research/brain/investigator.py's own "layer 2" note.

Run with:  python -m tests.test_research_ai_permissions
"""

import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

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


ROOT = Path(__file__).parent.parent
SETTINGS_PATH = ROOT / ".claude" / "settings.json"

raw_text = SETTINGS_PATH.read_text()
settings = json.loads(raw_text)
permissions = settings.get("permissions", {})
deny = permissions.get("deny", [])
allow = permissions.get("allow", [])


# ---------------------------------------------------------------------------
print("\n--- newly required: docs/RESEARCH_DEPLOY.md §5 research-control deny rules ---")
# ---------------------------------------------------------------------------

REQUIRED_RESEARCH_DENY = (
    "Edit(research/registry/*)",
    "Edit(research/market_memory.db)",
    "Edit(research/schema.sql)",
    "Edit(research/store.py)",
    "Edit(research/contracts.py)",
)
for rule in REQUIRED_RESEARCH_DENY:
    check(f"deny list includes {rule!r} (docs/RESEARCH_DEPLOY.md §5)", rule in deny)


# ---------------------------------------------------------------------------
print("\n--- regression: every pre-existing deny rule remains intact, unweakened ---")
# ---------------------------------------------------------------------------

PRE_EXISTING_DENY = (
    "Edit(memory/guardrails.md)",
    "Edit(engine/guardrails.py)",
    "Edit(engine/execute.py)",
    "Edit(memory/state.json)",
    "Edit(memory/trades.jsonl)",
    "Edit(memory/regime_log.jsonl)",
    "Edit(.env)",
    "Read(.env)",
    "Edit(.claude/settings.json)",
    "Edit(run_cycle.sh)",
    "Edit(tests/test_guardrails.py)",
    "Bash(rm:*)",
    "Bash(git push:*)",
    "Bash(pip:*)",
    "Bash(curl:*)",
)
for rule in PRE_EXISTING_DENY:
    check(f"pre-existing deny rule preserved exactly: {rule!r}", rule in deny)

check("this slice only ever ADDED to the deny list — pre-existing rule count "
      "plus the newly required rules all still fit within the current list",
      len(deny) >= len(PRE_EXISTING_DENY) + len(REQUIRED_RESEARCH_DENY),
      f"deny has {len(deny)} entries")


# ---------------------------------------------------------------------------
print("\n--- no rule silently shadows or reopens a protected artifact ---")
# ---------------------------------------------------------------------------

# A broader-looking rule (e.g. "Edit(research/*)") could make a naive
# substring search over `deny` look satisfied without the SPECIFIC documented
# rule actually being present — assert the exact documented strings exist,
# not merely something that overlaps them.
check("every required research-control rule is present in its EXACT "
      "documented form, not merely implied by a differently-shaped rule",
      all(rule in deny for rule in REQUIRED_RESEARCH_DENY))

PROTECTED_PATH_FRAGMENTS = (
    "research/registry", "research/market_memory.db", "research/schema.sql",
    "research/store.py", "research/contracts.py",
    "engine/guardrails.py", "engine/execute.py", "memory/state.json",
    "memory/trades.jsonl", "memory/regime_log.jsonl", ".env",
)
conflicting_allow = [a for a in allow
                     if any(frag in a for frag in PROTECTED_PATH_FRAGMENTS)]
check("the allow list contains no rule naming any protected path "
      "(new or pre-existing) that would shadow its deny rule",
      conflicting_allow == [], str(conflicting_allow))


# ---------------------------------------------------------------------------
print("\n--- settings.json remains well-formed ---")
# ---------------------------------------------------------------------------

check("settings.json parses as valid JSON", isinstance(settings, dict))
check("permissions.deny is a non-empty list", isinstance(deny, list) and len(deny) > 0)
check("no duplicate entries were introduced in the deny list",
      len(deny) == len(set(deny)), str(deny))
check("every deny rule is a non-empty string of the form Tool(pattern)",
      all(isinstance(r, str) and "(" in r and r.endswith(")") for r in deny), str(deny))


print(f"\n{'=' * 52}")
print(f"  {PASSED} passed, {FAILED} failed")
print(f"{'=' * 52}\n")
sys.exit(1 if FAILED else 0)
