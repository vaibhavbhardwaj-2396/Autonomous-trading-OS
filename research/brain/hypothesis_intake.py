"""
Hypothesis Intake — Phase 1 Slice C (revised after audit).

    Claude proposal (JSON)
          |
          v
    scratch file, human review        <- happens OUTSIDE this module
          |
          v
    validate_proposal()   -- pure, structural, no writes
          |
          v
    create_draft()        -- PROPOSED -> DRAFT  (Contract saved, status="draft")
          |
          v
    ═══ a human reads the draft file and decides ═══
          |
          v
    approve_and_lock()    -- DRAFT -> HUMAN APPROVED -> LOCKED, atomically,
                              and ONLY given an explicit `approved_by` string
          |
          v
    ───── STOP ─────   (nothing downstream of this exists yet)

Audit fix #1 — locking is no longer implicit. `intake(...)` (the old
single-call convenience path) now defaults to `lock=False` and produces a
DRAFT, never a locked contract, unless the caller ALSO passes a non-empty
`approved_by`. There is no code path anywhere in this module that reaches
`Contract.lock()` without a human-identifying string attached to that specific
call — "remember to review it first" is no longer the only thing standing
between a proposal and a preregistered contract. The real state machine is:

    PROPOSED  — a raw dict, not yet even validated. Lives outside this module
                (a scratch JSON file, or in-memory in whatever proposes it).
    DRAFT     — validate_proposal() passed; create_draft() has built and saved
                a Contract with status="draft". Nothing about it is frozen —
                re-running create_draft() with a corrected proposal is
                expected and safe.
    APPROVED
    + LOCKED  — a single atomic step, approve_and_lock(), taken only by
                something that supplies `approved_by`. These two are not
                split into separate persisted states on purpose: a
                persisted "approved but not yet locked" state would just be
                a second window in which the approved content could still
                drift before the hash freezes it, which is a worse property,
                not a better one. The barrier is the explicit function call
                and its required argument, not a database column.

Audit fix #4 — identity is no longer proposer-controlled. A proposal MUST NOT
include `contract_id` at all (rejected outright if present); the system mints
it from the hypothesis it belongs to (`EXP-<hyp-suffix>-<A|B|C|...>`), so a
single hypothesis's contracts are visibly grouped and a proposer can never
claim an id that collides with, or spoofs, another experiment's identity. A
proposal MAY reference an existing `hypothesis_id` to attach a new contract
variant to a previously recorded claim — but only if that hypothesis_id
already exists in research memory; inventing a plausible-looking one that was
never actually issued is rejected, not silently accepted.

KNOWN CONSTRAINT, recorded rather than fixed (Slice C audit, Vaibhav's call):
variant-letter assignment in `_next_contract_id` is state-derived — it reads
the current set of contract ids tied to a hypothesis and picks the next
unused letter — not concurrency-safe. Two `create_draft()` calls racing for
the same hypothesis_id at the same instant could compute the same letter.
Acceptable for the current single-operator, human-paced pipeline (nothing in
Phase 1 runs unattended or multi-worker). If that ever changes, replace this
with an atomic DB sequence or a UNIQUE constraint the second writer fails
against — do not ship concurrent/unattended operation on top of the current
implementation without that change.

Everything from the original design remains: a Claude-generated hypothesis is
DATA, never executable code. Every field is either a member of an explicit
whitelist (metric names, operators, universes, split names) or a
type/range-checked primitive (numbers, ISO dates, bounded text). Unknown keys
are rejected, not ignored. This module contains zero calls to eval, exec, or
compile — the whitelist is the only mechanism that ever turns a proposal into
a Contract; the free-text code-pattern scan below is defense-in-depth around
that, never the primary mechanism (see the module-level tests for both
layers checked independently).

What this module does NOT do, structurally (it imports none of the modules
that could do these things, so this is enforced by absence, not by a check
that could be forgotten):
  - execute a hypothesis or run a backtest      (that's research/experiments/
    runner.py — Slice D, not built yet)
  - call the broker or engine.execute
  - touch memory/state.json, guardrails, strategy.md, or Claude permissions
  - lock anything without an explicit `approved_by` on that exact call

Audit fix #2 (recorded, not changed here) — this module imports
`engine.watchlist.UNIVERSE` for one reason only: to let `"watchlist"` be a
valid `universe` value. `engine/watchlist.py` is verified (see
tests/test_research_engine_boundary.py) to contain nothing but two static,
literal module-level constants — no functions, no classes, no I/O, no
import-time side effects. It is configuration, not behaviour, so importing it
does not create a behavioural coupling to the execution kernel. That said,
this is the ONE and only research -> engine import in this module, and it
should stay that way; the longer-term fix — a small `shared/` package holding
universe/schema definitions that neither `engine/` nor `research/` has to
reach across the aisle for — is a real idea worth doing before Slice D grows
more reasons to want engine-side config. Not done now; recorded here so it
doesn't get forgotten.

Hypothesis vs Contract, preserved exactly as designed:
    a Hypothesis (research.memory.record_hypothesis_proposal) is the CLAIM.
    a Contract (research.contracts.Contract) is ONE precise, hash-locked test
    of a version of that claim. create_draft() always records the claim, then
    builds exactly one (unlocked) Contract from it — one hypothesis can have
    several contract variants, each a separate create_draft() + separate
    approve_and_lock() call, sharing one `hypothesis_id` via the optional
    `hypothesis_id` proposal field.
"""

from __future__ import annotations

import json
import math
import re
import datetime as dt
from dataclasses import replace as _replace
from pathlib import Path
from typing import Any, NamedTuple, Optional, Union

from .. import memory as rm
from ..contracts import Contract, ContractViolation, REGISTRY_DIR, registry as _registry

# ---------------------------------------------------------------------------
# The whitelist. This IS the rule language — deliberately the smallest one
# that can express a first real experiment: "when this metric crosses this
# threshold, enter; exit on a stop / target / time limit."
# ---------------------------------------------------------------------------

# metric -> the dataset it is computed from. Declared here as the single
# source of truth rather than as a separate "dataset" field on a condition,
# so a proposal can never claim a metric belongs to a dataset it doesn't
# (which would otherwise be a second, independently-spoofable input).
METRIC_DATASET = {
    "volume_zscore": "prices_eod",             # research.brain.observatory
    "price_move_zscore": "prices_eod",         # research.brain.observatory
    "event_frequency_zscore": "bse_announcement",  # research.brain.observatory
    "close": "prices_eod",
    "return_1d": "prices_eod",
}
SUPPORTED_METRICS = frozenset(METRIC_DATASET)

SUPPORTED_OPERATORS = frozenset({">", ">=", "<", "<=", "==", "!="})

