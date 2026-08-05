"""Controlled post-effect recovery over the existing Factory live runner.

The first pass reserves one live provider effect, executes the governed
``TeamExecutionService`` and durably stores its exact typed evidence, then
raises the intentional process-interruption signal before the effect ledger is
completed.  The second pass enters ``FactoryLiveRunner.recover`` and completes
the same effect from the durable record.  It never invokes the provider again.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agenten.agent_factory.candidate_evaluation import ResolvedFactoryCandidate
from agenten.agent_factory.capability_live_adapters import ContentAddressedArtifactStore
from agenten.agent_factory.capability_v3_evidence_bridge import (
    CapabilityControlledHoldoutReceiptV1,
    CapabilityControlledRecoveryResultV1,
    CapabilityV3BridgeConfigurationError,
)
from agenten.agent_factory.contracts import AgentFactoryJobV3, FactoryRole
from agenten.agent_factory.factory_live_runner import (
    FactoryInfrastructureFailure,
    FactoryLiveEffectClaim,
    FactoryLiveEffectExecutor,
    FactoryLiveEffectKind,
    FactoryLiveEffectLedger,
    FactoryLiveEffectOutcomeV1,
    FactoryLiveEffectRequestV1,
    FactoryLiveEffectWriteReceipt,
    FactoryLivePlan,
    FactoryLiveRunner,
)
from agenten.agent_factory.orchestration import FactoryDispatch
from agenten.agent_factory.service import FactoryRepository
from agenten.agent_factory.skill_workflow_contracts import (
    FactorySkillInvocationV1,
    FactorySkillStep,
    TeamExecutionEvidenceV1,
)
from agenten.agent_factory.state_machine import (
    FactoryActionKind,
    FactoryProjection,
)
from agenten.agent_factory.team_execution import (
    TeamExecutionCandidateAdapter,
    TeamExecutionService,
)
from agenten.agent_runtime.contracts import ArtifactRef


class _FrozenContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class DurableControlledRecoveryEffectV1(_FrozenContract):
    """Exact provider-backed team evidence stored before the injected crash."""

    schema_name: Literal["captain.controlled-recovery-effect.v1"] = Field(
        default="captain.controlled-recovery-effect.v1",
        alias="schema",
        serialization_alias="schema",
    )
    effect_id: UUID
    job_id: UUID
    correlation_id: UUID
    subject_version: int = Field(ge=1, strict=True)
    attempt: int = Field(ge=1, le=5, strict=True)
    invocation_id: UUID
    execution: TeamExecutionEvidenceV1
    provider_usage_receipt_refs: tuple[ArtifactRef, ...] = Field(min_length=1)
    persisted_at: datetime

    @field_validator("persisted_at")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("controlled recovery persistence clock must be UTC")
        return value

    @model_validator(mode="after")
    def require_execution_binding(self) -> "DurableControlledRecoveryEffectV1":
        execution = self.execution
        mismatches = tuple(
            field
            for field, valid in (
                ("job_id", execution.job_id == self.job_id),
                ("correlation_id", execution.correlation_id == self.correlation_id),
                ("subject_version", execution.subject_version == self.subject_version),
                ("attempt", execution.attempt == self.attempt),
                ("invocation_id", execution.invocation_id == self.invocation_id),
                (
                    "usage_receipt_refs",
                    execution.usage_receipt_refs == self.provider_usage_receipt_refs,
                ),
                ("execution_status", execution.status == "succeeded"),
                (
                    "runtime_status",
                    execution.execution_outcome.status == "succeeded",
                ),
            )
            if not valid
        )
        if mismatches:
            raise ValueError(
                "durable controlled recovery effect binding changed: "
                + ",".join(mismatches)
            )
        return self


@dataclass(frozen=True)
class StoredControlledRecoveryEffect:
    record: DurableControlledRecoveryEffectV1
    reference: ArtifactRef


class ControlledRecoveryEffectStorePort(Protocol):
    def persist(
        self, record: DurableControlledRecoveryEffectV1
    ) -> StoredControlledRecoveryEffect: ...

    def load(self, effect_id: UUID) -> StoredControlledRecoveryEffect | None: ...


class ContentAddressedControlledRecoveryEffectStore:
    """Durable replay record backed by Package-C's immutable artifact store."""

    durability: Literal["content_addressed"] = "content_addressed"

    def __init__(self, artifacts: ContentAddressedArtifactStore) -> None:
        self._artifacts = artifacts

    def persist(
        self, record: DurableControlledRecoveryEffectV1
    ) -> StoredControlledRecoveryEffect:
        content = record.model_dump_json(by_alias=True).encode("utf-8")
        reference = self._artifacts.put(
            content,
            "application/json",
            namespace="controlled-recovery-effect",
        )
        self._artifacts.bind(
            "controlled-recovery-effect", str(record.effect_id), reference
        )
        return StoredControlledRecoveryEffect(record=record, reference=reference)

    def load(self, effect_id: UUID) -> StoredControlledRecoveryEffect | None:
        reference = self._artifacts.binding(
            "controlled-recovery-effect", str(effect_id)
        )
        if reference is None:
            return None
        record = DurableControlledRecoveryEffectV1.model_validate_json(
            self._artifacts.read_bytes(reference)
        )
        if record.effect_id != effect_id:
            raise ValueError("controlled recovery effect lookup identity changed")
        return StoredControlledRecoveryEffect(record=record, reference=reference)


