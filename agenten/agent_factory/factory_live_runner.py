"""Captain-controlled live-effect orchestration with durable replay seams.

The runner does not execute candidate entrypoints and does not own lifecycle
transitions.  It coordinates injected, typed effects after an authoritative
claim and reconstructs release readiness from Gateway-owned evidence.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from threading import Lock
from typing import Callable, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agenten.agent_factory.contracts import AgentFactoryJobV3
from agenten.agent_factory.release_gate import (
    FactoryReleaseDecision,
    evaluate_factory_workflow_release,
)
from agenten.agent_factory.leases import validate_factory_lease
from agenten.agent_factory.service import FactoryCoordinator, FactoryRepository
from agenten.agent_factory.skill_workflow_contracts import (
    FactorySkillInvocationV1,
    FactorySkillStep,
    TeamEvaluationV1,
    TeamExecutionEvidenceV1,
)
from agenten.agent_factory.state_machine import FactoryProjection
from agenten.agent_runtime.contracts import ArtifactRef


class FactoryLiveEffectKind(str, Enum):
    CODEX = "codex"
    PROVIDER = "provider"


class FactoryLiveBlockReason(str, Enum):
    """Typed pre-dispatch reasons which never count as provider execution."""

    CREDENTIAL_REQUIRED = "credential_required"
    BUDGET_EXHAUSTED = "budget_exhausted"
    REQUIRED_TOOL = "required_tool"


_NON_DISPATCHED_STATUSES = frozenset(reason.value for reason in FactoryLiveBlockReason)


class _FrozenContract(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )


class FactoryLiveEffectRequestV1(_FrozenContract):
    """Immutable authority envelope persisted before an external effect."""

    schema_name: Literal["captain.factory-live-effect-request.v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    effect_id: UUID
    job_id: UUID
    correlation_id: UUID
    subject_version: int = Field(ge=1, strict=True)
    attempt: int = Field(ge=1, le=5, strict=True)
    kind: FactoryLiveEffectKind
    idempotency_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_ref: ArtifactRef
    invocation: FactorySkillInvocationV1

    @model_validator(mode="after")
    def require_invocation_binding(self) -> "FactoryLiveEffectRequestV1":
        invocation = self.invocation
        if (
            invocation.job_id != self.job_id
            or invocation.correlation_id != self.correlation_id
            or invocation.subject_version != self.subject_version
            or invocation.attempt != self.attempt
            or invocation.idempotency_key != self.idempotency_key
            or invocation.input_ref != self.input_ref
        ):
            raise ValueError("factory live effect invocation binding mismatch")
        allowed_steps = {
            FactoryLiveEffectKind.CODEX: {
                FactorySkillStep.BRIEF_CODEX,
                FactorySkillStep.IMPROVE_TEAM,
            },
            FactoryLiveEffectKind.PROVIDER: {FactorySkillStep.EXECUTE_TEAM},
        }[self.kind]
        if invocation.step not in allowed_steps:
            raise ValueError("factory live effect kind does not match invocation step")
        return self


class FactoryLiveEffectOutcomeV1(_FrozenContract):
    """Content-addressed result accepted after an effect or its recovery."""

    schema_name: Literal["captain.factory-live-effect-outcome.v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    outcome_id: UUID
    effect_id: UUID
    job_id: UUID
    correlation_id: UUID
    subject_version: int = Field(ge=1, strict=True)
    attempt: int = Field(ge=1, le=5, strict=True)
    status: Literal[
        "succeeded",
        "behavioral_failure",
        "credential_required",
        "budget_exhausted",
        "required_tool",
    ]
    evidence_ref: ArtifactRef | None = None
    reason: str | None = Field(default=None, min_length=1, max_length=512)
    completed_at: datetime

    @field_validator("completed_at")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
            raise ValueError("completed_at must be UTC")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def require_status_evidence(self) -> "FactoryLiveEffectOutcomeV1":
        if self.status in _NON_DISPATCHED_STATUSES:
            if self.reason is None or not self.reason.strip():
                raise ValueError("non-dispatched factory live block requires a reason")
            if self.evidence_ref is not None:
                raise ValueError(
                    "non-dispatched factory live block cannot carry provider evidence"
                )
        elif self.evidence_ref is None:
            raise ValueError("dispatched factory live outcome requires evidence")
        return self


class FactoryLiveEffectRecord(_FrozenContract):
    request: FactoryLiveEffectRequestV1
    outcome: FactoryLiveEffectOutcomeV1 | None = None

    @model_validator(mode="after")
    def require_outcome_binding(self) -> "FactoryLiveEffectRecord":
        if self.outcome is not None:
            _require_outcome_binding(self.request, self.outcome)
        return self


class FactoryLiveEffectClaim(_FrozenContract):
    record: FactoryLiveEffectRecord
    acquired: bool


class FactoryLiveEffectWriteReceipt(_FrozenContract):
    record: FactoryLiveEffectRecord
    replayed: bool


class FactoryLiveEffectLedger(Protocol):
    """Atomic, Gateway-backed pre-effect claim and completion port."""

    def claim(
        self,
        request: FactoryLiveEffectRequestV1,
    ) -> FactoryLiveEffectClaim: ...

    def complete(
        self,
        request: FactoryLiveEffectRequestV1,
        outcome: FactoryLiveEffectOutcomeV1,
    ) -> FactoryLiveEffectWriteReceipt: ...

    def history(self, job_id: UUID) -> tuple[FactoryLiveEffectRecord, ...]: ...


class InMemoryFactoryLiveEffectLedger:
    """Deterministic test adapter; production uses the Gateway implementation."""

    def __init__(self) -> None:
        self._records: dict[UUID, FactoryLiveEffectRecord] = {}
        self._effect_ids_by_key: dict[tuple[UUID, str], UUID] = {}
        self._effect_ids_by_invocation: dict[tuple[UUID, UUID], UUID] = {}
        self._lock = Lock()

    def claim(
        self,
        request: FactoryLiveEffectRequestV1,
    ) -> FactoryLiveEffectClaim:
        with self._lock:
            key = (request.job_id, request.idempotency_key)
            claimed_effect_id = self._effect_ids_by_key.get(key)
            if claimed_effect_id is not None and claimed_effect_id != request.effect_id:
                raise ValueError(
                    "factory live idempotency_key already binds another effect_id"
                )
            invocation_key = (request.job_id, request.invocation.invocation_id)
            claimed_invocation_effect_id = self._effect_ids_by_invocation.get(
                invocation_key
            )
            if (
                claimed_invocation_effect_id is not None
                and claimed_invocation_effect_id != request.effect_id
            ):
                raise ValueError(
                    "factory live invocation_id already binds another effect_id"
                )
            existing = self._records.get(request.effect_id)
            if existing is not None:
                if existing.request != request:
                    raise ValueError(
                        "factory live effect_id already exists with different content"
                    )
                return FactoryLiveEffectClaim(record=existing, acquired=False)
            record = FactoryLiveEffectRecord(request=request)
            self._records[request.effect_id] = record
            self._effect_ids_by_key[key] = request.effect_id
            self._effect_ids_by_invocation[invocation_key] = request.effect_id
            return FactoryLiveEffectClaim(record=record, acquired=True)

    def complete(
        self,
        request: FactoryLiveEffectRequestV1,
        outcome: FactoryLiveEffectOutcomeV1,
    ) -> FactoryLiveEffectWriteReceipt:
        _require_outcome_binding(request, outcome)
        with self._lock:
            existing = self._records.get(request.effect_id)
            if existing is None or existing.request != request:
                raise ValueError("factory live effect completion is missing its claim")
            if existing.outcome is not None:
                if existing.outcome != outcome:
                    raise ValueError(
                        "factory live effect already completed with different content"
                    )
                return FactoryLiveEffectWriteReceipt(record=existing, replayed=True)
            completed = FactoryLiveEffectRecord(request=request, outcome=outcome)
            self._records[request.effect_id] = completed
            return FactoryLiveEffectWriteReceipt(record=completed, replayed=False)

    def history(self, job_id: UUID) -> tuple[FactoryLiveEffectRecord, ...]:
        with self._lock:
            return tuple(
                record
                for record in self._records.values()
                if record.request.job_id == job_id
            )


class FactoryInfrastructureFailure(RuntimeError):
    """A recoverable external failure that must preserve the current attempt."""


class FactoryLiveEffectExecutor(Protocol):
    async def execute(
        self,
        request: FactoryLiveEffectRequestV1,
    ) -> FactoryLiveEffectOutcomeV1: ...

    async def recover(
        self,
        request: FactoryLiveEffectRequestV1,
    ) -> FactoryLiveEffectOutcomeV1 | None: ...


class FactoryLivePlan(Protocol):
    def effects_for(
        self,
        *,
        job: AgentFactoryJobV3,
        mode: Literal["demo", "release"],
        projection: FactoryProjection,
        workflow_artifacts: tuple[object, ...],
    ) -> tuple[FactoryLiveEffectRequestV1, ...]: ...


class FactoryLiveEffectReport(_FrozenContract):
    effect_id: UUID
    kind: FactoryLiveEffectKind
    attempt: int
    status: Literal[
        "reserved",
        "succeeded",
        "behavioral_failure",
        "credential_required",
        "budget_exhausted",
        "required_tool",
    ]
    evidence_ref: ArtifactRef | None = None
    reason: str | None = None
    provider_started: bool | None = None
    replayed: bool

    @model_validator(mode="after")
    def derive_provider_state(self) -> "FactoryLiveEffectReport":
        expected = (
            None
            if self.status == "reserved"
            else self.status not in _NON_DISPATCHED_STATUSES
        )
        if self.provider_started is not None and self.provider_started is not expected:
            raise ValueError("factory live effect report provider state mismatch")
        if self.status == "reserved":
            if self.evidence_ref is not None or self.reason is None:
                raise ValueError("reserved factory live effect report is malformed")
        elif self.status in _NON_DISPATCHED_STATUSES:
            if (
                self.evidence_ref is not None
                or self.reason is None
                or not self.reason.strip()
            ):
                raise ValueError("non-dispatched factory live effect report is malformed")
        elif self.evidence_ref is None:
            raise ValueError("dispatched factory live effect report requires evidence")
        object.__setattr__(self, "provider_started", expected)
        return self


class FactoryLiveRunReport(_FrozenContract):
    job_id: UUID
    correlation_id: UUID
    mode: Literal["demo", "release"]
    status: Literal[
        "blocked",
        "infrastructure_recovery_required",
        "behavioral_retry_required",
        "demo_ready",
        "ready",
    ]
    attempt: int = Field(ge=1, le=5, strict=True)
    next_attempt: int = Field(ge=1, le=5, strict=True)
    effects: tuple[FactoryLiveEffectReport, ...] = ()
    release_decision: FactoryReleaseDecision | None = None
    reasons: tuple[str, ...] = ()


class FactoryLiveRunner:
    """Run a Captain-planned slice without becoming a second lifecycle writer."""

    def __init__(
        self,
        *,
        repository: FactoryRepository,
        effect_ledger: FactoryLiveEffectLedger,
        plan: FactoryLivePlan,
        executor: FactoryLiveEffectExecutor,
        clock: Callable[[], datetime],
    ) -> None:
        self._repository = repository
        self._coordinator = FactoryCoordinator(repository)
        self._effect_ledger = effect_ledger
        self._plan = plan
        self._executor = executor
        self._clock = clock

    def history(self, job_id: UUID) -> tuple[FactoryLiveEffectReport, ...]:
        """Reconstruct ordered, durable effect state from the authority ledger."""

        job = self._resolve_job(job_id)
        reports: list[FactoryLiveEffectReport] = []
        for record in self._effect_ledger.history(job_id):
            request = record.request
            if (
                request.job_id != job.job_id
                or request.correlation_id != job.correlation_id
                or request.subject_version != job.subject_version
            ):
                raise ValueError("factory live effect history binding mismatch")
            if record.outcome is None:
                reports.append(
                    FactoryLiveEffectReport(
                        effect_id=request.effect_id,
                        kind=request.kind,
                        attempt=request.attempt,
                        status="reserved",
                        evidence_ref=None,
                        reason=(
                            "reserved external effect requires authoritative recovery evidence"
                        ),
                        provider_started=None,
                        replayed=True,
                    )
                )
            else:
                reports.append(_effect_report(request, record.outcome, replayed=True))
        return tuple(reports)

    async def run(
        self,
        job: UUID | AgentFactoryJobV3,
        *,
        mode: Literal["demo", "release"],
    ) -> FactoryLiveRunReport:
        authoritative = self._resolve_job(job)
        self._validate_run(authoritative, mode)
        projection = self._coordinator.projection(authoritative.job_id)
        artifacts = self._repository.workflow_artifacts(authoritative.job_id)
        requests = self._plan.effects_for(
            job=authoritative,
            mode=mode,
            projection=projection,
            workflow_artifacts=artifacts,
        )
        effect_reports: list[FactoryLiveEffectReport] = []
        for request in requests:
            self._validate_request(authoritative, projection, request)
            claim = self._effect_ledger.claim(request)
            if claim.record.outcome is not None:
                prior_outcome = claim.record.outcome
                effect_reports.append(
                    _effect_report(request, prior_outcome, replayed=True)
                )
                if prior_outcome.status == "behavioral_failure":
                    return self._behavioral_report(
                        authoritative,
                        mode,
                        projection,
                        effect_reports,
                    )
                if prior_outcome.status in _NON_DISPATCHED_STATUSES:
                    return self._blocked_report(
                        authoritative,
                        mode,
                        projection,
                        effect_reports,
                        prior_outcome.reason,
                    )
                continue
            try:
                if claim.acquired:
                    self._validate_new_dispatch(authoritative, request)
                    outcome = await self._executor.execute(request)
                else:
                    outcome = await self._executor.recover(request)
            except FactoryInfrastructureFailure as exc:
                return self._infrastructure_report(
                    authoritative,
                    mode,
                    projection,
                    effect_reports,
                    str(exc),
                )
            if outcome is None:
                return self._infrastructure_report(
                    authoritative,
                    mode,
                    projection,
                    effect_reports,
                    "reserved external effect requires authoritative recovery evidence",
                )
            receipt = self._effect_ledger.complete(request, outcome)
            effect_reports.append(
                _effect_report(request, outcome, replayed=receipt.replayed)
            )
            if outcome.status == "behavioral_failure":
                return self._behavioral_report(
                    authoritative,
                    mode,
                    projection,
                    effect_reports,
                )
            if outcome.status in _NON_DISPATCHED_STATUSES:
                return self._blocked_report(
                    authoritative,
                    mode,
                    projection,
                    effect_reports,
                    outcome.reason,
                )
        decision = self._release_decision(authoritative, projection.attempt)
        return FactoryLiveRunReport(
            job_id=authoritative.job_id,
            correlation_id=authoritative.correlation_id,
            mode=mode,
            status=decision.status,
            attempt=projection.attempt,
            next_attempt=projection.attempt,
            effects=tuple(effect_reports),
            release_decision=decision,
            reasons=decision.reasons,
        )

    def _resolve_job(self, supplied: UUID | AgentFactoryJobV3) -> AgentFactoryJobV3:
        job_id = supplied if isinstance(supplied, UUID) else supplied.job_id
        stored = self._repository.job(job_id)
        if not isinstance(stored, AgentFactoryJobV3):
            raise ValueError("factory live runner requires AgentFactoryJobV3")
        if isinstance(supplied, AgentFactoryJobV3) and supplied != stored:
            raise ValueError("factory live runner job does not match Gateway authority")
        return stored

    def _validate_run(
        self,
        job: AgentFactoryJobV3,
        mode: Literal["demo", "release"],
    ) -> None:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() != timezone.utc.utcoffset(now):
            raise ValueError("factory live runner clock must be UTC")
        if not job.occurred_at <= now < job.deadline_at:
            raise ValueError("factory live runner requires an active JobV3 deadline")
        if not job.execution_policy.live_execution:
            raise ValueError("factory live runner requires live_execution=true")
        if job.execution_policy.mode.value != mode:
            raise ValueError("factory live runner mode does not match Captain job policy")

    @staticmethod
    def _validate_request(
        job: AgentFactoryJobV3,
        projection: FactoryProjection,
        request: FactoryLiveEffectRequestV1,
    ) -> None:
        if (
            request.job_id != job.job_id
            or request.correlation_id != job.correlation_id
            or request.subject_version != job.subject_version
            or request.attempt != projection.attempt
            or request.attempt > job.max_behavioral_iterations
        ):
            raise ValueError("factory live effect request does not match Gateway projection")

    def _validate_new_dispatch(
        self,
        job: AgentFactoryJobV3,
        request: FactoryLiveEffectRequestV1,
    ) -> None:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() != timezone.utc.utcoffset(now):
            raise ValueError("factory live runner clock must be UTC")
        if not job.occurred_at <= now < job.deadline_at:
            raise ValueError("factory live runner requires an active JobV3 deadline")
        validate_factory_lease(
            request.invocation.lease,
            job=job,
            role=request.invocation.lease.role,
            attempt=request.attempt,
            now=now,
        )

    def _release_decision(
        self,
        job: AgentFactoryJobV3,
        attempt: int,
    ) -> FactoryReleaseDecision:
        artifacts = self._repository.workflow_artifacts(job.job_id)
        evaluations = tuple(
            artifact
            for artifact in artifacts
            if isinstance(artifact, TeamEvaluationV1) and artifact.attempt == attempt
        )
        executions = tuple(
            artifact
            for artifact in artifacts
            if isinstance(artifact, TeamExecutionEvidenceV1)
            and artifact.attempt == attempt
        )
        if len(evaluations) != 1:
            return FactoryReleaseDecision(
                job_id=job.job_id,
                correlation_id=job.correlation_id,
                status="blocked",
                reasons=("missing one exact Gateway workflow evaluation",),
            )
        return evaluate_factory_workflow_release(
            job,
            executions,
            evaluations[0],
            budget_projection=self._repository.workflow_budget_projection(job.job_id),
            usage_receipts=self._repository.workflow_usage_receipts(job.job_id),
        )

    @staticmethod
    def _behavioral_report(
        job: AgentFactoryJobV3,
        mode: Literal["demo", "release"],
        projection: FactoryProjection,
        effects: list[FactoryLiveEffectReport],
    ) -> FactoryLiveRunReport:
        if projection.attempt >= job.max_behavioral_iterations:
            next_attempt = projection.attempt
            reasons = ("behavioral iteration ceiling reached",)
            status = "blocked"
        else:
            next_attempt = projection.attempt + 1
            reasons = ("Captain behavioral retry decision required",)
            status = "behavioral_retry_required"
        return FactoryLiveRunReport(
            job_id=job.job_id,
            correlation_id=job.correlation_id,
            mode=mode,
            status=status,
            attempt=projection.attempt,
            next_attempt=next_attempt,
            effects=tuple(effects),
            reasons=reasons,
        )

    @staticmethod
    def _infrastructure_report(
        job: AgentFactoryJobV3,
        mode: Literal["demo", "release"],
        projection: FactoryProjection,
        effects: list[FactoryLiveEffectReport],
        reason: str,
    ) -> FactoryLiveRunReport:
        return FactoryLiveRunReport(
            job_id=job.job_id,
            correlation_id=job.correlation_id,
            mode=mode,
            status="infrastructure_recovery_required",
            attempt=projection.attempt,
            next_attempt=projection.attempt,
            effects=tuple(effects),
            reasons=(reason,),
        )

    @staticmethod
    def _blocked_report(
        job: AgentFactoryJobV3,
        mode: Literal["demo", "release"],
        projection: FactoryProjection,
        effects: list[FactoryLiveEffectReport],
        reason: str | None,
    ) -> FactoryLiveRunReport:
        if reason is None:
            raise ValueError("non-dispatched factory live block lacks its exact reason")
        return FactoryLiveRunReport(
            job_id=job.job_id,
            correlation_id=job.correlation_id,
            mode=mode,
            status="blocked",
            attempt=projection.attempt,
            next_attempt=projection.attempt,
            effects=tuple(effects),
            reasons=(reason,),
        )


def _require_outcome_binding(
    request: FactoryLiveEffectRequestV1,
    outcome: FactoryLiveEffectOutcomeV1,
) -> None:
    if (
        outcome.effect_id != request.effect_id
        or outcome.job_id != request.job_id
        or outcome.correlation_id != request.correlation_id
        or outcome.subject_version != request.subject_version
        or outcome.attempt != request.attempt
    ):
        raise ValueError("factory live effect outcome binding mismatch")


def _effect_report(
    request: FactoryLiveEffectRequestV1,
    outcome: FactoryLiveEffectOutcomeV1,
    *,
    replayed: bool,
) -> FactoryLiveEffectReport:
    return FactoryLiveEffectReport(
        effect_id=request.effect_id,
        kind=request.kind,
        attempt=request.attempt,
        status=outcome.status,
        evidence_ref=outcome.evidence_ref,
        reason=outcome.reason,
        provider_started=outcome.status not in _NON_DISPATCHED_STATUSES,
        replayed=replayed,
    )
