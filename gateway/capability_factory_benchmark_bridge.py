"""Canonical bridge from Factory V3 quality dispatch to the generic benchmark."""

from __future__ import annotations

from datetime import timezone
from uuid import NAMESPACE_URL, uuid5

from agenten.agent_factory.contracts import (
    AgentFactoryJobV3,
    FactoryBlockStatus,
    FactoryEvidenceBlock,
    FactoryPhase,
    FactoryRole,
    GenericBenchmarkQualityEvidenceV1,
)
from agenten.agent_factory.orchestration import FactoryDispatchError
from agenten.agent_factory.state_machine import FactoryActionKind


class AssemblyOwnedGenericBenchmark:
    """Bridge one persisted generic suite into immutable Factory V3 evidence."""

    def __init__(self, *, gateway_store: object, clock) -> None:
        lookup = getattr(gateway_store, "generic_benchmark_quality_evidence", None)
        if not callable(lookup) or not callable(clock):
            raise ValueError("generic benchmark quality authority is invalid")
        self._lookup = lookup
        self._clock = clock

    async def dispatch(self, request):
        if (
            type(request.job) is not AgentFactoryJobV3
            or request.action.kind is not FactoryActionKind.DISPATCH_QUALITY_WARDEN
            or request.action.attempt < 1
            or request.role is not FactoryRole.QUALITY_WARDEN
            or request.lease is None
            or request.lease.job_id != request.job.job_id
            or request.lease.correlation_id != request.job.correlation_id
            or request.lease.subject_version != request.job.subject_version
            or request.lease.attempt != request.action.attempt
            or request.lease.role is not FactoryRole.QUALITY_WARDEN
        ):
            raise FactoryDispatchError("generic benchmark quality dispatch is invalid")
        now = self._clock()
        if (
            now.tzinfo is None
            or now.utcoffset() != timezone.utc.utcoffset(now)
            or not request.lease.issued_at <= now < request.lease.expires_at
        ):
            raise FactoryDispatchError("generic benchmark quality lease is inactive")
        quality = self._lookup(request.job.job_id, request.action.attempt)
        if type(quality) is not GenericBenchmarkQualityEvidenceV1:
            raise FactoryDispatchError(
                "generic benchmark quality evidence is unavailable"
            )
        if (
            quality.job_id != request.job.job_id
            or quality.correlation_id != request.job.correlation_id
            or quality.subject_version != request.job.subject_version
            or quality.attempt != request.action.attempt
            or quality.acceptance_assertion_ids
            != request.job.acceptance_assertion_ids
        ):
            raise FactoryDispatchError(
                "generic benchmark quality evidence changed Factory authority"
            )
        return FactoryEvidenceBlock(
            schema="captain.agent-factory-block.v1",
            event_id=uuid5(
                NAMESPACE_URL,
                "generic-benchmark-quality|"
                f"{request.job.job_id}|{request.action.attempt}|"
                f"{quality.summary_ref.sha256}",
            ),
            job_id=request.job.job_id,
            correlation_id=request.job.correlation_id,
            causation_id=request.job.event_id,
            occurred_at=now,
            producer="hermes",
            subject_version=request.job.subject_version,
            attempt=request.action.attempt,
            phase=FactoryPhase.QUALITY_REVIEWED,
            role=FactoryRole.QUALITY_WARDEN,
            status=FactoryBlockStatus.SUCCEEDED,
            artifact_refs=(quality.suite_ref, quality.summary_ref),
            evidence_refs=(quality.summary_ref, *quality.evaluation_refs),
            assertion_ids=quality.acceptance_assertion_ids,
            lease_id=request.lease.lease_id,
            generic_benchmark_quality=quality,
        )
