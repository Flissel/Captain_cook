"""Authorize, dispatch, and read back one persistent capability continuation."""

from __future__ import annotations

import hashlib
import json
from typing import Callable, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from agenten.agent_runtime.contracts import (
    AgentRuntimeCommand,
    AgentRuntimeResult,
    RuntimeProviderCostSettlementV1,
    RuntimeOperation,
    RuntimeStatus,
)
from gateway.capability_live_pipeline import (
    CapabilityExecutionAuthority,
    CapabilityResumeAuthorization,
    CapabilityResumeExecutionRequest,
)
from gateway.capability_resume_contracts import (
    CapabilityResumeAuthorizationRecordV1,
    CapabilityResumeDispatchStartedV1,
    CapabilityResumeSourceSnapshotV1,
    CapabilityResumeTerminalV1,
)
from gateway.contracts import RuntimeOperationProjection


class CapabilityResumeError(RuntimeError):
    """Base error for sanitized HTTP translation."""


class CapabilityResumeConflict(CapabilityResumeError):
    """A caller or persisted record changed an immutable binding."""


class CapabilityResumeNotFound(CapabilityResumeError):
    """The requested authorization does not exist."""


class CapabilityResumeUnavailable(CapabilityResumeError):
    """Required persistent authority or terminal runtime truth is absent."""


class CapabilityResumeDispatchFailed(CapabilityResumeError):
    """Authoritative Runtime truth reports a terminal failed outcome."""

    def __init__(self, failure: CapabilityResumeTerminalV1) -> None:
        super().__init__("capability resume dispatch failed")
        self.failure = failure


class CapabilityResumeRepository(Protocol):
    def authorize_once(
        self,
        request: CapabilityResumeExecutionRequest,
        source: "CapabilityResumeAuthoritySource",
        build: Callable[
            [CapabilityResumeSourceSnapshotV1], CapabilityResumeAuthorizationRecordV1
        ],
    ) -> CapabilityResumeAuthorizationRecordV1: ...

    def authorization_for_reservation(
        self, reservation_id: UUID
    ) -> CapabilityResumeAuthorizationRecordV1 | None: ...

    def authorization(
        self, receipt_id: UUID
    ) -> CapabilityResumeAuthorizationRecordV1 | None: ...

    def record_authorization(
        self, record: CapabilityResumeAuthorizationRecordV1
    ) -> CapabilityResumeAuthorizationRecordV1: ...

    def dispatch_started(self, receipt_id: UUID) -> bool: ...

    def record_dispatch_started(self, receipt_id: UUID) -> bool: ...

    def terminal(self, receipt_id: UUID) -> CapabilityResumeTerminalV1 | None: ...

    def record_terminal(
        self, terminal: CapabilityResumeTerminalV1
    ) -> CapabilityResumeTerminalV1: ...


class CapabilityResumeAuthoritySource(Protocol):
    def resolve(
        self, request: CapabilityResumeExecutionRequest
    ) -> CapabilityResumeSourceSnapshotV1: ...


class CapabilityResumeRuntimeReader(Protocol):
    def runtime_operation(
        self, command_id: UUID
    ) -> RuntimeOperationProjection | None: ...

    def runtime_provider_settlement(
        self, command_id: UUID, result_id: UUID
    ) -> RuntimeProviderCostSettlementV1 | None: ...


class CapabilityResumeExecutor(Protocol):
    async def execute(self, command: AgentRuntimeCommand) -> AgentRuntimeResult: ...