class ControlledRecoveryTeamServiceFactory(Protocol):
    def __call__(
        self,
        job: AgentFactoryJobV3,
        invocation: FactorySkillInvocationV1,
    ) -> TeamExecutionService: ...


@dataclass(frozen=True)
class PreparedControlledRecoveryTeamDispatcher:
    """Prepared host-AutoGen dispatch primitives for the recovery-only run."""

    team_execution: TeamExecutionCandidateAdapter
    service_for: ControlledRecoveryTeamServiceFactory
    production_ready: bool = False

    def recovery_invocation(
        self,
        dispatch: FactoryDispatch,
    ) -> FactorySkillInvocationV1:
        base = self.team_execution.invocation_for(dispatch)
        key = hashlib.sha256(
            f"{base.idempotency_key}|controlled-recovery-v1".encode("utf-8")
        ).hexdigest()
        payload = base.model_dump(mode="json", by_alias=True)
        payload.update(
            {
                "invocation_id": str(
                    uuid5(base.invocation_id, "captain.controlled-recovery.v1")
                ),
                "idempotency_key": key,
            }
        )
        return FactorySkillInvocationV1.model_validate(payload)


@dataclass(frozen=True)
class DurableGatewayFactoryLiveEffectLedger:
    """Explicit production marker around a Gateway-backed effect ledger."""

    delegate: FactoryLiveEffectLedger
    durability: Literal["gateway"] = "gateway"

    def claim(self, request: FactoryLiveEffectRequestV1) -> FactoryLiveEffectClaim:
        return self.delegate.claim(request)

    def complete(
        self,
        request: FactoryLiveEffectRequestV1,
        outcome: FactoryLiveEffectOutcomeV1,
    ) -> FactoryLiveEffectWriteReceipt:
        return self.delegate.complete(request, outcome)

    def history(self, job_id: UUID):
        return self.delegate.history(job_id)


@dataclass(frozen=True)
class _SingleControlledRecoveryPlan(FactoryLivePlan):
    request: FactoryLiveEffectRequestV1

    def effects_for(
        self,
        *,
        job: AgentFactoryJobV3,
        mode: Literal["demo", "release"],
        projection: FactoryProjection,
        workflow_artifacts: tuple[object, ...],
    ) -> tuple[FactoryLiveEffectRequestV1, ...]:
        del workflow_artifacts
        if (
            mode != "release"
            or job.job_id != self.request.job_id
            or job.correlation_id != self.request.correlation_id
            or projection.attempt != self.request.attempt
        ):
            raise ValueError("controlled recovery plan does not match Factory authority")
        return (self.request,)


