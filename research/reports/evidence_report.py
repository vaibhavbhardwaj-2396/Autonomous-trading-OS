"""
Research Evidence Report — Phase 1 Slice F.

The last step of the research loop Slices A-E built:

    OBSERVE -> HYPOTHESIZE -> APPROVE -> LOCK -> REPLAY -> EVALUATE -> COMPARE
        -> REMEMBER (research.memory / comparison.record_evidence)
        -> REPORT                                          <- THIS FILE

Everything upstream of this module already exists and already writes to
research memory. This module reads it back and renders it as one
deterministic, human-readable document. It adds no new judgment: every
verdict, rationale, and classification shown here was already computed by
research.experiments.comparison — this module only gathers, groups, and
formats what already exists.

THE GOVERNANCE BOUNDARY — read this before changing anything here
--------------------------------------------------------------------
A PROMISING research verdict is evidence, not authorization. The live
trading system has its own, older, independently-governed learning loop
(memory/review_process.md): a minimum of 20 live trades of a specific setup
before memory/strategy.md may change on performance grounds, one parameter
change at a time, every change written with its statistic, its sample size,
and what result would prove it wrong. That loop is not touched, informed
automatically, or shortcut by anything in this module. Vaibhav's framing,
kept verbatim because it is exactly the design constraint:

    "Research -> candidate, not Research -> permission."

Concretely, and permanently, this module does NOT:
  - write to memory/strategy.md, or any other file under memory/
  - write to, or import, memory/trades.jsonl in any form
  - write to a Contract's registry file, or any locked/running/reported/
    abandoned contract's status, notes, or hash (read-only throughout —
    every Contract.load() here is never followed by a Contract.save())
  - generate an order, touch portfolio state, or touch broker state
  - lower, bypass, or even reference the 20-live-trade requirement — that
    number belongs to review_process.md and is never repeated here
  - promote a research setup into live trading, or produce anything that
    reads as an instruction rather than a description of what was found
  - import engine.journal, engine.execute, engine.guardrails, engine.broker,
    engine.broker_kite, or engine.broker_indstocks — checked directly by
    tests/test_kernel_isolation.py's FORBIDDEN set, same as every other file
    under research/
  - import ANY engine module. Slice D/E's runner and evaluator needed
    engine.costs (pure P&L math) and engine.watchlist (a static symbol
    list); this module needs neither — it reads only what research memory
    and the contract registry already hold, so it imports nothing from
    engine/ at all.
  - introduce an engine -> research dependency in the other direction.
    engine/ must never import research/, permanently and unconditionally;
    nothing in this module changes that, and this module is never imported
    by anything under engine/.

What it DOES produce: a Markdown document a human, or the live agent acting
as itself during its own weekly review (not as engine/ code — a reasoning
process reading a file, exactly like it already reads research_log.md and
trade_log.md per CLAUDE.md's read order), can bring into that review as one
more input. Nothing here compels that review to act on it.

Determinism, same discipline as digest.py: build_report(store) called twice
against an unchanged store produces an identical result. Every hypothesis is
sorted by hypothesis_id; every variant list within a hypothesis is already
sorted by contract_id (research.experiments.comparison does that); nothing
here depends on sqlite row order, filesystem glob order, or wall-clock time
beyond what's already recorded in research memory.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .. import memory as rm
from ..contracts import Contract, REGISTRY_DIR
from ..store import Store
from ..experiments import comparison, evaluator


# ---------------------------------------------------------------------------
# Gathering — read-only throughout
# ---------------------------------------------------------------------------

def _hypothesis_ids_with_evidence(store: Store) -> list[str]:
    """Every hypothesis_id that has at least one scored contract, sorted for
    a deterministic report order. Derived from the hypothesis-claim log
    (research.memory.DATASET_HYPOTHESIS) rather than trusting a verdict
    row's own copy of hypothesis_id, so this stays correct even if a verdict
    was ever recorded without that field populated."""
    rows = rm.query_research_log(store, rm.DATASET_HYPOTHESIS)
    all_hids = sorted({
        r["payload"].get("hypothesis_id") for r in rows
        if r["payload"].get("hypothesis_id")
    })
    return [
        hid for hid in all_hids
        if any(
            evaluator.verdict_for_contract(store, cid) is not None
            for cid in evaluator.contract_ids_for_hypothesis(store, hid)
        )
    ]


def _hypothesis_claim(store: Store, hypothesis_id: str) -> str:
    """The original claim text — the first (oldest) proposal row recorded
    against this hypothesis_id. query_research_log returns rows oldest
    first, so rows[0] is deterministic."""
    rows = [
        r for r in rm.query_research_log(store, rm.DATASET_HYPOTHESIS)
        if r["payload"].get("hypothesis_id") == hypothesis_id
    ]
    return rows[0]["payload"].get("claim", "") if rows else ""


def _contract_status(contract_id: str, registry_dir: Path) -> str:
    try:
        return Contract.load(contract_id, registry_dir).status
    except FileNotFoundError:
        return "unknown"


def hypothesis_report(
    store: Store, hypothesis_id: str, *, registry_dir: Path = REGISTRY_DIR,
) -> Optional[dict]:
    """Everything the report needs for one hypothesis: the claim, the
    aggregate verdict, every unique variant's classification, and every
    contract excluded as a duplicate (shown, not hidden — visibility into
    what was tested is part of the point of a report). Returns None if
    `hypothesis_id` has no scored contract at all (nothing to report yet).

    Reuses research.experiments.comparison.evaluate_hypothesis_evidence
    verbatim rather than re-deriving verdict/duplicate logic — that
    function's result is identical regardless of which of a hypothesis's
    scored, non-duplicate contracts is passed to it (Slice E's own
    determinism/CONTRADICTED tests establish this), so this always asks
    using the alphabetically-first scored contract_id for a fully
    deterministic choice.
    """
    contract_ids = evaluator.contract_ids_for_hypothesis(store, hypothesis_id)
    scored_ids = sorted(
        cid for cid in contract_ids
        if evaluator.verdict_for_contract(store, cid) is not None
    )
    if not scored_ids:
        return None

    summary = comparison.evaluate_hypothesis_evidence(
        store, scored_ids[0], registry_dir=registry_dir)

    variant_ids = {v["contract_id"] for v in summary["variants"]}
    duplicates = []
    for cid in scored_ids:
        if cid in variant_ids:
            continue
        dup_summary = comparison.evaluate_hypothesis_evidence(
            store, cid, registry_dir=registry_dir)
        duplicates.append({
            "contract_id": cid,
            "duplicate_of": dup_summary["duplicate_of"],
        })

    verdict_rows = [
        r for r in rm.query_research_log(store, rm.DATASET_VERDICT)
        if r["payload"].get("contract_id") in scored_ids
    ]
    latest_experiment_at = max(
        (r["event_time"] for r in verdict_rows), default=None)

    return {
        "hypothesis_id": hypothesis_id,
        "claim": _hypothesis_claim(store, hypothesis_id),
        "verdict": summary["verdict"],
        "rationale": summary["rationale"],
        "n_variants_scored": summary["n_variants_scored"],
        "n_unique_rule_variants": summary["n_unique_rule_variants"],
        "positive_count": summary["positive_count"],
        "negative_count": summary["negative_count"],
        "variants": summary["variants"],
        "duplicates": duplicates,
        "contract_statuses": {
            cid: _contract_status(cid, registry_dir) for cid in scored_ids
        },
        "latest_experiment_at": latest_experiment_at,
    }


def build_report(store: Store, *, registry_dir: Path = REGISTRY_DIR) -> dict:
    """Assemble the whole-registry report: every hypothesis with recorded
    evidence, sorted by hypothesis_id, whatever its verdict — PROMISING,
    WEAK, INCONCLUSIVE, CONTRADICTED, and REDUNDANT hypotheses are all
    included. A hypothesis is never dropped for having failed; the point of
    a pre-registered research programme is that the failures stay visible
    too (research.contracts.comparison_count's whole rationale, restated
    here at the report layer)."""
    hids = _hypothesis_ids_with_evidence(store)
    hypotheses = [
        h for h in (hypothesis_report(store, hid, registry_dir=registry_dir)
                    for hid in hids)
        if h is not None
    ]
    counts_by_verdict = {v: 0 for v in comparison.VALID_VERDICTS}
    for h in hypotheses:
        counts_by_verdict[h["verdict"]] = counts_by_verdict.get(h["verdict"], 0) + 1

    return {
        "n_hypotheses_with_evidence": len(hypotheses),
        "counts_by_verdict": counts_by_verdict,
        "hypotheses": hypotheses,
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _variant_row(v: dict, status: str) -> str:
    direction = v["direction"] or "—"
    return (f"| {v['contract_id']} | {status} | {v['n_trades']} | {direction} | "
            f"{v['statistically_significant']} | {v['economically_meaningful']} | "
            f"{v['label']} |")


def to_markdown(report: dict) -> str:
    """A deterministic Markdown rendering of build_report()'s output. Same
    input always produces the same string, byte for byte — no wall-clock,
    no set-ordering, nothing sourced outside the `report` dict itself."""
    lines: list[str] = ["# Research Evidence Report", ""]
    lines.append(
        f"**{report['n_hypotheses_with_evidence']} hypothesis(es) with recorded evidence.**")
    lines.append("")
    for verdict in comparison.VALID_VERDICTS:
        lines.append(f"- {verdict}: {report['counts_by_verdict'].get(verdict, 0)}")
    lines.append("")
    lines.append(
        "_A verdict here is evidence for a human or agent review to consider, "
        "not authorization to change memory/strategy.md. Every setup still "
        "earns its place through the live 20-trade review process "
        "(memory/review_process.md), independent of anything below._")
    lines.append("")
    lines.append("---")
    lines.append("")

    if not report["hypotheses"]:
        lines.append("_No hypotheses have recorded evidence yet._")
        return "\n".join(lines) + "\n"

    for h in report["hypotheses"]:
        lines.append(f"## {h['hypothesis_id']} — {h['verdict']}")
        lines.append("")
        if h["claim"]:
            lines.append(f"**Claim:** {h['claim']}")
            lines.append("")
        lines.append(f"**Rationale:** {h['rationale']}")
        lines.append("")
        lines.append(f"- Variants scored: {h['n_variants_scored']} "
                     f"(unique rule sets: {h['n_unique_rule_variants']})")
        lines.append(f"- Positive: {h['positive_count']} · Negative: {h['negative_count']}")
        if h["latest_experiment_at"]:
            lines.append(f"- Latest experiment recorded: {h['latest_experiment_at']}")
        lines.append("")
        lines.append("| Contract | Status | Trades | Direction | Significant | Meaningful | Label |")
        lines.append("|---|---|---|---|---|---|---|")
        for v in h["variants"]:
            status = h["contract_statuses"].get(v["contract_id"], "unknown")
            lines.append(_variant_row(v, status))
        if h["duplicates"]:
            lines.append("")
            lines.append("Duplicate experiments (excluded from the counts above):")
            for d in h["duplicates"]:
                lines.append(f"- {d['contract_id']} — duplicate of {d['duplicate_of']}")
        lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines).rstrip("\n") + "\n"


def render(store: Store, *, registry_dir: Path = REGISTRY_DIR) -> str:
    """build_report() + to_markdown() in one call — the function most
    callers want."""
    return to_markdown(build_report(store, registry_dir=registry_dir))


def write_report(
    store: Store, path: Path, *, registry_dir: Path = REGISTRY_DIR,
) -> Path:
    """Render and write the report to `path` — the caller's choice of
    location, always outside engine/ and memory/. This function writes
    exactly one file: `path` itself. It touches nothing else — no contract,
    no research-memory row, no live file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(store, registry_dir=registry_dir))
    return path