# engine.watchlist.UNIVERSE is imported lazily inside _valid_universes() —
# research importing engine is the sanctioned direction (research/replay.py
# already does it); it is never the other way around. See the module
# docstring's "Audit fix #2" note for why this specific import is judged safe.
VALID_UNIVERSES = frozenset({"Nifty 50", "Nifty 500", "watchlist"})

CONDITION_REQUIRED_KEYS = frozenset({"metric", "op", "value"})
CONDITION_OPTIONAL_KEYS = frozenset({"window_days"})
CONDITION_ALLOWED_KEYS = CONDITION_REQUIRED_KEYS | CONDITION_OPTIONAL_KEYS
MAX_CONDITIONS_PER_RULE = 3

EXIT_ALLOWED_KEYS = frozenset({"stop_loss_pct", "target_pct", "max_hold_days"})

VALID_SPLIT_KEYS = frozenset({"discovery", "validation", "holdout"})

FREE_TEXT_FIELDS = (
    "title", "hypothesis", "null_hypothesis", "signal",
    "independence", "falsification", "abandon_condition", "notes", "source",
)
FREE_TEXT_MAX_LEN = {"title": 200, "signal": 200, "notes": 2000}
FREE_TEXT_DEFAULT_MAX_LEN = 1000

# contract_id is deliberately ABSENT from both sets below — see Audit fix #4.
# The proposer supplies the claim and the test specification; the system
# supplies identity.
REQUIRED_FIELDS = frozenset({
    "title", "hypothesis", "null_hypothesis",
    "universe", "signal", "entry_rule", "exit_rule", "splits",
    "independence", "falsification", "abandon_condition",
    "evaluation_start", "evaluation_end",
})
OPTIONAL_FIELDS = frozenset({
    "hypothesis_id", "source", "llm_features", "llm_model_id",
    "llm_knowledge_cutoff", "notes",
})
ALLOWED_TOP_LEVEL_KEYS = REQUIRED_FIELDS | OPTIONAL_FIELDS

_HYPOTHESIS_ID_RE = re.compile(r"^HYP-\d{8}-[0-9a-f]{8}$")

# Defense-in-depth: even though every structured field is whitelist-checked
# (so there is nowhere for an expression to be *interpreted*), every free-text
# string in the proposal is also scanned for code- or shell-like content. This
# catches a payload that tries to smuggle something dangerous into a field
# meant to be read, not evaluated — belt AND suspenders, not either/or. The
# whitelist above is the primary firewall; this regex is secondary to it, on
# purpose (see the module docstring and Audit fix #3).
_CODE_PATTERNS = (
    r"\beval\s*\(", r"\bexec\s*\(", r"__import__", r"\bimport\s+\w+",
    r"\bos\.(system|popen)", r"\bsubprocess\b", r"\bopen\s*\(",
    r"\blambda\b", r"\bcompile\s*\(", r"\bgetattr\s*\(", r"\bsetattr\s*\(",
    r"\bglobals\s*\(", r"\blocals\s*\(", r"`", r"\$\(", r"&&", r"\|\|",
    r";\s*rm\s", r"\bctypes\b", r"\bsocket\.", r"\bsys\.exit",
)
_CODE_RE = re.compile("|".join(_CODE_PATTERNS), re.IGNORECASE)


class IntakeRejected(RuntimeError):
    """A proposal (or an approval attempt) failed the firewall. Carries every
    reason found, not just the first, so a human reviewer doesn't have to
    resubmit N times to learn about N problems. Nothing is written to
    research memory or the registry when this is raised from create_draft() —
    validation runs to completion before any write. Raised from
    approve_and_lock() only for a reason that made locking itself refuse; the
    draft on disk is left exactly as it was."""

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = list(reasons)
        super().__init__("; ".join(self.reasons) or "rejected (no reason given)")


class IntakeResult(NamedTuple):
    hypothesis_id: str
    contract: Contract


# ---------------------------------------------------------------------------
# Validation — pure function, never raises, never writes anything, never
# touches a Store. (Store-dependent checks — does this hypothesis_id already
# exist, what's the next free contract_id — live in create_draft() below,
# for the same reason the original design kept them out of this function:
# so the whitelist itself stays testable with no database at all.)
# ---------------------------------------------------------------------------

def _valid_universes() -> frozenset:
    try:
        from engine.watchlist import UNIVERSE as _WATCHLIST  # noqa: F401
    except Exception:
        pass  # "watchlist" stays a valid universe name even if engine/ is
              # unavailable in this environment — intake never depends on
              # engine/ being importable to do its job of validating shape.
    return VALID_UNIVERSES


def _walk_strings(obj: Any, path: str = "$"):
    """Yield (json_path, string_value) for every string anywhere in obj."""
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk_strings(v, f"{path}.{k}")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            yield from _walk_strings(v, f"{path}[{i}]")


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _is_str(v: Any) -> bool:
    return isinstance(v, str)


def _parse_date(s: Any) -> Optional[dt.date]:
    if not isinstance(s, str):
        return None
    try:
        return dt.date.fromisoformat(s[:10])
    except ValueError:
        return None


def _check_condition(cond: Any, where: str, problems: list[str]) -> None:
    if not isinstance(cond, dict):
        problems.append(f"{where} must be an object, got {type(cond).__name__}")
        return
    extra = set(cond) - CONDITION_ALLOWED_KEYS
    if extra:
        problems.append(f"{where} has unsupported field(s): {sorted(extra)}")
    missing = CONDITION_REQUIRED_KEYS - set(cond)
    if missing:
        problems.append(f"{where} is missing required field(s): {sorted(missing)}")
        return

    metric = cond.get("metric")
    if not _is_str(metric) or metric not in SUPPORTED_METRICS:
        problems.append(
            f"{where}.metric {metric!r} is not a supported metric "
            f"(supported: {sorted(SUPPORTED_METRICS)})")

    op = cond.get("op")
    if not _is_str(op) or op not in SUPPORTED_OPERATORS:
        problems.append(
            f"{where}.op {op!r} is not a supported operator "
            f"(supported: {sorted(SUPPORTED_OPERATORS)})")

    value = cond.get("value")
    if not _is_number(value):
        problems.append(f"{where}.value must be a finite number, got {value!r}")

    if "window_days" in cond:
        wd = cond["window_days"]
        if not (isinstance(wd, int) and not isinstance(wd, bool) and 1 <= wd <= 500):
            problems.append(f"{where}.window_days must be an integer in [1, 500], got {wd!r}")


