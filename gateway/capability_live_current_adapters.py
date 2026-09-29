"""Adapters from recorded Captain workflow facts to capability-live ports."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from agenten.agent_factory.capability_live_runner import (
    CapabilityLiveCapabilityEvidence,
    CapabilityLiveRestartRequest,
    CapabilityLiveStage,
    CapabilityLiveStageRequest,
)
from agenten.agent_factory.contracts import FactoryPhase
from agenten.agent_factory.skill_workflow_contracts import (
    TeamEvaluationV1,
    TeamExecutionEvidenceV1,
)
from agenten.agent_runtime.contracts import (
    ArtifactRef,
    RuntimeOperation,
    RuntimeStatus,
)
from agenten.delivery.minibook_events import (
    MinibookProjectionAcknowledgementV1,
    MinibookProjectionEvent,
)
from gateway.capability_live_pipeline import (
    CapabilityBusinessBenchmarkAuthority,
    CapabilityExecutionAuthority,
    CapabilityFactoryAuthority,
    CapabilityLiveAuthorityUnavailable,
    CapabilityProjectionAuthority,
    CapabilityResumeAuthorization,
    CapabilityResumeExecutionRequest,
)
from gateway.registry_feed import factory_promotion_projection


class CapabilityJobBindingPort(Protocol):
    def job_id_for(self, request: CapabilityLiveStageRequest) -> UUID | None: ...


class CapabilityWorkflowCompositionPort(Protocol):
    async def run(self, job_id: UUID, **kwargs: object) -> object: ...

    def projection(self, job_id: UUID) -> object: ...

    def snapshot(self, job_id: UUID) -> object: ...

    def workflow_artifacts(self, job_id: UUID) -> tuple[object, ...]: ...

    def workflow_budget_projection(self, job_id: UUID) -> object: ...

    def runtime_operation(self, command_id: UUID) -> object: ...

    def business_benchmark_summary_by_artifact(self, reference: ArtifactRef) -> object | None: ...


@dataclass(frozen=True)
class CapabilityProjectionReadback:
    """One durable, redacted Minibook acknowledgement observed by Captain."""

    event: MinibookProjectionEvent
    acknowledgement: MinibookProjectionAcknowledgementV1
    evidence_ref: ArtifactRef


class CapabilityProjectionEffectsPort(Protocol):
    async def project_and_ack(
        self, event: MinibookProjectionEvent
    ) -> CapabilityProjectionReadback: ...

    async def readback(self, event_id: UUID) -> CapabilityProjectionReadback | None: ...


class CapabilityResumeExecutionPort(Protocol):
    """Current Gateway authority plus effectful runtime resume boundary."""

    async def authorize_resume(
        self,
        request: CapabilityResumeExecutionRequest,
    ) -> CapabilityResumeAuthorization | None: ...

    async def dispatch_resume(
        self,
        authorization: CapabilityResumeAuthorization,
    ) -> CapabilityExecutionAuthority | None: ...

    async def readback_resume(
        self,
        authorization: CapabilityResumeAuthorization,
    ) -> CapabilityExecutionAuthority | None: ...


class GatewayCapabilityBusinessBenchmarkAdapter:
    """Read Captain's benchmark result after its current runner advances it."""

    def __init__(
        self,
        *,
        composition: CapabilityWorkflowCompositionPort,
        bindings: CapabilityJobBindingPort,
    ) -> None:
        self._composition = composition
        self._bindings = bindings

    async def evaluate(
        self, request: CapabilityLiveStageRequest
    ) -> CapabilityBusinessBenchmarkAuthority | None:
        job_id = self._bindings.job_id_for(request)
        if job_id is None:
            raise CapabilityLiveAuthorityUnavailable(
                "current Factory job binding is unavailable for benchmark"
            )
        await self._composition.run(job_id)
        artifacts = self._composition.workflow_artifacts(job_id)
        evaluation = _one(artifacts, TeamEvaluationV1, "benchmark evaluation")
        summary = self._composition.business_benchmark_summary_by_artifact(
            evaluation.benchmark_summary_ref
        )
        if summary is None:
            raise CapabilityLiveAuthorityUnavailable(
                "current business benchmark summary is unavailable"
            )
        runs = _successful_runs(artifacts)
        if len(runs) < 3:
            raise CapabilityLiveAuthorityUnavailable(
                "business benchmark lacks three successful release runs"
            )
        job = getattr(self._composition.projection(job_id), "job", None)
        if job is None:
            raise CapabilityLiveAuthorityUnavailable("Factory projection is unavailable")
        return CapabilityBusinessBenchmarkAuthority(
            stage=CapabilityLiveStage.EVALUATE,
            input_id=request.input.input_id,
            source_ref=request.input.source_ref,
            job_id=job_id,
            correlation_id=request.correlation_id,
            subject_version=job.subject_version,
            capability_id=job.required_capability,
            capability_version=job.subject_version,
            suite_ref=request.suite_ref,
            hermes_result_ref=request.hermes_result_ref,
            evidence_refs=_refs((*runs, evaluation, summary)),
            booked_cost_usd=_consumed_cost(self._composition, job_id),
            normal_trace_ids=tuple(run.execution_outcome.result_id for run in runs),
            benchmark_authority_id=evaluation.invocation_id,
            benchmark_summary_ref=summary.artifact_ref,
        )


