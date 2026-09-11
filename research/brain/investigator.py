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
                                       docstring — RESEARCH MEMORY and the
                                       REGISTRY get no write on any exception
                                       path). Priority Task 0 (operational
                                       stabilization) adds the ONE exception
                                       to that: an InvestigatorError also
                                       appends one bounded diagnostic row to
                                       AI_FAILURE_LOG (research/ai_failures.
                                       jsonl) via _record_ai_failure() — see
                                       that constant's own docstring for why
                                       this is operational telemetry, not a
                                       second research/registry write.

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
import os
import subprocess
from pathlib import Path
from typing import Callable, NamedTuple, Optional

from .. import memory as rm
from ..store import now_ist, iso
from . import hypothesis_intake as hi
from .digest import build_digest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
PROMPT_PATH = PROJECT_ROOT / "routines" / "research_investigate.md"

# Priority Task 0 (operational stabilization) — a bounded, append-only
# diagnostic log for exactly ONE thing: an InvestigatorError, with enough
# context to understand WHY it happened without re-running anything. This
# is deliberately NOT research memory and NOT the registry (the module
# docstring's "nothing is written in either case" claim is about those two
# specific stores, and stays true) — it is operational telemetry, the same
# category and convention as research/worker_runs.jsonl /
# research/recorder_runs.jsonl, just for the one failure mode those files
# never had room to describe: what the Research AI actually said.
AI_FAILURE_LOG = PROJECT_ROOT / "research" / "ai_failures.jsonl"
AI_FAILURE_RAW_EXCERPT_MAX = 4000
"""How much of a raw AI response `_record_ai_failure()` will persist —
bounded so a single pathological response (or a run of them) cannot grow
this file without limit. Diagnostic excerpt, not a full archive."""

# Directories the Research AI subprocess is granted, beyond its own cwd —
# deliberately narrower than run_cycle.sh's live-agent invocation, which
# grants the whole project directory. See the permission-boundary note above
# for what this can and cannot be proven to enforce.
RESEARCH_AI_ADD_DIRS = ("research", "routines")
RESEARCH_AI_TIMEOUT_SECONDS = 300

# The Claude Code executable the Research AI subprocess invokes. A bare
# "claude" resolves via the invoking process's PATH — which is exactly what
# cron does not have (cron's PATH is minimal and does not include
# ~/.local/bin, where the Claude Code installer puts the binary; run_cycle.sh
# hits the identical issue and works around it with its own explicit
# CLAUDE_BIN lookup). ENV_CLAUDE_BIN lets that path be configured per
# deployment without editing code; DEFAULT_CLAUDE_BIN is this deployment's
# known-good absolute path (the VPS's cron user is root). Both are read at
# call time inside resolve_claude_binary(), never cached at import, so a
# fix takes effect on the very next heartbeat.
ENV_CLAUDE_BIN = "RESEARCH_AI_CLAUDE_BIN"
DEFAULT_CLAUDE_BIN = "/root/.local/bin/claude"


def resolve_claude_binary() -> str:
    """The absolute path to the Claude Code executable, deterministically:
    `RESEARCH_AI_CLAUDE_BIN` if set (stripped of surrounding whitespace),
    else `DEFAULT_CLAUDE_BIN`. Never falls back to a bare "claude" — that
    would silently reintroduce the PATH dependency this function exists to
    remove."""
    override = (os.environ.get(ENV_CLAUDE_BIN) or "").strip()
    return override or DEFAULT_CLAUDE_BIN


