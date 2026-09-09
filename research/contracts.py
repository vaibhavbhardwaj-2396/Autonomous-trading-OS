"""
Experiment contracts and the two firewalls.

A contract is a hypothesis written down BEFORE it is tested, hashed, and then
refused execution if it changes. Editing a hypothesis after seeing results is
the single most common way a research programme fools itself, and a hash is the
cheapest possible defence against it.

Two firewalls, and they are genuinely different failures:

    DATA FIREWALL   The experiment cannot see facts published after `as_of`.
                    Enforced structurally by research/store.py's AsOfView, and
                    falsifiably by the deletion test. This one is solvable.

    MODEL FIREWALL  A historically evaluated experiment cannot use judgments
                    from an LLM whose training data covers the evaluation
                    period. This one is NOT solvable by any amount of database
                    hygiene: the model is not reading the database, it is
                    recalling. The only defence is refusing to run the
                    combination at all.

The model firewall is why Experiment A is forward-only permanently rather than
"forward-only for now". A contract that declares LLM features and an evaluation
window starting before the model's cutoff is refused by `runner`, not warned
about — a warning is something you learn to click past at 2am.

The registry is also the multiple-comparisons ledger. The number of locked
contracts is the denominator you divide by, which is what turns the
false-discovery problem from an anxiety into an arithmetic operation.
"""

from __future__ import annotations

import json
import hashlib
import datetime as dt
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

REGISTRY_DIR = Path(__file__).parent / "registry"

# Fields that define the hypothesis. Anything not in here (notes, results, the
# lock timestamp) can change without invalidating the contract; anything in here
# cannot change at all.
HASHED_FIELDS = (
    "id", "title", "hypothesis", "null_hypothesis",
    "universe", "signal", "entry_rule", "exit_rule",
    "splits", "independence", "falsification", "abandon_condition",
    "llm_features", "llm_model_id", "llm_knowledge_cutoff",
    "evaluation_start", "evaluation_end", "cost_model",
)

VALID_STATUS = ("draft", "locked", "running", "reported", "abandoned", "superseded")


class ContractViolation(RuntimeError):
    """Raised when a contract is asked to do something its lock forbids."""


