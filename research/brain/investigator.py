"""
Research Investigator — Phase 2 Slice I (Research AI v0).

Activates the seam `digest.py`'s and `observatory.py`'s own docstrings both
call out as "the next slice, not built yet": a bounded AI call that reads the
deterministic research digest and proposes ONE hypothesis. This module is
that call, and nothing more.

    build_digest(store, as_of)        research/brain/digest.py — UNCHANGED
          |
          v
    build_prompt()                    fixed instructions + digest JSON,
          |                           nothing else goes in
          v
    runner(prompt)                    the Research AI process — injectable;
          |                           production default shells out to the
          |                           same `claude -p ...` mechanism
          |                           run_cycle.sh already uses, as its own
          |                           separate process, scoped narrower
          v
    parse_ai_output()                 strict JSON parse only — no eval, no
          |                           exec, no compile, anywhere in this file
          v
    submit_proposal()                 hypothesis_intake.validate_proposal()
          |                           then hypothesis_intake.create_draft() —
          |                           BOTH REUSED UNCHANGED, no second rule
          |                           language, no new validation logic
          v
         DRAFT                        never locked. approve_and_lock is never
          |                           imported, referenced, or reachable from
          |                           anywhere in this module.
          v
    record_discovery_search()         Phase 2 Slice N — a minimal provenance
                                       row linking the digest `as_of` snapshot
                                       and this module's version to the
                                       resulting hypothesis_id. Written ONLY
                                       after a genuine DRAFT exists; never for
                                       a NoProposal or a rejected/malformed
                                       AI response (see submit_proposal()'s
                                       docstring — nothing is written on any
                                       exception path either).

What this module does NOT do, structurally (it imports none of the modules
that could do these things, so this is enforced by absence — the same
pattern hypothesis_intake.py itself uses, and the same pattern
tests/test_kernel_isolation.py verifies for the rest of research/):
  - import engine, engine.execute, engine.guardrails, engine.journal, or any
    broker module
  - touch memory/state.json, memory/guardrails.md, or any live trading state
  - call approve_and_lock() or Contract.lock() — this module has no
    reference to either name
  - run an experiment, compute a verdict, or touch research/experiments/* at
    all
  - query research memory or the contract registry directly. The ONLY read
    this module performs against the store is the single build_digest() call
    in build_context() — everything downstream of that operates on the
    dict digest() returns, never on the store again. That preserves
    Store/AsOfView -> Digest -> AI, not AI -> arbitrary database exploration.

Phase 2 Slice N adds exactly one write beyond what Slice I already did:
after a genuine DRAFT is created, investigate() calls
research.memory.record_discovery_search() — a plain append to research
memory, the same kind of write hypothesis_intake.create_draft() itself
already performs for the hypothesis claim. This is provenance ONLY: it
carries no ability to approve a hypothesis, lock a Contract, run an
experiment, modify an existing Contract, or touch engine/broker/guardrail
state, because it does nothing but call research.memory.append() through
that one typed wrapper. It records the digest `as_of` string and this
module's own RESEARCH_AI_VERSION/prompt path — never the digest body itself
(build_digest(store, as_of) already reproduces that later, deterministically,
from `as_of` alone) — and it is written after submit_proposal() has already
succeeded, so a rejected or malformed AI response creates no discovery
record, misleading or otherwise.

Permission boundary, stated precisely rather than assumed
-----------------------------------------------------------
Three layers, of different strength, and this module is honest about which
ones it can actually prove:

  1. `.claude/settings.json`'s EXISTING deny list (unmodified by this slice)
     already blocks Edit on engine/guardrails.py, engine/execute.py,
     memory/state.json, memory/trades.jsonl, memory/regime_log.jsonl, and
     both Read and Edit on .env — for ANY Claude Code process invoked from
     this project root, because that settings file is auto-loaded from the
     project root regardless of which script launched the process. The
     Research AI runner inherits this for free, without this slice touching
     that file.
  2. The Research AI subprocess (`_default_runner`) is its own process,
     launched with its own prompt file (routines/research_investigate.md,
     never run_cycle.sh's live prompts), scoped via `--add-dir` to
     `research/` and `routines/` only — narrower than the live agent's
     whole-project `--add-dir`. Whether the `claude` CLI's own runtime
     actually prevents the AI's free-form tool calls from reaching outside
     that scope is NOT something a unit test in this repository can verify
     (it would require invoking the real binary, which the test mandate for
     this slice explicitly forbids) — flagged here rather than overstated.
  3. This module's own Python import graph, which IS fully verified by
     tests/test_research_investigator.py the same way test_kernel_isolation
     verifies the rest of research/: zero imports of engine, engine.execute,
     engine.guardrails, engine.journal, or any broker module, anywhere in
     this file. This is the layer this slice can actually prove, and it is
     also the layer that determines what the Python BRIDGE code (as opposed
     to the AI's own tool calls) is capable of regardless of how the CLI
     sandboxes the subprocess — the bridge simply has no code path to
     engine/ or live state to begin with.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable, NamedTuple, Optional

from .. import memory as rm
from . import hypothesis_intake as hi
from .digest import build_digest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
PROMPT_PATH = PROJECT_ROOT / "routines" / "research_investigate.md"

# Directories the Research AI subprocess is granted, beyond its own cwd —
# deliberately narrower than run_cycle.sh's live-agent invocation, which
# grants the whole project directory. See the permission-boundary note above
# for what this can and cannot be proven to enforce.
RESEARCH_AI_ADD_DIRS = ("research", "routines")
RESEARCH_AI_TIMEOUT_SECONDS = 300
CLAUDE_BINARY = "claude"

DEFAULT_SOURCE = "research_investigator.ai"

# Phase 2 Slice N — this module's own discovery-provenance identity. Bump
# RESEARCH_AI_VERSION by hand whenever a change here (or to
# routines/research_investigate.md) would matter for reproducing a past
# discovery run — e.g. the prompt's instructions change, or build_prompt()'s
# framing of the digest changes. Deliberately a single plain string, the
# same convention research/store.py's own SCHEMA_VERSION already uses,
# rather than a new versioning scheme: see research.memory.
# record_discovery_search()'s docstring for why this slice does not build a
# general AI/discovery-mechanism registry around it.
RESEARCH_AI_VERSION = "investigator.v2"
DISCOVERY_TYPE_RESEARCH_AI = "research_ai"


class InvestigatorError(RuntimeError):
    """The Research AI boundary itself failed: the process could not be
    invoked, timed out, or returned something that isn't even parseable
    JSON. Distinct from hypothesis_intake.IntakeRejected, which means the AI
    returned well-formed JSON that fails the EXISTING content validation —
    a proposal problem, not a boundary problem. Nothing is written to
    research memory or the registry in either case; every step between the
    digest and a successful create_draft() call is either a pure function or
    raises before writing anything."""


class InvestigatorResult(NamedTuple):
    hypothesis_id: str
    contract_id: str
    raw_ai_output: str


class NoProposal(NamedTuple):
    """Returned instead of InvestigatorResult when the AI explicitly
    declines to propose anything this run (see routines/research_investigate
    .md's `{"no_proposal": true, ...}` escape hatch). Not an error — a
    Research AI run producing nothing is a normal, expected outcome, not a
    failure of the boundary or of validation."""

    reason: str


class DuplicateProposal(NamedTuple):
    """Returned instead of InvestigatorResult when the caller-supplied
    `duplicate_check` (Phase 2 Slice P — see investigate()'s docstring)
    reports that this proposal's rule specification already exists as
    `existing_contract_id` elsewhere in the registry. Not an error, the
    same way NoProposal is not an error — a discovery process correctly
    declining to draft a redundant experiment is a normal, expected
    outcome. Nothing is written anywhere for this outcome: no draft, no
    hypothesis claim, no provenance — submit_proposal() is never reached."""

    existing_contract_id: str
    raw_ai_output: str


# ---------------------------------------------------------------------------
# Step 1 — the deterministic input boundary. The ONLY store read this module
# performs. Nothing downstream of this dict is allowed to go back to the
# store for more.
# ---------------------------------------------------------------------------

def build_context(store, as_of) -> dict:
    """The Research AI's entire view of the world: digest.build_digest(),
    completely unmodified. This function exists only so investigate() has
    one obvious place to point at — it must never grow a second query path
    alongside it."""
    return build_digest(store, as_of)


# ---------------------------------------------------------------------------
# Step 2 — prompt construction. Fixed instruction file + digest JSON, and
# nothing else. The digest is embedded as fenced DATA, never interpolated
# into instruction text, so nothing inside a past hypothesis's free-text
# field or an anomaly's payload can be read as a new instruction.
# ---------------------------------------------------------------------------

def build_prompt(digest: dict, *, prompt_path: Path = PROMPT_PATH) -> str:
    """Deterministic given a deterministic digest: same digest in, same
    prompt text out, every time (json.dumps with sort_keys=True, same as
    digest.to_json() already uses for the same reason)."""
    instructions = prompt_path.read_text()
    digest_json = json.dumps(digest, indent=2, sort_keys=True, default=str)
    return (
        f"{instructions}\n\n"
        f"```json\n{digest_json}\n```\n"
    )


# ---------------------------------------------------------------------------
# Step 3 — invoke the Research AI. Injectable: every test in this slice
# passes its own `runner` and never shells out for real, per the mandate not
# to exercise real Claude network/model behavior in unit tests.
# ---------------------------------------------------------------------------

def _default_runner(prompt: str) -> str:
    """The real Research AI process — its own identity, its own prompt, its
    own (narrower) --add-dir scope. Reuses exactly the invocation shape
    run_cycle.sh already uses (`claude -p <prompt> --permission-mode
    acceptEdits --add-dir <dir>`), never run_cycle.sh itself, and never the
    live agent's prompts, briefing, or session. Not exercised by any test in
    this slice."""
    cmd = [CLAUDE_BINARY, "-p", prompt, "--permission-mode", "acceptEdits"]
    for d in RESEARCH_AI_ADD_DIRS:
        cmd += ["--add-dir", str(PROJECT_ROOT / d)]
    try:
        proc = subprocess.run(
            cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            timeout=RESEARCH_AI_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as e:
        raise InvestigatorError(
            f"Research AI process timed out after "
            f"{RESEARCH_AI_TIMEOUT_SECONDS}s") from e
    except OSError as e:
        raise InvestigatorError(f"could not invoke Research AI process: {e}") from e

    if proc.returncode != 0:
        raise InvestigatorError(
            f"Research AI process exited {proc.returncode}: "
            f"{(proc.stderr or '')[:500]}")
    return proc.stdout


# ---------------------------------------------------------------------------
# Step 4 — parse. Strict JSON only. No eval, no exec, no compile — searched
# for and asserted absent from this whole file by
# tests/test_research_investigator.py, the same way
# tests/test_research_engine_boundary.py already asserts it for the rest of
# research/brain/.
# ---------------------------------------------------------------------------

def parse_ai_output(raw: str) -> dict:
    """Turn the Research AI's raw stdout into a dict. The only parsing
    performed is json.loads on a single JSON object, optionally wrapped in a
    ```json fence — nothing more permissive is attempted. A response that
    isn't clean, single-JSON-object output is rejected outright as an
    InvestigatorError: fail closed, not "try to salvage it"."""
    text = (raw or "").strip()

    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    if not text:
        raise InvestigatorError("Research AI returned an empty response")

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        raise InvestigatorError(
            f"Research AI response is not valid JSON: {e}") from e

    if not isinstance(parsed, dict):
        raise InvestigatorError(
            f"Research AI response must be a single JSON object, got "
            f"{type(parsed).__name__}")

    return parsed


# ---------------------------------------------------------------------------
# Step 5 — the thin bridge. Two calls, both already built and tested by
# Slice C: hypothesis_intake.validate_proposal() (called explicitly here,
# AND again inside create_draft() itself — belt and suspenders, not
# either/or, matching hypothesis_intake.py's own stated philosophy), then
# hypothesis_intake.create_draft(). No new validation logic, no second rule
# language. approve_and_lock is never imported into this module and is not
# reachable from any function in it.
# ---------------------------------------------------------------------------

def submit_proposal(
    store,
    proposal: dict,
    *,
    registry_dir: Path = hi.REGISTRY_DIR,
    default_source: str = DEFAULT_SOURCE,
    persist_draft: bool = True,
) -> hi.IntakeResult:
    """The entire bridge from an accepted Research AI proposal to a DRAFT
    Contract. Raises hi.IntakeRejected — with every problem found, writing
    nothing — for any proposal that fails hypothesis_intake's existing
    content validation, including an unsupported metric/operator (the DSL
    firewall) or a missing/malformed field (the schema firewall). Never
    calls, imports, or references approve_and_lock."""
    problems = hi.validate_proposal(proposal)
    if problems:
        raise hi.IntakeRejected(problems)
    return hi.create_draft(
        store, proposal, default_source=default_source,
        registry_dir=registry_dir, persist_draft=persist_draft,
    )


# ---------------------------------------------------------------------------
# Orchestration — the one public entry point most callers want.
# ---------------------------------------------------------------------------

def investigate(
    store,
    as_of,
    *,
    runner: Optional[Callable[[str], str]] = None,
    registry_dir: Path = hi.REGISTRY_DIR,
    persist_draft: bool = True,
    digest: Optional[dict] = None,
    duplicate_check: Optional[Callable[[dict], Optional[str]]] = None,
):
    """The whole seam, end to end — see the module docstring's diagram.

    Returns an InvestigatorResult on a successful DRAFT, a NoProposal when
    the AI explicitly declined (routines/research_investigate.md's
    `{"no_proposal": true, ...}` escape hatch), or a DuplicateProposal when
    `duplicate_check` reports the proposal already exists (Phase 2 Slice P
    — see below). All three are normal outcomes, not errors.

    Raises InvestigatorError for any boundary failure (empty/non-JSON/
    malformed-shape AI output, a runner exception, a timeout) and
    hi.IntakeRejected for a well-formed proposal that fails hypothesis_
    intake's existing content validation. Neither exception writes anything:
    create_draft() validates to completion before any write, and every step
    before it in this module is a pure function or a call that itself raises
    before writing.

    Two optional Phase 2 Slice P parameters, both additive and inert unless
    supplied (every Slice I/N caller is unaffected):

    `digest`, if given, is used as-is instead of calling build_context()
    (i.e. build_digest()) here. Exists so a caller making several bounded
    attempts in one run (research/overnight.py) can build the digest ONCE
    for the whole run and pass the identical snapshot to every attempt,
    rather than each attempt re-querying research memory and potentially
    seeing a digest that already reflects an earlier attempt's own draft
    from the same run. When omitted (the default), behavior is byte-for-
    byte identical to before this parameter existed — this module still
    performs exactly one store read, via build_context().

    `duplicate_check`, if given, is called with the parsed `proposal` dict
    after the no_proposal check and before submit_proposal() — i.e. before
    any draft would be created. A non-None return is treated as the
    contract_id of an existing Contract this proposal would exactly
    duplicate, and investigate() returns DuplicateProposal instead of
    creating anything. This module does not implement the duplicate check
    itself and imports nothing from research/experiments/* to do so — the
    same "narrow import graph" invariant this module has always kept (see
    the module docstring). The actual fingerprint logic lives entirely in
    the caller supplying this callable (research/overnight.py reuses
    research.experiments.comparison._rule_fingerprint, the same function
    research/brain/similarity.py's global duplicate detection already uses
    — see that module for why this is "reuse, don't reimplement").
    """
    runner = runner or _default_runner
    digest = digest if digest is not None else build_context(store, as_of)
    prompt = build_prompt(digest)

    try:
        raw = runner(prompt)
    except InvestigatorError:
        raise
    except Exception as e:  # a broken injected runner fails closed, not open
        raise InvestigatorError(f"Research AI invocation failed: {e}") from e

    proposal = parse_ai_output(raw)

    if proposal.get("no_proposal") is True:
        reason = proposal.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            reason = "(no reason given)"
        return NoProposal(reason=reason)

    if duplicate_check is not None:
        existing_id = duplicate_check(proposal)
        if existing_id:
            return DuplicateProposal(existing_contract_id=existing_id, raw_ai_output=raw)

    result = submit_proposal(
        store, proposal, registry_dir=registry_dir, persist_draft=persist_draft,
    )

    # Phase 2 Slice N — provenance, recorded only now that submit_proposal()
    # has genuinely succeeded (it raises hi.IntakeRejected, before writing
    # anything, for any proposal that fails validation — so this line is
    # never reached for a rejected or malformed AI response). Records the
    # digest's own `as_of` string, not the digest body — build_digest(store,
    # as_of) reproduces the same digest later from that string alone.
    rm.record_discovery_search(
        store,
        hypothesis_id=result.hypothesis_id,
        discovery_type=DISCOVERY_TYPE_RESEARCH_AI,
        as_of=digest["as_of"],
        version=RESEARCH_AI_VERSION,
        source=DEFAULT_SOURCE,
        extra={"prompt_path": str(PROMPT_PATH.relative_to(PROJECT_ROOT))},
    )

    return InvestigatorResult(
        hypothesis_id=result.hypothesis_id,
        contract_id=result.contract.id,
        raw_ai_output=raw,
    )
