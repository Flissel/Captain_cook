"""Captain-owned construction of a release-mode workflow promotion block."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid5

import httpx
from pydantic import BaseModel, ConfigDict

from agenten.agent_factory.contracts import (
    AgentFactoryJobV3,
    FactoryBlockStatus,
    FactoryEvidenceBlock,
    FactoryPhase,
)
from agenten.agent_factory.execution_policy import FactoryExecutionMode
from agenten.agent_factory.skill_workflow_contracts import (
    FactoryFeedbackRecommendation,
)
from agenten.agent_factory.state_machine import FactoryLifecycleStatus
from gateway.contracts import FactoryJobProjection, FactoryWriteReceipt


_PROMOTION_NAMESPACE = UUID("768313d0-4a52-5f8f-91ef-46a06da80d34")


class WorkflowPromotionResult(BaseModel):
    """Verified Gateway outcome for one Captain workflow promotion."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: UUID
    promotion_event_id: UUID
    replayed: bool
    status: FactoryLifecycleStatus
    phase: FactoryPhase


def build_release_workflow_promotion(
    snapshot: FactoryJobProjection,
    *,
    occurred_at: datetime,
) -> FactoryEvidenceBlock:
    """Build the sole Captain block that asks Gateway to recompute promotion."""

    job = snapshot.job
    projection = snapshot.projection
    if (
        not isinstance(job, AgentFactoryJobV3)
        or job.execution_policy.mode is not FactoryExecutionMode.RELEASE
    ):
        raise ValueError("workflow promotion requires release execution mode")
    if (
        projection.status is not FactoryLifecycleStatus.RUNNING
        or projection.phase is not FactoryPhase.QUALITY_REVIEWED
        or projection.feedback_recommendation
        is not FactoryFeedbackRecommendation.PROMOTE_CANDIDATE
    ):
        raise ValueError("workflow promotion requires a promotable quality review")
    if (
        projection.workflow_evaluation_ref is None
        or projection.feedback_ref is None
    ):
        raise ValueError("workflow promotion requires complete quality evidence")
    quality = next(
        block
        for block in snapshot.blocks
        if block.attempt == projection.attempt
        and block.phase is FactoryPhase.QUALITY_REVIEWED
    )
    expected_artifacts = (
        projection.workflow_evaluation_ref,
        projection.feedback_ref,
    )
    event_id = uuid5(
        _PROMOTION_NAMESPACE,
        f"{job.job_id}|{projection.attempt}|{quality.event_id}|capability_promoted",
    )
    return FactoryEvidenceBlock(
        schema="captain.agent-factory-block.v1",
        event_id=event_id,
        job_id=job.job_id,
        correlation_id=job.correlation_id,
        causation_id=quality.event_id,
        occurred_at=occurred_at,
        producer="captain",
        subject_version=job.subject_version,
        attempt=projection.attempt,
        phase=FactoryPhase.CAPABILITY_PROMOTED,
        status=FactoryBlockStatus.SUCCEEDED,
        artifact_refs=expected_artifacts,
        evidence_refs=quality.evidence_refs,
        assertion_ids=job.acceptance_assertion_ids,
    )


def promote_release_workflow(
    *,
    client: httpx.Client,
    job_id: UUID,
    occurred_at: datetime,
) -> WorkflowPromotionResult:
    """Ask Gateway to promote one release workflow and verify its projection."""

    response = client.get(f"/v1/factory/jobs/{job_id}")
    response.raise_for_status()
    snapshot = FactoryJobProjection.model_validate(response.json())
    if (
        snapshot.projection.status is FactoryLifecycleStatus.READY_TO_USE
        and snapshot.projection.phase is FactoryPhase.CAPABILITY_PROMOTED
    ):
        promotions = tuple(
            block
            for block in snapshot.blocks
            if block.phase is FactoryPhase.CAPABILITY_PROMOTED
            and block.attempt == snapshot.projection.attempt
            and block.event_id in snapshot.projection.block_ids
        )
        if len(promotions) != 1:
            raise RuntimeError("ready workflow lacks one authoritative promotion block")
        return WorkflowPromotionResult(
            job_id=job_id,
            promotion_event_id=promotions[0].event_id,
            replayed=True,
            status=snapshot.projection.status,
            phase=snapshot.projection.phase,
        )
    promotion = build_release_workflow_promotion(snapshot, occurred_at=occurred_at)

    response = client.post(
        "/v1/factory/blocks",
        json=promotion.model_dump(mode="json", by_alias=True),
    )
    response.raise_for_status()
    receipt = FactoryWriteReceipt.model_validate(response.json())

    response = client.get(f"/v1/factory/jobs/{job_id}")
    response.raise_for_status()
    promoted = FactoryJobProjection.model_validate(response.json())
    if (
        promoted.projection.status is not FactoryLifecycleStatus.READY_TO_USE
        or promoted.projection.phase is not FactoryPhase.CAPABILITY_PROMOTED
    ):
        raise RuntimeError("Gateway did not verify the workflow promotion")

    return WorkflowPromotionResult(
        job_id=job_id,
        promotion_event_id=receipt.event_id,
        replayed=receipt.replayed,
        status=promoted.projection.status,
        phase=promoted.projection.phase,
    )


__all__ = [
    "WorkflowPromotionResult",
    "build_release_workflow_promotion",
    "promote_release_workflow",
]
