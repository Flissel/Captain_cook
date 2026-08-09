"""Authorize, dispatch, and read back one persistent capability continuation."""

from __future__ import annotations

import hashlib
import json
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from agenten.agent_runtime.contracts import (
    AgentRuntimeCommand,
    AgentRuntimeResult,
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


class CapabilityResumeRepository(Protocol):
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
        existing = self._repository.authorization_for_reservation(
            request.reservation_id
        )
        if existing is not None:
            if existing.request != request:
                raise CapabilityResumeConflict(
                    "capability resume reservation binding does not match"
                )
            return existing.authorization

        snapshot = self._authority_source.resolve(request)
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
            causation_id=result.event_id,
            occurred_at=result.occurred_at,
            producer="captain",
            subject_id=original.command.subject_id,
            subject_version=original.command.subject_version,
            payload=original.command.payload.model_copy(
                update={"operation": RuntimeOperation.CODEX_RESUME}
            ),
        )
        record = CapabilityResumeAuthorizationRecordV1(
            request=request,
            authorization=authorization,
            resume_command=resume_command,
            booked_cost_usd=snapshot.booked_cost_usd,
        )
        persisted = self._repository.record_authorization(record)
        if persisted != record:
            raise CapabilityResumeConflict(
                "persisted capability resume authorization does not match"
            )
        return persisted.authorization

    async def dispatch(
        self, authorization: CapabilityResumeAuthorization
    ) -> CapabilityExecutionAuthority:
        record = self._exact_record(authorization)
        terminal = self._repository.terminal(
            authorization.authorization_receipt_id
        )
        if terminal is not None:
            return terminal.execution
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
                    "capability resume effect did not persist a runtime result"
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
            return terminal.execution
        if not self._repository.dispatch_started(
            authorization.authorization_receipt_id
        ):
            raise CapabilityResumeUnavailable("capability resume was not dispatched")
        return self._recover(record)

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
        evidence_refs = _result_evidence(result)
        if not evidence_refs:
            raise CapabilityResumeUnavailable(
                "persisted resume runtime result has no evidence"
            )
        authorization = record.authorization
        execution = CapabilityExecutionAuthority(
            job_id=authorization.job_id,
            correlation_id=authorization.correlation_id,
            capability_id=authorization.capability_id,
            capability_version=authorization.capability_version,
            execution_authority_id=authorization.authorization_receipt_id,
            execution_command_id=record.resume_command.event_id,
            execution_result_id=result.event_id,
            evidence_refs=evidence_refs,
            booked_cost_usd=record.booked_cost_usd,
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
        return persisted.execution

    @staticmethod
    def _require_request_binding(
        request: CapabilityResumeExecutionRequest,
        snapshot: CapabilityResumeSourceSnapshotV1,
    ) -> None:
        operation = snapshot.original_operation
        result = operation.result
        if (
            request.job_id != snapshot.job_id
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


__all__ = [
    "CapabilityResumeAuthoritySource",
    "CapabilityResumeConflict",
    "CapabilityResumeError",
    "CapabilityResumeExecutor",
    "CapabilityResumeNotFound",
    "CapabilityResumeRepository",
    "CapabilityResumeRuntimeReader",
    "CapabilityResumeService",
    "CapabilityResumeUnavailable",
]
