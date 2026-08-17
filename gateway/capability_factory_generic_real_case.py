"""Factory real-case bridge backed only by the admitted generic D10 suite."""

from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

from agenten.agent_factory.contracts import (
    AgentFactoryJobV3,
    FactoryBlockStatus,
    FactoryEvidenceBlock,
    FactoryPhase,
    FactoryRole,
    GenericBenchmarkQualityEvidenceV1,
)
from agenten.agent_factory.leases import validate_factory_lease
from agenten.agent_factory.orchestration import FactoryDispatch, FactoryDispatchError
from agenten.agent_factory.state_machine import FactoryActionKind


class GatewayD10GenericCandidateValidator:
    """Keep build validation concrete and bridge real-case evidence from D10."""

    def __init__(self, *, gateway_store: object, build_validator: object) -> None:
        lookup = getattr(gateway_store, "generic_benchmark_quality_evidence", None)
        if not callable(lookup):
            raise ValueError("D10 generic quality authority is invalid")
        if (
            not callable(getattr(build_validator, "dispatch", None))
            or not callable(getattr(build_validator, "candidate_for", None))
        ):
            raise ValueError("Factory build validator authority is invalid")
        self._gateway_store = gateway_store
        self._lookup = lookup
        self._build_validator = build_validator

    def candidate_for(self, job: AgentFactoryJobV3):
        return self._build_validator.candidate_for(job)

    async def record_creation_result(self, request, result):
        method = getattr(self._build_validator, "record_creation_result", None)
        if not callable(method):
            raise FactoryDispatchError("Factory creation-result authority is unavailable")
        return await method(request, result)

    async def dispatch(self, request: FactoryDispatch) -> FactoryEvidenceBlock:
        kind = request.action.kind
        if kind not in {
            FactoryActionKind.DISPATCH_REAL_CASE_TESTER,
            FactoryActionKind.DISPATCH_TECHNICAL_REVALIDATION,
        }:
            return await self._build_validator.dispatch(request)
        if kind is FactoryActionKind.DISPATCH_TECHNICAL_REVALIDATION:
            raise FactoryDispatchError(
                "generic D10 evidence cannot be replaced by legacy technical revalidation"
            )
        job = request.job
        attempt = request.action.attempt
        quality = self._lookup(getattr(job, "job_id", None), attempt)
        if type(quality) is not GenericBenchmarkQualityEvidenceV1:
            raise FactoryDispatchError("D10 generic quality evidence is unavailable")
        if (
            type(job) is not AgentFactoryJobV3
            or request.role is not FactoryRole.REAL_CASE_TESTER
            or request.lease is None
            or request.lease.role is not FactoryRole.REAL_CASE_TESTER
        ):
            raise FactoryDispatchError("generic D10 real-case dispatch is invalid")
        validate_factory_lease(
            request.lease, job=job, role=FactoryRole.REAL_CASE_TESTER,
            attempt=attempt, now=request.lease.issued_at,
        )
        if (
            quality.job_id != job.job_id
            or quality.correlation_id != job.correlation_id
            or quality.subject_version != job.subject_version
            or quality.attempt != attempt
            or quality.acceptance_assertion_ids != job.acceptance_assertion_ids
            or quality.passed is not True
        ):
            raise FactoryDispatchError("D10 generic quality authority changed")
        candidate = self.candidate_for(job)
        candidate_value = getattr(candidate, "candidate", None)
        candidate_ref = getattr(candidate_value, "source_archive_ref", None)
        team_manifest = getattr(getattr(candidate_value, "team_manifest", None), "reference", None)
        if candidate_ref is None or team_manifest is None:
            raise FactoryDispatchError("released generic candidate is unavailable")
        return FactoryEvidenceBlock(
            schema="captain.agent-factory-block.v1",
            event_id=uuid5(
                NAMESPACE_URL,
                f"generic-d10-real-case|{job.job_id}|{attempt}|{quality.summary_ref.sha256}",
            ),
            job_id=job.job_id, correlation_id=job.correlation_id,
            causation_id=job.event_id, occurred_at=request.lease.issued_at,
            producer="hermes", subject_version=job.subject_version,
            attempt=attempt, phase=FactoryPhase.REAL_CASE_EVIDENCE,
            role=FactoryRole.REAL_CASE_TESTER,
            status=FactoryBlockStatus.SUCCEEDED,
            artifact_refs=(candidate_ref, team_manifest, quality.suite_ref),
            evidence_refs=(quality.summary_ref, *quality.evaluation_refs),
            assertion_ids=quality.acceptance_assertion_ids,
            lease_id=request.lease.lease_id,
        )


__all__ = ["GatewayD10GenericCandidateValidator"]
