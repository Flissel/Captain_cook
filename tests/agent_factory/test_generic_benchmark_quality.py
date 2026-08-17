from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest

from agenten.agent_factory.contracts import (
    FactoryBlockStatus,
    FactoryEvidenceBlock,
    FactoryPhase,
    FactoryRole,
    GenericBenchmarkQualityEvidenceV1,
)
from agenten.agent_factory.state_machine import (
    FactoryActionKind,
    FactoryLifecycleError,
    FactoryLifecycleStatus,
    FactoryProjection,
    apply_block,
    next_action,
)
from agenten.agent_factory.workflow_promotion import build_release_workflow_promotion
from gateway.contracts import FactoryJobProjection
from tests.agent_factory.test_state_machine import job_v3


NOW = datetime(2026, 8, 17, 12, tzinfo=timezone.utc)


def _ref(label: str, digest: str):
    from agenten.agent_runtime.contracts import ArtifactRef

    return ArtifactRef(
        uri=f"artifact://{label}/{digest}",
        sha256=digest,
        media_type="application/json",
    )


def _quality() -> GenericBenchmarkQualityEvidenceV1:
    job = job_v3()
    categories = (
        "normal",
        "boundary",
        "missing",
        "conflicting",
        "mandatory_escalation",
    )
    return GenericBenchmarkQualityEvidenceV1(
        schema="captain.generic-benchmark-quality-evidence.v1",
        authority_id=UUID("248612c1-87cb-5196-8f64-7a2640c798ba"),
        job_id=job.job_id,
        correlation_id=job.correlation_id,
        subject_version=job.subject_version,
        attempt=1,
        suite_ref=_ref("capability-benchmark", "1" * 64),
        summary_ref=_ref("sha256", "2" * 64),
        case_ids=tuple(f"case-{index:02d}" for index in range(15)),
        case_digests=tuple(f"{index + 3:064x}" for index in range(15)),
        categories=tuple(category for category in categories for _ in range(3)),
        acceptance_assertion_ids=job.acceptance_assertion_ids,
        evaluation_refs=tuple(
            _ref("sha256", f"{index + 32:064x}") for index in range(30)
        ),
        passed=True,
    )


def _quality_block() -> FactoryEvidenceBlock:
    job = job_v3()
    quality = _quality()
    return FactoryEvidenceBlock(
        schema="captain.agent-factory-block.v1",
        event_id=UUID("00000000-0000-0000-0000-000000000901"),
        job_id=job.job_id,
        correlation_id=job.correlation_id,
        causation_id=job.event_id,
        occurred_at=NOW,
        producer="hermes",
        subject_version=job.subject_version,
        attempt=1,
        phase=FactoryPhase.QUALITY_REVIEWED,
        role=FactoryRole.QUALITY_WARDEN,
        status=FactoryBlockStatus.SUCCEEDED,
        artifact_refs=(quality.suite_ref, quality.summary_ref),
        evidence_refs=(quality.summary_ref, *quality.evaluation_refs),
        assertion_ids=quality.acceptance_assertion_ids,
        lease_id="quality-lease",
        generic_benchmark_quality=quality,
    )


def test_generic_quality_review_reaches_validate_then_immutable_promotion() -> None:
    job = job_v3()
    before = FactoryProjection(
        job=job,
        status=FactoryLifecycleStatus.RUNNING,
        phase=FactoryPhase.REAL_CASE_EVIDENCE,
        attempt=1,
        observed_assertion_ids=job.acceptance_assertion_ids,
    )
    quality_block = _quality_block()

    quality = apply_block(before, quality_block)

    assert quality.phase is FactoryPhase.QUALITY_REVIEWED
    assert quality.generic_benchmark_summary_ref == _quality().summary_ref
    assert next_action(quality).kind is FactoryActionKind.VALIDATE_FOR_PROMOTION

    promotion = build_release_workflow_promotion(
        FactoryJobProjection(
            job=job,
            blocks=(quality_block,),
            leases=(),
            projection=quality,
        ),
        occurred_at=NOW,
    )
    assert promotion.artifact_refs == (_quality().summary_ref,)
    assert promotion.evidence_refs == quality_block.evidence_refs
    promoted = apply_block(quality, promotion)
    assert promoted.status is FactoryLifecycleStatus.READY_TO_USE
    assert promoted.phase is FactoryPhase.CAPABILITY_PROMOTED


def test_generic_quality_review_rejects_job_assertion_drift() -> None:
    job = job_v3()
    before = FactoryProjection(
        job=job,
        status=FactoryLifecycleStatus.RUNNING,
        phase=FactoryPhase.REAL_CASE_EVIDENCE,
        attempt=1,
        observed_assertion_ids=job.acceptance_assertion_ids,
    )
    quality_block = _quality_block()
    changed = quality_block.model_copy(
        update={
            "generic_benchmark_quality": _quality().model_copy(
                update={"acceptance_assertion_ids": ("foreign",)}
            )
        }
    )

    with pytest.raises(FactoryLifecycleError, match="assertion"):
        apply_block(before, changed)