def _check_entry_rule(entry_rule: Any, problems: list[str]) -> None:
    if not isinstance(entry_rule, dict):
        problems.append("entry_rule must be an object of the form "
                         '{"conditions": [...]}')
        return
    extra = set(entry_rule) - {"conditions"}
    if extra:
        problems.append(f"entry_rule has unsupported field(s): {sorted(extra)}")
    conditions = entry_rule.get("conditions")
    if not isinstance(conditions, list) or not conditions:
        problems.append("entry_rule.conditions must be a non-empty list")
        return
    if len(conditions) > MAX_CONDITIONS_PER_RULE:
        problems.append(
            f"entry_rule.conditions has {len(conditions)} conditions; "
            f"at most {MAX_CONDITIONS_PER_RULE} are supported in this slice")
    for i, cond in enumerate(conditions[:MAX_CONDITIONS_PER_RULE + 1]):
        _check_condition(cond, f"entry_rule.conditions[{i}]", problems)


def _check_exit_rule(exit_rule: Any, problems: list[str]) -> None:
    if not isinstance(exit_rule, dict):
        problems.append("exit_rule must be an object with any of "
                         f"{sorted(EXIT_ALLOWED_KEYS)}")
        return
    extra = set(exit_rule) - EXIT_ALLOWED_KEYS
    if extra:
        problems.append(f"exit_rule has unsupported field(s): {sorted(extra)}")
    if not (set(exit_rule) & EXIT_ALLOWED_KEYS):
        problems.append(
            f"exit_rule must declare at least one of {sorted(EXIT_ALLOWED_KEYS)}")

    for pct_key in ("stop_loss_pct", "target_pct"):
        if pct_key in exit_rule:
            v = exit_rule[pct_key]
            if not (_is_number(v) and 0 < v <= 50):
                problems.append(f"exit_rule.{pct_key} must be a number in (0, 50], got {v!r}")

    if "max_hold_days" in exit_rule:
        v = exit_rule["max_hold_days"]
        if not (isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 250):
            problems.append(f"exit_rule.max_hold_days must be an integer in [1, 250], got {v!r}")


def _check_splits(splits: Any, problems: list[str]) -> None:
    if not isinstance(splits, dict) or not splits:
        problems.append("splits must be a non-empty object")
        return
    extra = set(splits) - VALID_SPLIT_KEYS
    if extra:
        problems.append(f"splits has unknown key(s): {sorted(extra)} "
                         f"(valid: {sorted(VALID_SPLIT_KEYS)})")
    if "discovery" not in splits:
        problems.append('splits must include a "discovery" window')
    for key, window in splits.items():
        if key not in VALID_SPLIT_KEYS:
            continue
        if not (isinstance(window, list) and len(window) == 2):
            problems.append(f'splits["{key}"] must be a 2-element [start, end] list')
            continue
        start, end = _parse_date(window[0]), _parse_date(window[1])
        if start is None or end is None:
            problems.append(f'splits["{key}"] has an unparseable ISO date: {window!r}')
        elif end <= start:
            problems.append(f'splits["{key}"] end must be after start: {window!r}')


def validate_proposal(proposal: Any) -> list[str]:
    """Validate a hypothesis proposal. Returns a list of problems — empty
    means valid. Never raises on bad input; bad input is the expected case
    this function exists to handle, not an exceptional one.

    Does NOT check hypothesis_id existence or mint contract_id — those need a
    Store and the current registry state, so they live in create_draft().
    """
    problems: list[str] = []

    if not isinstance(proposal, dict):
        return [f"proposal must be a JSON object, got {type(proposal).__name__}"]

    if "contract_id" in proposal:
        problems.append(
            "contract_id must not be supplied in a proposal — experiment "
            "identity is assigned by the system (create_draft()), never by "
            "the proposer. Remove this field; if you're proposing a new "
            "variant of an existing claim, pass its hypothesis_id instead.")

    unknown = set(proposal) - ALLOWED_TOP_LEVEL_KEYS - {"contract_id"}
    if unknown:
        problems.append(f"proposal has unsupported top-level field(s): {sorted(unknown)}")

    missing = REQUIRED_FIELDS - set(proposal)
    if missing:
        problems.append(f"proposal is missing required field(s): {sorted(missing)}")

    # -- free text: type, non-empty, bounded length, no code-like content ---
    for field_name in FREE_TEXT_FIELDS:
        if field_name not in proposal:
            continue
        v = proposal[field_name]
        if not _is_str(v):
            problems.append(f"{field_name} must be a string, got {type(v).__name__}")
            continue
        if field_name != "notes" and not v.strip():
            problems.append(f"{field_name} must not be empty")
        max_len = FREE_TEXT_MAX_LEN.get(field_name, FREE_TEXT_DEFAULT_MAX_LEN)
        if len(v) > max_len:
            # The observed length is diagnostic, never the offending text itself — a
            # bounded, non-sensitive integer that makes a future rejection like this
            # investigable from telemetry alone (worker_runs.jsonl only ever records
            # str(exception), never the rejected proposal dict — see
            # research.brain.investigator.investigate()'s docstring).
            problems.append(
                f"{field_name} exceeds the maximum length of {max_len} characters "
                f"(length={len(v)})")

    # -- hypothesis_id shape (existence is checked later, against the store) -
    hid = proposal.get("hypothesis_id")
    if hid is not None and (not _is_str(hid) or not _HYPOTHESIS_ID_RE.match(hid)):
        problems.append(f"hypothesis_id {hid!r} must match {_HYPOTHESIS_ID_RE.pattern} "
                         f"(omit it to have create_draft() mint a new one)")

    # -- universe -------------------------------------------------------------
    universe = proposal.get("universe")
    if _is_str(universe) and universe not in _valid_universes():
        problems.append(f"universe {universe!r} is not recognized "
                         f"(valid: {sorted(_valid_universes())})")

    # -- rules ------------------------------------------------------------------
    if "entry_rule" in proposal:
        _check_entry_rule(proposal["entry_rule"], problems)
    if "exit_rule" in proposal:
        _check_exit_rule(proposal["exit_rule"], problems)
    if "splits" in proposal:
        _check_splits(proposal["splits"], problems)

    # -- dates ------------------------------------------------------------------
    start = _parse_date(proposal.get("evaluation_start"))
    end = _parse_date(proposal.get("evaluation_end"))
    if "evaluation_start" in proposal and start is None:
        problems.append(f"evaluation_start is not a valid ISO date: "
                         f"{proposal.get('evaluation_start')!r}")
    if "evaluation_end" in proposal and end is None:
        problems.append(f"evaluation_end is not a valid ISO date: "
                         f"{proposal.get('evaluation_end')!r}")
    if start is not None and end is not None and end <= start:
        problems.append("evaluation_end must be after evaluation_start")

    # -- llm firewall declaration (mirrors Contract's own check, but here so
    #    a bad declaration is rejected at intake rather than at lock time) --
    llm_features = proposal.get("llm_features", False)
    if "llm_features" in proposal and not isinstance(llm_features, bool):
        problems.append(f"llm_features must be a boolean, got {llm_features!r}")
    if llm_features:
        cutoff = proposal.get("llm_knowledge_cutoff")
        if not proposal.get("llm_model_id"):
            problems.append("llm_features is true but llm_model_id is missing")
        if not cutoff:
            problems.append("llm_features is true but llm_knowledge_cutoff is missing")
        elif _parse_date(cutoff) is None:
            problems.append(f"llm_knowledge_cutoff is not a valid ISO date: {cutoff!r}")

    # -- defense in depth: no code-like content anywhere in the payload ------
    # Secondary to the whitelist above, on purpose — see the module docstring.
    for path, s in _walk_strings(proposal):
        if _CODE_RE.search(s):
            problems.append(f"{path} contains code-like or shell-like content, "
                             f"which is never accepted in a hypothesis proposal: {s!r}")

    return problems


