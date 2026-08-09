"""Current-authority orchestration for the three-input capability live runner.

This module owns no Factory, benchmark, execution, or Minibook decisions.  It
orders four typed authority ports and rejects missing or cross-boundary
receipts before the generic live runner can persist a successful checkpoint.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agenten.agent_factory.capability_live_runner import (
    CapabilityLiveCapabilityEvidence,
    CapabilityLivePipelinePort,
    CapabilityLiveRestartEvidence,
    CapabilityLiveRestartRequest,
    CapabilityLiveStage,
    CapabilityLiveStageRequest,
    CapabilityLiveStageResult,
)
from agenten.agent_runtime.contracts import ArtifactRef


class CapabilityLiveAuthorityUnavailable(RuntimeError):
    """A current external authority required for a success claim is absent."""


class _FrozenAuthority(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class CapabilityFactoryAuthority(_FrozenAuthority):
    """Redacted observation issued from the current Captain/Gateway lifecycle."""

    stage: CapabilityLiveStage
    input_id: str = Field(min_length=1)
    source_ref: ArtifactRef
    job_id: UUID
    correlation_id: UUID
    subject_version: int = Field(ge=1, strict=True)
    capability_id: str = Field(min_length=1)
    capability_version: int = Field(ge=1, strict=True)
    suite_ref: ArtifactRef
    hermes_result_ref: ArtifactRef
    evidence_refs: tuple[ArtifactRef, ...] = Field(min_length=1)
    booked_cost_usd: Decimal
    controlled_recovery_id: UUID | None = None
    normal_trace_ids: tuple[UUID, ...] = ()
    benchmark_authority_id: UUID | None = None
    benchmark_summary_ref: ArtifactRef | None = None
    release_authority_id: UUID | None = None

    @field_validator("booked_cost_usd", mode="before")
    @classmethod
    def require_known_cost(cls, value: object) -> Decimal:
        return _nonnegative_decimal(value, "factory booked cost")

    @model_validator(mode="after")
    def require_promotion_authority(self) -> "CapabilityFactoryAuthority":
        if self.stage is not CapabilityLiveStage.PROMOTE:
            return self
        if (
            self.release_authority_id is None
            or self.benchmark_authority_id is None
            or self.benchmark_summary_ref is None
            or len(self.normal_trace_ids) < 3
            or len(set(self.normal_trace_ids)) != len(self.normal_trace_ids)
        ):
            raise ValueError(
                "promoted Factory authority requires benchmark, traces, and release"
            )
        return self


class CapabilityBusinessBenchmarkAuthority(CapabilityFactoryAuthority):
    """Captain-accepted benchmark state for the exact current Factory job."""

    benchmark_authority_id: UUID
    benchmark_summary_ref: ArtifactRef
    normal_trace_ids: tuple[UUID, ...] = Field(min_length=3)

    @model_validator(mode="after")
    def require_evaluation_stage(self) -> "CapabilityBusinessBenchmarkAuthority":
        if self.stage is not CapabilityLiveStage.EVALUATE:
            raise ValueError("business benchmark authority must describe evaluate")
        if len(set(self.normal_trace_ids)) != len(self.normal_trace_ids):
            raise ValueError("business benchmark trace identities must be distinct")
        return self


class CapabilityExecutionAuthority(_FrozenAuthority):
    """Gateway-recorded execution IDs; no ID is minted by this orchestrator."""

    job_id: UUID
    correlation_id: UUID
    capability_id: str = Field(min_length=1)
    capability_version: int = Field(ge=1, strict=True)
    execution_authority_id: UUID
    execution_command_id: UUID
    execution_result_id: UUID
    evidence_refs: tuple[ArtifactRef, ...] = Field(min_length=1)
    booked_cost_usd: Decimal
    terminal_status: Literal["succeeded", "cancelled", "timed_out", "failed"]
    replayed: bool

    @field_validator("booked_cost_usd", mode="before")
    @classmethod
    def require_known_cost(cls, value: object) -> Decimal:
        return _nonnegative_decimal(value, "execution booked cost")


class CapabilityProjectionAuthority(_FrozenAuthority):
    """Exact Gateway event and durable Minibook read-back acknowledgement."""

    job_id: UUID
    correlation_id: UUID
    capability_id: str = Field(min_length=1)
    capability_version: int = Field(ge=1, strict=True)
    execution_command_id: UUID
    execution_result_id: UUID
    projection_event_id: UUID
    projection_readback_id: UUID
    evidence_refs: tuple[ArtifactRef, ...] = Field(min_length=1)


class CapabilityResumeAuthorization(_FrozenAuthority):
    """Current Gateway receipt permitting one exact fresh resume dispatch."""

    job_id: UUID
    reservation_id: UUID
    correlation_id: UUID
    capability_id: str = Field(min_length=1)
    capability_version: int = Field(ge=1, strict=True)
    authorization_receipt_id: UUID
    evidence_refs: tuple[ArtifactRef, ...] = Field(min_length=1)


class CapabilityResumeExecutionRequest(_FrozenAuthority):
    """Stable original identity submitted to Gateway for resume authorization."""

    job_id: UUID
    reservation_id: UUID
    run_id: UUID
    input_id: str = Field(min_length=1)
    maximum_cost_usd: Decimal
    correlation_id: UUID
    capability_id: str = Field(min_length=1)
    capability_version: int = Field(ge=1, strict=True)
    original_execution_command_id: UUID
    original_execution_result_id: UUID

    @field_validator("maximum_cost_usd", mode="before")
    @classmethod
    def require_resume_ceiling(cls, value: object) -> Decimal:
        amount = _nonnegative_decimal(value, "resume maximum cost")
        if amount <= 0:
            raise ValueError("resume maximum cost must be positive")
        return amount


class CapabilityFactoryPort(Protocol):
    """Generic provisioning plus current AgentFactoryLiveComposition stages."""

    async def scaffold(
        self, request: CapabilityLiveStageRequest
    ) -> CapabilityFactoryAuthority | None: ...

    async def implement(
        self, request: CapabilityLiveStageRequest
    ) -> CapabilityFactoryAuthority | None: ...

    async def seal(
        self, request: CapabilityLiveStageRequest
    ) -> CapabilityFactoryAuthority | None: ...

    async def promote(
        self, request: CapabilityLiveStageRequest
    ) -> CapabilityFactoryAuthority | None: ...

    async def released(
        self, request: CapabilityLiveStageRequest
    ) -> CapabilityFactoryAuthority | None: ...


class CapabilityBusinessBenchmarkPort(Protocol):
    """Current business benchmark composition for one exact Factory job."""

    async def evaluate(
        self, request: CapabilityLiveStageRequest
    ) -> CapabilityBusinessBenchmarkAuthority | None: ...


class PromotedCapabilityExecutionPort(Protocol):
    """Execute, or verify recorded execution, under current Gateway authority."""

    async def execute(
        self,
        request: CapabilityLiveStageRequest,
        release: CapabilityFactoryAuthority,
    ) -> CapabilityExecutionAuthority | None: ...

    async def readback(
        self,
        request: CapabilityLiveStageRequest,
        release: CapabilityFactoryAuthority,
    ) -> CapabilityExecutionAuthority | None: ...

    async def restart_and_resume(
        self,
        request: CapabilityLiveRestartRequest,
        release: CapabilityFactoryAuthority,
    ) -> CapabilityExecutionAuthority | None: ...


class CapabilityProjectionPort(Protocol):
    """Gateway feed -> Minibook projection -> acknowledgement/read-back seam."""

    async def project_and_readback(
        self,
        request: CapabilityLiveStageRequest,
        release: CapabilityFactoryAuthority,
        execution: CapabilityExecutionAuthority,
    ) -> CapabilityProjectionAuthority | None: ...

    async def readback(
        self,
        capability: CapabilityLiveCapabilityEvidence,
        release: CapabilityFactoryAuthority,
        execution: CapabilityExecutionAuthority,
    ) -> CapabilityProjectionAuthority | None: ...


class GatewayCapabilityLivePipeline(CapabilityLivePipelinePort):
    """Order all seven stages while leaving every decision at its current port."""

    def __init__(
        self,
        *,
        factory: CapabilityFactoryPort,
        benchmark: CapabilityBusinessBenchmarkPort,
        execution: PromotedCapabilityExecutionPort,
        projection: CapabilityProjectionPort,
    ) -> None:
        if any(item is None for item in (factory, benchmark, execution, projection)):
            raise ValueError("capability live authority ports are required")
        self._factory = factory
        self._benchmark = benchmark
        self._execution = execution
        self._projection = projection

    async def run_stage(
        self,
        request: CapabilityLiveStageRequest,
    ) -> CapabilityLiveStageResult:
        if request.stage is CapabilityLiveStage.SCAFFOLD_AND_TEST_HARNESS:
            authority = await self._factory.scaffold(request)
            return self._factory_result(
                request,
                authority,
                missing="factory job authority is unavailable",
            )
        if request.stage is CapabilityLiveStage.CODEX_IMPLEMENTATION:
            authority = await self._factory.implement(request)
            return self._factory_result(
                request,
                authority,
                missing="Codex implementation authority is unavailable",
            )
        if request.stage is CapabilityLiveStage.SEAL:
            authority = await self._factory.seal(request)
            return self._factory_result(
                request,
                authority,
                missing="Captain seal authority is unavailable",
            )
        if request.stage is CapabilityLiveStage.EVALUATE:
            authority = await self._benchmark.evaluate(request)
            if authority is None:
                raise CapabilityLiveAuthorityUnavailable(
                    "business benchmark authority is unavailable"
                )
            return self._factory_result(
                request,
                authority,
                missing="business benchmark authority is unavailable",
            )
        if request.stage is CapabilityLiveStage.PROMOTE:
            authority = await self._factory.promote(request)
            return self._factory_result(
                request,
                authority,
                missing="release authority is unavailable",
            )
        if request.stage is CapabilityLiveStage.EXECUTE:
            release = await self._released(request)
            execution = await self._execution.execute(request, release)
            if execution is None:
                raise CapabilityLiveAuthorityUnavailable(
                    "execution authority is unavailable"
                )
            self._require_execution(request, release, execution)
            return CapabilityLiveStageResult(
                stage=request.stage,
                suite_ref=release.suite_ref,
                hermes_result_ref=release.hermes_result_ref,
                evidence_refs=_unique_refs(
                    (*release.evidence_refs, *execution.evidence_refs)
                ),
                booked_cost_usd=execution.booked_cost_usd,
            )
        if request.stage is CapabilityLiveStage.PROJECT:
            release = await self._released(request)
            execution = await self._execution.readback(request, release)
            if execution is None:
                raise CapabilityLiveAuthorityUnavailable(
                    "execution authority is unavailable"
                )
            self._require_execution(request, release, execution)
            projection = await self._projection.project_and_readback(
                request,
                release,
                execution,
            )
            if projection is None:
                raise CapabilityLiveAuthorityUnavailable(
                    "Minibook projection authority is unavailable"
                )
            self._require_projection(release, execution, projection)
            capability = CapabilityLiveCapabilityEvidence(
                input_id=request.input.input_id,
                conversation_pattern=request.input.conversation_pattern,
                job_id=release.job_id,
                correlation_id=request.correlation_id,
                capability_id=release.capability_id,
                capability_version=release.capability_version,
                controlled_recovery_id=release.controlled_recovery_id,
                normal_trace_ids=release.normal_trace_ids,
                release_authority_id=release.release_authority_id,
                execution_authority_id=execution.execution_authority_id,
                execution_command_id=execution.execution_command_id,
                execution_result_id=execution.execution_result_id,
                projection_event_id=projection.projection_event_id,
                projection_readback_id=projection.projection_readback_id,
                suite_ref=release.suite_ref,
                hermes_result_ref=release.hermes_result_ref,
                booked_cost_usd=execution.booked_cost_usd,
                checkpoint_refs=(),
            )
            return CapabilityLiveStageResult(
                stage=request.stage,
                suite_ref=release.suite_ref,
                hermes_result_ref=release.hermes_result_ref,
                evidence_refs=_unique_refs(
                    (
                        *release.evidence_refs,
                        *execution.evidence_refs,
                        *projection.evidence_refs,
                    )
                ),
                booked_cost_usd=execution.booked_cost_usd,
                capability_evidence=capability,
            )
        raise ValueError("unsupported capability live stage")

    async def restart_and_resume(
        self,
        request: CapabilityLiveRestartRequest,
    ) -> CapabilityLiveRestartEvidence:
        capability = request.capability
        stage_request = CapabilityLiveStageRequest(
            run_id=request.run_id,
            input=request.input,
            correlation_id=capability.correlation_id,
            reservation=request.reservation,
            stage=CapabilityLiveStage.PROJECT,
            controlled_recovery_required=capability.controlled_recovery_id is not None,
            suite_ref=capability.suite_ref,
            hermes_result_ref=capability.hermes_result_ref,
            retry_number=0,
        )
        release = await self._released(stage_request)
        if (
            capability.job_id != release.job_id
            or capability.correlation_id != release.correlation_id
            or capability.capability_id != release.capability_id
            or capability.capability_version != release.capability_version
        ):
            raise CapabilityLiveAuthorityUnavailable(
                "restart capability changed the stable release binding"
            )
        execution = await self._execution.restart_and_resume(request, release)
        if execution is None:
            raise CapabilityLiveAuthorityUnavailable(
                "restart execution authority is unavailable"
            )
        self._require_execution(stage_request, release, execution)
        projection = await self._projection.readback(
            capability,
            release,
            execution,
        )
        if projection is None:
            raise CapabilityLiveAuthorityUnavailable(
                "restart Minibook projection read-back is unavailable"
        )
        self._require_projection(release, execution, projection)
        if execution.replayed:
            raise CapabilityLiveAuthorityUnavailable(
                "restart execution cannot count an evidence replay as a new run"
            )
        if execution.execution_command_id == capability.execution_command_id:
            raise CapabilityLiveAuthorityUnavailable(
                "restart execution reused original command"
            )
        if execution.execution_result_id == capability.execution_result_id:
            raise CapabilityLiveAuthorityUnavailable(
                "restart execution reused original result"
            )
        if (
            projection.projection_event_id != capability.projection_event_id
            or projection.projection_readback_id != capability.projection_readback_id
        ):
            raise CapabilityLiveAuthorityUnavailable(
                "restart projection changed after resume"
            )
        return CapabilityLiveRestartEvidence(
            run_id=request.run_id,
            job_id=release.job_id,
            reservation_id=request.reservation.reservation_id,
            correlation_id=release.correlation_id,
            capability_id=release.capability_id,
            capability_version=release.capability_version,
            original_execution_command_id=capability.execution_command_id,
            original_execution_result_id=capability.execution_result_id,
            resumed_execution_authority_id=execution.execution_authority_id,
            resumed_execution_command_id=execution.execution_command_id,
            resumed_execution_result_id=execution.execution_result_id,
            projection_event_id=projection.projection_event_id,
            projection_readback_id=projection.projection_readback_id,
        )

    async def _released(
        self,
        request: CapabilityLiveStageRequest,
    ) -> CapabilityFactoryAuthority:
        release = await self._factory.released(request)
        if release is None:
            raise CapabilityLiveAuthorityUnavailable("release authority is unavailable")
        self._require_factory(request, release, expected=CapabilityLiveStage.PROMOTE)
        return release

    def _factory_result(
        self,
        request: CapabilityLiveStageRequest,
        authority: CapabilityFactoryAuthority | None,
        *,
        missing: str,
    ) -> CapabilityLiveStageResult:
        if authority is None:
            raise CapabilityLiveAuthorityUnavailable(missing)
        self._require_factory(request, authority, expected=request.stage)
        return CapabilityLiveStageResult(
            stage=request.stage,
            suite_ref=authority.suite_ref,
            hermes_result_ref=authority.hermes_result_ref,
            evidence_refs=authority.evidence_refs,
            booked_cost_usd=authority.booked_cost_usd,
        )

    @staticmethod
    def _require_factory(
        request: CapabilityLiveStageRequest,
        authority: CapabilityFactoryAuthority,
        *,
        expected: CapabilityLiveStage,
    ) -> None:
        if (
            authority.stage is not expected
            or authority.input_id != request.input.input_id
            or authority.source_ref != request.input.source_ref
            or authority.correlation_id != request.correlation_id
            or authority.booked_cost_usd > request.reservation.maximum_cost_usd
        ):
            raise CapabilityLiveAuthorityUnavailable(
                "Factory authority does not match the live stage request"
            )
        if request.suite_ref is not None and authority.suite_ref != request.suite_ref:
            raise CapabilityLiveAuthorityUnavailable(
                "Factory authority changed the benchmark suite"
            )
        if (
            request.hermes_result_ref is not None
            and authority.hermes_result_ref != request.hermes_result_ref
        ):
            raise CapabilityLiveAuthorityUnavailable(
                "Factory authority changed the Hermes result"
            )
        if expected is CapabilityLiveStage.PROMOTE:
            if request.controlled_recovery_required and (
                authority.controlled_recovery_id is None
            ):
                raise CapabilityLiveAuthorityUnavailable(
                    "release authority lacks controlled recovery evidence"
                )

    @staticmethod
    def _require_execution(
        request: CapabilityLiveStageRequest,
        release: CapabilityFactoryAuthority,
        execution: CapabilityExecutionAuthority,
    ) -> None:
        if (
            execution.job_id != release.job_id
            or execution.correlation_id != release.correlation_id
            or execution.capability_id != release.capability_id
            or execution.capability_version != release.capability_version
            or execution.booked_cost_usd != release.booked_cost_usd
            or execution.booked_cost_usd > request.reservation.maximum_cost_usd
            or execution.terminal_status != "succeeded"
        ):
            raise CapabilityLiveAuthorityUnavailable(
                "execution authority does not match the promoted capability"
            )

    @staticmethod
    def _require_projection(
        release: CapabilityFactoryAuthority,
        execution: CapabilityExecutionAuthority,
        projection: CapabilityProjectionAuthority,
    ) -> None:
        if (
            projection.job_id != release.job_id
            or projection.correlation_id != release.correlation_id
            or projection.capability_id != release.capability_id
            or projection.capability_version != release.capability_version
            or projection.execution_command_id != execution.execution_command_id
            or projection.execution_result_id != execution.execution_result_id
        ):
            raise CapabilityLiveAuthorityUnavailable(
                "Minibook projection does not match the Gateway execution"
            )


def _nonnegative_decimal(value: object, label: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a finite decimal")
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{label} must be a finite decimal") from None
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"{label} must be a non-negative finite decimal")
    return parsed


def _unique_refs(references: tuple[ArtifactRef, ...]) -> tuple[ArtifactRef, ...]:
    unique: list[ArtifactRef] = []
    identities: set[tuple[str, str, str]] = set()
    for reference in references:
        identity = (reference.uri, reference.sha256, reference.media_type)
        if identity in identities:
            continue
        identities.add(identity)
        unique.append(reference)
    if not unique:
        raise CapabilityLiveAuthorityUnavailable("authority evidence refs are unavailable")
    return tuple(unique)


__all__ = [
    "CapabilityBusinessBenchmarkAuthority",
    "CapabilityBusinessBenchmarkPort",
    "CapabilityExecutionAuthority",
    "CapabilityFactoryAuthority",
    "CapabilityFactoryPort",
    "CapabilityLiveAuthorityUnavailable",
    "CapabilityProjectionAuthority",
    "CapabilityProjectionPort",
    "CapabilityResumeAuthorization",
    "CapabilityResumeExecutionRequest",
    "GatewayCapabilityLivePipeline",
    "PromotedCapabilityExecutionPort",
]
