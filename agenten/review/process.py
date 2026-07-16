"""Independent review of immutable Captain plans."""

from __future__ import annotations

from typing import Awaitable, Callable, List

from agenten.planning.canonical_contracts import CanonicalPlan
from agenten.review.contracts import (
    PlanReview,
    ReviewDecision,
    ReviewFinding,
    ReviewSeverity,
    compute_review_id,
    digest_plan,
)


Reviewer = Callable[[CanonicalPlan], Awaitable[List[ReviewFinding]]]


class PlanReviewProcess:
    """Run review separately; it can return findings but cannot alter a plan."""

    def __init__(self, *, reviewer_id: str, reviewer_version: str) -> None:
        if not reviewer_id.strip() or not reviewer_version.strip():
            raise ValueError("reviewer identity and version are required")
        self._reviewer_id = reviewer_id
        self._reviewer_version = reviewer_version

    async def review(self, plan: CanonicalPlan, reviewer: Reviewer) -> PlanReview:
        findings = await reviewer(plan.model_copy(deep=True))
        decision = (
            ReviewDecision.FAILED
            if any(finding.severity is ReviewSeverity.ERROR for finding in findings)
            else ReviewDecision.PASSED
        )
        plan_digest = digest_plan(plan)
        return PlanReview(
            review_id=compute_review_id(
                plan_id=plan.plan_id,
                plan_digest=plan_digest,
                reviewer_id=self._reviewer_id,
                reviewer_version=self._reviewer_version,
                decision=decision,
                findings=tuple(findings),
            ),
            plan_id=plan.plan_id,
            plan_digest=plan_digest,
            reviewer_id=self._reviewer_id,
            reviewer_version=self._reviewer_version,
            decision=decision,
            findings=tuple(findings),
        )