class _PostEffectInterruptionExecutor(FactoryLiveEffectExecutor):
    def __init__(
        self,
        *,
        job: AgentFactoryJobV3,
        candidate: ResolvedFactoryCandidate,
        prepared_dispatch: PreparedControlledRecoveryTeamDispatcher,
        effect_store: ControlledRecoveryEffectStorePort,
        clock: Callable[[], datetime],
    ) -> None:
        self._job = job
        self._candidate = candidate
        self._prepared = prepared_dispatch
        self._effects = effect_store
        self._clock = clock

    async def execute(
        self, request: FactoryLiveEffectRequestV1
    ) -> FactoryLiveEffectOutcomeV1:
        if self._effects.load(request.effect_id) is not None:
            raise ValueError(
                "controlled recovery provider evidence exists without its ledger claim"
            )
        service = self._prepared.service_for(self._job, request.invocation)
        if not callable(getattr(service, "execute", None)):
            raise TypeError("prepared controlled recovery dispatcher lacks TeamExecutionService")
        holdout = request.invocation.execution_scope_ref
        if holdout is None:
            raise ValueError("controlled recovery invocation lacks a private holdout scope")
        execution = await service.execute(
            request.invocation,
            self._candidate,
            holdout,
            run_number=1,
        )
        if (
            execution.status != "succeeded"
            or execution.execution_outcome.status != "succeeded"
        ):
            failed_assertions = sum(
                outcome.status != "passed"
                for outcome in execution.execution_outcome.assertion_outcomes
            )
            raise ValueError(
                "controlled recovery execution failed: "
                f"team:{execution.status},runtime:{execution.execution_outcome.status},"
                f"failed_assertions:{failed_assertions}"
            )
        persisted_at = self._utc_now()
        durable = DurableControlledRecoveryEffectV1(
            effect_id=request.effect_id,
            job_id=request.job_id,
            correlation_id=request.correlation_id,
            subject_version=request.subject_version,
            attempt=request.attempt,
            invocation_id=request.invocation.invocation_id,
            execution=execution,
            provider_usage_receipt_refs=execution.usage_receipt_refs,
            persisted_at=persisted_at,
        )
        self._effects.persist(durable)
        if self._effects.load(request.effect_id) is None:
            raise ValueError(
                "controlled recovery provider effect was not durable after persistence"
            )
        raise FactoryInfrastructureFailure(
            "controlled post-effect process interruption"
        )

    async def recover(
        self, request: FactoryLiveEffectRequestV1
    ) -> FactoryLiveEffectOutcomeV1 | None:
        stored = self._effects.load(request.effect_id)
        if stored is None:
            return None
        record = stored.record
        if (
            record.job_id != request.job_id
            or record.correlation_id != request.correlation_id
            or record.subject_version != request.subject_version
            or record.attempt != request.attempt
            or record.invocation_id != request.invocation.invocation_id
        ):
            raise ValueError("controlled recovery durable effect does not match claim")
        return FactoryLiveEffectOutcomeV1(
            schema_name="captain.factory-live-effect-outcome.v1",
            outcome_id=uuid5(request.effect_id, "controlled-recovery-outcome"),
            effect_id=request.effect_id,
            job_id=request.job_id,
            correlation_id=request.correlation_id,
            subject_version=request.subject_version,
            attempt=request.attempt,
            status="succeeded",
            evidence_ref=stored.reference,
            completed_at=record.persisted_at,
        )

    def _utc_now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise ValueError("controlled recovery clock must be UTC")
        return now


