"""
Hypothesis Family / Research Area Map — Phase 2 Slice M.

The gap this closes: Slice L (research/brain/similarity.py) answers "have we
already tested this EXACT experiment specification, anywhere in the
registry?" It says nothing about the broader question a researcher (human or
AI) actually needs when deciding what to work on next: "have we already
poked at this general area — momentum, volatility, event-driven — at all,
and how much?" That's a navigation/prioritization question, not a duplicate-
detection one, and this module is deliberately nothing more than that: a
label on a hypothesis_id, and a deterministic way to group hypotheses by
that label.

Explicitly NOT built here (all real, all out of scope for this slice):
semantic similarity, embeddings, a vector database, LLM-driven
classification, automatic taxonomy inference from hypothesis prose,
clustering, a research scheduler, priority scoring, or a research budget.
The area label is always supplied explicitly by whatever calls
tag_hypothesis() (a human, or a Research AI told to pick from areas already
visible in the digest) — this module never looks at a hypothesis's `claim`
text and never invents a label.

Data model — reuses the existing append-only research-memory pattern
(research/memory.py), exactly like every other dataset there. NO new SQLite
schema or table: this is another `dataset` value in the same `observations`
table research/store.py already owns, written via
`research.memory.record_research_area_tag()` and read back via
`research.memory.query_research_log()`. A tag row's payload is just
    {"hypothesis_id": "HYP-...", "research_area": "momentum"}
No `family` field: the slice instructions make it optional and only worth
adding "if the existing data model and implementation make that clearly
useful" — nothing here needs a second grouping dimension yet, so it was left
out rather than added unused. If a real need for it shows up later, it's a
new optional payload key on the same dataset, not a new dataset.

Why a research area is metadata about a hypothesis_id, never about a
Contract: `research.contracts.Contract.HASHED_FIELDS` and the whole
lock/hash/verify machinery are completely untouched by this module — it
imports nothing from research.contracts, research.experiments, or
research.store's write paths for a Contract, and never opens a registry
file. A hypothesis can be retagged into a different research area at any
time without the Contract(s) that test it changing in any way, because
nothing here ever looks at, builds, or writes a Contract in the first
place. That's a structural guarantee (there is nothing to touch), not a
runtime check that could be forgotten.

Retagging: append-only means a tag is never edited or deleted in place —
tagging a hypothesis into a new area appends a new row rather than mutating
the old one. The most recently appended tag for a given hypothesis_id (by
event_ts, then id — the same ordering research.memory.query_research_log()
already returns rows in) is treated as that hypothesis's current area. This
mirrors the same "last write wins over an append-only log" pattern already
used elsewhere in research/brain (e.g. hypothesis_intake.py's handling of
its own claim log) rather than inventing a new convention.

Case sensitivity: research_area is stored and grouped as an exact string —
"momentum" and "Momentum" are different areas as far as this module is
concerned. That is deliberate, not an oversight: silently folding case (or
otherwise normalizing labels) would be a first step toward automatic
taxonomy behavior, which is explicitly out of scope. Whatever calls
tag_hypothesis() is responsible for using a consistent label.

Safety, structurally true by absence, same pattern as observatory.py,
digest.py, and similarity.py: this module imports no `engine` module, no
broker module, never calls Contract.lock() or Contract.save() (it never
imports Contract at all), never calls store.append() directly (only through
research.memory's typed wrapper, itself append-only), and never runs an
experiment. It reads research memory and returns plain data.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .. import memory as rm
from ..store import Store, TimeLike


@dataclass(frozen=True)
class AreaGroup:
    """One research area and every hypothesis currently tagged into it,
    using each hypothesis's most recent tag."""

    name: str
    hypothesis_ids: tuple

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "hypothesis_ids": list(self.hypothesis_ids),
            "hypothesis_count": len(self.hypothesis_ids),
        }


def tag_hypothesis(
    store: Store,
    *,
    hypothesis_id: str,
    research_area: str,
    source: str,
    entity: str = rm.MARKET_ENTITY,
) -> Optional[int]:
    """Record `hypothesis_id -> research_area`. Purely additive research-memory
    metadata — never touches research.contracts, never mints or requires a
    Contract, never locks anything. Tagging the same hypothesis again with a
    different (or the same) area appends a new row rather than editing the
    old one; see the module docstring for how retagging resolves.

    Both fields are required and must be non-empty strings — this function
    does not check that `hypothesis_id` refers to a hypothesis that actually
    exists elsewhere in research memory, on purpose: a research area is
    independent organizational metadata, not a foreign-key relationship to
    validate, and requiring existence would be exactly the kind of
    unnecessary coupling/abstraction this slice was asked to avoid.
    """
    if not isinstance(hypothesis_id, str) or not hypothesis_id.strip():
        raise ValueError("hypothesis_id must be a non-empty string")
    if not isinstance(research_area, str) or not research_area.strip():
        raise ValueError("research_area must be a non-empty string")
    return rm.record_research_area_tag(
        store, hypothesis_id=hypothesis_id, research_area=research_area,
        source=source, entity=entity,
    )


def _latest_tags(store: Store, *, as_of: Optional[TimeLike] = None) -> dict:
    """hypothesis_id -> its most recently tagged research_area, as of `as_of`
    (defaults to "now" via research.memory.query_research_log's own default).
    Rows come back in ascending (event_ts, id) order, so simply overwriting a
    dict entry as we walk them gives last-write-wins with no extra sorting or
    dedup logic needed."""
    rows = rm.query_research_log(store, rm.DATASET_RESEARCH_AREA, as_of=as_of)
    latest: dict = {}
    for r in rows:
        payload = r.get("payload") or {}
        hid, area = payload.get("hypothesis_id"), payload.get("research_area")
        if not hid or not area:
            continue  # malformed row (shouldn't happen via tag_hypothesis) — skip, don't raise
        latest[hid] = area
    return latest


def area_of(
    store: Store, hypothesis_id: str, *, as_of: Optional[TimeLike] = None,
) -> Optional[str]:
    """The current (most recently tagged) research area for one hypothesis,
    or None if it has never been tagged. A thin lookup over _latest_tags() —
    no separate query path that could disagree with groups()."""
    return _latest_tags(store, as_of=as_of).get(hypothesis_id)


def groups(store: Store, *, as_of: Optional[TimeLike] = None) -> list:
    """Every research area currently in use, each with the hypothesis_ids
    tagged into it (their most recent tag only — see the module docstring).

    Deterministic: hypothesis_ids within a group are sorted, and groups
    themselves are sorted by area name, so two calls against an unchanged
    research memory always return identical output regardless of sqlite row
    order or dict insertion order. An untagged research memory returns an
    empty list, not an error.
    """
    latest = _latest_tags(store, as_of=as_of)
    by_area: dict = {}
    for hid, area in latest.items():
        by_area.setdefault(area, []).append(hid)

    result = [
        AreaGroup(name=area, hypothesis_ids=tuple(sorted(hids)))
        for area, hids in by_area.items()
    ]
    result.sort(key=lambda g: g.name)
    return result


def groups_as_dicts(store: Store, *, as_of: Optional[TimeLike] = None) -> list:
    """JSON-ready form of groups() — plain dicts, same deterministic
    ordering, for a caller (e.g. digest.py) that wants to serialize this
    directly."""
    return [g.to_dict() for g in groups(store, as_of=as_of)]