# ---------------------------------------------------------------------------
# Identity — system-controlled, per Audit fix #4. A contract_id is always
# derived from its hypothesis_id plus a spreadsheet-style variant letter
# (A, B, C, ..., Z, AA, ...), never chosen by the proposer.
# ---------------------------------------------------------------------------

def _variant_letter(n: int) -> str:
    """0 -> 'A', 1 -> 'B', ..., 25 -> 'Z', 26 -> 'AA', 27 -> 'AB', ..."""
    n += 1
    letters = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


def _hypothesis_claims(store, hypothesis_id: str) -> list[dict]:
    """Every recorded proposal row for this hypothesis_id, regardless of
    as_of — identity bookkeeping is live/operational, not a backtested read,
    so unlike everything in observatory.py this legitimately looks at
    research memory as of right now rather than through a replay gate."""
    rows = rm.query_research_log(store, rm.DATASET_HYPOTHESIS)
    return [r for r in rows if r["payload"].get("hypothesis_id") == hypothesis_id]


def _hypothesis_exists(store, hypothesis_id: str) -> bool:
    return len(_hypothesis_claims(store, hypothesis_id)) > 0


def _prior_split_derivation(
    claims: list[dict], parent_contract_id: str, split_key: str,
) -> Optional[str]:
    """Slice K — the single-use check for derive_split_contract(). Has this
    exact (parent_contract_id, split_key) pair already been derived once?

    Reuses metadata derive_split_contract() has ALWAYS written — this slice
    adds no new dataset, no new query path, and no new store table. Every
    call already records a hypothesis-claim row carrying `split_of` and
    `split` in its payload's `extra`; this function just reads that back.

    Returns the earlier sibling's contract_id if one exists, in ANY status —
    draft, locked, running, reported, OR abandoned (see derive_split_
    contract()'s docstring for why abandoned is deliberately not forgiven)
    — else None. A claim row that doesn't carry a matching `split_of`/
    `split` pair (the parent's own original creation claim, an unrelated
    hypothesis's claims, a claim for a different split_key or a different
    parent, or a pre-Slice-K claim with neither field at all) simply fails
    the equality check rather than raising — malformed or unrelated
    metadata is ignored, never mistaken for a match.
    """
    for r in claims:
        payload = r["payload"]
        if (payload.get("split_of") == parent_contract_id
                and payload.get("split") == split_key):
            return payload.get("contract_id")
    return None


def _next_contract_id(store, hypothesis_id: str) -> str:
    # NOT concurrency-safe — see "KNOWN CONSTRAINT" in the module docstring.
    # Reads current state and picks the next free letter; fine for a single
    # human-paced operator, not fine if this is ever called from more than
    # one worker at once.
    existing_ids = {
        r["payload"]["contract_id"] for r in _hypothesis_claims(store, hypothesis_id)
        if r["payload"].get("contract_id")
    }
    hid_suffix = hypothesis_id.split("-")[-1][:6].upper()
    i = 0
    while True:
        candidate = f"EXP-{hid_suffix}-{_variant_letter(i)}"
        if candidate not in existing_ids:
            return candidate
        i += 1


# ---------------------------------------------------------------------------
# create_draft() — PROPOSED -> DRAFT. Writes a hypothesis claim + a draft
# (unlocked) Contract. Never locks anything.
# ---------------------------------------------------------------------------

def create_draft(
    store,
    proposal: dict,
    *,
    default_source: str = "research_cycle.claude",
    registry_dir: Path = REGISTRY_DIR,
    persist_draft: bool = True,
) -> IntakeResult:
    """Validate `proposal`; on success, record the hypothesis claim and build
    a Contract with status="draft" — NOT locked, NOT preregistered. Raises
    IntakeRejected, with every problem found, on any validation failure or on
    referencing a hypothesis_id that was never actually issued. Nothing is
    written anywhere when that happens.
    """
    problems = validate_proposal(proposal)
    if problems:
        raise IntakeRejected(problems)

    hid = proposal.get("hypothesis_id")
    if hid is not None and not _hypothesis_exists(store, hid):
        raise IntakeRejected([
            f"hypothesis_id {hid!r} does not correspond to any previously "
            f"recorded hypothesis. Omit hypothesis_id to propose a new claim, "
            f"or pass the id exactly as it was returned from an earlier "
            f"create_draft() call."
        ])
    if hid is None:
        hid = rm.new_hypothesis_id()

    contract_id = _next_contract_id(store, hid)
    existing = {c.id: c for c in _registry(registry_dir)}
    prior = existing.get(contract_id)
    if prior is not None and prior.status != "draft":
        # Effectively unreachable now that ids are system-generated, but kept
        # as a defense-in-depth guard: a draft is never allowed to clobber an
        # already-locked file, whatever produced the collision.
        raise IntakeRejected([
            f"generated contract_id {contract_id!r} unexpectedly collides with "
            f"an already-{prior.status} contract in the registry — refusing "
            f"to overwrite it."
        ])

    source = proposal.get("source", default_source)
    rm.record_hypothesis_proposal(
        store, claim=proposal["hypothesis"], source=source,
        hypothesis_id=hid, extra={"contract_id": contract_id},
    )

    entry_rule_str = json.dumps(proposal["entry_rule"], sort_keys=True, separators=(",", ":"))
    exit_rule_str = json.dumps(proposal["exit_rule"], sort_keys=True, separators=(",", ":"))

    contract = Contract(
        id=contract_id,
        title=proposal["title"],
        hypothesis=proposal["hypothesis"],
        null_hypothesis=proposal["null_hypothesis"],
        universe=proposal["universe"],
        signal=proposal["signal"],
        entry_rule=entry_rule_str,
        exit_rule=exit_rule_str,
        splits=proposal["splits"],
        independence=proposal["independence"],
        falsification=proposal["falsification"],
        abandon_condition=proposal["abandon_condition"],
        evaluation_start=proposal["evaluation_start"],
        evaluation_end=proposal["evaluation_end"],
        llm_features=bool(proposal.get("llm_features", False)),
        llm_model_id=proposal.get("llm_model_id"),
        llm_knowledge_cutoff=proposal.get("llm_knowledge_cutoff"),
        notes=proposal.get("notes", ""),
    )
    # Contract() defaults status="draft" — left as-is; never call .lock() here.

    if persist_draft:
        contract.save(registry_dir)

    return IntakeResult(hypothesis_id=hid, contract=contract)


