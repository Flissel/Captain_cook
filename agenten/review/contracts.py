"""Immutable contracts shared by review producers and execution consumers."""

import hashlib
import json
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agenten.planning.canonical_contracts import CanonicalPlan


REVIEW_SCHEMA_VERSION = "captain-plan-review/v1"


class ReviewSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class ReviewDecision(str, Enum):
    PASSED = "passed"
    FAILED = "failed"


class ReviewFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    severity: ReviewSeverity
    message: str = Field(min_length=1)
    evidence_refs: tuple[str, ...] = ()


class PlanReview(BaseModel):
    """A reviewer-owned verdict bound to the exact canonical plan bytes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[REVIEW_SCHEMA_VERSION] = REVIEW_SCHEMA_VERSION
    review_id: str = Field(pattern=r"^review-[0-9a-f]{24}$")
    plan_id: str = Field(pattern=r"^plan-[0-9a-f]{24}$")
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewer_id: str = Field(min_length=1)
    reviewer_version: str = Field(min_length=1)
    decision: ReviewDecision
    findings: tuple[ReviewFinding, ...] = ()

    @model_validator(mode="after")
    def decision_is_derived_from_findings(self) -> "PlanReview":
        expected = (
            ReviewDecision.FAILED
            if any(finding.severity is ReviewSeverity.ERROR for finding in self.findings)
            else ReviewDecision.PASSED
        )
        if self.decision is not expected:
            raise ValueError("review decision must be derived from error findings")
        if not self.reviewer_id.strip() or not self.reviewer_version.strip():
            raise ValueError("reviewer identity and version must not be blank")
        expected_review_id = compute_review_id(
            plan_id=self.plan_id,
            plan_digest=self.plan_digest,
            reviewer_id=self.reviewer_id,
            reviewer_version=self.reviewer_version,
            decision=self.decision,
            findings=self.findings,
        )
        if self.review_id != expected_review_id:
            raise ValueError("review_id does not match review content")
        return self


def digest_plan(plan: CanonicalPlan) -> str:
    payload = json.dumps(
        plan.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def compute_review_id(
    *,
    plan_id: str,
    plan_digest: str,
    reviewer_id: str,
    reviewer_version: str,
    decision: ReviewDecision,
    findings: tuple[ReviewFinding, ...],
) -> str:
    payload = {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "plan_id": plan_id,
        "plan_digest": plan_digest,
        "reviewer_id": reviewer_id,
        "reviewer_version": reviewer_version,
        "decision": decision.value,
        "findings": [finding.model_dump(mode="json") for finding in findings],
    }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    return f"review-{digest[:24]}"


def digest_review(review: PlanReview) -> str:
    payload = json.dumps(
        review.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