@dataclass
class Contract:
    id: str
    title: str
    hypothesis: str
    null_hypothesis: str

    universe: str
    signal: str
    entry_rule: str
    exit_rule: str

    splits: dict                       # {"discovery": [...], "validation": [...], ...}
    independence: str                  # how correlated observations are handled
    falsification: str                 # what result would kill it
    abandon_condition: str             # the pre-committed stop

    evaluation_start: str              # ISO date — earliest data the study touches
    evaluation_end: str

    # --- model firewall declaration -------------------------------------------
    llm_features: bool = False
    llm_model_id: Optional[str] = None
    llm_knowledge_cutoff: Optional[str] = None   # ISO date

    cost_model: str = "engine.costs.equity_round_trip"

    # --- bookkeeping (not hashed) ---------------------------------------------
    status: str = "draft"
    locked_at: Optional[str] = None
    locked_hash: Optional[str] = None
    notes: str = ""
    research_debt: list[str] = field(default_factory=list)

    # -- hashing ---------------------------------------------------------------

    def canonical(self) -> str:
        d = {k: getattr(self, k) for k in HASHED_FIELDS}
        return json.dumps(d, sort_keys=True, separators=(",", ":"), default=str)

    def content_hash(self) -> str:
        return hashlib.sha256(self.canonical().encode()).hexdigest()[:16]

    # -- the two firewalls -----------------------------------------------------

    def data_firewall(self) -> str:
        """Structural, and identical for every contract: reads go through
        AsOfView. Declared here so the registry records that it applied."""
        return "as_of_view"

    def model_firewall_violations(self) -> list[str]:
        """The check that makes 'forward-only' mechanical instead of aspirational."""
        if not self.llm_features:
            return []

        problems: list[str] = []
        if not self.llm_model_id:
            problems.append(
                "llm_features is true but no llm_model_id is declared — an "
                "unnamed model has an unknowable training cutoff.")
        if not self.llm_knowledge_cutoff:
            problems.append(
                "llm_features is true but no llm_knowledge_cutoff is declared. "
                "Without it the firewall cannot be evaluated, so the contract "
                "cannot be run.")
            return problems

        cutoff = _date(self.llm_knowledge_cutoff)
        start = _date(self.evaluation_start)
        if start < cutoff:
            problems.append(
                f"MODEL FIREWALL BREACH: evaluation starts {start} but "
                f"{self.llm_model_id} may know everything through {cutoff}. "
                f"An LLM judgment about this period cannot be shown to be "
                f"independent of the outcome — and no database gate can fix "
                f"that, because the leak is in the weights, not the query. "
                f"Move evaluation_start to {cutoff} or later, or remove the "
                f"LLM feature.")
        return problems

    def check(self) -> list[str]:
        problems = list(self.model_firewall_violations())
        if _date(self.evaluation_end) <= _date(self.evaluation_start):
            problems.append("evaluation_end must be after evaluation_start")
        if self.status not in VALID_STATUS:
            problems.append(f"unknown status {self.status!r}")
        if not self.abandon_condition.strip():
            problems.append(
                "no abandon_condition — a study you cannot lose is not a study")
        return problems

    # -- lifecycle -------------------------------------------------------------

    def lock(self) -> "Contract":
        """Freeze the hypothesis. After this, any change to a hashed field makes
        the contract unrunnable — deliberately. The remedy is a new contract with
        a new id, and the registry keeps both, because an abandoned hypothesis is
        part of the multiple-comparisons count whether or not it is convenient."""
        problems = self.check()
        if problems:
            raise ContractViolation(
                f"{self.id} cannot be locked:\n  - " + "\n  - ".join(problems))
        self.locked_hash = self.content_hash()
        self.locked_at = dt.datetime.now().isoformat(timespec="seconds")
        self.status = "locked"
        return self

    def verify(self) -> None:
        """Called by the runner before every execution."""
        if self.status == "draft":
            raise ContractViolation(
                f"{self.id} is still a draft. Lock it before running it — that "
                f"ordering is the whole mechanism.")
        if not self.locked_hash:
            raise ContractViolation(f"{self.id} has no locked hash")
        current = self.content_hash()
        if current != self.locked_hash:
            raise ContractViolation(
                f"{self.id} has been edited since it was locked "
                f"({self.locked_hash} -> {current}). This is refused by design. "
                f"If the hypothesis genuinely needs to change, register it as a "
                f"NEW contract; do not amend this one.")
        problems = self.model_firewall_violations()
        if problems:
            raise ContractViolation(
                f"{self.id} violates the model firewall:\n  - " + "\n  - ".join(problems))

    # -- persistence -----------------------------------------------------------

    def to_dict(self) -> dict:
        d = asdict(self)
        d["_content_hash"] = self.content_hash()
        d["_data_firewall"] = self.data_firewall()
        return d

    def save(self, directory: Path = REGISTRY_DIR) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.id}.json"
        path.write_text(json.dumps(self.to_dict(), indent=2, default=str))
        return path

    @classmethod
    def load(cls, contract_id: str, directory: Path = REGISTRY_DIR) -> "Contract":
        raw = json.loads((directory / f"{contract_id}.json").read_text())
        for k in ("_content_hash", "_data_firewall"):
            raw.pop(k, None)
        return cls(**raw)


def _date(value: Any) -> dt.date:
    if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
        return value
    if isinstance(value, dt.datetime):
        return value.date()
    return dt.date.fromisoformat(str(value)[:10])


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def registry(directory: Path = REGISTRY_DIR) -> list[Contract]:
    if not directory.exists():
        return []
    out = []
    for p in sorted(directory.glob("*.json")):
        try:
            out.append(Contract.load(p.stem, directory))
        except Exception:
            continue
    return out


def comparison_count(directory: Path = REGISTRY_DIR) -> int:
    """How many hypotheses have been locked. This is the denominator.

    It counts abandoned contracts too, on purpose. The whole reason
    pre-registration works is that failed hypotheses stay in the count; a
    registry that quietly forgets its failures is a worse instrument than no
    registry, because it produces confident numbers that are wrong.
    """
    return sum(1 for c in registry(directory) if c.status != "draft")


def summary(directory: Path = REGISTRY_DIR) -> str:
    cs = registry(directory)
    if not cs:
        return "Registry empty — no hypotheses locked yet."
    lines = [f"{len(cs)} contract(s) | locked (multiple-comparisons denominator): "
             f"{comparison_count(directory)}", ""]
    for c in cs:
        fw = "LLM/forward-only" if c.llm_features else "deterministic"
        lines.append(f"  {c.id:<10} {c.status:<10} {fw:<18} {c.title}")
        lines.append(f"             hash={c.locked_hash or '—'} "
                     f"window={c.evaluation_start}..{c.evaluation_end}")
    return "\n".join(lines)