# ---------------------------------------------------------------------------
# Research Budget Governance (Slice O) — a structural ceiling on how many
# Contracts may enter the LOCKED state within a rolling period. Checked
# inside approve_and_lock(), immediately before the (still sole) Contract.
# lock() call below — draft creation (create_draft(), above) is completely
# unaffected, on purpose: the budget governs commitment (a locked,
# pre-registered, hash-frozen experiment), not exploration.
#
# This is a RESEARCH-governance control, not a trading/risk control: nothing
# in this section reads or writes position sizing, risk-per-trade, drawdown
# limits, portfolio limits, broker state, or anything under engine/ — it
# reads only the Contract registry (already imported above) and writes only
# through research.memory.record_research_note(), the same generic,
# already-existing audit mechanism approve_and_lock() already used for its
# ordinary "approved and locked by" note before this slice.
# ---------------------------------------------------------------------------

MAX_LOCKS_PER_PERIOD = 5
"""A deliberately conservative placeholder ceiling — this slice does not
assume a production-tuned number is known yet (per its own instructions).
Named and overridable per-call (approve_and_lock(..., max_locks=...) /
check_research_budget(..., max_locks=...)) rather than buried as a magic
number inside the gate itself."""

LOCK_BUDGET_PERIOD_DAYS = 7
"""Width, in days, of the trailing window the budget is measured over,
ending at `now`. A week, as a conservative starting point for a human-paced
review workflow — also overridable per-call."""


def locks_in_period(
    registry_dir: Path = REGISTRY_DIR,
    *,
    now: Optional[dt.datetime] = None,
    period_days: int = LOCK_BUDGET_PERIOD_DAYS,
) -> int:
    """How many Contracts actually entered the locked state within the
    trailing `period_days` window ending at `now`.

    "Actually entered the locked state" means status != "draft" — exactly
    research.contracts.comparison_count()'s own population. Contract status
    only ever moves forward from "draft" (draft -> locked -> running ->
    reported/abandoned, or -> superseded) and never back, so any contract
    whose CURRENT status is not "draft" was locked at some point, whatever
    has happened to it since. Abandoned and superseded contracts still
    count, deliberately, mirroring comparison_count()'s own explicit "a
    registry that quietly forgets its failures is a worse instrument than
    no registry" rule — an abandoned contract still consumed a lock, and
    still consumed research budget, whether or not the experiment finished
    cleanly. Drafts are never counted; create_draft() does not touch this
    number at all.

    Derived entirely by reading the current on-disk registry each call —
    there is no separately maintained counter that could drift out of sync
    with it (the slice's own explicit "do not rely on a manually maintained
    counter" requirement).

    `now` defaults to dt.datetime.now() — the same bare, naive, local-clock
    call Contract.lock() itself uses to stamp `locked_at` (research/
    contracts.py, not modified by this slice). `locked_at` is read exactly
    as Contract.lock() writes it, rather than being reinterpreted against
    research/store.py's separate, IST-aware now_ist()/iso() convention used
    everywhere else in research/ — mixing a naive and a timezone-aware
    datetime here would either raise or silently miscompare. This is a
    real, pre-existing inconsistency between two timestamp conventions in
    this repository (Contract.locked_at vs. research.store's IST
    convention); it is not introduced by this slice and Contract is out of
    scope to fix here — see the completion report.

    A contract whose `locked_at` is missing or fails to parse is excluded
    from the count rather than raising — the same defensive posture
    research/experiments/comparison.py's `_parsed()` already takes toward
    malformed/legacy data.
    """
    now = now or dt.datetime.now()
    window_start = now - dt.timedelta(days=period_days)
    count = 0
    for c in _registry(registry_dir):
        if c.status == "draft" or not c.locked_at:
            continue
        try:
            locked_at = dt.datetime.fromisoformat(c.locked_at)
        except (TypeError, ValueError):
            continue
        if window_start <= locked_at <= now:
            count += 1
    return count


def check_research_budget(
    registry_dir: Path = REGISTRY_DIR,
    *,
    now: Optional[dt.datetime] = None,
    max_locks: int = MAX_LOCKS_PER_PERIOD,
    period_days: int = LOCK_BUDGET_PERIOD_DAYS,
) -> tuple[bool, int]:
    """(within_budget, current_count) — current_count is locks_in_period()'s
    result; within_budget is whether ANOTHER lock may proceed right now.

    Policy, stated precisely and deliberately, because the slice explicitly
    asked for an intuitive "at budget" boundary to be chosen and documented
    rather than left ambiguous:

        within_budget  <=>  current_count < max_locks

    `current_count` is the number of locks ALREADY in the rolling period,
    measured BEFORE the lock currently being requested is added to it. With
    the default max_locks=5: the 1st through 5th locks in a period are each
    permitted (each one is checked while current_count is 0, 1, 2, 3, and 4
    respectively — all < 5), bringing the period's total to 5 once the 5th
    completes; the 6th is refused (checked while current_count is already
    5, which is not < 5). So max_locks is the maximum number of locks the
    period may ever contain — not "how many are allowed before this one" —
    which matches the slice's own stated budget model verbatim: `locks
    during a rolling period < configured maximum`, evaluated on the count
    as it stands immediately before this lock would join it.
    """
    count = locks_in_period(registry_dir, now=now, period_days=period_days)
    return count < max_locks, count


# ---------------------------------------------------------------------------
# approve_and_lock() — DRAFT -> HUMAN APPROVED -> LOCKED, atomically, and
# only given an explicit approved_by. The only function in this module that
# ever calls Contract.lock(). Now also the sole enforcement point for the
# research budget above (Slice O) — checked immediately before that same
# Contract.lock() call, never inside create_draft().
# ---------------------------------------------------------------------------

