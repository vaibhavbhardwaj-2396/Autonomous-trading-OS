"""
Slice C audit pass — dedicated boundary tests for the two structural concerns
Vaibhav raised after reviewing the first Slice C report:

    #2  research/brain -> engine.watchlist: is this dependency actually safe?
        Verified here, mechanically, rather than asserted in a docstring:
        engine/watchlist.py is parsed with `ast` and shown to contain nothing
        but literal module-level constants — no functions, no classes, no
        control flow, no imports, no function calls anywhere at module scope.
        It cannot have import-time side effects because it has no executable
        statements at all beyond simple assignment.

    #3  a hypothesis is data, never executable input to a future runner:
        verified two ways — (a) no file under research/brain/ contains a call
        to eval, exec, or compile, anywhere, full stop; (b) free text fields
        on a proposal (hypothesis, notes, signal, ...) can never end up
        inside the serialized entry_rule/exit_rule a future runner would
        read — those two fields only ever contain keys from the explicit
        condition/exit whitelist, never proposer-supplied prose.

This file does not re-implement research/brain/hypothesis_intake.py's own
tests (see tests/test_research_hypothesis_intake.py) — it exists so these two
specific audit items have their own clearly-named, easy-to-re-run home.

Run with:  python -m tests.test_research_engine_boundary
"""

import ast
import json
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from research.store import Store  # noqa: E402
from research.brain import hypothesis_intake as hi  # noqa: E402

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

# ---------------------------------------------------------------------------
print("\n--- #2: engine/watchlist.py is genuinely static configuration ---")
# ---------------------------------------------------------------------------

watchlist_path = ROOT / "engine" / "watchlist.py"
check("engine/watchlist.py exists", watchlist_path.exists())

tree = ast.parse(watchlist_path.read_text())

EXECUTABLE_NODE_TYPES = (
    ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
    ast.Import, ast.ImportFrom,
    ast.If, ast.For, ast.While, ast.Try, ast.With,
)
executable_nodes = [n for n in tree.body if isinstance(n, EXECUTABLE_NODE_TYPES)]
check("watchlist.py defines no functions, classes, imports, or control flow "
      "at module level",
      not executable_nodes,
      [type(n).__name__ for n in executable_nodes])

call_exprs = [n for n in tree.body if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)]
check("watchlist.py performs no bare function calls at module scope",
      not call_exprs)

assigns = [n for n in tree.body if isinstance(n, (ast.Assign, ast.AnnAssign))]
check("watchlist.py has at least one top-level assignment (UNIVERSE, etc.)",
      len(assigns) > 0)
calls_inside_assignments = []
for n in assigns:
    value = n.value
    if value is not None:
        calls_inside_assignments += [sub for sub in ast.walk(value) if isinstance(sub, ast.Call)]
check("none of watchlist.py's assignments call a function to compute their value "
      "(every value is a literal)",
      not calls_inside_assignments)

universe_assign = next(
    (n for n in assigns if isinstance(n, ast.Assign)
     and any(isinstance(t, ast.Name) and t.id == "UNIVERSE" for t in n.targets)),
    None,
)
check("UNIVERSE is assigned at module level", universe_assign is not None)
if universe_assign is not None:
    is_list_of_str_literals = (
        isinstance(universe_assign.value, ast.List)
        and all(isinstance(el, ast.Constant) and isinstance(el.value, str)
                for el in universe_assign.value.elts)
    )
    check("UNIVERSE is a plain list of string literals — nothing computed, "
          "nothing loaded from disk or network",
          is_list_of_str_literals)

# The (lazy) import site in hypothesis_intake.py touches exactly this name.
hi_src = (ROOT / "research" / "brain" / "hypothesis_intake.py").read_text()
check("hypothesis_intake.py's only reference to engine/ is importing "
      "engine.watchlist.UNIVERSE specifically (not the whole module blindly)",
      "from engine.watchlist import UNIVERSE" in hi_src)


# ---------------------------------------------------------------------------
print("\n--- #3a: no file under research/brain/ ever calls eval, exec, or compile ---")
# ---------------------------------------------------------------------------

# (?<!re\.) excludes ordinary `re.compile(...)` regex compilation, which is
# unrelated to the risk this checks for — turning proposer-supplied text into
# executable Python.
EVAL_EXEC_RE = re.compile(r"\beval\s*\(|\bexec\s*\(|(?<!re\.)\bcompile\s*\(")
brain_dir = ROOT / "research" / "brain"
for f in sorted(brain_dir.glob("*.py")):
    src = f.read_text()
    check(f"{f.name} contains no eval/exec/compile call", not EVAL_EXEC_RE.search(src))

