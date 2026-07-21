"""Captain-controlled live-effect orchestration with durable replay seams.

The runner does not execute candidate entrypoints and does not own lifecycle
transitions.  It coordinates injected, typed effects after an authoritative
claim and reconstructs release readiness from Gateway-owned evidence.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from threading import Lock
from typing import Callable, Literal, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agenten.agent_factory.contracts import AgentFactoryJobV3
from agenten.agent_factory.execution_budget import FactoryBudgetProjection
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
    run_id: UUID | None = None
    run_effect_index: int | None = Field(default=None, ge=1, strict=True)
    run_effect_count: int | None = Field(default=None, ge=1, strict=True)

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
        run_binding = (self.run_id, self.run_effect_index, self.run_effect_count)
        if any(value is not None for value in run_binding):
            if any(value is None for value in run_binding):
                raise ValueError("factory live effect run binding must be complete")
            if self.run_effect_index > self.run_effect_count:
                raise ValueError("factory live effect run index exceeds its count")
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
    completion_origin: Literal["execute", "recover"] = "execute"
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
    completion_origin: Literal["execute", "recover"] | None = None
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
            if (
                self.evidence_ref is not None
                or self.reason is None
                or self.completion_origin is not None
            ):
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

    def history(self, job_id: UUID) -> tuple[FactoryLiveRunReport, ...]:
        """Reconstruct typed run reports from the authority-owned effect stream."""

        job = self._resolve_job(job_id)
        mode = job.execution_policy.mode.value
        records = self._effect_ledger.history(job_id)
        grouped: dict[UUID, list[FactoryLiveEffectRecord]] = {}
        group_order: list[UUID] = []
        for record in records:
            request = record.request
            if (
                request.job_id != job.job_id
                or request.correlation_id != job.correlation_id
                or request.subject_version != job.subject_version
            ):
                raise ValueError("factory live effect history binding mismatch")
            group_id = request.run_id or request.effect_id
            if group_id not in grouped:
                grouped[group_id] = []
                group_order.append(group_id)
            grouped[group_id].append(record)

        reports: list[FactoryLiveRunReport] = []
        visible_invocation_ids: set[UUID] = set()
        for group_id in group_order:
            group = self._ordered_run_records(grouped[group_id])
            visible_invocation_ids.update(
                record.request.invocation.invocation_id for record in group
            )
            reports.extend(
                self._rebuild_run_history(
                    job,
                    mode,
                    group,
                    visible_invocation_ids=frozenset(visible_invocation_ids),
                )
            )
        return tuple(reports)

    @staticmethod
    def _ordered_run_records(
        records: list[FactoryLiveEffectRecord],
    ) -> tuple[FactoryLiveEffectRecord, ...]:
        first = records[0].request
        if first.run_id is None:
            return tuple(records)
        if any(
            record.request.run_id != first.run_id
            or record.request.run_effect_count != first.run_effect_count
            or record.request.attempt != first.attempt
            for record in records
        ):
            raise ValueError("factory live effect run binding mismatch")
        ordered = tuple(
            sorted(records, key=lambda record: record.request.run_effect_index or 0)
        )
        expected_count = first.run_effect_count
        indexes = tuple(record.request.run_effect_index for record in ordered)
        if (
            expected_count is None
            or len(ordered) > expected_count
            or indexes != tuple(range(1, len(ordered) + 1))
        ):
            raise ValueError("factory live effect run history is incomplete")
        return ordered

    def _rebuild_run_history(
        self,
        job: AgentFactoryJobV3,
        mode: Literal["demo", "release"],
        records: tuple[FactoryLiveEffectRecord, ...],
        *,
        visible_invocation_ids: frozenset[UUID],
    ) -> tuple[FactoryLiveRunReport, ...]:
        attempt = records[0].request.attempt
        projection = FactoryProjection.from_job(job).model_copy(
            update={"attempt": attempt}
        )
        reason = "reserved external effect requires authoritative recovery evidence"
        effects = tuple(
            _effect_report(record.request, record.outcome, replayed=True)
            if record.outcome is not None
            else FactoryLiveEffectReport(
                effect_id=record.request.effect_id,
                kind=record.request.kind,
                attempt=record.request.attempt,
                status="reserved",
                reason=reason,
                replayed=True,
            )
            for record in records
        )
        expected_count = records[0].request.run_effect_count
        prefix_is_incomplete = (
            expected_count is not None and len(records) < expected_count
        )
        if any(record.outcome is None for record in records) or prefix_is_incomplete:
            if prefix_is_incomplete and all(
                record.outcome is not None for record in records
            ):
                reason = "planned run suffix was not claimed before process restart"
            return (
                self._infrastructure_report(
                    job, mode, projection, list(effects), reason
                ),
            )

        rebuilt: list[FactoryLiveRunReport] = []
        if any(
            record.outcome is not None
            and record.outcome.completion_origin == "recover"
            for record in records
        ):
            before_recovery = [
                FactoryLiveEffectReport(
                    effect_id=record.request.effect_id,
                    kind=record.request.kind,
                    attempt=record.request.attempt,
                    status="reserved",
                    reason=reason,
                    replayed=True,
                )
                if record.outcome is not None
                and record.outcome.completion_origin == "recover"
                else _effect_report(record.request, record.outcome, replayed=True)
                for record in records
            ]
            rebuilt.append(
                self._infrastructure_report(
                    job, mode, projection, before_recovery, reason
                )
            )

        outcomes = tuple(record.outcome for record in records)
        if any(outcome.status == "behavioral_failure" for outcome in outcomes):
            rebuilt.append(self._behavioral_report(job, mode, projection, list(effects)))
        else:
            blocked = next(
                (
                    outcome
                    for outcome in outcomes
                    if outcome.status in _NON_DISPATCHED_STATUSES
                ),
                None,
            )
            if blocked is not None:
                rebuilt.append(
                    self._blocked_report(
                        job, mode, projection, list(effects), blocked.reason
                    )
                )
            else:
                decision = self._release_decision(
                    job,
                    attempt,
                    visible_invocation_ids=visible_invocation_ids,
                )
                rebuilt.append(
                    FactoryLiveRunReport(
                        job_id=job.job_id,
                        correlation_id=job.correlation_id,
                        mode=mode,
                        status=decision.status,
                        attempt=attempt,
                        next_attempt=attempt,
                        effects=effects,
                        release_decision=decision,
                        reasons=decision.reasons,
                    )
                )
        return tuple(rebuilt)

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
        planned_requests = self._plan.effects_for(
            job=authoritative,
            mode=mode,
            projection=projection,
            workflow_artifacts=artifacts,
        )
        requests = self._bind_planned_run(authoritative, planned_requests)
        preflight_now = self._clock()
        for request in requests:
            self._validate_request(authoritative, projection, request)
            self._validate_dispatch_authority(
                authoritative,
                request,
                now=preflight_now,
            )
        effect_reports: list[FactoryLiveEffectReport] = []
        for request in requests:
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
            outcome = outcome.model_copy(
                update={
                    "completion_origin": "execute" if claim.acquired else "recover"
                }
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

    def _bind_planned_run(
        self,
        job: AgentFactoryJobV3,
        requests: tuple[FactoryLiveEffectRequestV1, ...],
    ) -> tuple[FactoryLiveEffectRequestV1, ...]:
        """Bind a planner batch as one deterministic, replay-safe run."""

        if not requests:
            return requests
        if job.execution_policy.mode.value != "release":
            return requests
        if (
            len({request.effect_id for request in requests}) != len(requests)
            or any(request.attempt != requests[0].attempt for request in requests)
        ):
            raise ValueError("factory live effect run binding mismatch")

        existing_by_effect = {
            record.request.effect_id: record.request
            for record in self._effect_ledger.history(job.job_id)
        }
        replayed = tuple(
            existing_by_effect[request.effect_id]
            for request in requests
            if request.effect_id in existing_by_effect
        )
        if replayed and any(request.run_id is None for request in replayed):
            if any(request.run_id is not None for request in replayed):
                raise ValueError("factory live effect run binding mismatch")
            if any(request.run_id is not None for request in requests):
                raise ValueError("factory live effect run binding mismatch")
            return requests

        bound = tuple(request for request in requests if request.run_id is not None)
        if bound:
            first = bound[0]
            expected_count = len(requests)
            if (
                len(bound) != expected_count
                or first.run_effect_count != expected_count
                or tuple(request.run_effect_index for request in requests)
                != tuple(range(1, expected_count + 1))
                or any(
                    request.run_id != first.run_id
                    or request.run_effect_count != expected_count
                    or request.attempt != first.attempt
                    for request in requests
                )
            ):
                raise ValueError("factory live effect run binding mismatch")
            return requests

        run_id = uuid5(
            NAMESPACE_URL,
            "|".join(
                (
                    "captain.factory-live-run.v1",
                    str(job.job_id),
                    str(job.subject_version),
                    str(requests[0].attempt),
                    *(str(request.effect_id) for request in requests),
                )
            ),
        )
        effect_count = len(requests)
        return tuple(
            request.model_copy(
                update={
                    "run_id": run_id,
                    "run_effect_index": index,
                    "run_effect_count": effect_count,
                }
            )
            for index, request in enumerate(requests, start=1)
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
        self._validate_dispatch_authority(job, request, now=self._clock())

    @staticmethod
    def _validate_dispatch_authority(
        job: AgentFactoryJobV3,
        request: FactoryLiveEffectRequestV1,
        *,
        now: datetime,
    ) -> None:
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
        *,
        visible_invocation_ids: frozenset[UUID] | None = None,
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
            and (
                visible_invocation_ids is None
                or artifact.invocation.invocation_id in visible_invocation_ids
            )
        )
        if len(evaluations) != 1:
            return FactoryReleaseDecision(
                job_id=job.job_id,
                correlation_id=job.correlation_id,
                status="blocked",
                reasons=("missing one exact Gateway workflow evaluation",),
            )
        budget = self._repository.workflow_budget_projection(job.job_id)
        receipts = self._repository.workflow_usage_receipts(job.job_id)
        if visible_invocation_ids is not None and budget is not None:
            visible_receipt_refs = {
                reference
                for execution in executions
                for reference in execution.usage_receipt_refs
            }
            receipts = tuple(
                receipt
                for receipt in receipts
                if receipt.evidence_ref in visible_receipt_refs
            )
            consumed = sum(
                (receipt.cost_usd for receipt in receipts),
                start=Decimal("0.00"),
            )
            budget = FactoryBudgetProjection(
                job_id=budget.job_id,
                limit_usd=budget.limit_usd,
                consumed_usd=consumed,
                reserved_usd=Decimal("0.00"),
                remaining_usd=budget.limit_usd - consumed,
                active_reservation_ids=(),
            )
        return evaluate_factory_workflow_release(
            job,
            executions,
            evaluations[0],
            budget_projection=budget,
            usage_receipts=receipts,
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
        completion_origin=outcome.completion_origin,
        replayed=replayed,
    )