def approve_and_lock(
    store,
    contract_id: str,
    *,
    approved_by: str,
    registry_dir: Path = REGISTRY_DIR,
    budget_override_by: Optional[str] = None,
    budget_override_reason: Optional[str] = None,
    max_locks: int = MAX_LOCKS_PER_PERIOD,
    period_days: int = LOCK_BUDGET_PERIOD_DAYS,
    now: Optional[dt.datetime] = None,
) -> Contract:
    """Load the draft `contract_id` from `registry_dir`, lock it, save it, and
    write an audit note to research memory recording who approved it and
    when. Raises IntakeRejected — without changing anything on disk — if:
      - approved_by is missing or blank (a lock with no named approver is
        refused outright; this is the actual human gate)
      - no draft with that id exists in registry_dir
      - the contract is not currently a draft (already locked, or further
        along — approving twice is refused, not silently repeated)
      - the research budget (see check_research_budget() above) is
        exceeded and no valid `budget_override_by` was supplied (Slice O)
      - the contract fails Contract.check() (e.g. a model-firewall breach)

    Research budget gate (Slice O), evaluated after the checks above but
    strictly before Contract.lock() is called — a rejection here changes
    nothing: no registry mutation, no Contract hash mutation, no note
    written, exactly like every other IntakeRejected path in this function.

    `budget_override_by` is DELIBERATELY a separate field from
    `approved_by`, not a reuse of it. `approved_by` is already required for
    every lock, budget or no budget — treating its mere presence as budget
    authorization would make the gate a no-op. A lock is refused for being
    over budget unless a human supplies a second, distinct, non-empty
    `budget_override_by` naming who is authorizing the override; ordinary
    approval (however routine) can never silently satisfy it. Like
    `approved_by`, `budget_override_by` is a free-text identifier this
    module cannot structurally verify actually names a human rather than
    an automated process supplying an arbitrary string — that limitation is
    pre-existing and unchanged for `approved_by`, and is not solved here;
    it is documented rather than assumed away for both fields. What Slice O
    does add structurally is that no code path in this module (or in
    research/brain/investigator.py, which never imports or calls
    approve_and_lock at all) can supply `budget_override_by` on a caller's
    behalf — it is a plain keyword argument a human-driven call site must
    pass explicitly, every time.

    When the override is used, a SECOND, separate research-memory note is
    written — after the lock genuinely succeeds and is saved, never before
    (an override note is never written for a lock attempt that then fails
    Contract.check(), so the audit trail never claims an override happened
    when no lock actually did) — recording who overrode, when, how many
    locks were already in the period, the configured ceiling and window,
    and (if supplied) why.

    `now`, `max_locks`, and `period_days` exist for callers (chiefly tests)
    that need a deterministic, injectable notion of "the current governance
    clock" rather than the live wall clock — unrelated to research/store.py's
    as_of/knowledge-time concept, which gates backtested DATA visibility,
    not this operational governance decision.
    """
    if not approved_by or not approved_by.strip():
        raise IntakeRejected([
            "approved_by is required to lock a contract — a lock performed "
            "without naming who approved it is not permitted."])

    try:
        contract = Contract.load(contract_id, registry_dir)
    except FileNotFoundError:
        raise IntakeRejected([f"no draft contract {contract_id!r} found in {registry_dir}"])

    if contract.status != "draft":
        raise IntakeRejected([
            f"{contract_id} is not a draft (status={contract.status!r}); only "
            f"a draft can be approved and locked. If you intend to test a "
            f"variant, create a new draft instead of re-approving this one."])

    within_budget, current_count = check_research_budget(
        registry_dir, now=now, max_locks=max_locks, period_days=period_days)
    budget_overridden = False
    if not within_budget:
        if not budget_override_by or not budget_override_by.strip():
            raise IntakeRejected([
                f"research budget exceeded: {current_count} contract(s) already "
                f"locked in the trailing {period_days}-day period (limit "
                f"{max_locks}). Locking {contract_id} would exceed the "
                f"configured research budget. This is a research-governance "
                f"control, not a trading/risk control — it does not affect "
                f"position sizing, risk limits, drawdown limits, or live "
                f"execution. To lock anyway, an explicit human must supply "
                f"budget_override_by (a separate field from approved_by, "
                f"precisely so that an ordinary approval can never silently "
                f"bypass the research budget)."])
        budget_overridden = True

    try:
        contract.lock()
    except ContractViolation as e:
        raise IntakeRejected([f"{contract_id} failed to lock: {e}"])

    approved_at = dt.datetime.now().isoformat(timespec="seconds")
    note = f"Approved and locked by {approved_by} at {approved_at}."
    contract.notes = f"{contract.notes}\n{note}".strip() if contract.notes else note
    contract.save(registry_dir)

    rm.record_research_note(
        store, note=note, source="hypothesis_intake.approve_and_lock",
        extra={"contract_id": contract.id, "approved_by": approved_by,
               "locked_hash": contract.locked_hash},
    )

    if budget_overridden:
        override_note = (
            f"RESEARCH BUDGET OVERRIDE: {contract_id} locked by "
            f"{budget_override_by} despite {current_count} contract(s) "
            f"already locked in the trailing {period_days}-day period "
            f"(limit {max_locks}).")
        if budget_override_reason and budget_override_reason.strip():
            override_note += f" Reason: {budget_override_reason.strip()}"
        rm.record_research_note(
            store, note=override_note,
            source="hypothesis_intake.approve_and_lock.budget_override",
            extra={"contract_id": contract.id, "approved_by": approved_by,
                   "override_by": budget_override_by,
                   "override_reason": budget_override_reason,
                   "locks_in_period_before_this_one": current_count,
                   "max_locks": max_locks, "period_days": period_days},
        )

    return contract


def pending_drafts(registry_dir: Path = REGISTRY_DIR) -> list[Contract]:
    """Every contract in the registry still awaiting approve_and_lock() — the
    literal to-review queue for a human doing the review step."""
    return [c for c in _registry(registry_dir) if c.status == "draft"]


# ---------------------------------------------------------------------------
# intake() — thin convenience wrapper over create_draft() (+ approve_and_lock()
# only if explicitly asked for, with an explicit approver). Kept for simple
# scripts and tests; the two-call path above is the one intended for the real
# human-review workflow.
# ---------------------------------------------------------------------------