class CapabilityResumeService:
    """Persist a dispatch fence and trust only terminal runtime read-back."""

    def __init__(
        self,
        *,
        repository: CapabilityResumeRepository,
        authority_source: CapabilityResumeAuthoritySource,
        runtime_reader: CapabilityResumeRuntimeReader,
        executor: CapabilityResumeExecutor | None,
    ) -> None:
        self._repository = repository
        self._authority_source = authority_source
        self._runtime_reader = runtime_reader
        self._executor = executor

    def authorize(
        self, request: CapabilityResumeExecutionRequest
    ) -> CapabilityResumeAuthorization:
        persisted = self._repository.authorize_once(
            request,
            self._authority_source,
            lambda snapshot: self._authorization_record(request, snapshot),
        )
        return persisted.authorization

    def _authorization_record(
        self,
        request: CapabilityResumeExecutionRequest,
        snapshot: CapabilityResumeSourceSnapshotV1,
    ) -> CapabilityResumeAuthorizationRecordV1:
        self._require_request_binding(request, snapshot)
        original = snapshot.original_operation
        result = original.result
        if result is None:
            raise CapabilityResumeUnavailable(
                "original persisted runtime result is unavailable"
            )
        evidence_refs = _result_evidence(result)
        if not evidence_refs:
            raise CapabilityResumeUnavailable(
                "original persisted runtime result has no evidence"
            )
        if not _is_resumable_result(result):
            raise CapabilityResumeUnavailable(
                "original persisted RuntimeCodex state is not resumable"
            )

        authorization_id = _authorization_id(request)
        authorization = CapabilityResumeAuthorization(
            job_id=request.job_id,
            reservation_id=request.reservation_id,
            correlation_id=request.correlation_id,
            capability_id=request.capability_id,
            capability_version=request.capability_version,
            authorization_receipt_id=authorization_id,
            evidence_refs=evidence_refs,
        )
        resume_command = AgentRuntimeCommand(
            schema_name="captain.agent-runtime-command.v1",
            event_id=uuid5(authorization_id, "captain.capability-resume-command.v1"),
            correlation_id=original.command.correlation_id,
            causation_id=original.command.event_id,
            occurred_at=result.occurred_at,
            producer="captain",
            subject_id=original.command.subject_id,
            subject_version=original.command.subject_version,
            payload=original.command.payload.model_copy(
                update={
                    "operation": RuntimeOperation.CODEX_RESUME,
                    "maximum_cost_usd": snapshot.resume_budget_usd,
                    "budget_reservation_id": request.reservation_id,
                    "cost_job_id": request.job_id,
                    "cost_run_id": request.run_id,
                    "cost_input_id": request.input_id,
                    "cost_capability_id": request.capability_id,
                    "cost_capability_version": request.capability_version,
                    "cost_authority_ref": (
                        "gateway://capability-resume-authorizations/"
                        f"{authorization_id}"
                    ),
                }
            ),
        )
        record = CapabilityResumeAuthorizationRecordV1(
            request=request,
            authorization=authorization,
            resume_command=resume_command,
            booked_cost_usd=snapshot.booked_cost_usd,
            resume_budget_usd=snapshot.resume_budget_usd,
        )
        return record

    async def dispatch(
        self, authorization: CapabilityResumeAuthorization
    ) -> CapabilityExecutionAuthority:
        record = self._exact_record(authorization)
        terminal = self._repository.terminal(
            authorization.authorization_receipt_id
        )
        if terminal is not None:
            return self._validated_terminal_execution(record, terminal)
        if self._repository.dispatch_started(
            authorization.authorization_receipt_id
        ):
            return self._recover(record)
        if self._executor is None:
            raise CapabilityResumeUnavailable("capability resume executor is unavailable")

        dispatch_created = self._repository.record_dispatch_started(
            authorization.authorization_receipt_id
        )
        if not dispatch_created:
            return self._recover(record)
        try:
            await self._executor.execute(record.resume_command)
        except Exception:
            try:
                return self._recover(record)
            except CapabilityResumeUnavailable:
                raise CapabilityResumeUnavailable(
                    "capability resume outcome is ambiguous pending Runtime readback"
                ) from None
        return self._recover(record)

    def readback(
        self, authorization: CapabilityResumeAuthorization
    ) -> CapabilityExecutionAuthority:
        record = self._exact_record(authorization)
        terminal = self._repository.terminal(
            authorization.authorization_receipt_id
        )
        if terminal is not None:
            return self._validated_terminal_execution(record, terminal)
        if not self._repository.dispatch_started(
            authorization.authorization_receipt_id
        ):
            raise CapabilityResumeUnavailable("capability resume was not dispatched")
        return self._recover(record)

    def _validated_terminal_execution(
        self,
        record: CapabilityResumeAuthorizationRecordV1,
        terminal: CapabilityResumeTerminalV1,
    ) -> CapabilityExecutionAuthority:
        operation = self._runtime_reader.runtime_operation(
            record.resume_command.event_id
        )
        result = operation.result if operation is not None else None
        if result is None or result.event_id != terminal.execution.execution_result_id:
            raise CapabilityResumeUnavailable(
                "persisted terminal runtime result is unavailable"
            )
        settlement = self._runtime_reader.runtime_provider_settlement(
            record.resume_command.event_id,
            result.event_id,
        )
        if settlement is None:
            raise CapabilityResumeUnavailable(
                "persisted runtime provider settlement is unavailable"
            )
        _require_result_binding(result, record.resume_command)
        _require_provider_settlement_binding(
            settlement,
            record.resume_command,
            result,
        )
        actual = settlement.receipt.actual_cost_usd
        execution = terminal.execution
        if (
            execution.resume_actual_spend_usd != actual
            or execution.booked_cost_usd != record.booked_cost_usd + actual
            or execution.resume_budget_violation
            != (actual > record.resume_budget_usd)
        ):
            raise CapabilityResumeConflict(
                "persisted terminal provider settlement binding changed"
            )
        return self._terminal_execution(terminal)

    def _exact_record(
        self, authorization: CapabilityResumeAuthorization
    ) -> CapabilityResumeAuthorizationRecordV1:
        record = self._repository.authorization(
            authorization.authorization_receipt_id
        )
        if record is None:
            raise CapabilityResumeNotFound("capability resume authorization not found")
        if record.authorization != authorization:
            raise CapabilityResumeConflict(
                "capability resume authorization binding does not match"
            )
        return record

    def _recover(
        self, record: CapabilityResumeAuthorizationRecordV1
    ) -> CapabilityExecutionAuthority:
        operation = self._runtime_reader.runtime_operation(
            record.resume_command.event_id
        )
        if operation is None or operation.result is None:
            raise CapabilityResumeUnavailable(
                "persisted runtime result is unavailable"
            )
        if operation.command != record.resume_command:
            raise CapabilityResumeConflict(
                "persisted resume command binding does not match"
            )
        result = operation.result
        _require_result_binding(result, record.resume_command)
        provider_settlement = self._runtime_reader.runtime_provider_settlement(
            record.resume_command.event_id,
            result.event_id,
        )
        if provider_settlement is None:
            raise CapabilityResumeUnavailable(
                "persisted runtime provider settlement is unavailable"
            )
        _require_provider_settlement_binding(
            provider_settlement,
            record.resume_command,
            result,
        )
        evidence_refs = _result_evidence(result)
        if not evidence_refs:
            raise CapabilityResumeUnavailable(
                "persisted resume runtime result has no evidence"
            )
        authorization = record.authorization
        if result.status is RuntimeStatus.SUCCEEDED and result.cost_evidence is None:
            raise CapabilityResumeUnavailable(
                "successful resume runtime result has no actual cost evidence"
            )
        actual_spend = provider_settlement.receipt.actual_cost_usd
        execution = CapabilityExecutionAuthority(
            job_id=authorization.job_id,
            correlation_id=authorization.correlation_id,
            capability_id=authorization.capability_id,
            capability_version=authorization.capability_version,
            execution_authority_id=authorization.authorization_receipt_id,
            execution_command_id=record.resume_command.event_id,
            execution_result_id=result.event_id,
            evidence_refs=evidence_refs,
            booked_cost_usd=(
                record.booked_cost_usd + actual_spend
                if actual_spend is not None
                else record.booked_cost_usd
            ),
            resume_actual_spend_usd=actual_spend,
            resume_budget_violation=(
                actual_spend is not None and actual_spend > record.resume_budget_usd
            ),
            terminal_status=_terminal_status(result),
            replayed=False,
        )
        terminal = CapabilityResumeTerminalV1(
            authorization_receipt_id=authorization.authorization_receipt_id,
            execution=execution,
        )
        persisted = self._repository.record_terminal(terminal)
        if persisted != terminal:
            raise CapabilityResumeConflict(
                "persisted capability resume terminal does not match"
            )
        return self._terminal_execution(persisted)

    @staticmethod
    def _terminal_execution(
        terminal: CapabilityResumeTerminalV1,
    ) -> CapabilityExecutionAuthority:
        if terminal.execution.terminal_status == "failed":
            raise CapabilityResumeDispatchFailed(terminal)
        return terminal.execution

    @staticmethod
    def _require_request_binding(
        request: CapabilityResumeExecutionRequest,
        snapshot: CapabilityResumeSourceSnapshotV1,
    ) -> None:
        operation = snapshot.original_operation
        result = operation.result
        if (
            request.job_id != snapshot.job_id
            or request.reservation_id != snapshot.reservation_id
            or request.run_id != snapshot.run_id
            or request.input_id != snapshot.input_id
            or request.maximum_cost_usd != snapshot.maximum_cost_usd
            or request.booked_cost_usd != snapshot.booked_cost_usd
            or request.correlation_id != snapshot.correlation_id
            or request.capability_id != snapshot.capability_id
            or request.capability_version != snapshot.capability_version
            or request.original_execution_command_id != operation.command.event_id
            or result is None
            or request.original_execution_result_id != result.event_id
            or operation.operation_id != operation.command.event_id
            or result.command_id != operation.command.event_id
            or result.correlation_id != request.correlation_id
            or operation.command.correlation_id != request.correlation_id
            or result.subject_version != request.capability_version
            or operation.command.subject_version != request.capability_version
            or snapshot.booked_cost_usd > snapshot.maximum_cost_usd
            or snapshot.resume_budget_usd
            != snapshot.maximum_cost_usd - snapshot.booked_cost_usd
            or snapshot.resume_budget_usd <= 0
        ):
            raise CapabilityResumeConflict(
                "capability resume request binding does not match Gateway truth"
            )


