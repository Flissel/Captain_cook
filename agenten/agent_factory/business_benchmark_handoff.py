"""Typed Captain human-review boundary for business benchmark execution."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agenten.agent_factory.business_benchmark_provider_state import (
    BusinessBenchmarkProviderBindingV1,
)
from agenten.agent_runtime.contracts import ArtifactRef, IDENTIFIER_PATTERN


class _FrozenContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class CaptainHumanReviewRequestV1(_FrozenContract):
    schema_name: Literal["captain.business-benchmark-human-review-request.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    review_request_id: UUID
    binding: BusinessBenchmarkProviderBindingV1
    reason_code: str = Field(pattern=IDENTIFIER_PATTERN)
    requested_at: datetime

    @field_validator("requested_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        return _utc(value)


class CaptainHumanReviewReceiptV1(_FrozenContract):
    schema_name: Literal["captain.business-benchmark-human-review-receipt.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    review_request_id: UUID
    binding: BusinessBenchmarkProviderBindingV1
    authority: Literal["captain_human_review"]
    status: Literal["accepted", "completed"]
    evidence_ref: ArtifactRef
    recorded_at: datetime

    @field_validator("recorded_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        return _utc(value)


class CaptainHumanReviewPort(Protocol):
    async def request_review(
        self, request: CaptainHumanReviewRequestV1
    ) -> CaptainHumanReviewReceiptV1: ...


def validate_captain_human_review_receipt(
    request: CaptainHumanReviewRequestV1,
    receipt: CaptainHumanReviewReceiptV1,
) -> CaptainHumanReviewReceiptV1:
    """Accept only Captain authority bound to the exact external effect fence."""

    canonical_request = CaptainHumanReviewRequestV1.model_validate(
        request.model_dump(mode="json", by_alias=True)
    )
    canonical_receipt = CaptainHumanReviewReceiptV1.model_validate(
        receipt.model_dump(mode="json", by_alias=True)
    )
    if canonical_receipt.review_request_id != canonical_request.review_request_id:
        raise ValueError("human review receipt does not match the review request")
    if canonical_receipt.binding != canonical_request.binding:
        raise ValueError("human review receipt must bind the exact effect and fence")
    if canonical_receipt.recorded_at < canonical_request.requested_at:
        raise ValueError("human review receipt cannot be recorded before the request")
    # ``authority`` and ``status`` are closed Literal fields. Therefore an
    # AutoGen agent-to-agent transfer cannot be parsed as a Captain receipt.
    return canonical_receipt


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("human review timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)
