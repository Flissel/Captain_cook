"""Gateway-authoritative persistence and recovery for supervised Codex runs."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import subprocess
from collections.abc import Callable
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agenten.delivery.gateway_client import GatewayDeliveryClient
from agenten.execution.codex_events import CodexParseWarning, CodexProcessEvent
from agenten.execution.codex_supervisor import CodexRunRequest
from gateway.contracts import (
    CodexSessionEventPayload,
    CodexSessionFinishedPayload,
    CodexSessionStartedPayload,
    CodexSessionWarningPayload,
    DeliveryEventEnvelope,
    TraceContext,
)


_EVENT_NAMESPACE = UUID("4f0761de-1a82-4b36-b853-878bfddb637d")
OutcomeClass = Literal[
    "succeeded",
    "behavioral_failure",
    "infrastructure_failure",
    "policy_failure",
    "cancelled",
    "lost_process",
]
CancellationReason = Literal["operator", "timeout", "shutdown", "claim_lost"]


class CodexOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    classification: OutcomeClass
    exit_code: int | None = None
    cancellation_reason: CancellationReason | None = None
    behavioral_repair_increment: Literal[0, 1] = 0

    @model_validator(mode="after")
    def require_consistent_classification(self) -> "CodexOutcome":
        expected = 1 if self.classification == "behavioral_failure" else 0
        if self.behavioral_repair_increment != expected:
            raise ValueError("only behavioral_failure increments repair")
        if self.classification == "cancelled":
            if self.cancellation_reason is None:
                raise ValueError("cancelled outcome requires cancellation_reason")
        elif self.cancellation_reason is not None:
            raise ValueError("cancellation_reason requires cancelled outcome")
        return self


class CancellationExecutionError(RuntimeError):
    """The session-bound process could not be safely cancelled."""


class CancellationPersistenceRequired(RuntimeError):
    """The process was cancelled but terminal Gateway evidence needs recovery."""

    def __init__(self, result: "CodexCancellationResult") -> None:
        super().__init__("cancelled process terminal evidence requires recovery")
        self.result = result


class CodexProcessIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    session_id: str = Field(min_length=1)
    pid: int = Field(ge=1)
    started_at_utc: datetime
    start_time_utc_ticks: int = Field(ge=1)
    executable: str = Field(min_length=1)


class CodexCancellationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    session_id: str = Field(min_length=1)
    outcome: Literal["cancelled"]
    cancellation_reason: CancellationReason


class CodexRunRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    project_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    batch_id: str = Field(min_length=1)
    worker_id: str = Field(min_length=1)
    claim_id: str = Field(min_length=1)
    fencing_token: int = Field(ge=1)
    session_id: str = Field(min_length=1)
    iteration: int = Field(ge=1)
    process_ref: str = Field(pattern=r"^artifact://")
    command_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    started_at: datetime
    outcome: CodexOutcome | None = None


class CodexRunRepository(Protocol):
    async def start(self, request: CodexRunRequest) -> CodexRunRecord: ...

    async def append(
        self, event: CodexProcessEvent | CodexParseWarning
    ) -> None: ...

    async def active(
        self, *, worker_id: str
    ) -> tuple[CodexRunRecord, ...]: ...

    async def finish(
        self, session_id: str, outcome: CodexOutcome
    ) -> None: ...

    async def persist_cancellation(
        self, result: CodexCancellationResult
    ) -> None: ...



class CodexCancellationCoordinator:
    """Cancel one verified process tree and durably finish its active session."""

    def __init__(
        self,
        *,
        repository: CodexRunRepository,
        worker_id: str,
        pwsh_path: Path,
        script_path: Path,
    ) -> None:
        if not worker_id:
            raise ValueError("worker_id must not be empty")
        self._repository = repository
        self._worker_id = worker_id
        self._pwsh_path = pwsh_path.resolve(strict=True)
        self._script_path = script_path.resolve(strict=True)

    async def cancel(
        self,
        *,
        session_id: str,
        state_path: Path,
        reason: CancellationReason,
    ) -> CodexCancellationResult:
        active = await self._repository.active(worker_id=self._worker_id)
        if not any(record.session_id == session_id for record in active):
            raise CancellationExecutionError("cancellation requires an active session")

        try:
            identity = CodexProcessIdentity.model_validate_json(
                state_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            raise CancellationExecutionError(
                "cancellation process identity is invalid"
            ) from None
        if identity.session_id != session_id:
            raise CancellationExecutionError(
                "cancellation process identity does not match session"
            )

        completed = await asyncio.to_thread(
            subprocess.run,
            [
                str(self._pwsh_path),
                "-NoProfile",
                "-File",
                str(self._script_path),
                "-CancelStatePath",
                str(state_path.resolve()),
                "-SessionId",
                session_id,
                "-CancellationReason",
                reason,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise CancellationExecutionError("session-bound cancellation failed")
        try:
            result = CodexCancellationResult.model_validate_json(
                completed.stdout.strip()
            )
        except ValueError:
            raise CancellationExecutionError(
                "session-bound cancellation returned invalid evidence"
            ) from None
        if (
            result.session_id != session_id
            or result.outcome != "cancelled"
            or result.cancellation_reason != reason
        ):
            raise CancellationExecutionError(
                "session-bound cancellation result does not match request"
            )

        try:
            await self._repository.persist_cancellation(result)
        except Exception:
            raise CancellationPersistenceRequired(result) from None
        return result


class GatewayCodexRunRepository:
    """Store and reconstruct Codex runs exclusively from Gateway event history."""

    def __init__(
        self,
        *,
        client: GatewayDeliveryClient,
        project_id: str,
        run_id: str,
        actor: str,
        now: Callable[[], datetime],
    ) -> None:
        self._client = client
        self._project_id = project_id
        self._run_id = run_id
        self._actor = actor
        self._now = now
        self._current_session_id: str | None = None

    async def start(self, request: CodexRunRequest) -> CodexRunRecord:
        record = self._record_from_request(request)
        events = await self._history()
        existing = self._started(events).get(record.session_id)
        if existing is not None:
            if record.session_id in self._finished(events):
                raise ValueError("terminal Codex session cannot be restarted")
            if self._identity(existing) != self._identity(record):
                raise ValueError("Codex session identity conflicts with Gateway history")
            self._current_session_id = record.session_id
            return existing

        payload = CodexSessionStartedPayload(
            event_type="codex_session_started",
            session_id=record.session_id,
            process_ref=record.process_ref,
            started_at=record.started_at,
            iteration=record.iteration,
            command_sha256=record.command_sha256,
            workspace_sha256=record.workspace_sha256,
        )
        await self._append_envelope(
            event_type="codex_session_started",
            trace=self._trace(record),
            payload=payload,
            event_key=f"started:{record.session_id}",
            occurred_at=record.started_at,
        )
        self._current_session_id = record.session_id
        return record

    async def record_codex_event(
        self,
        batch_id: str,
        claim_token: str,
        *,
        iteration: int,
        session_id: str,
        event: CodexProcessEvent | CodexParseWarning,
    ) -> None:
        del claim_token  # Gateway validates current claim identity atomically.
        records = self._started(await self._history())
        record = records.get(session_id)
        if (
            record is None
            or record.batch_id != batch_id
            or record.iteration != iteration
        ):
            raise ValueError("Codex event does not match its Gateway session")
        self._current_session_id = session_id
        await self.append(event)

    async def append(
        self, event: CodexProcessEvent | CodexParseWarning
    ) -> None:
        session_id = (
            event.session_id
            if isinstance(event, CodexProcessEvent) and event.session_id is not None
            else self._current_session_id
        )
        if session_id is None:
            raise ValueError("Codex event has no active session")
        if event.source_sequence is None:
            raise ValueError("Codex event requires source_sequence")
        events = await self._history()
        record = self._started(events).get(session_id)
        if record is None or session_id in self._finished(events):
            raise ValueError("Codex event requires an active session")
        trace = self._trace(record)
        if isinstance(event, CodexProcessEvent):
            safe = event.model_dump(exclude={"event_type", "session_id"})
            payload = CodexSessionEventPayload(
                event_type="codex_session_event",
                session_id=session_id,
                **safe,
            )
            event_type = "codex_session_event"
        else:
            payload = CodexSessionWarningPayload(
                event_type="codex_session_warning",
                session_id=session_id,
                source_sequence=event.source_sequence,
                warning_type=event.warning_type,
                line_sha256=event.line_sha256,
            )
            event_type = "codex_session_warning"
        canonical = payload.model_dump(mode="json")
        digest = hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        await self._append_envelope(
            event_type=event_type,
            trace=trace,
            payload=payload,
            event_key=f"{event_type}:{session_id}:{digest}",
        )

    async def active(
        self, *, worker_id: str
    ) -> tuple[CodexRunRecord, ...]:
        events = await self._history()
        finished = self._finished(events)
        return tuple(
            record
            for record in self._started(events).values()
            if record.worker_id == worker_id and record.session_id not in finished
        )

    async def finish(
        self, session_id: str, outcome: CodexOutcome
    ) -> None:
        events = await self._history()
        terminal_outcomes = self._terminal_outcomes(events)
        if session_id in terminal_outcomes:
            if terminal_outcomes[session_id] != outcome:
                raise ValueError("terminal outcome conflicts with Gateway history")
            return
        record = self._started(events).get(session_id)
        if record is None:
            raise ValueError("Codex session was not started")
        ended_at = self._now()
        payload = CodexSessionFinishedPayload(
            event_type="codex_session_finished",
            session_id=session_id,
            process_ref=record.process_ref,
            started_at=record.started_at,
            ended_at=ended_at,
            outcome=outcome.classification,
            exit_code=outcome.exit_code,
            cancellation_reason=outcome.cancellation_reason,
            behavioral_repair_increment=outcome.behavioral_repair_increment,
        )
        await self._append_envelope(
            event_type="codex_session_finished",
            trace=self._trace(record),
            payload=payload,
            event_key=f"finished:{session_id}",
            occurred_at=ended_at,
        )

    async def persist_cancellation(
        self,
        result: CodexCancellationResult,
    ) -> None:
        await self.finish(
            result.session_id,
            CodexOutcome(
                classification=result.outcome,
                cancellation_reason=result.cancellation_reason,
            ),
        )

    async def reconcile(
        self,
        *,
        worker_id: str,
        live_process_ids: frozenset[str],
    ) -> tuple[CodexRunRecord, ...]:
        reconciled: list[CodexRunRecord] = []
        for record in await self.active(worker_id=worker_id):
            if record.process_ref in live_process_ids:
                continue
            outcome = CodexOutcome(classification="lost_process")
            await self.finish(record.session_id, outcome)
            reconciled.append(record.model_copy(update={"outcome": outcome}))
        return tuple(reconciled)

    async def _history(self) -> tuple[DeliveryEventEnvelope, ...]:
        return await self._client.delivery_events(
            project_id=self._project_id,
            run_id=self._run_id,
        )
    async def _append_envelope(
        self,
        *,
        event_type: str,
        trace: TraceContext,
        payload: BaseModel,
        event_key: str,
        occurred_at: datetime | None = None,
    ) -> None:
        event_id = uuid5(_EVENT_NAMESPACE, event_key)
        existing = next(
            (event for event in await self._history() if event.event_id == event_id),
            None,
        )
        if existing is not None:
            return
        envelope = DeliveryEventEnvelope.model_validate(
            {
                "event_id": event_id,
                "event_type": event_type,
                "occurred_at": occurred_at or self._now(),
                "actor": self._actor,
                "trace": trace,
                "payload": payload,
            }
        )
        await self._client.append_delivery_event(envelope)

    def _record_from_request(self, request: CodexRunRequest) -> CodexRunRecord:
        required = (
            request.project_id,
            request.run_id,
            request.trace_id,
            request.batch_id,
            request.worker_id,
            request.claim_id,
            request.fencing_token,
        )
        if any(value is None for value in required):
            raise ValueError("complete delivery trace context is required")
        if request.project_id != self._project_id or request.run_id != self._run_id:
            raise ValueError("request does not belong to repository delivery run")
        command_sha256 = hashlib.sha256(
            "\0".join(request.command).encode("utf-8")
        ).hexdigest()
        workspace_sha256 = hashlib.sha256(
            str(request.workspace.resolve()).encode("utf-8")
        ).hexdigest()
        process_digest = hashlib.sha256(
            request.session_id.encode("utf-8")
        ).hexdigest()[:32]
        return CodexRunRecord(
            project_id=request.project_id,
            run_id=request.run_id,
            trace_id=request.trace_id,
            batch_id=request.batch_id,
            worker_id=request.worker_id,
            claim_id=request.claim_id,
            fencing_token=request.fencing_token,
            session_id=request.session_id,
            iteration=request.iteration,
            process_ref=f"artifact://processes/{process_digest}",
            command_sha256=command_sha256,
            workspace_sha256=workspace_sha256,
            started_at=self._now(),
        )

    @staticmethod
    def _trace(record: CodexRunRecord) -> TraceContext:
        return TraceContext(
            project_id=record.project_id,
            run_id=record.run_id,
            trace_id=record.trace_id,
            batch_id=record.batch_id,
            worker_id=record.worker_id,
            claim_id=record.claim_id,
            fencing_token=record.fencing_token,
            session_id=record.session_id,
        )

    @staticmethod
    def _started(
        events: tuple[DeliveryEventEnvelope, ...],
    ) -> dict[str, CodexRunRecord]:
        records: dict[str, CodexRunRecord] = {}
        for event in events:
            payload = event.payload
            if not isinstance(payload, CodexSessionStartedPayload):
                continue
            trace = event.trace
            if (
                trace.batch_id is None
                or trace.worker_id is None
                or trace.claim_id is None
                or trace.fencing_token is None
                or trace.session_id is None
            ):
                continue
            records.setdefault(
                payload.session_id,
                CodexRunRecord(
                    project_id=trace.project_id,
                    run_id=trace.run_id,
                    trace_id=trace.trace_id,
                    batch_id=trace.batch_id,
                    worker_id=trace.worker_id,
                    claim_id=trace.claim_id,
                    fencing_token=trace.fencing_token,
                    session_id=payload.session_id,
                    iteration=payload.iteration,
                    process_ref=payload.process_ref,
                    command_sha256=payload.command_sha256,
                    workspace_sha256=payload.workspace_sha256,
                    started_at=payload.started_at,
                ),
            )
        return records

    @staticmethod
    def _identity(record: CodexRunRecord) -> dict[str, object]:
        return record.model_dump(exclude={"started_at", "outcome"})

    @staticmethod
    def _terminal_outcomes(
        events: tuple[DeliveryEventEnvelope, ...],
    ) -> dict[str, CodexOutcome]:
        outcomes: dict[str, CodexOutcome] = {}
        for event in events:
            payload = event.payload
            if not isinstance(payload, CodexSessionFinishedPayload):
                continue
            outcome = CodexOutcome(
                classification=payload.outcome,
                exit_code=payload.exit_code,
                cancellation_reason=payload.cancellation_reason,
                behavioral_repair_increment=payload.behavioral_repair_increment,
            )
            existing = outcomes.get(payload.session_id)
            if existing is not None and existing != outcome:
                raise ValueError("conflicting terminal Codex session history")
            outcomes.setdefault(payload.session_id, outcome)
        return outcomes

    @classmethod
    def _finished(
        cls,
        events: tuple[DeliveryEventEnvelope, ...],
    ) -> frozenset[str]:
        return frozenset(cls._terminal_outcomes(events))
