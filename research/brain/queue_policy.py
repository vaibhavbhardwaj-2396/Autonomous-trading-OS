"""Backpressure policy for the autonomous research queue.

This module is deliberately pure: it neither reads a Store nor changes a
Contract.  The worker supplies observed queue counts and remains responsible
for executing any selected action.  Keeping the policy here makes discovery
admission explicit, testable, and independent of priority-score tuning.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict


@dataclass(frozen=True)
class ResearchQueueSnapshot:
    """Observed queue state at one worker selection point."""

    draft_count: int
    locked_runnable_count: int
    running_count: int = 0
    reported_count: int = 0
    promising_count: int = 0
    robust_count: int = 0
    strategy_candidate_count: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class QueuePolicyDecision:
    discovery_allowed: bool
    blocking_reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "discovery_allowed": self.discovery_allowed,
            "blocking_reasons": list(self.blocking_reasons),
        }


@dataclass(frozen=True)
class ResearchQueuePolicy:
    """Admission control for new discovery work.

    Discovery is exploratory work; draft promotion and runnable experiments
    are already-created commitments.  Once either downstream queue reaches
    its configured capacity, discovery is withheld until the queue drains.
    Equality deliberately blocks: a watermark is a capacity limit, not an
    alert threshold to exceed by one more item.
    """

    draft_high_watermark: int = 3
    runnable_experiment_high_watermark: int = 2

    def __post_init__(self) -> None:
        for field in ("draft_high_watermark", "runnable_experiment_high_watermark"):
            if getattr(self, field) < 1:
                raise ValueError(f"ResearchQueuePolicy.{field} must be >= 1")

    def decide(self, snapshot: ResearchQueueSnapshot) -> QueuePolicyDecision:
        reasons: list[str] = []
        if snapshot.draft_count >= self.draft_high_watermark:
            reasons.append(
                f"draft backlog {snapshot.draft_count} reached high-water mark "
                f"{self.draft_high_watermark}"
            )
        if snapshot.locked_runnable_count >= self.runnable_experiment_high_watermark:
            reasons.append(
                f"runnable experiment backlog {snapshot.locked_runnable_count} reached "
                f"high-water mark {self.runnable_experiment_high_watermark}"
            )
        return QueuePolicyDecision(discovery_allowed=not reasons, blocking_reasons=tuple(reasons))
