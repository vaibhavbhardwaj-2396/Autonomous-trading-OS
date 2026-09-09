"""
Discovery Search Provenance — Phase 2 Slice N.

The gap this closes: once a hypothesis exists, nothing in the system could
answer "what was the Research AI looking at when it produced this?" or
"which discovery run, using which prompt/implementation version, generated
HYP-X?" Slice L (similarity.py) and Slice M (research_areas.py) both answer
questions about the CONTENT of the research state (duplicates, topic areas).
This slice answers a different question — provenance of the PROCESS that
produced a hypothesis in the first place — and is deliberately narrow about
it: only the first link in the eventual

    digest snapshot -> discovery process -> hypothesis -> contracts -> evidence

chain is built here (digest snapshot -> discovery process -> hypothesis).
Experiment lineage (hypothesis -> contracts -> evidence) already exists
elsewhere (evaluator.resolve_hypothesis_id, comparison.py) and is untouched;
wiring THIS module's provenance into that longer chain is explicitly future
work, not attempted here.

Data model: no new SQLite table. This reads back the
`research_discovery_search` dataset research.memory.record_discovery_search()
writes (see that function's docstring for the exact payload shape and for
why only `as_of` + a version string is persisted rather than the whole
digest body). This module is the read side only — the write side lives in
research/memory.py (the typed wrapper, matching how record_research_area_tag
sits in memory.py while research_areas.py holds the read/group side) and is
called directly from research/brain/investigator.py after a successful
DRAFT, not through any function in this file.

Not a general provenance framework: this module knows about exactly one
dataset and answers exactly one question ("which discovery search rows
produced this hypothesis_id?"). It does not attempt to generalize to other
kinds of provenance, does not build a lineage graph, and does not interpret
or validate `discovery_type`/`version` — those are opaque strings owned by
whichever discovery mechanism wrote them (research/brain/investigator.py,
for the only one that exists today).

Safety, structurally true by absence, same pattern as similarity.py and
research_areas.py: this module imports no `engine` module, no broker
module, never imports research.contracts or Contract, never calls
Contract.lock()/.save(), never calls store.append() directly (only through
research.memory's typed wrapper, itself append-only), and never runs an
experiment. It reads research memory and returns plain data.
"""

from __future__ import annotations

from typing import Optional

from .. import memory as rm
from ..store import Store, TimeLike


def discovery_search_for_hypothesis(
    store: Store, hypothesis_id: str, *, as_of: Optional[TimeLike] = None,
) -> list:
    """Every discovery-search provenance row recorded for this hypothesis_id,
    oldest first (the same chronological order research.memory.
    query_research_log() already returns rows in) — as plain payload dicts,
    each carrying discovery_type/as_of/version/hypothesis_id and whatever
    the writer put in `extra` (e.g. investigator.py's prompt_path).

    Ordinarily exactly one row: one investigate() run producing one
    hypothesis. Returned as a list, not a single dict, because nothing about
    the append-only log or this function prevents (or needs to prevent) more
    than one discovery run eventually being linked to the same
    hypothesis_id — callers that only care about "was there a discovery
    record at all" can just check for a non-empty list.
    """
    rows = rm.query_research_log(store, rm.DATASET_DISCOVERY_SEARCH, as_of=as_of)
    return [r["payload"] for r in rows if r["payload"].get("hypothesis_id") == hypothesis_id]


def has_discovery_provenance(
    store: Store, hypothesis_id: str, *, as_of: Optional[TimeLike] = None,
) -> bool:
    """Convenience: does this hypothesis_id have at least one recorded
    discovery-search provenance row? A thin read over
    discovery_search_for_hypothesis() — no separate query path."""
    return len(discovery_search_for_hypothesis(store, hypothesis_id, as_of=as_of)) > 0