def intake(
    store,
    proposal: dict,
    *,
    lock: bool = False,
    approved_by: Optional[str] = None,
    default_source: str = "research_cycle.claude",
    registry_dir: Path = REGISTRY_DIR,
) -> IntakeResult:
    """create_draft(), and — only if `lock=True` AND `approved_by` is given —
    immediately approve_and_lock() the result. Defaults to lock=False: a bare
    `intake(store, proposal)` call always produces a draft, never a locked
    contract. There is no way to pass `lock=True` without also naming an
    approver; omitting `approved_by` while `lock=True` is itself rejected.
    """
    result = create_draft(store, proposal, default_source=default_source,
                           registry_dir=registry_dir)
    if not lock:
        return result
    if not approved_by:
        raise IntakeRejected([
            "lock=True requires approved_by — a draft is never locked without "
            "naming who approved it."])
    locked = approve_and_lock(store, result.contract.id, approved_by=approved_by,
                               registry_dir=registry_dir)
    return IntakeResult(hypothesis_id=result.hypothesis_id, contract=locked)


def intake_from_file(store, path: Union[str, Path], **kwargs) -> IntakeResult:
    """The human-review entry point: `path` is a JSON file a person has
    already read, per the mandated flow (proposal -> scratch JSON -> human
    review -> this function). Nothing calls this automatically. Forwards
    every kwarg to intake() — defaults to producing a draft only."""
    proposal = json.loads(Path(path).read_text())
    return intake(store, proposal, **kwargs)


# ---------------------------------------------------------------------------
# derive_split_contract() — closes the Contract.splits enforcement gap.
#
# THE GAP: a locked Contract carries `splits` (discovery/validation/holdout
# windows, each validated by _check_splits() above) but research/experiments/
# runner.py's simulate() only ever reads evaluation_start/evaluation_end. It
# never looks at `splits` at all. So a contract can declare a validation
# window and nothing in the pipeline ever actually walks it — the field is
# checked at intake and then silently ignored forever after. Vaibhav's own
# example fixture in tests/test_research_runner.py demonstrates the gap
# directly: splits={"discovery": ["2020-01-01","2020-12-31"]} while
# evaluation_start/evaluation_end are "2020-01-01"/"2020-06-30" — two
# different windows, and only the second one is ever actually run.
#
# THE FIX (Option B, approved over Option A — see the conversation this
# function was proposed in): rather than teach runner.simulate()/run_experiment
# to walk a `split` argument (which would need a `split` column added to
# experiment_results' (contract_id, trade_seq) uniqueness key in schema.sql,
# and SQLite's UNIQUE index treats NULL != NULL, which risks silently
# breaking idempotency for every already-recorded row), this function derives
# a SIBLING Contract from an already-locked parent: identical in every
# hashed field except `id` (system-minted, same as create_draft()) and
# `evaluation_start`/`evaluation_end` (replaced with the requested split's
# own window). The sibling is a completely ordinary Contract from every other
# module's point of view — it goes through the SAME approve_and_lock() this
# file already has, and then the SAME, completely unmodified,
# runner.run_experiment() / evaluator.record_verdict() / comparison /
# evidence_report pipeline. Zero changes anywhere else:
#   - research/schema.sql        untouched — no new column, no migration
#   - research/store.py          untouched
#   - research/contracts.py      untouched — Contract itself is generic
#   - research/experiments/runner.py     untouched — still only reads
#     evaluation_start/evaluation_end, which is now unambiguous per contract
#   - research/experiments/evaluator.py  untouched — resolve_hypothesis_id()/
#     contract_ids_for_hypothesis() already resolve a contract's hypothesis
#     purely from the hypothesis-claim log's `contract_id` field, and the
#     claim this function records below is written through the exact same
#     rm.record_hypothesis_proposal() call create_draft() already uses — so
#     the sibling is discovered as a variant of the same hypothesis with no
#     new reader code required anywhere.
#   - research/experiments/comparison.py untouched
#   - research/reports/evidence_report.py untouched
#
# ARCHITECTURAL NOTE (Vaibhav, on approving this): this mechanism is specific
# to the existing hypothesis/experiment research pipeline — a locked Contract
# is immutable by design, and deriving a sibling per declared split is the
# narrowest way to let that pipeline actually test discovery/validation/
# holdout separately without reopening anything frozen. It must NOT become
# the template for the future Strategy domain (StrategyVersion,
# StrategyBacktest) described in the Strategy Lab design review — that
# domain is deliberately a separate concept from Contract, with its own
# versioning and its own reasons for needing to run something more than once
# under different windows. Keep the two apart.
# ---------------------------------------------------------------------------

