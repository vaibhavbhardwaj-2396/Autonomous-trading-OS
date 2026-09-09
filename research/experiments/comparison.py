"""
Cross-Experiment Evaluation — Phase 1 Slice E.

Slice D answered "can I safely run one experiment". This module answers the
next question: given one experiment's verdict, what does it mean in the
context of everything else already tested against the same hypothesis?

    LOCKED CONTRACT
          |
          v
      EXPERIMENT                 research/experiments/runner.py  (Slice D)
          |
          v
    INDIVIDUAL RESULT
          |
          v
       VERDICT                   research/experiments/evaluator.py  (Slice D)
          |
          v
    COMPARE WITH PREVIOUS RESEARCH     <- THIS FILE
          |
          v
    EVIDENCE SUMMARY
          |
          v
    RESEARCH MEMORY             research.memory.record_evidence_summary

This is explicitly NOT a second execution path. Nothing here calls
run_experiment, load_runnable_contract, or simulate — it only reads verdicts
that ALREADY exist in research memory (written exclusively by
evaluator.record_verdict, which only runner.run_experiment calls) and reads
Contract objects to compare their locked rules. A contract that has never
been run has no verdict to compare, and this module has no way to produce
one — it cannot cause an experiment to happen, only interpret ones that
already did.

Nor does it change anything upstream. It never writes to a contract's
registry file (contracts stay exactly as run_experiment left them:
LOCKED/RUNNING/REPORTED/ABANDONED), never touches engine/ in any form, and
never proposes, approves, or locks a new contract. Its only write is one
append-only research_evidence_summary row per call to `record_evidence`.
"Learning" here means exactly that: an accumulating, queryable body of
comparisons — not a model that gets trained, and not anything that reaches
back to change a contract, a strategy, a position, or an order.

What "evidence" means here, precisely
--------------------------------------
A verdict being net-positive is not enough to call a hypothesis promising,
and the reverse is just as important: a big t-stat on a tiny, real-world-
irrelevant average P&L is not evidence worth acting on either. Every scored
variant is independently classified along three separate axes before
anything is concluded:

    direction                 net_pnl > 0 / < 0 / == 0
    statistically_significant |t_stat| >= T_STAT_THRESHOLD (and n_trades is
                               large enough for a t-stat to mean anything)
    economically_meaningful   avg_net_pnl, as a fraction of the fixed
                               research notional, clears a floor

Only when a variant is positive AND statistically significant AND
economically meaningful — with no other sufficiently-sampled variant of the
same hypothesis showing a significant result in the OPPOSITE direction — is
the hypothesis called PROMISING. Every other combination lands in WEAK,
INCONCLUSIVE, or CONTRADICTED; see `_aggregate_verdict` for the exact rule
table. This is a fixed set of deterministic comparisons, not a model or a
score that's fit to data — the same inputs always produce the same verdict.

Duplicate contracts (identical entry_rule/exit_rule/universe/splits/
evaluation window under the same hypothesis, however many separate
contract_ids that rule set was locked and run under) are detected by content
fingerprint and are never counted as independent replication: only the
earliest-locked instance of a given fingerprint contributes to the aggregate
statistics, and asking for the evidence summary of a later duplicate returns
REDUNDANT outright, pointing at the original, rather than silently inflating
apparent replication.

Threshold constants below are deliberately separate from
research.brain.observatory's WINDOW_DAYS/MIN_OBSERVATIONS/Z_THRESHOLD: those
answer "is this market observation unusual", these answer "is this trade-
outcome sample large enough, and its effect large enough, to trust" — a
different statistical question, governed by its own constants so a change
made for one reason can never silently move the other.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Optional

from .. import memory as rm
from ..contracts import Contract, REGISTRY_DIR
from ..store import Store
from . import evaluator
from .runner import RESEARCH_POSITION_NOTIONAL

# -- thresholds ---------------------------------------------------------------
# A sample below this is never treated as statistically informative,
# regardless of what its t-stat happens to say.
MIN_TRADES_FOR_SIGNIFICANCE = 10

# |t_stat| at or above this is treated as a statistically convincing result
# (~two-tailed 95% for a reasonably sized sample). Below it, a result is
# "not significant" however large it looks, because with too few trades a
# large average is exactly what noise looks like.
T_STAT_THRESHOLD = 2.0

# avg_net_pnl, as a fraction of the fixed research notional (see
# runner.RESEARCH_POSITION_NOTIONAL), that a result must clear to be called
# economically meaningful rather than statistically real but practically
# irrelevant (e.g. real but tiny after-cost edge).
ECONOMIC_SIGNIFICANCE_PCT = 0.003

VALID_VERDICTS = ("PROMISING", "WEAK", "INCONCLUSIVE", "CONTRADICTED", "REDUNDANT")


class ComparisonRejected(RuntimeError):
    """The comparison could not be produced — e.g. `contract_id` has never
    been run, or is not linked to any recorded hypothesis. Nothing is
    written when this is raised."""


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------

def _rule_fingerprint(contract: Contract) -> str:
    """A content fingerprint over exactly the fields that define what was
    actually tested — deliberately narrower than Contract.content_hash(),
    which also hashes title/hypothesis/falsification/etc. Two contracts with
    different prose but identical universe/entry_rule/exit_rule/splits/
    evaluation window ran the SAME experiment and should be recognised as
    duplicates; two contracts with the same prose but a different rule are
    not duplicates at all."""
    payload = {
        "universe": contract.universe,
        "entry_rule": _parsed(contract.entry_rule),
        "exit_rule": _parsed(contract.exit_rule),
        "splits": contract.splits,
        "evaluation_start": str(contract.evaluation_start),
        "evaluation_end": str(contract.evaluation_end),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _parsed(rule: str):
    try:
        return json.loads(rule)
    except (TypeError, ValueError):
        return rule


# ---------------------------------------------------------------------------
# Per-variant classification
# ---------------------------------------------------------------------------

def classify_variant(contract_id: str, verdict: dict) -> dict:
    """Classify one contract's already-computed verdict along the three
    independent axes described in the module docstring. Reuses the numbers
    evaluator.compute_verdict already produced (n_trades, net_pnl,
    avg_net_pnl, t_stat) — nothing here recomputes a statistic from raw
    trades; this is pure interpretation of numbers that already exist."""
    n_trades = verdict.get("n_trades") or 0
    net_pnl = verdict.get("net_pnl")
    avg_net_pnl = verdict.get("avg_net_pnl")
    t_stat = verdict.get("t_stat")

    if n_trades == 0:
        return {
            "contract_id": contract_id, "n_trades": 0, "direction": None,
            "insufficient_sample": True, "statistically_significant": None,
            "economically_meaningful": None, "label": "no_trades",
        }

    if net_pnl is None or net_pnl == 0:
        direction = "flat"
    else:
        direction = "positive" if net_pnl > 0 else "negative"

    insufficient = n_trades < MIN_TRADES_FOR_SIGNIFICANCE
    stat_sig = (t_stat is not None) and (abs(t_stat) >= T_STAT_THRESHOLD)
    econ_meaningful = (avg_net_pnl is not None) and (
        abs(avg_net_pnl) / RESEARCH_POSITION_NOTIONAL >= ECONOMIC_SIGNIFICANCE_PCT)

    if insufficient:
        label = "insufficient_sample"
    elif direction == "flat":
        label = "flat"
    elif stat_sig and econ_meaningful:
        label = f"{direction}_significant"
    elif stat_sig:
        label = f"{direction}_significant_but_negligible"
    else:
        label = f"{direction}_not_significant"

    return {
        "contract_id": contract_id,
        "n_trades": n_trades,
        "direction": direction,
        "insufficient_sample": insufficient,
        "statistically_significant": stat_sig,
        "economically_meaningful": econ_meaningful,
        "label": label,
    }


# ---------------------------------------------------------------------------
# Aggregate verdict — the deterministic rule table
# ---------------------------------------------------------------------------

def _aggregate_verdict(contract_id: str, variants: list[dict],
                       duplicate_of: Optional[str]) -> tuple[str, str]:
    """Returns (verdict, rationale). `variants` already excludes every
    contract this function's caller determined to be a duplicate of an
    earlier-locked identical rule set — see evaluate_hypothesis_evidence."""
    if duplicate_of:
        return "REDUNDANT", (
            f"{contract_id} tests the exact same universe/entry_rule/exit_rule/"
            f"splits/evaluation window as {duplicate_of}, already scored under "
            f"this hypothesis. Not counted as independent replication.")

    with_evidence = [v for v in variants if v["n_trades"] > 0]
    if not with_evidence:
        return "INCONCLUSIVE", "No tested variant of this hypothesis produced any trades."

    sufficient = [v for v in with_evidence if not v["insufficient_sample"]]
    if not sufficient:
        return "INCONCLUSIVE", (
            f"{len(with_evidence)} variant(s) tested but none reached the "
            f"{MIN_TRADES_FOR_SIGNIFICANCE}-trade minimum this system requires "
            f"before treating a result as statistically readable.")

    stat_sig = [v for v in sufficient if v["statistically_significant"]]
    sig_positive = [v for v in stat_sig if v["direction"] == "positive"]
    sig_negative = [v for v in stat_sig if v["direction"] == "negative"]

    if sig_positive and sig_negative:
        return "CONTRADICTED", (
            f"{len(sig_positive)} variant(s) show a statistically significant "
            f"POSITIVE result and {len(sig_negative)} show a statistically "
            f"significant NEGATIVE result for the same hypothesis — the "
            f"evidence disagrees with itself.")

    if sig_positive and any(v["economically_meaningful"] for v in sig_positive):
        return "PROMISING", (
            f"{len(sig_positive)} of {len(sufficient)} sufficiently-sampled "
            f"variant(s) show a statistically significant AND economically "
            f"meaningful positive result, with no significant result in the "
            f"opposite direction.")

    return "WEAK", (
        f"{len(sufficient)} sufficiently-sampled variant(s) tested; "
        f"{len(sig_positive)} significant-positive, {len(sig_negative)} "
        f"significant-negative, but none combined statistical significance "
        f"with economic meaningfulness in a consistent positive direction.")


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def evaluate_hypothesis_evidence(
    store: Store, contract_id: str, *, registry_dir: Path = REGISTRY_DIR,
) -> dict:
    """Compare `contract_id`'s verdict against every other scored variant of
    the same hypothesis and produce a deterministic evidence summary. Reads
    only what already exists (verdicts research memory already holds,
    contracts already on disk in `registry_dir`) — never executes anything,
    never mutates a contract, never writes anything itself (see
    `record_evidence` for the one function that persists the result)."""
    hypothesis_id = evaluator.resolve_hypothesis_id(store, contract_id)
    if hypothesis_id is None:
        raise ComparisonRejected(
            f"{contract_id} is not linked to any recorded hypothesis — it was "
            f"never drafted through hypothesis_intake.create_draft().")

    sibling_ids = evaluator.contract_ids_for_hypothesis(store, hypothesis_id)

    scored: list[tuple[Contract, dict]] = []
    for cid in sibling_ids:
        try:
            contract = Contract.load(cid, registry_dir)
        except FileNotFoundError:
            continue  # drafted but its registry file is gone — nothing to compare
        verdict = evaluator.verdict_for_contract(store, cid)
        if verdict is None:
            continue  # locked/running but never scored — not evidence yet
        scored.append((contract, verdict))

    if not any(c.id == contract_id for c, _ in scored):
        raise ComparisonRejected(
            f"{contract_id} has no recorded verdict yet — run it via "
            f"research.experiments.runner.run_experiment() before comparing "
            f"its evidence.")

    # Group by rule fingerprint; within each group with more than one member,
    # the earliest-locked contract is the original and every later one is a
    # duplicate of it. Ties (equal locked_at, e.g. same-second locks in a
    # test) break on contract_id for a fully deterministic ordering.
    fingerprints: dict[str, list[Contract]] = {}
    for c, _ in scored:
        fingerprints.setdefault(_rule_fingerprint(c), []).append(c)

    duplicate_of: dict[str, str] = {}
    for group in fingerprints.values():
        if len(group) <= 1:
            continue
        ordered = sorted(group, key=lambda c: (c.locked_at or "", c.id))
        original = ordered[0]
        for c in ordered[1:]:
            duplicate_of[c.id] = original.id

    variants = [
        classify_variant(c.id, verdict)
        for c, verdict in scored
        if c.id not in duplicate_of
    ]
    variants.sort(key=lambda v: v["contract_id"])

    verdict_label, rationale = _aggregate_verdict(
        contract_id, variants, duplicate_of.get(contract_id))

    return {
        "hypothesis_id": hypothesis_id,
        "contract_id": contract_id,
        "n_variants_scored": len(scored),
        "n_unique_rule_variants": len(fingerprints),
        "is_duplicate": contract_id in duplicate_of,
        "duplicate_of": duplicate_of.get(contract_id),
        "variants": variants,
        "positive_count": sum(1 for v in variants if v["direction"] == "positive"),
        "negative_count": sum(1 for v in variants if v["direction"] == "negative"),
        "verdict": verdict_label,
        "rationale": rationale,
    }


def record_evidence(
    store: Store, contract_id: str, *, registry_dir: Path = REGISTRY_DIR,
) -> dict:
    """Compute the evidence summary and persist it — the one function
    outside this module should call. Two steps, one function, same pattern
    as evaluator.record_verdict: compute, then write exactly once."""
    summary = evaluate_hypothesis_evidence(store, contract_id, registry_dir=registry_dir)
    rm.record_evidence_summary(
        store,
        hypothesis_id=summary["hypothesis_id"],
        contract_id=contract_id,
        summary=summary,
    )
    return summary