class RecordedFactoryCapabilityExecutionAdapter:
    """Read release evidence and require a fresh Gateway-authorized resume."""

    def __init__(
        self,
        *,
        composition: CapabilityWorkflowCompositionPort,
        resume_execution: CapabilityResumeExecutionPort | None = None,
    ) -> None:
        self._composition = composition
        self._resume_execution = resume_execution

    async def execute(
        self,
        request: CapabilityLiveStageRequest,
        release: CapabilityFactoryAuthority,
    ) -> CapabilityExecutionAuthority | None:
        return self._authority(request, release)

    async def readback(
        self,
        request: CapabilityLiveStageRequest,
        release: CapabilityFactoryAuthority,
    ) -> CapabilityExecutionAuthority | None:
        return self._authority(request, release)

    async def restart_and_resume(
        self,
        request: CapabilityLiveRestartRequest,
        release: CapabilityFactoryAuthority,
    ) -> CapabilityExecutionAuthority | None:
        capability = request.capability
        projection = self._composition.projection(release.job_id)
        if (
            capability.job_id != release.job_id
            or capability.correlation_id != release.correlation_id
            or capability.capability_id != release.capability_id
            or capability.capability_version != release.capability_version
            or request.reservation.run_id != request.run_id
            or request.reservation.input_id != capability.input_id
            or getattr(projection, "phase", None) is not FactoryPhase.CAPABILITY_PROMOTED
        ):
            raise CapabilityLiveAuthorityUnavailable(
                "restart capability changed the stable release binding"
            )
        if self._resume_execution is None:
            raise CapabilityLiveAuthorityUnavailable(
                "restart authorization receipt is unavailable"
            )
        resumable = self._resumable_execution(release)
        if resumable is None:
            raise CapabilityLiveAuthorityUnavailable(
                "restart resumable Runtime execution is unavailable"
            )
        failed_run, operation = resumable
        budget = self._composition.workflow_budget_projection(release.job_id)
        authorization_request = CapabilityResumeExecutionRequest(
            job_id=release.job_id,
            reservation_id=request.reservation.reservation_id,
            run_id=request.run_id,
            input_id=request.input.input_id,
            maximum_cost_usd=budget.limit_usd,
            booked_cost_usd=budget.consumed_usd,
            correlation_id=release.correlation_id,
            capability_id=release.capability_id,
            capability_version=release.capability_version,
            original_execution_command_id=failed_run.execution_outcome.command_id,
            original_execution_result_id=operation.result.event_id,
        )
        authorization = await self._resume_execution.authorize_resume(
            authorization_request
        )
        if authorization is None:
            raise CapabilityLiveAuthorityUnavailable(
                "restart authorization receipt is unavailable"
            )
        expected_authorization = (
            release.job_id,
            request.reservation.reservation_id,
            release.correlation_id,
            release.capability_id,
            release.capability_version,
        )
        observed_authorization = (
            authorization.job_id,
            authorization.reservation_id,
            authorization.correlation_id,
            authorization.capability_id,
            authorization.capability_version,
        )
        if observed_authorization != expected_authorization:
            raise CapabilityLiveAuthorityUnavailable(
                "restart authorization changed the stable release binding"
            )
        dispatched = await self._resume_execution.dispatch_resume(authorization)
        if dispatched is None:
            raise CapabilityLiveAuthorityUnavailable(
                "restart dispatch result is unavailable"
            )
        self._require_fresh_resume(
            dispatched,
            authorization=authorization,
            capability=capability,
            release=release,
        )
        readback = await self._resume_execution.readback_resume(authorization)
        if readback is None:
            raise CapabilityLiveAuthorityUnavailable(
                "restart read-back result is unavailable"
            )
        self._require_fresh_resume(
            readback,
            authorization=authorization,
            capability=capability,
            release=release,
        )
        if readback != dispatched:
            raise CapabilityLiveAuthorityUnavailable(
                "restart read-back does not match dispatched result"
            )
        return readback

    def _resumable_execution(
        self,
        release: CapabilityFactoryAuthority,
    ) -> tuple[TeamExecutionEvidenceV1, object] | None:
        candidates: list[tuple[TeamExecutionEvidenceV1, object]] = []
        for artifact in self._composition.workflow_artifacts(release.job_id):
            if not isinstance(artifact, TeamExecutionEvidenceV1):
                continue
            outcome = artifact.execution_outcome
            if artifact.status == "succeeded":
                continue
            operation = self._composition.runtime_operation(outcome.command_id)
            result = getattr(operation, "result", None)
            command = getattr(operation, "command", None)
            if (
                result is None
                or command is None
                or getattr(operation, "operation_id", None) != outcome.command_id
                or result.event_id != outcome.result_id
                or result.command_id != outcome.command_id
                or command.payload.operation is not RuntimeOperation.CODEX_RUN
                or result.operation is not RuntimeOperation.CODEX_RUN
                or result.correlation_id != release.correlation_id
                or result.subject_version != release.capability_version
                or not result.evidence_refs
            ):
                continue
            resumable = result.status in {RuntimeStatus.FAILED, RuntimeStatus.CANCELLED}
            timed_out = (
                result.status is RuntimeStatus.INFRASTRUCTURE_FAILED
                and result.error == "codex execution timed out (exit 124)"
            )
            if resumable or timed_out:
                candidates.append((artifact, operation))
        if len(candidates) != 1:
            return None
        return candidates[0]

    @staticmethod
    def _require_fresh_resume(
        execution: CapabilityExecutionAuthority,
        *,
        authorization: CapabilityResumeAuthorization,
        capability: CapabilityLiveCapabilityEvidence,
        release: CapabilityFactoryAuthority,
    ) -> None:
        if (
            execution.job_id != release.job_id
            or execution.correlation_id != release.correlation_id
            or execution.capability_id != release.capability_id
            or execution.capability_version != release.capability_version
            or execution.execution_authority_id
            != authorization.authorization_receipt_id
            or execution.booked_cost_usd
            != release.booked_cost_usd
            + (execution.resume_actual_spend_usd or Decimal("0"))
            or execution.resume_budget_violation
        ):
            raise CapabilityLiveAuthorityUnavailable(
                "restart execution changed the stable release binding"
            )
        if execution.execution_command_id == capability.execution_command_id:
            raise CapabilityLiveAuthorityUnavailable(
                "restart execution reused original command"
            )
        if execution.execution_result_id == capability.execution_result_id:
            raise CapabilityLiveAuthorityUnavailable(
                "restart execution reused original result"
            )
        if execution.replayed:
            raise CapabilityLiveAuthorityUnavailable(
                "restart execution cannot count an evidence replay"
            )
        if execution.terminal_status != "succeeded":
            raise CapabilityLiveAuthorityUnavailable(
                "restart requires a successful terminal result"
            )

    def _authority(
        self,
        request: CapabilityLiveStageRequest,
        release: CapabilityFactoryAuthority,
    ) -> CapabilityExecutionAuthority:
        if request.input.input_id != release.input_id:
            raise CapabilityLiveAuthorityUnavailable("execution input does not match release")
        return self._authority_for_release(release)

    def _authority_for_release(
        self,
        release: CapabilityFactoryAuthority,
    ) -> CapabilityExecutionAuthority:
        projection = self._composition.projection(release.job_id)
        if getattr(projection, "phase", None) is not FactoryPhase.CAPABILITY_PROMOTED:
            raise CapabilityLiveAuthorityUnavailable("released Factory execution is unavailable")
        runs = _successful_runs(self._composition.workflow_artifacts(release.job_id))
        if len(runs) < 3:
            raise CapabilityLiveAuthorityUnavailable("release lacks a third successful run")
        selected = sorted(runs, key=lambda run: run.run_number)[2]
        outcome = selected.execution_outcome
        if (
            outcome.capability_id != release.capability_id
            or outcome.capability_version != release.capability_version
            or outcome.correlation_id != release.correlation_id
        ):
            raise CapabilityLiveAuthorityUnavailable(
                "recorded execution does not match released capability"
            )
        return CapabilityExecutionAuthority(
            job_id=release.job_id,
            correlation_id=release.correlation_id,
            capability_id=release.capability_id,
            capability_version=release.capability_version,
            execution_authority_id=selected.invocation_id,
            execution_command_id=outcome.command_id,
            execution_result_id=outcome.result_id,
            evidence_refs=_refs((selected,)),
            booked_cost_usd=_consumed_cost(self._composition, release.job_id),
            terminal_status="succeeded",
            replayed=True,
        )