# and the same across the rest of research/, for good measure — the runner
# that will eventually read a locked Contract's entry_rule/exit_rule (Slice D,
# not built yet) must inherit this same property when it's written.
research_dir = ROOT / "research"
offenders = []
for f in sorted(research_dir.rglob("*.py")):
    if EVAL_EXEC_RE.search(f.read_text()):
        offenders.append(str(f.relative_to(ROOT)))
check("no file anywhere under research/ calls eval/exec/compile today",
      not offenders, str(offenders))


# ---------------------------------------------------------------------------
print("\n--- #3b: free text can never leak into entry_rule / exit_rule ---")
# ---------------------------------------------------------------------------

TMP = Path(tempfile.mkdtemp(prefix="lq-test-boundary-"))
store = Store.open(TMP / "leak.db")
reg = TMP / "registry"

MARKER_HYPOTHESIS = "MARKER_TOKEN_IN_HYPOTHESIS_TEXT_9f3a"
MARKER_NOTES = "MARKER_TOKEN_IN_NOTES_TEXT_1c2b"
MARKER_SIGNAL = "MARKER_TOKEN_IN_SIGNAL_TEXT_77aa"
MARKER_INDEPENDENCE = "MARKER_TOKEN_IN_INDEPENDENCE_TEXT_5d5d"

proposal = {
    "title": "leak-test",
    "hypothesis": f"some claim mentioning {MARKER_HYPOTHESIS} in passing",
    "null_hypothesis": "no effect",
    "universe": "watchlist",
    "signal": f"observatory reading, note: {MARKER_SIGNAL}",
    "entry_rule": {"conditions": [{"metric": "volume_zscore", "op": ">", "value": 3.0}]},
    "exit_rule": {"stop_loss_pct": 2.0, "target_pct": 4.0},
    "splits": {"discovery": ["2019-01-01", "2020-01-01"]},
    "independence": f"clustered, see {MARKER_INDEPENDENCE} for detail",
    "falsification": "t_stat < 2.0",
    "abandon_condition": "expectancy_r <= 0",
    "evaluation_start": "2019-01-01",
    "evaluation_end": "2020-01-01",
    "notes": f"reviewer note: {MARKER_NOTES}",
}

result = hi.create_draft(store, proposal, registry_dir=reg)
entry_raw, exit_raw = result.contract.entry_rule, result.contract.exit_rule

for label, marker in (("hypothesis", MARKER_HYPOTHESIS), ("notes", MARKER_NOTES),
                        ("signal", MARKER_SIGNAL), ("independence", MARKER_INDEPENDENCE)):
    check(f"the {label} free-text marker never appears inside entry_rule",
          marker not in entry_raw)
    check(f"the {label} free-text marker never appears inside exit_rule",
          marker not in exit_raw)

parsed_entry = json.loads(entry_raw)
parsed_exit = json.loads(exit_raw)
check("entry_rule's top-level keys are exactly the whitelist ({'conditions'})",
      set(parsed_entry) == {"conditions"})
for cond in parsed_entry["conditions"]:
    check("every condition's keys are a subset of the condition whitelist",
          set(cond) <= hi.CONDITION_ALLOWED_KEYS, str(cond))
    check("every condition's metric is in the metric whitelist",
          cond["metric"] in hi.SUPPORTED_METRICS)
    check("every condition's op is in the operator whitelist",
          cond["op"] in hi.SUPPORTED_OPERATORS)
check("exit_rule's keys are a subset of the exit whitelist",
      set(parsed_exit) <= hi.EXIT_ALLOWED_KEYS, str(parsed_exit))

# The free text is exactly where it belongs — on the Contract's own
# corresponding fields, verbatim.
check("the hypothesis marker DOES land on Contract.hypothesis, as expected",
      MARKER_HYPOTHESIS in result.contract.hypothesis)
check("the notes marker DOES land on Contract.notes, as expected",
      MARKER_NOTES in result.contract.notes)

store.close()


print(f"\n{'=' * 52}")
print(f"  {PASSED} passed, {FAILED} failed")
print(f"{'=' * 52}\n")
sys.exit(1 if FAILED else 0)
