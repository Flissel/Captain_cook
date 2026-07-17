"""Captain-owned gateway review decisions with no persistence dependency."""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator


class GatewayReviewDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    batch_id: str = Field(min_length=1, max_length=32)
    iteration: int = Field(ge=1, strict=True)
    review_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    decision: Literal["passed", "failed"]
    evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=64)

    @field_validator("evidence_refs")
    @classmethod
    def require_opaque_unique_references(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value) or any(
            not reference.startswith("artifact://") or "\\" in reference
            for reference in value
        ):
            raise ValueError("evidence_refs must be unique opaque artifact references")
        return value


class ReviewGateway(Protocol):
    async def record_review(
        self, decision: GatewayReviewDecision
    ) -> GatewayReviewDecision: ...


class GatewayReviewController:
    """Validate a review decision and delegate the append to the gateway."""

    def __init__(self, gateway: ReviewGateway) -> None:
        self._gateway = gateway

    async def record(
        self,
        *,
        batch_id: str,
        iteration: int,
        review_id: str,
        decision: Literal["passed", "failed"],
        evidence_refs: tuple[str, ...],
    ) -> GatewayReviewDecision:
        review = GatewayReviewDecision(
            batch_id=batch_id,
            iteration=iteration,
            review_id=review_id,
            decision=decision,
            evidence_refs=evidence_refs,
        )
        return await self._gateway.record_review(review)