class GatewayMinibookCapabilityProjectionAdapter:
    """Project the exact promotion event and require Minibook acknowledgement."""

    def __init__(
        self,
        *,
        composition: CapabilityWorkflowCompositionPort,
        effects: CapabilityProjectionEffectsPort,
    ) -> None:
        self._composition = composition
        self._effects = effects

    async def project_and_readback(
        self,
        request: CapabilityLiveStageRequest,
        release: CapabilityFactoryAuthority,
        execution: CapabilityExecutionAuthority,
    ) -> CapabilityProjectionAuthority | None:
        del request
        event = self._event(release)
        readback = await self._effects.project_and_ack(event)
        return self._authority(release, execution, event, readback)

    async def readback(
        self,
        capability: CapabilityLiveCapabilityEvidence,
        release: CapabilityFactoryAuthority,
        execution: CapabilityExecutionAuthority,
    ) -> CapabilityProjectionAuthority | None:
        del capability
        event = self._event(release)
        readback = await self._effects.readback(event.event_id)
        if readback is None:
            raise CapabilityLiveAuthorityUnavailable("Minibook projection read-back is unavailable")
        return self._authority(release, execution, event, readback)

    def _event(self, release: CapabilityFactoryAuthority) -> MinibookProjectionEvent:
        snapshot = self._composition.snapshot(release.job_id)
        job = getattr(snapshot, "job", None)
        blocks = tuple(getattr(snapshot, "blocks", ()))
        promotion = tuple(
            block
            for block in blocks
            if getattr(block, "phase", None) is FactoryPhase.CAPABILITY_PROMOTED
            and getattr(block, "event_id", None) == release.release_authority_id
        )
        if job is None or len(promotion) != 1:
            raise CapabilityLiveAuthorityUnavailable("Gateway promotion event is unavailable")
        summary = (
            None
            if release.benchmark_summary_ref is None
            else self._composition.business_benchmark_summary_by_artifact(
                release.benchmark_summary_ref
            )
        )
        if summary is None:
            raise CapabilityLiveAuthorityUnavailable("Gateway benchmark summary is unavailable")
        return factory_promotion_projection(
            promotion[0].model_dump(mode="json", by_alias=True),
            job.model_dump(mode="json", by_alias=True),
            benchmark_summary=summary,
        )

    @staticmethod
    def _authority(
        release: CapabilityFactoryAuthority,
        execution: CapabilityExecutionAuthority,
        event: MinibookProjectionEvent,
        readback: CapabilityProjectionReadback,
    ) -> CapabilityProjectionAuthority:
        acknowledgement = readback.acknowledgement
        if (
            readback.event != event
            or acknowledgement.projection_event_id != event.event_id
            or acknowledgement.correlation_id != release.correlation_id
            or acknowledgement.subject_version != release.capability_version
        ):
            raise CapabilityLiveAuthorityUnavailable(
                "Minibook acknowledgement does not match Gateway promotion"
            )
        return CapabilityProjectionAuthority(
            job_id=release.job_id,
            correlation_id=release.correlation_id,
            capability_id=release.capability_id,
            capability_version=release.capability_version,
            execution_command_id=execution.execution_command_id,
            execution_result_id=execution.execution_result_id,
            projection_event_id=event.event_id,
            projection_readback_id=acknowledgement.acknowledgement_id,
            evidence_refs=(readback.evidence_ref,),
        )


