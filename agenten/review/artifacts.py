"""Content-addressed artifact review without build-workspace authority."""

from __future__ import annotations

import hashlib
import json
from typing import Awaitable, Callable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agenten.review.contracts import ReviewDecision, ReviewFinding, ReviewSeverity


ARTIFACT_REVIEW_SCHEMA_VERSION = "captain-artifact-review/v1"


class ReviewerIndependenceError(RuntimeError):
    """The assigned reviewer is also the artifact builder."""


class BuildArtifactRef(BaseModel):
    """Portable immutable evidence; deliberately contains no workspace path."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    version: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ArtifactReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = ARTIFACT_REVIEW_SCHEMA_VERSION
    plan_id: str = Field(pattern=r"^plan-[0-9a-f]{24}$")
    batch_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,31}$")
    builder_id: str = Field(min_length=1)
    artifacts: tuple[BuildArtifactRef, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def artifact_ids_are_unique(self) -> "ArtifactReviewRequest":
        identities = [(artifact.artifact_id, artifact.version) for artifact in self.artifacts]
        if len(identities) != len(set(identities)):
            raise ValueError("artifact id and version pairs must be unique")
        return self


class ArtifactReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = ARTIFACT_REVIEW_SCHEMA_VERSION
    plan_id: str
    batch_id: str
    reviewer_id: str
    artifact_set_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: ReviewDecision
    findings: tuple[ReviewFinding, ...] = ()


ArtifactReviewer = Callable[[ArtifactReviewRequest], Awaitable[list[ReviewFinding]]]


class ArtifactReviewProcess:
    """Review sealed references in a process boundary separate from builders."""

    async def review(
        self,
        request: ArtifactReviewRequest,
        *,
        reviewer_id: str,
        reviewer: ArtifactReviewer,
    ) -> ArtifactReviewResult:
        if not reviewer_id.strip():
            raise ValueError("reviewer_id must not be blank")
        if reviewer_id == request.builder_id:
            raise ReviewerIndependenceError("artifact builder cannot be its reviewer")

        findings = tuple(await reviewer(request.model_copy(deep=True)))
        decision = (
            ReviewDecision.FAILED
            if any(finding.severity is ReviewSeverity.ERROR for finding in findings)
            else ReviewDecision.PASSED
        )
        return ArtifactReviewResult(
            plan_id=request.plan_id,
            batch_id=request.batch_id,
            reviewer_id=reviewer_id,
            artifact_set_digest=self._artifact_set_digest(request),
            decision=decision,
            findings=findings,
        )

    @staticmethod
    def _artifact_set_digest(request: ArtifactReviewRequest) -> str:
        artifacts = sorted(
            (artifact.model_dump(mode="json") for artifact in request.artifacts),
            key=lambda artifact: (artifact["artifact_id"], artifact["version"]),
        )
        payload = json.dumps(
            artifacts,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()
