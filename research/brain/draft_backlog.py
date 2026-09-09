"""
Research Draft Backlog — Phase 2 Slice Q.

The gap this closes: Slice P wired up an unattended overnight Research AI
that creates DRAFT hypotheses on its own, and Slice O put a structural
ceiling on how many Contracts may ever be LOCKED. Nothing yet lets a human
actually SEE the queue of drafts those two things are quietly building up —
`hypothesis_intake.pending_drafts()` returns the bare `Contract` objects and
nothing else; a reviewer still has to manually cross-reference research
memory, `similarity.py`, `research_areas.py`, and `discovery_provenance.py`
by hand to answer "what is this, where did it come from, and has it (or
something just like it) already been tried?" for every draft.

This module is exactly that cross-reference, read-only, and nothing more:

    Overnight Research AI -> DRAFT hypotheses -> THIS MODULE -> human review
                                                                      |
                                                                      v
                                                     existing approve_and_lock()

It is a REVIEW SURFACE, not a decision-maker. It never approves, locks,
executes, retags, or overrides anything — see the "Read-only, structurally"
section below for exactly how that is enforced by absence rather than by a
check that could be forgotten.

Design principle, same as similarity.py/research_areas.py/
discovery_provenance.py before it: REUSE, don't reimplement.
  - which contracts are drafts               -> hypothesis_intake.pending_drafts()
  - which hypothesis owns a draft contract    -> experiments.evaluator.resolve_hypothesis_id()
  - every contract id tied to that hypothesis -> experiments.evaluator.contract_ids_for_hypothesis()
  - exact-duplicate status, anywhere in the
    registry, in any status                   -> brain.similarity.duplicate_groups()
  - which statuses count as "already tested"  -> brain.digest.TESTED_STATUSES
  - research-area tag, if any                 -> brain.research_areas.area_of()
  - discovery provenance, if any              -> brain.discovery_provenance.discovery_search_for_hypothesis()
Nothing here defines a second notion of "duplicate", "tested", "area", or
"provenance" — every one of those words means exactly what the module that
already owns it says it means. The only genuinely new logic in this file is
assembly, ordering, bounding, and rendering.

Explicitly NOT built here (all real, all out of scope for this slice, all
already ruled out for the modules this one reuses): near-duplicate/fuzzy
matching, semantic similarity, hypothesis-family inference, a "quality
score" or any other machine-generated ranking, an automatic recommendation
("approve"/"reject") derived from expected profitability, a scheduler, a
queue, automatic promotion. A `Recommendation:` line appears in this slice's
own conceptual example markdown — but the same instructions immediately say
"do not invent a recommendation," so this module renders none at all, not
even a duplicate-derived one. See `to_markdown()` below for where that line
would have gone and stayed empty. This is a documented design choice, not an
oversight.

Draft inclusion policy (Slice Q's own "B. Non-draft exclusion/inclusion"
requirement — determine the appropriate semantics from existing repository
conventions and document them): the backlog shows exactly the contracts
`hypothesis_intake.pending_drafts()` already defines as pending review —
`status == "draft"`, nothing else. That function is the established,
already-tested definition of "the literal to-review queue for a human doing
the review step" (its own docstring, verbatim); this module does not narrow
or widen that definition. A contract that has moved on to locked / running /
reported / abandoned / superseded is, by that same existing convention, no
longer part of the to-review queue and is excluded — that history is still
reachable through "related contract IDs" and "already tested" below for any
draft that shares a hypothesis or an exact rule fingerprint with one, so
nothing is hidden, it just isn't re-listed as if it were still pending.

Ordering (deterministic, as required): newest draft first, by the draft's
own creation timestamp (the `event_time` of the hypothesis-claim row that
minted this specific `contract_id` — the same repository-standard,
IST-aware timestamp `research.store.now_ist()`/`iso()` produce and every
other research-memory row already uses), with `hypothesis_id` as a stable
ascending tie-break. A draft whose creation row cannot be found (should not
happen via the normal `create_draft()`/`derive_split_contract()` paths, but
handled rather than assumed impossible) sorts as if oldest, after every
draft with a known timestamp, so a missing timestamp can never masquerade as
the newest thing in the queue.

No machine-generated "quality score": drafts are ordered by *when they were
proposed*, a fact, never by any judgment about which one looks more
promising.

Bounded output: `limit` follows the exact convention `digest.py` and
`research_areas.py`/`similarity.py`'s digest integration already established
— `None` means unbounded, an integer bounds the *shown* list while
`shown_count`/`total_count`/`truncated` still report the true totals, and
`limit=0` means "show none" (checked with `is not None`, never by
truthiness, precisely because `0` is falsy and must not be silently treated
as "unbounded" — this is the exact bug Slice L found and fixed in
`digest.py`'s own duplicate-group bounding).

Read-only, structurally: this module imports no `engine` module, no broker
module, never imports `research.contracts.Contract` for anything other than
reading fields off objects `pending_drafts()`/`registry()` already loaded,
never calls `Contract.save()` or `Contract.lock()`, never calls
`hypothesis_intake.approve_and_lock()` (imported for `pending_drafts()`
only), never calls `research.experiments.runner.run_experiment()` (not
imported at all), never calls `store.append()` or any other Store write
method, and never touches `research_area_tag`/`research_discovery_search`
write paths (`research_areas.tag_hypothesis()` /
`memory.record_discovery_search()` are never imported here). It reads the
registry and research memory and returns plain data.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .. import memory as rm
from ..contracts import REGISTRY_DIR
from ..experiments import evaluator
from ..store import Store, TimeLike, iso, now_ist, to_dt
from . import discovery_provenance as dp
from . import hypothesis_intake as hi
from . import research_areas, similarity
from .digest import TESTED_STATUSES

DEFAULT_BACKLOG_LIMIT = 20
"""A deliberately conservative, clearly-named default — same posture as
every other bounded default in this codebase (digest.py's
DEFAULT_ANOMALY_LIMIT/DEFAULT_DUPLICATE_GROUP_LIMIT/DEFAULT_AREA_LIMIT,
overnight.py's MAX_PROPOSAL_ATTEMPTS_PER_RUN): not claimed to be
production-tuned, just a sane starting point for a human skimming a review
list, and always overridable per call."""


def _creation_row(store: Store, contract_id: str, *, as_of: Optional[TimeLike]) -> Optional[dict]:
    """The single hypothesis-claim row that minted `contract_id` — the same
    walk `research.experiments.evaluator.resolve_hypothesis_id()` already
    does (payload.contract_id == contract_id), kept local rather than reused
    because this needs the whole row (event_time, source), not just the
    hypothesis_id it resolves to. One extra local pass over the same
    DATASET_HYPOTHESIS rows evaluator.py itself reads — no new dataset, no
    new write path, nothing evaluator.py doesn't already do for a different
    field."""
    rows = rm.query_research_log(store, rm.DATASET_HYPOTHESIS, as_of=as_of)
    for r in rows:
        if r["payload"].get("contract_id") == contract_id:
            return r
    return None


def _draft_entry(
    store: Store,
    contract,
    *,
    registry_dir: Path,
    as_of: Optional[TimeLike],
    dup_by_contract_id: dict,
) -> dict:
    """Everything Slice Q's "Required information" list asks for, about one
    draft Contract — gathered entirely by reading, never by inferring beyond
    what's explicitly recorded."""
    contract_id = contract.id

    creation_row = _creation_row(store, contract_id, as_of=as_of)
    created_at = creation_row.get("event_time") if creation_row else None
    source = creation_row.get("source") if creation_row else None

    hypothesis_id = evaluator.resolve_hypothesis_id(store, contract_id)

    area = (research_areas.area_of(store, hypothesis_id, as_of=as_of)
            if hypothesis_id else None)

    provenance_rows = (dp.discovery_search_for_hypothesis(store, hypothesis_id, as_of=as_of)
                        if hypothesis_id else [])
    provenance = None
    if provenance_rows:
        p = provenance_rows[0]  # oldest first; ordinarily exactly one row
        provenance = {
            "discovery_type": p.get("discovery_type"),
            "version": p.get("version"),
            "as_of": p.get("as_of"),
            "prompt_path": p.get("prompt_path"),
        }

    sibling_ids = (evaluator.contract_ids_for_hypothesis(store, hypothesis_id)
                   if hypothesis_id else [contract_id])

    group = dup_by_contract_id.get(contract_id)
    exact_duplicate = group is not None
    duplicate_member_ids = (
        sorted(m.contract_id for m in group.members if m.contract_id != contract_id)
        if group is not None else []
    )
    already_tested = (
        any(m.status in TESTED_STATUSES for m in group.members if m.contract_id != contract_id)
        if group is not None else False
    )

    related_contract_ids = sorted(
        (set(sibling_ids) | set(duplicate_member_ids)) - {contract_id}
    )

    return {
        "hypothesis_id": hypothesis_id,
        "contract_id": contract_id,
        "claim": contract.title or contract.hypothesis,
        "status": contract.status,
        "source": source,
        "created_at": created_at,
        "research_area": area,
        "discovery_provenance": provenance,
        "exact_duplicate": exact_duplicate,
        "duplicate_of_contract_ids": duplicate_member_ids,
        "already_tested": already_tested,
        "related_contract_ids": related_contract_ids,
    }


def list_drafts(
    store: Store,
    *,
    registry_dir: Path = REGISTRY_DIR,
    as_of: Optional[TimeLike] = None,
    limit: Optional[int] = DEFAULT_BACKLOG_LIMIT,
) -> list[dict]:
    """The bounded, deterministically ordered draft backlog itself — a list
    of plain dicts, one per pending draft, newest first with a stable
    hypothesis_id tie-break (see the module docstring). `limit=None` means
    unbounded; `limit=0` means show none (checked with `is not None`, never
    by truthiness — see the module docstring's "Bounded output" section).

    Read-only throughout: `hypothesis_intake.pending_drafts()` only reads
    the registry directory; everything else here reads research memory via
    already-established, read-only functions. Nothing is written, locked,
    executed, or modified.
    """
    drafts = hi.pending_drafts(registry_dir)

    # Exact-duplicate groups computed ONCE for the whole backlog, not once
    # per draft — duplicate_groups() already scans the entire registry, so
    # calling it per-draft would be O(n^2) for no benefit. Keyed by
    # contract_id for an O(1) lookup per draft below.
    groups = similarity.duplicate_groups(store, registry_dir=registry_dir)
    dup_by_contract_id = {m.contract_id: g for g in groups for m in g.members}

    entries = [
        _draft_entry(store, c, registry_dir=registry_dir, as_of=as_of,
                     dup_by_contract_id=dup_by_contract_id)
        for c in drafts
    ]

    # Sort ascending by (has_ts, created_at, hypothesis_id) then reverse the
    # WHOLE ordering — Python's sort is stable, so reversing a list already
    # sorted ascending on a 3-key tuple would also reverse the hypothesis_id
    # tie-break, which is wrong (the tie-break must stay ascending while the
    # timestamp goes descending). Instead: sort ascending on hypothesis_id
    # first (stable), THEN sort descending on (has_ts, created_at) — the
    # second, stable sort only reorders groups that tie on timestamp,
    # leaving the hypothesis_id-ascending order intact *within* each tie.
    entries.sort(key=lambda e: e["hypothesis_id"] or "")
    entries.sort(key=lambda e: (e["created_at"] is not None, e["created_at"] or ""),
                 reverse=True)

    return entries[:limit] if limit is not None else entries


def build_backlog(
    store: Store,
    *,
    registry_dir: Path = REGISTRY_DIR,
    as_of: Optional[TimeLike] = None,
    limit: Optional[int] = DEFAULT_BACKLOG_LIMIT,
) -> dict:
    """list_drafts() plus the same shown/total/truncated bookkeeping
    digest.py's own bounded sections already use, so a caller (or a human
    reading the JSON) can always tell "how many drafts actually exist" apart
    from "how many are shown here"."""
    all_drafts = list_drafts(store, registry_dir=registry_dir, as_of=as_of, limit=None)
    shown = all_drafts[:limit] if limit is not None else all_drafts
    return {
        "as_of": iso(to_dt(as_of, end_of_day=True)) if as_of is not None else iso(now_ist()),
        "drafts": shown,
        "shown_count": len(shown),
        "total_count": len(all_drafts),
        "truncated": len(all_drafts) > len(shown),
    }


def _fmt_provenance(p: Optional[dict]) -> str:
    if not p:
        return "none"
    bits = f"{p.get('discovery_type') or '?'} {p.get('version') or '?'} / {p.get('as_of') or '?'}"
    if p.get("prompt_path"):
        bits += f" (prompt: {p['prompt_path']})"
    return bits


def to_markdown(backlog: dict) -> str:
    """A deterministic Markdown rendering of build_backlog()'s output —
    concise, factual, and deliberately free of any recommendation, expected-
    profitability language, or trading instruction (see the module
    docstring for why no `Recommendation:` line is rendered at all, even
    though this slice's own conceptual example sketched one)."""
    lines: list[str] = [f"Research Draft Backlog — {backlog['as_of']}", ""]
    lines.append(
        f"{backlog['shown_count']} of {backlog['total_count']} draft(s) shown"
        + (" (truncated)" if backlog["truncated"] else "") + ".")
    lines.append("")
    lines.append(
        "_This is a review surface, not a decision. Nothing here is approved, "
        "locked, or tested. Use the existing human-approval workflow to act "
        "on any of these._")
    lines.append("")

    if not backlog["drafts"]:
        lines.append("_No drafts are currently pending review._")
        return "\n".join(lines) + "\n"

    for i, d in enumerate(backlog["drafts"], start=1):
        lines.append(f"{i}. {d['hypothesis_id']} ({d['contract_id']})")
        lines.append(f"   Claim: {d['claim']}")
        lines.append(f"   Status: {d['status']}")
        lines.append(f"   Area: {d['research_area'] or 'unassigned'}")
        lines.append(f"   Source: {d['source'] or 'unknown'}")
        lines.append(f"   Created: {d['created_at'] or 'unknown'}")
        lines.append(f"   Exact duplicate: {'YES' if d['exact_duplicate'] else 'NO'}")
        if d["exact_duplicate"]:
            existing = ", ".join(d["duplicate_of_contract_ids"]) or "none"
            lines.append(f"   Existing contracts (same spec): {existing}")
            lines.append(
                f"   Already tested (same spec): {'YES' if d['already_tested'] else 'NO'}")
        if d["related_contract_ids"]:
            lines.append(f"   Related contracts: {', '.join(d['related_contract_ids'])}")
        lines.append(f"   Provenance: {_fmt_provenance(d['discovery_provenance'])}")
        lines.append("")

    return "\n".join(lines).rstrip("\n") + "\n"


def render(
    store: Store,
    *,
    registry_dir: Path = REGISTRY_DIR,
    as_of: Optional[TimeLike] = None,
    limit: Optional[int] = DEFAULT_BACKLOG_LIMIT,
) -> str:
    """build_backlog() + to_markdown() in one call — the function most
    callers doing a human-review dump want."""
    return to_markdown(
        build_backlog(store, registry_dir=registry_dir, as_of=as_of, limit=limit))