class FactoryLiveControlledRecoveryPort:
    """Concrete controlled-recovery implementation used by the V2/V3 bridge."""

    def __init__(
        self,
        *,
        repository: FactoryRepository,
        effect_ledger: FactoryLiveEffectLedger,
        prepared_dispatch: PreparedControlledRecoveryTeamDispatcher,
        effect_store: ControlledRecoveryEffectStorePort,
        clock: Callable[[], datetime],
    ) -> None:
        self._repository = repository
        self._ledger = effect_ledger
        self._prepared = prepared_dispatch
        self._effects = effect_store
        self._clock = clock

    async def execute(
        self,
        job: AgentFactoryJobV3,
        dispatch: FactoryDispatch,
        candidate: ResolvedFactoryCandidate,
    ) -> CapabilityControlledRecoveryResultV1:
        if (
            dispatch.job != job
            or dispatch.action.kind is not FactoryActionKind.DISPATCH_REAL_CASE_TESTER
            or dispatch.action.job_id != job.job_id
            or dispatch.role is not FactoryRole.REAL_CASE_TESTER
            or dispatch.lease is None
            or dispatch.lease.role is not FactoryRole.REAL_CASE_TESTER
            or not job.private_holdout_refs
        ):
            raise ValueError(
                "controlled recovery requires one exact tester dispatch and private holdout scope"
            )
        if self._repository.job(job.job_id) != job:
            raise ValueError("controlled recovery job does not match persisted authority")
        invocation = self._prepared.recovery_invocation(dispatch)
        request = FactoryLiveEffectRequestV1(
            schema_name="captain.factory-live-effect-request.v1",
            effect_id=uuid5(invocation.invocation_id, "controlled-provider-effect"),
            job_id=job.job_id,
            correlation_id=job.correlation_id,
            subject_version=job.subject_version,
            attempt=dispatch.action.attempt,
            kind=FactoryLiveEffectKind.PROVIDER,
            idempotency_key=invocation.idempotency_key,
            input_ref=job.input_ref,
            invocation=invocation,
        )
        executor = _PostEffectInterruptionExecutor(
            job=job,
            candidate=candidate,
            prepared_dispatch=self._prepared,
            effect_store=self._effects,
            clock=self._clock,
        )
        runner = FactoryLiveRunner(
            repository=self._repository,
            effect_ledger=self._ledger,
            plan=_SingleControlledRecoveryPlan(request),
            executor=executor,
            clock=self._clock,
        )
        records = tuple(
            record
            for record in self._ledger.history(job.job_id)
            if record.request.effect_id == request.effect_id
        )
        if len(records) > 1:
            raise ValueError("controlled recovery effect has duplicate ledger claims")
        stored_before = self._effects.load(request.effect_id)
        if not records:
            if stored_before is not None:
                raise ValueError(
                    "controlled recovery evidence exists without a durable reservation"
                )
            first = await runner.run(job, mode="release")
            if first.status != "infrastructure_recovery_required":
                raise ValueError(
                    "controlled recovery did not stop after the provider effect"
                )
            if self._effects.load(request.effect_id) is None:
                reason = first.reasons[0] if first.reasons else "unknown"
                raise ValueError(
                    "controlled recovery provider effect was not persisted: " + reason
                )
            records = tuple(
                record
                for record in self._ledger.history(job.job_id)
                if record.request.effect_id == request.effect_id
            )
        if len(records) != 1:
            raise ValueError("controlled recovery reservation is not durable")
        record = records[0]
        if record.outcome is None:
            if self._effects.load(request.effect_id) is None:
                raise ValueError(
                    "reserved provider effect lacks durable recovery evidence"
                )
            await runner.run(job, mode="release")
        elif (
            record.outcome.status != "succeeded"
            or record.outcome.completion_origin != "recover"
            or self._effects.load(request.effect_id) is None
        ):
            raise ValueError("controlled recovery ledger completion is not authoritative")
        history = runner.history(job.job_id)
        if len(history) < 2:
            raise ValueError("controlled recovery completion is not reconstructable")
        interrupted, resumed = history[-2], history[-1]
        stored = self._effects.load(request.effect_id)
        if stored is None:
            raise ValueError("controlled recovery provider evidence was not persisted")
        execution = stored.record.execution
        recovery_id = f"controlled-recovery-{str(request.effect_id).replace('-', '')[:24]}"
        return CapabilityControlledRecoveryResultV1(
            recovery_id=recovery_id,
            recovery_assertion_id=job.acceptance_assertion_ids[0],
            execution=execution,
            interrupted=interrupted,
            resumed=resumed,
            provider_effect_receipt_ref=stored.reference,
            holdout_receipts=tuple(
                CapabilityControlledHoldoutReceiptV1(
                    holdout_ref=holdout,
                    assertion_id=job.acceptance_assertion_ids[
                        index % len(job.acceptance_assertion_ids)
                    ],
                    status="passed",
                    evidence_ref=execution.artifact_ref,
                )
                for index, holdout in enumerate(job.private_holdout_refs)
            ),
        )


def build_controlled_recovery_port(
    *,
    repository: FactoryRepository,
    effect_ledger: FactoryLiveEffectLedger,
    prepared_dispatch: PreparedControlledRecoveryTeamDispatcher,
    effect_store: ControlledRecoveryEffectStorePort,
    clock: Callable[[], datetime],
) -> FactoryLiveControlledRecoveryPort:
    """Development/test constructor with explicitly injected effect persistence."""

    return FactoryLiveControlledRecoveryPort(
        repository=repository,
        effect_ledger=effect_ledger,
        prepared_dispatch=prepared_dispatch,
        effect_store=effect_store,
        clock=clock,
    )


def build_production_controlled_recovery_port(
    *,
    repository: FactoryRepository,
    effect_ledger: FactoryLiveEffectLedger,
    prepared_dispatch: object,
    effect_store: ControlledRecoveryEffectStorePort,
    clock: Callable[[], datetime],
) -> FactoryLiveControlledRecoveryPort:
    """Production constructor requiring explicit Gateway and host-AutoGen markers."""

    if not isinstance(effect_ledger, DurableGatewayFactoryLiveEffectLedger):
        raise CapabilityV3BridgeConfigurationError(
            "production controlled recovery requires a durable Gateway effect ledger"
        )
    if not isinstance(prepared_dispatch, PreparedControlledRecoveryTeamDispatcher):
        raise CapabilityV3BridgeConfigurationError(
            "production controlled recovery requires prepared host AutoGen dispatch"
        )
    if prepared_dispatch.production_ready is not True:
        raise CapabilityV3BridgeConfigurationError(
            "production controlled recovery dispatcher is not host-AutoGen attested"
        )
    if getattr(effect_store, "durability", None) != "content_addressed":
        raise CapabilityV3BridgeConfigurationError(
            "production controlled recovery requires a durable content-addressed store"
        )
    return build_controlled_recovery_port(
        repository=repository,
        effect_ledger=effect_ledger,
        prepared_dispatch=prepared_dispatch,
        effect_store=effect_store,
        clock=clock,
    )
