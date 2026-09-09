"""
Global Research Duplicate Detection — Phase 2 Slice L.

The gap this closes: research/experiments/comparison.py already detects
duplicate rule sets, but only ever within ONE hypothesis_id — its
`evaluate_hypothesis_evidence()` groups a hypothesis's own scored variants by
`_rule_fingerprint()` and marks a later duplicate REDUNDANT relative to the
earlier one, but it never looks across hypotheses at all. Two independently
created hypotheses (a human, an AI research role, a mathematical-discovery
engine — anything that can call hypothesis_intake.create_draft()) can lock
and run the exact same universe/entry_rule/exit_rule/splits/evaluation
window under two different hypothesis_ids, and nothing today notices.

This module is the fix, and ONLY the fix described above — EXACT duplicates,
scanned globally. It deliberately does none of the following, all of which
are real, useful, and explicitly out of scope for this slice: near-duplicate
/ fuzzy matching (volume_zscore >= 3 vs >= 3.5), semantic similarity (two
different mechanisms for the same economic idea, e.g. post-earnings drift),
hypothesis families, clustering, or anything AI-driven. "Same canonical
experiment specification, byte for byte" is the entire question this module
answers.

Design principle: REUSE, don't reimplement. `_rule_fingerprint()` in
research/experiments/comparison.py already defines, precisely and
correctly, what "the same experiment specification" means — universe,
entry_rule, exit_rule, splits, evaluation_start, evaluation_end, canonically
hashed. This module imports that function directly (yes, despite its
leading underscore — comparison.py is on this slice's do-not-modify list,
so adding a public alias there is not an option, and importing the private
name is the only way to guarantee this module can never define "duplicate"
differently than comparison.py does). Nothing here reimplements or narrows
that definition.

Inclusion policy — stated explicitly, because it's a real decision, not an
default: this module scans `research.contracts.registry()`, i.e. EVERY
contract file present, in ANY status (draft, locked, running, reported,
abandoned, superseded). This is deliberately NOT the same inclusion policy
research/experiments/comparison.py itself uses for its within-hypothesis
check, which is narrower and answers a different question — "has this
contract been scored" (comparison.py only ever compares contracts with a
recorded verdict, since it's interpreting RESULTS). This module answers
"does this rule specification already exist anywhere", which is exactly as
true of a still-draft proposal as of a finished one — arguably more useful
for a draft, since catching the duplicate before anyone spends a run on it
is the entire point of exposing this through the digest (see
research/brain/digest.py). This also isn't a NEW inclusion policy invented
for this slice: it's the same one digest.py's own `contract_registry.
contracts` section already uses (every contract, unfiltered) — this module
just applies the same, already-established convention.

Safety, all structurally true by absence rather than by a check that could
be forgotten (the same pattern research/brain/observatory.py and digest.py
already use): this module imports no `engine` module, no broker module,
never calls Contract.lock() or Contract.save(), never calls store.append()
or any other Store write method, and never runs an experiment. It reads the
registry (research.contracts.registry) and research memory's hypothesis-
claim log (via research.experiments.evaluator.resolve_hypothesis_id, itself
read-only) and returns plain data. Nothing here mutates anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..contracts import REGISTRY_DIR, registry as _registry
from ..experiments.comparison import _rule_fingerprint
from ..experiments import evaluator
from ..store import Store


@dataclass(frozen=True)
class DuplicateMember:
    """One contract that shares a fingerprint with at least one other."""

    contract_id: str
    hypothesis_id: Optional[str]
    status: str

    def to_dict(self) -> dict:
        return {"contract_id": self.contract_id,
                "hypothesis_id": self.hypothesis_id, "status": self.status}


@dataclass(frozen=True)
class DuplicateGroup:
    """Two or more contracts — from any hypothesis, in any status — that
    hash to the exact same `_rule_fingerprint()`."""

    fingerprint: str
    members: tuple[DuplicateMember, ...]

    def to_dict(self) -> dict:
        return {"fingerprint": self.fingerprint,
                "members": [m.to_dict() for m in self.members]}


def duplicate_groups(
    store: Store, *, registry_dir: Path = REGISTRY_DIR,
) -> list[DuplicateGroup]:
    """Every fingerprint shared by two or more contracts anywhere in the
    registry, regardless of hypothesis_id or status. Read-only: loads
    contracts via research.contracts.registry() (a plain read of the
    registry directory) and resolves each one's hypothesis_id via
    research.experiments.evaluator.resolve_hypothesis_id() (a plain read of
    research memory) — nothing is written, executed, locked, or modified.

    Deterministic: contracts within a group are sorted by contract_id, and
    groups themselves are sorted by fingerprint, so two calls against an
    unchanged registry/store always return identical output — no reliance
    on filesystem glob order, sqlite row order, or dict insertion order.
    """
    contracts = _registry(registry_dir)

    by_fingerprint: dict[str, list] = {}
    for c in contracts:
        by_fingerprint.setdefault(_rule_fingerprint(c), []).append(c)

    groups: list[DuplicateGroup] = []
    for fingerprint, members in by_fingerprint.items():
        if len(members) < 2:
            continue
        ordered = sorted(members, key=lambda c: c.id)
        member_objs = tuple(
            DuplicateMember(
                contract_id=c.id,
                hypothesis_id=evaluator.resolve_hypothesis_id(store, c.id),
                status=c.status,
            )
            for c in ordered
        )
        groups.append(DuplicateGroup(fingerprint=fingerprint, members=member_objs))

    groups.sort(key=lambda g: g.fingerprint)
    return groups


def duplicate_groups_as_dicts(
    store: Store, *, registry_dir: Path = REGISTRY_DIR,
) -> list[dict]:
    """JSON-ready form of duplicate_groups() — plain dicts, same
    deterministic ordering, for a caller (e.g. digest.py) that wants to
    serialize this directly rather than work with the dataclasses."""
    return [g.to_dict() for g in duplicate_groups(store, registry_dir=registry_dir)]


def is_duplicate(
    store: Store, contract_id: str, *, registry_dir: Path = REGISTRY_DIR,
) -> bool:
    """Convenience: is `contract_id` a member of any exact-duplicate group?
    A thin read over duplicate_groups() — no separate query path."""
    return any(
        m.contract_id == contract_id
        for g in duplicate_groups(store, registry_dir=registry_dir)
        for m in g.members
    )
