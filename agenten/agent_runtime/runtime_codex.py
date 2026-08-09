"""Redacted coordination and recovery over Captain's existing Codex runner."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Protocol
from uuid import UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agenten.agent_factory.orchestration import FactoryDispatchError
from agenten.agent_runtime.confined_files import ConfinedFileError, ConfinedFileStore
from agenten.agent_runtime.contracts import ArtifactRef
from agenten.execution.codex_policy import AuthorizedCodexRun, FrozenEnvironment
from agenten.execution.codex_supervisor import (
    CodexOutputEvidenceError,
    CodexOutputJournalPolicy,
    PowerShellCodexRunner,
    canonical_codex_event_type,
)


RuntimeCodexTerminalStatus = Literal["succeeded", "failed", "timed_out", "cancelled"]
RuntimeCodexProcessCleanupStatus = Literal["not_required", "verified_cancelled", "unresolved"]
RuntimeCodexCancelOutcome = Literal[
    "no_active_process",
    "requested_unverified",
    "verified_cancelled",
]
RuntimeCodexFailureKind = Literal[
    "journal_persistence_failed",
    "output_read_failed",
    "invalid_json_object",
    "record_size_limit_exceeded",
    "unterminated_record",
    "journal_size_limit_exceeded",
    "journal_record_count_exceeded",
    "observer_failed",
    "stream_mismatch",
]


@dataclass(frozen=True)
class RuntimeCodexInvocation:
    command_id: UUID
    correlation_id: UUID
    subject_id: str
    prompt_sha256: str
    command_identity_sha256: str
    session_id: str
    workspace_ref: str
    workspace_binding_sha256: str
    workspace: Path
    prompt: str
    timeout_seconds: int
    request_id: UUID | None = None
    model: str | None = None
    pricing_snapshot_id: str | None = None
    pricing_snapshot_sha256: str | None = None
    maximum_cost_usd: str | None = None
    cost_authority_ref: str | None = None
    hard_ceiling_enforced: bool = False
    provider_proxy_url: str | None = None
    provider_policy_sha256: str | None = None
    provider_price_card_sha256: str | None = None
    provider_context_sha256: str | None = None
    provider_result_id: UUID | None = None
    resume: bool = False
    prior_checkpoint_sha256: str | None = None


@dataclass(frozen=True)
class RuntimeCodexUsageV1(BaseModel):
    """Raw-free token usage derived only from recognized Codex JSONL events."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, populate_by_name=True)

    schema_name: Literal["captain.runtime-codex-usage.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    request_id: UUID
    command_id: UUID
    command_identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    session_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    model: str = Field(min_length=1)
    input_units: int = Field(ge=0, strict=True)
    cached_input_units: int = Field(ge=0, strict=True)
    output_units: int = Field(ge=0, strict=True)
    pricing_snapshot_id: str = Field(min_length=1)
    pricing_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    started_at: datetime
    ended_at: datetime

    @model_validator(mode="after")
    def require_valid_usage(self) -> "RuntimeCodexUsageV1":
        if self.cached_input_units > self.input_units:
            raise ValueError("cached input usage exceeds input usage")
        if self.ended_at < self.started_at:
            raise ValueError("runtime Codex usage time is invalid")
        return self


@dataclass(frozen=True)
class RuntimeCodexProcessResult:
    exit_code: int
    terminal_status: RuntimeCodexTerminalStatus
    process_cleanup_status: RuntimeCodexProcessCleanupStatus
    elapsed_ms: int
    session_id: str
    event_count: int
    event_types: tuple[str, ...]
    last_event_sha256: str | None
    failure_kind: RuntimeCodexFailureKind | None = None
    usage: RuntimeCodexUsageV1 | None = None

    def __post_init__(self) -> None:
        expected = (
            "timed_out"
            if self.exit_code == 124
            else "cancelled"
            if self.exit_code == 130
            else "succeeded"
            if self.exit_code == 0
            else "failed"
        )
        if self.terminal_status != expected:
            raise ValueError("runtime Codex exit and terminal status do not match")
        if self.elapsed_ms < 0 or self.event_count < 0:
            raise ValueError("runtime Codex bounded counts must not be negative")
        if tuple(sorted(set(self.event_types))) != self.event_types:
            raise ValueError("runtime Codex event types must be sorted and unique")
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", self.session_id) is None:
            raise ValueError("runtime Codex session identity is invalid")


class RuntimeCodexTerminalEvidenceV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, populate_by_name=True)

    schema_name: Literal["captain.runtime-codex-terminal-evidence.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    command_id: UUID
    correlation_id: UUID
    command_identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_ref: str = Field(pattern=r"^workspace://[a-z0-9][a-z0-9/-]{0,126}[a-z0-9]$")
    workspace_binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    session_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    status: RuntimeCodexTerminalStatus
    exit_code: int
    elapsed_ms: int = Field(ge=0, strict=True)
    last_event_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    event_count: int = Field(ge=0, strict=True)
    event_types: tuple[str, ...] = ()
    process_cleanup_status: RuntimeCodexProcessCleanupStatus
    failure_kind: RuntimeCodexFailureKind | None = None
    resumable_checkpoint: ArtifactRef | None = None
    usage: RuntimeCodexUsageV1 | None = None

    @field_validator("event_types")
    @classmethod
    def require_unique_event_types(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if tuple(sorted(set(value))) != value:
            raise ValueError("runtime Codex event types must be sorted and unique")
        return value

    @model_validator(mode="after")
    def require_terminal_shape(self) -> "RuntimeCodexTerminalEvidenceV1":
        if self.exit_code == 124:
            if self.status != "timed_out" or self.resumable_checkpoint is None:
                raise ValueError("runtime Codex timeout requires a resumable checkpoint")
        elif self.status == "timed_out":
            raise ValueError("runtime Codex timeout status requires exit 124")
        if self.failure_kind is not None and self.status != "failed":
            raise ValueError("runtime Codex failure kind requires failed status")
        return self


RuntimeCodexObserver = Callable[[dict[str, object]], Awaitable[None]]


class RuntimeCodexProcessRunner(Protocol):
    async def run(self, invocation: RuntimeCodexInvocation, observer: RuntimeCodexObserver) -> RuntimeCodexProcessResult: ...

    async def cancel(self, session_id: str) -> RuntimeCodexCancelOutcome: ...


class _RuntimeObserverFailed(RuntimeError):
    pass


@dataclass
class _EventSummary:
    count: int = 0
    types: set[str] = field(default_factory=set)
    last_sha256: str | None = None
    session_id: str | None = None
    input_units: int = 0
    cached_input_units: int = 0
    output_units: int = 0
    usage_events: int = 0

    def observe(self, event: Mapping[str, object]) -> dict[str, object]:
        canonical = _runtime_codex_event(event)
        self.count += 1
        self.types.add(canonical_codex_event_type(canonical.get("type")))
        self.last_sha256 = hashlib.sha256(_runtime_codex_json(canonical)).hexdigest()
        if canonical.get("type") == "thread.started":
            candidate = canonical.get("thread_id")
            if isinstance(candidate, str) and re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", candidate
            ):
                self.session_id = candidate
        elif canonical.get("type") == "turn.completed":
            usage = canonical.get("usage")
            if usage is None:
                return canonical
            if not isinstance(usage, dict):
                raise FactoryDispatchError("Codex JSONL usage is invalid")
            values = (
                usage.get("input_tokens"),
                usage.get("cached_input_tokens"),
                usage.get("output_tokens"),
            )
            if any(type(value) is not int or value < 0 for value in values):
                raise FactoryDispatchError("Codex JSONL usage is invalid")
            input_units, cached_input_units, output_units = values
            if cached_input_units > input_units:
                raise FactoryDispatchError("Codex JSONL usage is invalid")
            self.input_units += input_units
            self.cached_input_units += cached_input_units
            self.output_units += output_units
            self.usage_events += 1
        return canonical


@dataclass
class _RuntimeCodexAttempt:
    cancellation_attempted: bool = False
    operator_cancel_outcome: RuntimeCodexCancelOutcome | None = None


class RuntimeCodexExecution:
    """Single-flight runtime state and immutable terminal recovery."""

    def __init__(self, *, runner: RuntimeCodexProcessRunner, checkpoint_root: Path) -> None:
        self._runner = runner
        self._checkpoints = ConfinedFileStore(checkpoint_root)
        self._terminal: dict[UUID, RuntimeCodexTerminalEvidenceV1] = {}
        self._identity_index: dict[tuple[UUID, str, str, str, str], UUID] = {}
        self._sessions: dict[UUID, str] = {}
        self._active: dict[
            UUID,
            tuple[
                str,
                asyncio.Task[RuntimeCodexTerminalEvidenceV1],
                _RuntimeCodexAttempt,
            ],
        ] = {}
        self._lock = asyncio.Lock()
        self._owner_id = uuid4().hex

    async def start(
        self,
        *,
        command_id: UUID,
        correlation_id: UUID,
        subject_id: str,
        prompt_sha256: str,
        workspace_ref: str,
        workspace: Path,
        prompt: str,
        timeout_seconds: int,
        observer: RuntimeCodexObserver,
        request_id: UUID | None = None,
        model: str | None = None,
        pricing_snapshot_id: str | None = None,
        pricing_snapshot_sha256: str | None = None,
    ) -> RuntimeCodexTerminalEvidenceV1:
        workspace_binding = _workspace_binding(workspace)
        identity = _runtime_codex_command_identity(
            command_id=command_id,
            correlation_id=correlation_id,
            subject_id=subject_id,
            prompt_sha256=prompt_sha256,
            workspace_ref=workspace_ref,
            workspace_binding_sha256=workspace_binding,
        )
        self._identity_index[
            (correlation_id, subject_id, prompt_sha256, workspace_ref, workspace_binding)
        ] = command_id
        invocation = RuntimeCodexInvocation(
            command_id=command_id,
            correlation_id=correlation_id,
            subject_id=subject_id,
            prompt_sha256=prompt_sha256,
            command_identity_sha256=identity,
            session_id=f"runtime-{identity[:24]}",
            workspace_ref=workspace_ref,
            workspace_binding_sha256=workspace_binding,
            workspace=workspace,
            prompt=prompt,
            timeout_seconds=timeout_seconds,
            request_id=request_id or command_id,
            model=model,
            pricing_snapshot_id=pricing_snapshot_id,
            pricing_snapshot_sha256=pricing_snapshot_sha256,
        )
        return await self._single_flight(invocation, observer)

    async def resume(
        self,
        *,
        correlation_id: UUID,
        causation_id: UUID | None,
        subject_id: str,
        prompt_sha256: str,
        workspace_ref: str,
        workspace: Path,
        prompt: str,
        timeout_seconds: int,
        observer: RuntimeCodexObserver,
        request_id: UUID | None = None,
        model: str | None = None,
        pricing_snapshot_id: str | None = None,
        pricing_snapshot_sha256: str | None = None,
        maximum_cost_usd: str | None = None,
        cost_authority_ref: str | None = None,
        hard_ceiling_enforced: bool = False,
        provider_proxy_url: str | None = None,
        provider_policy_sha256: str | None = None,
        provider_price_card_sha256: str | None = None,
        provider_context_sha256: str | None = None,
        provider_session_id: str | None = None,
        provider_result_id: UUID | None = None,
    ) -> RuntimeCodexTerminalEvidenceV1:
        workspace_binding = _workspace_binding(workspace)
        key = (correlation_id, subject_id, prompt_sha256, workspace_ref, workspace_binding)
        command_id = causation_id or self._identity_index.get(key)
        if command_id is None:
            raise FactoryDispatchError("Codex resume checkpoint is unavailable")
        prior = self.terminal_evidence(command_id)
        if prior is None or prior.status not in {"timed_out", "cancelled", "failed"}:
            raise FactoryDispatchError("Codex resume checkpoint is not resumable")
        expected_identity = _runtime_codex_command_identity(
            command_id=command_id,
            correlation_id=correlation_id,
            subject_id=subject_id,
            prompt_sha256=prompt_sha256,
            workspace_ref=workspace_ref,
            workspace_binding_sha256=workspace_binding,
        )
        if (
            prior.correlation_id != correlation_id
            or prior.command_identity_sha256 != expected_identity
            or prior.workspace_ref != workspace_ref
            or prior.workspace_binding_sha256 != workspace_binding
        ):
            raise FactoryDispatchError("Codex resume command identity changed")
        if provider_session_id is not None and provider_session_id != prior.session_id:
            raise FactoryDispatchError("Codex provider session binding changed")
        if (
            provider_result_id is not None
            and (request_id is None or provider_result_id != uuid5(request_id, "captain.runtime-result"))
        ):
            raise FactoryDispatchError("Codex provider result binding changed")
        self._identity_index[key] = command_id
        invocation = RuntimeCodexInvocation(
            command_id=command_id,
            correlation_id=correlation_id,
            subject_id=subject_id,
            prompt_sha256=prompt_sha256,
            command_identity_sha256=expected_identity,
            session_id=prior.session_id,
            workspace_ref=workspace_ref,
            workspace_binding_sha256=workspace_binding,
            workspace=workspace,
            prompt=prompt,
            timeout_seconds=timeout_seconds,
            request_id=request_id,
            model=model,
            pricing_snapshot_id=pricing_snapshot_id,
            pricing_snapshot_sha256=pricing_snapshot_sha256,
            maximum_cost_usd=maximum_cost_usd,
            cost_authority_ref=cost_authority_ref,
            hard_ceiling_enforced=hard_ceiling_enforced,
            provider_proxy_url=provider_proxy_url,
            provider_policy_sha256=provider_policy_sha256,
            provider_price_card_sha256=provider_price_card_sha256,
            provider_context_sha256=provider_context_sha256,
            provider_result_id=provider_result_id,
            resume=True,
            prior_checkpoint_sha256=(
                prior.resumable_checkpoint.sha256
                if prior.resumable_checkpoint is not None
                else None
            ),
        )
        return await self._single_flight(invocation, observer, replace_checkpoint=True)

    async def cancel(
        self,
        *,
        correlation_id: UUID,
        causation_id: UUID | None,
        subject_id: str,
        prompt_sha256: str,
        workspace_ref: str,
        workspace: Path,
    ) -> RuntimeCodexTerminalEvidenceV1:
        workspace_binding = _workspace_binding(workspace)
        key = (correlation_id, subject_id, prompt_sha256, workspace_ref, workspace_binding)
        command_id = causation_id or self._identity_index.get(key)
        if command_id is None:
            raise FactoryDispatchError("Codex cancellation target is unavailable")
        async with self._lock:
            active = self._active.get(command_id)
            terminal = self._terminal.get(command_id)
            if active is None:
                terminal = terminal or self.terminal_evidence(command_id)
                if terminal is None:
                    raise FactoryDispatchError("Codex cancellation target is unavailable")
                return terminal
            task = active[1]
            attempt = active[2]
            if not attempt.cancellation_attempted:
                attempt.cancellation_attempted = True
                attempt.operator_cancel_outcome = await self._runner.cancel(
                    self._sessions[command_id]
                )
        return await asyncio.shield(task)

    def terminal_evidence(self, command_id: UUID) -> RuntimeCodexTerminalEvidenceV1 | None:
        return self._terminal.get(command_id) or self._load_checkpoint(command_id)

    def find_terminal(
        self,
        *,
        correlation_id: UUID,
        causation_id: UUID | None,
        subject_id: str,
        prompt_sha256: str,
        workspace_ref: str,
        workspace: Path,
    ) -> RuntimeCodexTerminalEvidenceV1 | None:
        workspace_binding = _workspace_binding(workspace)
        command_id = causation_id or self._identity_index.get(
            (correlation_id, subject_id, prompt_sha256, workspace_ref, workspace_binding)
        )
        if command_id is None or command_id in self._active:
            return None
        evidence = self.terminal_evidence(command_id)
        if evidence is None:
            return None
        expected = _runtime_codex_command_identity(
            command_id=command_id,
            correlation_id=correlation_id,
            subject_id=subject_id,
            prompt_sha256=prompt_sha256,
            workspace_ref=workspace_ref,
            workspace_binding_sha256=workspace_binding,
        )
        if evidence.command_identity_sha256 != expected:
            raise FactoryDispatchError("Codex terminal checkpoint identity changed")
        return evidence

    async def _single_flight(
        self,
        invocation: RuntimeCodexInvocation,
        observer: RuntimeCodexObserver,
        *,
        replace_checkpoint: bool = False,
    ) -> RuntimeCodexTerminalEvidenceV1:
        async with self._lock:
            if replace_checkpoint and invocation.command_id not in self._active:
                self._terminal.pop(invocation.command_id, None)
            existing = self._terminal.get(invocation.command_id)
            if existing is None and not replace_checkpoint:
                existing = self._load_checkpoint(invocation.command_id)
            if existing is not None:
                if existing.command_identity_sha256 != invocation.command_identity_sha256:
                    raise FactoryDispatchError("Codex duplicate command identity conflicts")
                self._terminal[invocation.command_id] = existing
                return existing
            active = self._active.get(invocation.command_id)
            if active is not None:
                if active[0] != invocation.command_identity_sha256:
                    raise FactoryDispatchError("Codex active command identity conflicts")
                task = active[1]
            else:
                acquired = self._claim_launch(invocation)
                if not acquired:
                    task = None
                else:
                    self._sessions[invocation.command_id] = invocation.session_id
                    attempt = _RuntimeCodexAttempt()
                    task = asyncio.create_task(
                        self._run(
                            invocation,
                            observer,
                            attempt=attempt,
                            replace_checkpoint=replace_checkpoint,
                        )
                    )
                    self._active[invocation.command_id] = (
                        invocation.command_identity_sha256,
                        task,
                        attempt,
                    )
        if task is None:
            return await self._wait_for_claim_owner(invocation)
        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                async with self._lock:
                    current = self._active.get(invocation.command_id)
                    if current is not None and current[1] is task:
                        self._active.pop(invocation.command_id, None)

    def _claim_launch(self, invocation: RuntimeCodexInvocation) -> bool:
        claim_kind = self._claim_kind(invocation)
        claimed_at = datetime.now(timezone.utc)
        content = _runtime_codex_json(
            {
                "schema": "captain.runtime-codex-launch-claim.v1",
                "command_id": str(invocation.command_id),
                "correlation_id": str(invocation.correlation_id),
                "command_identity_sha256": invocation.command_identity_sha256,
                "workspace_ref": invocation.workspace_ref,
                "workspace_binding_sha256": invocation.workspace_binding_sha256,
                "claim_kind": claim_kind,
                "owner_id": self._owner_id,
                "session_id": invocation.session_id,
                "claimed_at": claimed_at.isoformat(),
                "deadline_at": (
                    claimed_at + timedelta(seconds=invocation.timeout_seconds + 5)
                ).isoformat(),
                "process_state_ref": f"process-state://{invocation.session_id}",
            }
        )
        relative = f"claims/{invocation.command_id}/{claim_kind}.json"
        try:
            return self._checkpoints.write_once(
                relative,
                content,
                conflict="runtime Codex launch claim conflicts",
            )
        except ConfinedFileError as exc:
            try:
                existing = json.loads(self._checkpoints.read(relative))
            except (ConfinedFileError, json.JSONDecodeError):
                raise FactoryDispatchError(str(exc)) from None
            if (
                not isinstance(existing, dict)
                or existing.get("command_identity_sha256")
                != invocation.command_identity_sha256
                or existing.get("workspace_ref") != invocation.workspace_ref
                or existing.get("workspace_binding_sha256")
                != invocation.workspace_binding_sha256
                or existing.get("claim_kind") != claim_kind
            ):
                raise FactoryDispatchError(str(exc)) from None
            return False

    async def _wait_for_claim_owner(
        self,
        invocation: RuntimeCodexInvocation,
    ) -> RuntimeCodexTerminalEvidenceV1:
        deadline = asyncio.get_running_loop().time() + invocation.timeout_seconds + 5
        inspection: str = "unavailable"
        while asyncio.get_running_loop().time() < deadline:
            evidence = self._load_checkpoint(invocation.command_id)
            if evidence is not None:
                prior_sha = invocation.prior_checkpoint_sha256
                current_sha = (
                    evidence.resumable_checkpoint.sha256
                    if evidence.resumable_checkpoint is not None
                    else None
                )
                if prior_sha is None or current_sha != prior_sha:
                    if evidence.command_identity_sha256 != invocation.command_identity_sha256:
                        raise FactoryDispatchError(
                            "Codex durable launch claim identity changed"
                        )
                    self._terminal[invocation.command_id] = evidence
                    return evidence
            inspector = getattr(self._runner, "inspect", None)
            if callable(inspector):
                try:
                    inspection = await inspector(invocation.session_id)
                except Exception:
                    inspection = "unresolved"
            await asyncio.sleep(0.01)
        self._record_orphaned_claim(invocation, inspection=inspection)
        raise FactoryDispatchError(
            "Codex durable launch owner did not terminalize; recovery is required"
        )

    @staticmethod
    def _claim_kind(invocation: RuntimeCodexInvocation) -> str:
        return (
            f"resume/{invocation.prior_checkpoint_sha256}"
            if invocation.resume and invocation.prior_checkpoint_sha256 is not None
            else "start"
        )

    def _record_orphaned_claim(
        self,
        invocation: RuntimeCodexInvocation,
        *,
        inspection: str,
    ) -> None:
        content = _runtime_codex_json(
            {
                "schema": "captain.runtime-codex-orphaned-claim.v1",
                "command_id": str(invocation.command_id),
                "command_identity_sha256": invocation.command_identity_sha256,
                "session_id": invocation.session_id,
                "inspection": inspection,
                "recovery_required": True,
            }
        )
        digest = hashlib.sha256(content).hexdigest()
        try:
            self._checkpoints.write_once(
                f"orphaned-claims/{invocation.command_id}/{digest}.json",
                content,
                conflict="runtime Codex orphan reconciliation conflicts",
            )
        except ConfinedFileError as exc:
            raise FactoryDispatchError(str(exc)) from None

    async def _run(
        self,
        invocation: RuntimeCodexInvocation,
        observer: RuntimeCodexObserver,
        *,
        attempt: _RuntimeCodexAttempt,
        replace_checkpoint: bool,
    ) -> RuntimeCodexTerminalEvidenceV1:
        summary = _EventSummary()

        async def observe(event: dict[str, object]) -> None:
            canonical = summary.observe(event)
            try:
                await observer(dict(canonical))
            except BaseException:
                raise _RuntimeObserverFailed from None

        try:
            result = await self._runner.run(invocation, observe)
        except Exception as exc:
            failure = exc
            result = None
        else:
            failure = None
        async with self._lock:
            if result is None:
                cleanup: RuntimeCodexProcessCleanupStatus = "not_required"
                if not attempt.cancellation_attempted:
                    attempt.cancellation_attempted = True
                    try:
                        outcome = await self._runner.cancel(invocation.session_id)
                    except Exception:
                        outcome = "requested_unverified"
                    if outcome == "verified_cancelled":
                        cleanup = "verified_cancelled"
                    elif outcome == "requested_unverified":
                        cleanup = "unresolved"
                result = RuntimeCodexProcessResult(
                    exit_code=70,
                    terminal_status="failed",
                    process_cleanup_status=cleanup,
                    elapsed_ms=0,
                    session_id=summary.session_id or invocation.session_id,
                    event_count=summary.count,
                    event_types=tuple(sorted(summary.types)),
                    last_event_sha256=summary.last_sha256,
                    failure_kind=(
                        "observer_failed"
                        if isinstance(failure, _RuntimeObserverFailed)
                        else "output_read_failed"
                    ),
                )
            if attempt.operator_cancel_outcome == "verified_cancelled":
                result = RuntimeCodexProcessResult(
                    exit_code=130,
                    terminal_status="cancelled",
                    process_cleanup_status="verified_cancelled",
                    elapsed_ms=result.elapsed_ms,
                    session_id=result.session_id,
                    event_count=result.event_count,
                    event_types=result.event_types,
                    last_event_sha256=result.last_event_sha256,
                )
            if (
                result.event_count != summary.count
                or result.event_types != tuple(sorted(summary.types))
                or result.last_event_sha256 != summary.last_sha256
            ):
                result = RuntimeCodexProcessResult(
                    exit_code=70,
                    terminal_status="failed",
                    process_cleanup_status=result.process_cleanup_status,
                    elapsed_ms=result.elapsed_ms,
                    session_id=result.session_id,
                    event_count=summary.count,
                    event_types=tuple(sorted(summary.types)),
                    last_event_sha256=summary.last_sha256,
                    failure_kind="stream_mismatch",
                )
            evidence = self._terminal_evidence(
                invocation,
                result,
                replace_checkpoint=replace_checkpoint,
            )
            self._sessions[invocation.command_id] = result.session_id
            self._terminal[invocation.command_id] = evidence
            return evidence

    def _terminal_evidence(
        self,
        invocation: RuntimeCodexInvocation,
        result: RuntimeCodexProcessResult,
        *,
        replace_checkpoint: bool,
    ) -> RuntimeCodexTerminalEvidenceV1:
        payload = {
            "schema": "captain.runtime-codex-checkpoint.v1",
            "command_id": str(invocation.command_id),
            "correlation_id": str(invocation.correlation_id),
            "subject_id": invocation.subject_id,
            "prompt_sha256": invocation.prompt_sha256,
            "command_identity_sha256": invocation.command_identity_sha256,
            "workspace_ref": invocation.workspace_ref,
            "workspace_binding_sha256": invocation.workspace_binding_sha256,
            "session_id": result.session_id,
            "status": result.terminal_status,
            "exit_code": result.exit_code,
            "elapsed_ms": result.elapsed_ms,
            "last_event_sha256": result.last_event_sha256,
            "event_count": result.event_count,
            "event_types": list(result.event_types),
            "process_cleanup_status": result.process_cleanup_status,
            "failure_kind": result.failure_kind,
            "usage": (
                result.usage.model_dump(mode="json", by_alias=True)
                if result.usage is not None
                else None
            ),
        }
        record = self._persist_checkpoint(payload)
        checkpoint = record if result.terminal_status in {"timed_out", "cancelled", "failed"} else None
        evidence = RuntimeCodexTerminalEvidenceV1(
            schema_name="captain.runtime-codex-terminal-evidence.v1",
            command_id=invocation.command_id,
            correlation_id=invocation.correlation_id,
            command_identity_sha256=invocation.command_identity_sha256,
            workspace_ref=invocation.workspace_ref,
            workspace_binding_sha256=invocation.workspace_binding_sha256,
            session_id=result.session_id,
            status=result.terminal_status,
            exit_code=result.exit_code,
            elapsed_ms=result.elapsed_ms,
            last_event_sha256=result.last_event_sha256,
            event_count=result.event_count,
            event_types=result.event_types,
            process_cleanup_status=result.process_cleanup_status,
            failure_kind=result.failure_kind,
            resumable_checkpoint=checkpoint,
            usage=result.usage,
        )
        self._write_checkpoint_index(invocation.command_id, record, replace=replace_checkpoint)
        return evidence

    def _persist_checkpoint(self, payload: Mapping[str, object]) -> ArtifactRef:
        content = _runtime_codex_json(payload)
        digest = hashlib.sha256(content).hexdigest()
        try:
            self._checkpoints.write_once(
                f"{digest[:2]}/{digest}.json",
                content,
                conflict="runtime Codex checkpoint replay conflicts",
            )
        except ConfinedFileError as exc:
            raise FactoryDispatchError(str(exc)) from None
        return ArtifactRef(
            uri=f"artifact://runtime-codex-checkpoint/{digest}",
            sha256=digest,
            media_type="application/json",
        )

    def _write_checkpoint_index(
        self,
        command_id: UUID,
        reference: ArtifactRef,
        *,
        replace: bool = False,
    ) -> None:
        relative = f"by-command/{command_id}.json"
        content = _runtime_codex_json(reference.model_dump(mode="json"))
        if replace:
            prior = self._load_checkpoint(command_id)
            if prior is None or prior.status not in {"timed_out", "cancelled", "failed"}:
                raise FactoryDispatchError("runtime Codex checkpoint replacement is unauthorized")
            try:
                prior_indexes = self._checkpoints.regular_files(
                    f"by-resume/{command_id}"
                )
            except ConfinedFileError:
                prior_indexes = ()
            ordinal = len(prior_indexes) + 1
            relative = f"by-resume/{command_id}/{ordinal:08d}.json"
        try:
            self._checkpoints.write_once(
                relative,
                content,
                conflict="runtime Codex checkpoint index conflicts",
            )
        except ConfinedFileError as exc:
            raise FactoryDispatchError(str(exc)) from None

    def _load_checkpoint(self, command_id: UUID) -> RuntimeCodexTerminalEvidenceV1 | None:
        try:
            try:
                resume_indexes = self._checkpoints.regular_files(
                    f"by-resume/{command_id}"
                )
            except ConfinedFileError:
                resume_indexes = ()
            reference_path = (
                resume_indexes[-1]
                if resume_indexes
                else Path("by-command") / f"{command_id}.json"
            )
            raw_reference = self._checkpoints.read(reference_path)
        except ConfinedFileError:
            return None
        try:
            reference = ArtifactRef.model_validate_json(raw_reference)
            expected_uri = f"artifact://runtime-codex-checkpoint/{reference.sha256}"
            if reference.uri != expected_uri:
                raise FactoryDispatchError("runtime Codex checkpoint namespace changed")
            content = self._checkpoints.read(
                f"{reference.sha256[:2]}/{reference.sha256}.json"
            )
            if hashlib.sha256(content).hexdigest() != reference.sha256:
                raise FactoryDispatchError("runtime Codex checkpoint digest changed")
            payload = json.loads(content)
            if not isinstance(payload, dict) or UUID(str(payload["command_id"])) != command_id:
                raise FactoryDispatchError("runtime Codex checkpoint command binding changed")
            recalculated_identity = _runtime_codex_command_identity(
                command_id=command_id,
                correlation_id=UUID(str(payload["correlation_id"])),
                subject_id=str(payload["subject_id"]),
                prompt_sha256=str(payload["prompt_sha256"]),
                workspace_ref=str(payload["workspace_ref"]),
                workspace_binding_sha256=str(payload["workspace_binding_sha256"]),
            )
            if recalculated_identity != str(payload["command_identity_sha256"]):
                raise FactoryDispatchError(
                    "runtime Codex checkpoint identity binding changed"
                )
            status = str(payload["status"])
            return RuntimeCodexTerminalEvidenceV1(
                schema_name="captain.runtime-codex-terminal-evidence.v1",
                command_id=command_id,
                correlation_id=UUID(str(payload["correlation_id"])),
                command_identity_sha256=str(payload["command_identity_sha256"]),
                workspace_ref=str(payload["workspace_ref"]),
                workspace_binding_sha256=str(payload["workspace_binding_sha256"]),
                session_id=str(payload["session_id"]),
                status=status,
                exit_code=int(payload["exit_code"]),
                elapsed_ms=int(payload["elapsed_ms"]),
                last_event_sha256=payload.get("last_event_sha256"),
                event_count=int(payload["event_count"]),
                event_types=tuple(payload["event_types"]),
                process_cleanup_status=str(payload["process_cleanup_status"]),
                failure_kind=payload.get("failure_kind"),
                resumable_checkpoint=(
                    reference if status in {"timed_out", "cancelled", "failed"} else None
                ),
                usage=(
                    RuntimeCodexUsageV1.model_validate(payload["usage"])
                    if payload.get("usage") is not None
                    else None
                ),
            )
        except FactoryDispatchError:
            raise
        except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
            raise FactoryDispatchError("runtime Codex checkpoint binding is invalid") from None


class PowerShellRuntimeCodexRunner:
    """Translate runtime invocations onto the established PowerShell runner seam."""

    _ALLOWED_ENV = frozenset(
        {
            "PATH", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
            "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "USERNAME",
            "USERDOMAIN", "OS", "PROCESSOR_ARCHITECTURE", "LANG", "LC_ALL", "TERM",
            "NO_COLOR", "OPENAI_API_KEY",
        }
    )

    def __init__(
        self,
        *,
        repository_root: Path,
        executable: str,
        environ: Mapping[str, str],
        evidence_root: Path,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._repository_root = repository_root.resolve()
        self._executable = executable
        self._environ = dict(environ)
        self._state_store = ConfinedFileStore(evidence_root / "state")
        self._monotonic = monotonic or time.monotonic
        self._active: dict[str, PowerShellCodexRunner] = {}
        self._cancelled: set[str] = set()
        self._lock = asyncio.Lock()

    async def run(
        self,
        invocation: RuntimeCodexInvocation,
        observer: RuntimeCodexObserver,
    ) -> RuntimeCodexProcessResult:
        summary = _EventSummary()

        async def observe(event: dict[str, object]) -> None:
            canonical = summary.observe(event)
            await observer(canonical)

        runner = self._make_runner(invocation, observe)
        async with self._lock:
            if invocation.session_id in self._active:
                raise FactoryDispatchError("Codex runtime session already has an owner")
            self._active[invocation.session_id] = runner
            self._cancelled.discard(invocation.session_id)
        started = self._monotonic()
        started_at = datetime.now(timezone.utc)
        try:
            command_parts = ["codex", "exec"]
            if invocation.resume:
                command_parts.append("resume")
            command_parts.append("--json")
            if invocation.model is not None:
                command_parts.extend(("--model", invocation.model))
            if invocation.resume:
                command_parts.append(invocation.session_id)
            command_parts.append(invocation.prompt)
            proxy_bound = invocation.hard_ceiling_enforced
            if proxy_bound and (
                invocation.provider_proxy_url is None
                or invocation.provider_policy_sha256 is None
                or invocation.provider_price_card_sha256 is None
                or invocation.provider_context_sha256 is None
                or not self._environ.get("CAPTAIN_PROVIDER_PROXY_CLIENT_TOKEN", "").strip()
            ):
                raise FactoryDispatchError("Codex provider proxy binding is unavailable")
            allowed_environment = self._ALLOWED_ENV - ({"OPENAI_API_KEY"} if proxy_bound else set())
            child_values = {
                name: value for name, value in self._environ.items() if name in allowed_environment
            }
            if proxy_bound:
                child_values["CAPTAIN_PROVIDER_PROXY_CLIENT_TOKEN"] = self._environ[
                    "CAPTAIN_PROVIDER_PROXY_CLIENT_TOKEN"
                ]
            authorized = AuthorizedCodexRun(
                workspace=invocation.workspace,
                command=tuple(command_parts),
                environment=FrozenEnvironment(child_values),
            )
            result = await runner.run(authorized)
            failure_kind: RuntimeCodexFailureKind | None = None
        except CodexOutputEvidenceError as exc:
            result = None
            failure_kind = exc.failure_kind
            cleanup = exc.process_cleanup_status or "unresolved"
        finally:
            async with self._lock:
                self._active.pop(invocation.session_id, None)
        ended_at = datetime.now(timezone.utc)
        elapsed_ms = max(0, int((self._monotonic() - started) * 1000))
        session_id = summary.session_id or invocation.session_id
        if result is None:
            return RuntimeCodexProcessResult(
                exit_code=70,
                terminal_status="failed",
                process_cleanup_status=cleanup,
                elapsed_ms=elapsed_ms,
                session_id=session_id,
                event_count=summary.count,
                event_types=tuple(sorted(summary.types)),
                last_event_sha256=summary.last_sha256,
                failure_kind=failure_kind,
            )
        usage = None
        if (
            summary.usage_events > 0
            and invocation.request_id is not None
            and invocation.model is not None
            and invocation.pricing_snapshot_id is not None
            and invocation.pricing_snapshot_sha256 is not None
        ):
            usage = RuntimeCodexUsageV1(
                schema_name="captain.runtime-codex-usage.v1",
                request_id=invocation.request_id,
                command_id=invocation.command_id,
                command_identity_sha256=invocation.command_identity_sha256,
                session_id=session_id,
                model=invocation.model,
                input_units=summary.input_units,
                cached_input_units=summary.cached_input_units,
                output_units=summary.output_units,
                pricing_snapshot_id=invocation.pricing_snapshot_id,
                pricing_snapshot_sha256=invocation.pricing_snapshot_sha256,
                started_at=started_at,
                ended_at=ended_at,
            )
        return RuntimeCodexProcessResult(
            exit_code=result.exit_code,
            terminal_status=result.terminal_status,
            process_cleanup_status=result.process_cleanup_status,
            elapsed_ms=elapsed_ms,
            session_id=session_id,
            event_count=summary.count,
            event_types=tuple(sorted(summary.types)),
            last_event_sha256=summary.last_sha256,
            usage=usage,
        )

    async def cancel(self, session_id: str) -> RuntimeCodexCancelOutcome:
        async with self._lock:
            runner = self._active.get(session_id)
            if runner is None or session_id in self._cancelled:
                return "no_active_process"
            self._cancelled.add(session_id)
        return await runner.cancel()

    async def inspect(self, session_id: str) -> str:
        pwsh = shutil.which(self._environ.get("CAPTAIN_PWSH_EXECUTABLE", "pwsh"))
        if pwsh is None:
            return "unresolved"
        try:
            state_files = self._state_store.regular_files(session_id)
        except ConfinedFileError:
            return "missing"
        outcomes: set[str] = set()
        for state_file in state_files:
            try:
                decoded = json.loads(self._state_store.read(state_file))
            except (ConfinedFileError, TypeError, ValueError, json.JSONDecodeError):
                return "unresolved"
            if not isinstance(decoded, dict):
                return "unresolved"
            outcome = await PowerShellCodexRunner.inspect_process_identity(
                pwsh_path=Path(pwsh),
                script_path=self._repository_root / "scripts" / "codex-session.ps1",
                process_state=decoded,
                session_id=session_id,
            )
            outcomes.add(outcome)
        if "active" in outcomes:
            return "active"
        if "identity_mismatch" in outcomes:
            return "identity_mismatch"
        if "unresolved" in outcomes:
            return "unresolved"
        return "lost" if outcomes else "missing"

    def _make_runner(
        self,
        invocation: RuntimeCodexInvocation,
        observer: RuntimeCodexObserver,
    ) -> PowerShellCodexRunner:
        pwsh = shutil.which(self._environ.get("CAPTAIN_PWSH_EXECUTABLE", "pwsh"))
        codex = shutil.which(self._executable)
        if pwsh is None or codex is None:
            raise FactoryDispatchError("Codex runtime executable is unavailable")
        codex_home_value = self._environ.get("CODEX_HOME")
        codex_home = Path(codex_home_value) if codex_home_value else Path.home() / ".codex"
        attempt = uuid4().hex

        async def persist_process_state(state: dict[str, object]) -> None:
            self._state_store.write_once(
                f"{invocation.session_id}/{attempt}.json",
                _runtime_codex_json(state),
                conflict="runtime Codex process state is immutable",
            )

        return PowerShellCodexRunner(
            pwsh_path=Path(pwsh),
            script_path=self._repository_root / "scripts" / "codex-session.ps1",
            codex_path=Path(codex),
            session_id=invocation.session_id,
            state_path=None,
            journal_path=None,
            artifact_references=(),
            codex_home=codex_home,
            provider_proxy_url=(
                invocation.provider_proxy_url if invocation.hard_ceiling_enforced else None
            ),
            deadline_at=datetime.now(timezone.utc) + timedelta(seconds=invocation.timeout_seconds),
            timeout_seconds=float(invocation.timeout_seconds),
            output_policy=CodexOutputJournalPolicy(
                retain_raw_records=False,
                observer=observer,
                process_state_observer=persist_process_state,
            ),
        )


def _runtime_codex_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _runtime_codex_event(value: Mapping[str, object]) -> dict[str, object]:
    try:
        decoded = json.loads(_runtime_codex_json(dict(value)))
    except (TypeError, ValueError, json.JSONDecodeError):
        raise FactoryDispatchError("Codex JSONL event is invalid") from None
    if not isinstance(decoded, dict):
        raise FactoryDispatchError("Codex JSONL event is invalid")
    return decoded


def _workspace_binding(workspace: Path) -> str:
    return hashlib.sha256(str(workspace.resolve(strict=True)).casefold().encode("utf-8")).hexdigest()


def _runtime_codex_command_identity(
    *,
    command_id: UUID,
    correlation_id: UUID,
    subject_id: str,
    prompt_sha256: str,
    workspace_ref: str,
    workspace_binding_sha256: str,
) -> str:
    return hashlib.sha256(
        _runtime_codex_json(
            {
                "command_id": str(command_id),
                "correlation_id": str(correlation_id),
                "subject_id": subject_id,
                "prompt_sha256": prompt_sha256,
                "workspace_ref": workspace_ref,
                "workspace_binding_sha256": workspace_binding_sha256,
            }
        )
    ).hexdigest()


__all__ = [
    "PowerShellRuntimeCodexRunner",
    "RuntimeCodexExecution",
    "RuntimeCodexInvocation",
    "RuntimeCodexProcessResult",
    "RuntimeCodexTerminalEvidenceV1",
    "RuntimeCodexUsageV1",
]