def _authorization_id(request: CapabilityResumeExecutionRequest) -> UUID:
    canonical = json.dumps(
        request.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    return uuid5(
        NAMESPACE_URL,
        f"captain.capability-resume-authorization.v1:{digest}",
    )


def _result_evidence(result: AgentRuntimeResult):
    unique = {}
    for reference in (*result.evidence_refs, *result.artifact_refs):
        unique[(reference.uri, reference.sha256, reference.media_type)] = reference
    if result.cost_evidence is not None:
        reference = result.cost_evidence.evidence_ref
        unique[(reference.uri, reference.sha256, reference.media_type)] = reference
    return tuple(unique.values())


def _require_result_binding(
    result: AgentRuntimeResult,
    command: AgentRuntimeCommand,
) -> None:
    if (
        result.command_id != command.event_id
        or result.correlation_id != command.correlation_id
        or result.subject_id != command.subject_id
        or result.subject_version != command.subject_version
        or result.operation is not command.payload.operation
    ):
        raise CapabilityResumeConflict(
            "persisted resume result binding does not match"
        )


def _require_provider_settlement_binding(
    settlement: RuntimeProviderCostSettlementV1,
    command: AgentRuntimeCommand,
    result: AgentRuntimeResult,
) -> None:
    receipt = settlement.receipt
    evidence = result.cost_evidence
    if (
        receipt.request_id != command.event_id
        or receipt.command_id != (command.causation_id or command.event_id)
        or receipt.result_id != result.event_id
        or receipt.reservation_id != command.payload.budget_reservation_id
        or evidence is None
        or evidence.receipt_id != receipt.receipt_id
        or evidence.command_id != command.event_id
        or evidence.result_id != result.event_id
        or evidence.original_command_id != receipt.command_id
        or evidence.reservation_id != receipt.reservation_id
        or evidence.job_id != receipt.job_id
        or evidence.run_id != receipt.run_id
        or evidence.input_id != receipt.input_id
        or evidence.correlation_id != receipt.correlation_id
        or evidence.capability_id != receipt.capability_id
        or evidence.capability_version != receipt.capability_version
        or evidence.provider != receipt.provider
        or evidence.model != receipt.model
        or evidence.input_units != receipt.input_units
        or evidence.cached_input_units != receipt.cached_input_units
        or evidence.output_units != receipt.output_units
        or evidence.actual_cost_usd != receipt.actual_cost_usd
        or evidence.pricing_snapshot_id != receipt.pricing_snapshot_id
        or evidence.pricing_snapshot_sha256 != receipt.pricing_snapshot_sha256
        or evidence.started_at != receipt.started_at
        or evidence.ended_at != receipt.ended_at
    ):
        raise CapabilityResumeConflict(
            "persisted provider settlement does not match resume result"
        )


def _terminal_status(result: AgentRuntimeResult):
    if result.status is RuntimeStatus.SUCCEEDED:
        return "succeeded"
    if result.status is RuntimeStatus.CANCELLED:
        return "cancelled"
    if (
        result.status is RuntimeStatus.INFRASTRUCTURE_FAILED
        and result.error == "codex execution timed out (exit 124)"
    ):
        return "timed_out"
    if result.status in {
        RuntimeStatus.FAILED,
        RuntimeStatus.INFRASTRUCTURE_FAILED,
        RuntimeStatus.POLICY_FAILED,
    }:
        return "failed"
    raise CapabilityResumeUnavailable("persisted runtime result is not terminal")


def _is_resumable_result(result: AgentRuntimeResult) -> bool:
    return (
        result.operation is RuntimeOperation.CODEX_RUN
        and result.session_id is not None
        and bool(_result_evidence(result))
        and (
            result.status in {RuntimeStatus.FAILED, RuntimeStatus.CANCELLED}
            or (
                result.status is RuntimeStatus.INFRASTRUCTURE_FAILED
                and result.error == "codex execution timed out (exit 124)"
            )
        )
    )


__all__ = [
    "CapabilityResumeAuthoritySource",
    "CapabilityResumeConflict",
    "CapabilityResumeDispatchFailed",
    "CapabilityResumeError",
    "CapabilityResumeExecutor",
    "CapabilityResumeNotFound",
    "CapabilityResumeRepository",
    "CapabilityResumeRuntimeReader",
    "CapabilityResumeService",
    "CapabilityResumeUnavailable",
]