def _one(items: tuple[object, ...], kind: type[object], label: str) -> object:
    matches = tuple(item for item in items if isinstance(item, kind))
    if len(matches) != 1:
        raise CapabilityLiveAuthorityUnavailable(f"current {label} is unavailable")
    return matches[0]


def _successful_runs(items: tuple[object, ...]) -> tuple[TeamExecutionEvidenceV1, ...]:
    runs = tuple(
        item
        for item in items
        if isinstance(item, TeamExecutionEvidenceV1) and item.status == "succeeded"
    )
    if len({run.invocation_id for run in runs}) != len(runs):
        raise CapabilityLiveAuthorityUnavailable("recorded release runs are not distinct")
    return runs


def _refs(items: tuple[object, ...]) -> tuple[ArtifactRef, ...]:
    references: list[ArtifactRef] = []
    for item in items:
        reference = getattr(item, "artifact_ref", None)
        if isinstance(reference, ArtifactRef):
            references.append(reference)
        references.extend(
            reference
            for reference in getattr(item, "evidence_refs", ())
            if isinstance(reference, ArtifactRef)
        )
    result = tuple(dict.fromkeys(references))
    if not result:
        raise CapabilityLiveAuthorityUnavailable("current authority lacks evidence refs")
    return result


def _consumed_cost(composition: CapabilityWorkflowCompositionPort, job_id: UUID):
    return composition.workflow_budget_projection(job_id).consumed_usd


__all__ = [
    "CapabilityProjectionReadback",
    "CapabilityResumeExecutionPort",
    "GatewayCapabilityBusinessBenchmarkAdapter",
    "GatewayMinibookCapabilityProjectionAdapter",
    "RecordedFactoryCapabilityExecutionAdapter",
]