def _validate_claude_binary(path: str) -> None:
    """Fail closed, with a clear and actionable message, before ever calling
    subprocess.run — a bare `[Errno 2] No such file or directory: 'claude'`
    tells nobody that the fix is to set RESEARCH_AI_CLAUDE_BIN. Not cached:
    checked once per _default_runner() call, so re-installing or re-linking
    the binary is picked up on the very next heartbeat with no code change.
    Raises InvestigatorError (never a bare OSError) — the same boundary-
    failure type every other _default_runner() failure raises, so a caller
    never needs a second except clause for this."""
    p = Path(path)
    if not p.is_absolute():
        raise InvestigatorError(
            f"{ENV_CLAUDE_BIN} (or the default) must be an absolute path, got "
            f"{path!r} — a bare command name depends on the invoking process's "
            f"PATH, which is exactly what breaks this under cron.",
            stage="binary_config")
    if not p.exists():
        raise InvestigatorError(
            f"Claude Code executable not found at {path!r}. Set {ENV_CLAUDE_BIN} "
            f"to the correct absolute path (default: {DEFAULT_CLAUDE_BIN!r}).",
            stage="binary_config")
    if not os.access(p, os.X_OK):
        raise InvestigatorError(
            f"Claude Code executable at {path!r} is not executable "
            f"(chmod +x it, or set {ENV_CLAUDE_BIN} to a runnable path).",
            stage="binary_config")


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
    raises before writing anything.

    `stage` names WHERE in the boundary this failed — one of:
    "binary_config" (the configured Claude Code executable is missing/not
    absolute/not executable), "timeout", "process_error" (non-zero exit),
    "invocation_error" (the subprocess/runner itself raised), "empty_response",
    "malformed_json", or "not_object" (valid JSON, but not a single object).
    Defaults to "unknown" so every pre-existing single-argument
    `InvestigatorError("...")` construction (including in already-published
    tests) stays valid. `raw_response`, if given, is the untouched raw text
    the Research AI actually produced — kept on the exception instance only
    long enough for `investigate()` to hand it to `_record_ai_failure()`
    (a bounded, local diagnostic log — see AI_FAILURE_LOG above); this
    exception is never itself persisted anywhere with the raw text attached."""

    def __init__(self, message: str, *, stage: str = "unknown",
                raw_response: Optional[str] = None) -> None:
        super().__init__(message)
        self.stage = stage
        self.raw_response = raw_response


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
    run_cycle.sh already uses (`<claude_bin> -p <prompt> --permission-mode
    acceptEdits --add-dir <dir>`), never run_cycle.sh itself, and never the
    live agent's prompts, briefing, or session. The executable path is
    resolved via resolve_claude_binary() (RESEARCH_AI_CLAUDE_BIN, else
    DEFAULT_CLAUDE_BIN) and validated before invocation — never a bare
    "claude" looked up on PATH. Not exercised by any test in this slice
    (every test injects its own `runner`); resolve_claude_binary() and
    _validate_claude_binary() ARE tested directly."""
    claude_bin = resolve_claude_binary()
    _validate_claude_binary(claude_bin)
    cmd = [claude_bin, "-p", prompt, "--permission-mode", "acceptEdits"]
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
            f"{RESEARCH_AI_TIMEOUT_SECONDS}s", stage="timeout") from e
    except OSError as e:
        raise InvestigatorError(
            f"could not invoke Research AI process: {e}", stage="invocation_error") from e

    if proc.returncode != 0:
        raise InvestigatorError(
            f"Research AI process exited {proc.returncode}: "
            f"{(proc.stderr or '')[:500]}", stage="process_error")
    return proc.stdout


# ---------------------------------------------------------------------------
# Step 4 — parse. Strict JSON only. No eval, no exec, no compile — searched
# for and asserted absent from this whole file by
# tests/test_research_investigator.py, the same way
# tests/test_research_engine_boundary.py already asserts it for the rest of
# research/brain/.
# ---------------------------------------------------------------------------

