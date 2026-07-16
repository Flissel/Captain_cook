import hashlib

import pytest
from pydantic import ValidationError

from agenten.planning.canonical_plan import CanonicalPlanCompiler
from agenten.planning.input_parser import ParsedProjectInput
from agenten.review.process import (
    PlanReview,
    PlanReviewProcess,
    ReviewDecision,
    ReviewFinding,
    ReviewSeverity,
)
from agenten.validation.contracts import AcceptanceAssertion, AssertionKind, WorkBatch


def make_plan():
    project_input = ParsedProjectInput(
        source_reference="input.md",
        sha256=hashlib.sha256(b"goal").hexdigest(),
        byte_length=4,
        content="goal",
    )
    batch = WorkBatch(
        batch_id="build",
        title="Build",
        goal="Build safely",
        subtask_ids=["sub-1"],
        target="autogen",
        acceptance_criteria=[
            AcceptanceAssertion(
                assertion_id="build-done",
                kind=AssertionKind.STATUS_EQUALS,
                expected="succeeded",
            )
        ],
    )
    return CanonicalPlanCompiler(minimum_workers=5).compile(project_input, [batch])


@pytest.mark.asyncio
async def test_review_returns_an_immutable_plan_bound_decision() -> None:
    plan = make_plan()

    async def reviewer(received_plan):
        assert received_plan == plan
        return []

    result = await PlanReviewProcess(reviewer_id="quality-warden", reviewer_version="v1").review(
        plan, reviewer
    )

    assert result.plan_id == plan.plan_id
    assert result.review_id.startswith("review-")
    assert result.decision is ReviewDecision.PASSED
    assert result.findings == ()
    assert len(result.plan_digest) == 64


@pytest.mark.asyncio
async def test_error_finding_blocks_execution_approval() -> None:
    plan = make_plan()

    async def reviewer(_):
        return [
            ReviewFinding(
                code="missing-contract",
                severity=ReviewSeverity.ERROR,
                message="Tool schema is missing",
            )
        ]

    result = await PlanReviewProcess(reviewer_id="quality-warden", reviewer_version="v1").review(
        plan, reviewer
    )

    assert result.decision is ReviewDecision.FAILED


def test_review_contract_rejects_passed_decision_with_error_finding() -> None:
    plan = make_plan()
    finding = ReviewFinding(
        code="missing-contract",
        severity=ReviewSeverity.ERROR,
        message="Tool schema is missing",
    )

    with pytest.raises(ValidationError, match="decision must be derived"):
        PlanReview(
            review_id="review-" + "d" * 24,
            plan_id=plan.plan_id,
            plan_digest="c" * 64,
            reviewer_id="quality-warden",
            reviewer_version="v1",
            decision=ReviewDecision.PASSED,
            findings=(finding,),
        )
