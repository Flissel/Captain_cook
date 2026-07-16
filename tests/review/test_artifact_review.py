import pytest

from agenten.review.artifacts import (
    ArtifactReviewProcess,
    ArtifactReviewRequest,
    BuildArtifactRef,
    ReviewerIndependenceError,
)
from agenten.review.contracts import ReviewDecision, ReviewFinding, ReviewSeverity


def make_request() -> ArtifactReviewRequest:
    return ArtifactReviewRequest(
        plan_id="plan-" + "a" * 24,
        batch_id="build",
        builder_id="worker-01",
        artifacts=(
            BuildArtifactRef(
                artifact_id="team-source",
                version=1,
                sha256="b" * 64,
            ),
        ),
    )


@pytest.mark.asyncio
async def test_artifact_review_is_independent_and_content_addressed() -> None:
    request = make_request()

    async def reviewer(received):
        assert received == request
        assert not hasattr(received.artifacts[0], "path")
        return []

    result = await ArtifactReviewProcess().review(
        request,
        reviewer_id="quality-warden",
        reviewer=reviewer,
    )

    assert result.decision is ReviewDecision.PASSED
    assert result.plan_id == request.plan_id
    assert result.batch_id == request.batch_id
    assert len(result.artifact_set_digest) == 64


@pytest.mark.asyncio
async def test_builder_cannot_review_its_own_artifacts() -> None:
    request = make_request()

    async def reviewer(_):
        return [
            ReviewFinding(
                code="self-review",
                severity=ReviewSeverity.INFO,
                message="should never run",
            )
        ]

    with pytest.raises(ReviewerIndependenceError, match="builder"):
        await ArtifactReviewProcess().review(
            request,
            reviewer_id="worker-01",
            reviewer=reviewer,
        )