def _extract_json_object(text: str) -> Optional[str]:
    """If `text` is a single JSON object with extra, harmless characters
    around it (a stray sentence before/after, trailing whitespace, an
    unfenced "Here is my proposal:" preamble), return just the object's own
    substring — from its first `{` to the LAST `}` in the whole text.
    Returns None when there's nothing to trim (no braces at all, or the
    substring is the whole text already) or the substring couldn't possibly
    be a complete object (no closing brace at all).

    Purely structural — brace POSITION only. This never edits, completes,
    or guesses at the JSON body itself; the caller runs the exact same
    strict `json.loads` on whatever this returns, and a result that still
    isn't valid JSON is still rejected outright. Not a fuzzy parser: text
    containing more than one top-level object, or a brace character
    appearing in the surrounding prose itself, is explicitly out of scope
    for this narrow fallback — it will either extract the wrong span (and
    then fail the same strict parse) or return None."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    candidate = text[start:end + 1]
    if candidate == text:
        return None  # nothing to trim — the direct parse attempt already covers this
    return candidate


def parse_ai_output(raw: str) -> dict:
    """Turn the Research AI's raw stdout into a dict. The only parsing
    performed is json.loads on a single JSON object, optionally wrapped in a
    ```json fence, optionally surrounded by harmless extra text a direct
    parse would otherwise reject (see _extract_json_object() above) —
    nothing more permissive than that is attempted. A response that still
    isn't a clean, single-JSON-object once that narrow trimming is applied
    is rejected outright as an InvestigatorError: fail closed, not "try to
    salvage it". Every raised InvestigatorError here carries `raw_response`
    set to the ORIGINAL, untouched `raw` argument (not the stripped/fenced/
    trimmed `text`), so a caller logging it for diagnostics sees exactly
    what the Research AI produced."""
    text = (raw or "").strip()

    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    if not text:
        raise InvestigatorError(
            "Research AI returned an empty response", stage="empty_response",
            raw_response=raw)

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        candidate = _extract_json_object(text)
        if candidate is None:
            raise InvestigatorError(
                f"Research AI response is not valid JSON: {e}",
                stage="malformed_json", raw_response=raw) from e
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            # The trimmed candidate is STILL not valid JSON — report the
            # ORIGINAL error against the ORIGINAL text, not the failed
            # extraction attempt; trying to salvage it further is exactly
            # the "generic fuzzy parser" this function must not become.
            raise InvestigatorError(
                f"Research AI response is not valid JSON: {e}",
                stage="malformed_json", raw_response=raw) from e

    if not isinstance(parsed, dict):
        raise InvestigatorError(
            f"Research AI response must be a single JSON object, got "
            f"{type(parsed).__name__}", stage="not_object", raw_response=raw)

    return parsed


def _record_ai_failure(error: InvestigatorError, *, log_path: Path = AI_FAILURE_LOG) -> None:
    """Best-effort diagnostic record of one InvestigatorError — see
    AI_FAILURE_LOG's own module-level docstring for why this exists and
    what it deliberately is NOT (research memory, the registry, a second
    source of truth). Persists the failure `stage`, the exception message,
    and a BOUNDED excerpt of the raw response (never the full text, capped
    at AI_FAILURE_RAW_EXCERPT_MAX) — enough to distinguish "empty", "prose,
    no JSON", "truncated JSON", "JSON but the wrong shape", etc. from each
    other without re-running anything. Never raises: a failure to log a
    diagnostic about a failure must never itself become a second, different
    failure, so any exception here (a full disk, a permissions problem) is
    silently swallowed, the same posture every other best-effort telemetry
    write in this codebase already takes (research.brain.worker._persist_run,
    research.recorder._persist_run)."""
    try:
        excerpt = None
        raw_len = 0
        truncated = False
        if error.raw_response is not None:
            raw_len = len(error.raw_response)
            truncated = raw_len > AI_FAILURE_RAW_EXCERPT_MAX
            excerpt = error.raw_response[:AI_FAILURE_RAW_EXCERPT_MAX]
        row = {
            "ts": iso(now_ist()), "stage": error.stage, "message": str(error),
            "raw_response_excerpt": excerpt, "raw_response_length": raw_len,
            "raw_response_truncated": truncated,
        }
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a") as f:
            f.write(json.dumps(row, default=str) + "\n")
    except Exception:  # noqa: BLE001 — diagnostics must never mask the real failure
        pass


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
    ai_failure_log: Optional[Path] = None,
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
    # A bare global reference, resolved at CALL time, never bound as a
    # default-parameter value at import time — the same "read fresh every
    # call" posture resolve_claude_binary() already documents in this file.
    # This is what lets a test (or a future caller) redirect every
    # diagnostic write by setting `investigator.AI_FAILURE_LOG` once,
    # without having to thread `ai_failure_log=` through every call site
    # that doesn't care to override it individually.
    log_path = ai_failure_log if ai_failure_log is not None else AI_FAILURE_LOG

    try:
        raw = runner(prompt)
    except InvestigatorError as e:
        _record_ai_failure(e, log_path=log_path)
        raise
    except Exception as e:  # a broken injected runner fails closed, not open
        err = InvestigatorError(f"Research AI invocation failed: {e}",
                                stage="invocation_error")
        _record_ai_failure(err, log_path=log_path)
        raise err from e

    try:
        proposal = parse_ai_output(raw)
    except InvestigatorError as e:
        _record_ai_failure(e, log_path=log_path)
        raise

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