def derive_split_contract(
    store,
    parent_contract_id: str,
    split_key: str,
    hypothesis_id: str,
    *,
    registry_dir: Path = REGISTRY_DIR,
    persist_draft: bool = True,
) -> IntakeResult:
    """Build a new DRAFT Contract, a sibling of `parent_contract_id`, whose
    evaluation_start/evaluation_end are `parent.splits[split_key]` instead of
    the parent's own evaluation window — every other hashed field (universe,
    signal, entry_rule, exit_rule, splits itself, independence, falsification,
    abandon_condition, llm_* declaration, cost_model) is copied unchanged from
    the parent. The sibling is returned as a DRAFT: it is never auto-locked
    here, consistent with the rest of this module — call approve_and_lock()
    on its `.contract.id` separately, exactly like any other draft.

    `parent_contract_id` MUST already be locked. Deriving a split from a
    still-draft contract would mean testing a hypothesis that could still
    change — the entire point of locking. `split_key` MUST be a key the
    parent actually declared in its own `splits` (so this can never invent a
    window the original proposal never committed to), and MUST NOT be
    "discovery": the parent contract's own evaluation window already *is*
    its discovery-split test in the common case, and re-deriving "discovery"
    from itself is not a meaningful new experiment. `hypothesis_id` MUST be
    the hypothesis that actually owns `parent_contract_id` — checked against
    the same hypothesis-claim log _hypothesis_claims() already reads, so this
    needs no new store-reading helper anywhere in the codebase.

    Raises IntakeRejected, with every problem found, on any of the above; on
    success, records a new hypothesis-claim row (same call, same dataset, as
    create_draft()) so the sibling is discovered by every existing
    hypothesis->contract lookup with zero new code — its payload carries
    `split_of` and `split` in `extra` purely as additive traceability, so a
    human (or a future report) can tell a split-derived contract apart from
    an originally-proposed one without guessing from the id alone.

    SINGLE-USE VALIDATION/HOLDOUT SPLITS (Slice K). Every split_key that can
    reach this point is "validation" or "holdout" — "discovery" is refused
    above, and VALID_SPLIT_KEYS has no other member. Once ONE sibling has
    ever been derived for a given (parent_contract_id, split_key) pair, a
    second derivation for that exact pair is refused — permanently, and
    regardless of what happened to the first sibling: still a draft, still
    locked but never run, reported, or abandoned. This is deliberately
    stricter than "you can't rerun a finished one" (runner.py's
    load_runnable_contract() already guarantees that, unchanged by this
    slice): the point is that two INDEPENDENTLY RUNNABLE siblings for the
    same parent/split must never coexist, so nothing — human or AI — can
    derive several validation attempts up front and keep whichever result
    looks best. That is exactly the cherry-picking failure mode this
    mechanism exists to close, and closing it only after a second sibling
    has already been *run* would be too late: two locked-but-unrun siblings
    are already "multiple independently runnable validation siblings," the
    thing that must be impossible.

    An ABANDONED first attempt is NOT forgiven — it still blocks a second
    derivation, on purpose. `research.contracts.comparison_count()` already
    established this project's position on this exact question for the
    wider multiple-comparisons ledger: an abandoned contract still counts,
    because "a registry that quietly forgets its failures is a worse
    instrument than no registry." The same reasoning applies here, and
    matters more, not less, when the failure is a run exception rather than
    an unfavorable result: today `abandoned` is set by exactly one code path
    (run_experiment()'s except-block, on any exception raised during
    simulation/evaluation — see runner.py), so forgiving it would open a
    concrete, easy-to-trigger retry loop — get (or induce) a technical
    failure, then derive again — that a policy based on the RESULT looking
    bad would not even need. The documented, deliberate cost of this choice:
    a validation run that fails because of a real bug (since fixed) has no
    automatic retry for that exact (parent, split) pair either. The existing
    remedy is the same one this system already requires for amending any
    other locked contract: register a new hypothesis and a new parent
    contract if the claim genuinely needs to be tested again.
    """
    try:
        parent = Contract.load(parent_contract_id, registry_dir)
    except FileNotFoundError:
        raise IntakeRejected(
            [f"no contract {parent_contract_id!r} found in {registry_dir}"])

    problems: list[str] = []

    # Fetched once, reused by both the single-use check below and the
    # ownership check further down — one call, same as before this slice,
    # just no longer computed twice.
    claims = _hypothesis_claims(store, hypothesis_id)

    # The invariant that actually matters is "has this contract ever been
    # locked" (locked_hash set), not "is it status=='locked' right now" — a
    # contract's hashed fields (including `splits` itself) are frozen the
    # moment approve_and_lock() sets locked_hash, and stay frozen through
    # "running"/"reported"/"abandoned" exactly as much as while "locked";
    # only run_experiment() ever advances status past "locked", and it never
    # touches a hashed field when it does. So a sibling may be derived from
    # a parent in any of those four post-lock states — which is also the
    # ordinary case: derive the validation sibling *after* seeing the
    # discovery-window contract through to "reported". Only "draft" (never
    # locked, still editable) is refused.
    if parent.status == "draft" or not parent.locked_hash:
        problems.append(
            f"{parent_contract_id} has never been locked (status="
            f"{parent.status!r}); only a locked contract's splits are frozen "
            f"enough to derive a sibling from. Lock it with approve_and_lock() "
            f"first.")

    if split_key == "discovery":
        problems.append(
            'split_key must not be "discovery" — the parent contract\'s own '
            "evaluation_start/evaluation_end already represent its test of "
            "the discovery split; deriving a sibling from that same split "
            "would just duplicate the parent, not test anything new.")
    elif split_key not in VALID_SPLIT_KEYS:
        problems.append(
            f"split_key {split_key!r} is not a recognized split "
            f"(valid: {sorted(VALID_SPLIT_KEYS)})")
    elif split_key not in (parent.splits or {}):
        problems.append(
            f"{parent_contract_id} never declared a {split_key!r} split in "
            f"its own splits ({sorted((parent.splits or {}).keys())}) — a "
            f"sibling can only be derived for a window the parent actually "
            f"pre-registered.")
    else:
        window = parent.splits[split_key]
        if not (isinstance(window, list) and len(window) == 2
                and _parse_date(window[0]) and _parse_date(window[1])):
            problems.append(
                f'{parent_contract_id}.splits[{split_key!r}] is not a valid '
                f"[start, end] ISO-date pair: {window!r}")
        else:
            # SINGLE-USE GUARD (Slice K) — see the docstring's "SINGLE-USE
            # VALIDATION/HOLDOUT SPLITS" section for the full reasoning,
            # including why an abandoned first attempt still blocks a
            # second derivation.
            prior_sibling_id = _prior_split_derivation(claims, parent_contract_id, split_key)
            if prior_sibling_id is not None:
                problems.append(
                    f"{parent_contract_id}'s {split_key!r} split has already "
                    f"been derived once, as {prior_sibling_id!r}. Each "
                    f"(parent contract, split) pair may be derived at most "
                    f"once — this is what makes a validation or holdout "
                    f"result mean something, rather than something to retry "
                    f"until one attempt clears the bar. If {prior_sibling_id!r} "
                    f"failed to run, was abandoned, or came back unfavorable, "
                    f"that is evidence about the hypothesis, not a reason to "
                    f"retry the same split — register a new hypothesis and a "
                    f"new parent contract if the claim genuinely needs to be "
                    f"tested again, exactly as this system already requires "
                    f"for amending any other locked contract.")

    owns = any(r["payload"].get("contract_id") == parent_contract_id for r in claims)
    if not owns:
        problems.append(
            f"hypothesis_id {hypothesis_id!r} is not recorded as owning "
            f"{parent_contract_id!r} — pass the hypothesis_id that "
            f"create_draft() actually returned alongside this contract.")

    if problems:
        raise IntakeRejected(problems)

    window = parent.splits[split_key]
    new_id = _next_contract_id(store, hypothesis_id)
    existing = {c.id: c for c in _registry(registry_dir)}
    prior = existing.get(new_id)
    if prior is not None and prior.status != "draft":
        # Same defense-in-depth guard create_draft() carries — effectively
        # unreachable since ids are system-generated, kept anyway.
        raise IntakeRejected([
            f"generated contract_id {new_id!r} unexpectedly collides with "
            f"an already-{prior.status} contract in the registry — refusing "
            f"to overwrite it."
        ])

    sibling = _replace(
        parent,
        id=new_id,
        evaluation_start=window[0],
        evaluation_end=window[1],
        status="draft",
        locked_at=None,
        locked_hash=None,
        notes="",
        research_debt=list(parent.research_debt),
    )

    rm.record_hypothesis_proposal(
        store, claim=parent.hypothesis,
        source="hypothesis_intake.derive_split_contract",
        hypothesis_id=hypothesis_id,
        extra={
            "contract_id": new_id,
            "split_of": parent_contract_id,
            "split": split_key,
        },
    )

    if persist_draft:
        sibling.save(registry_dir)

    return IntakeResult(hypothesis_id=hypothesis_id, contract=sibling)
