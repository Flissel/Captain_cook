"""Gateway-fenced, injected Codex process supervision."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agenten.execution.codex_events import (
    CodexParseWarning,
    CodexProcessEvent,
    parse_codex_jsonl,
)
from agenten.execution.process import PackageExecutionResult, PackageExecutionStatus

if TYPE_CHECKING:
    from agenten.delivery.codex_runs import CodexOutcome, CodexRunRepository
    from agenten.execution.codex_policy import AuthorizedCodexRun

Identifier = str


class CodexRunRequest(BaseModel):
    """Fully validated immutable input for one supervised Codex process."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    run_id: Identifier | None = Field(
        default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
    )
    trace_id: Identifier | None = Field(
        default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
    )
    batch_id: Identifier | None = Field(
        default=None, pattern=r"^[a-z0-9][a-z0-9-]{0,31}$"
    )
    worker_id: Identifier | None = Field(
        default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
    )
    session_id: Identifier = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,121}$")
    claim_token: str = Field(min_length=1)
    iteration: int = Field(ge=1)
    command: tuple[str, ...] = Field(min_length=1)
    workspace: Path
    project_id: Identifier | None = Field(
        default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
    )
    claim_id: Identifier | None = Field(
        default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
    )
    fencing_token: int | None = Field(default=None, ge=1)
    project_root: Path | None = None

    @field_validator("command")
    @classmethod
    def command_is_an_argument_vector(cls, command: tuple[str, ...]) -> tuple[str, ...]:
        if any(not argument for argument in command):
            raise ValueError("command arguments must not be empty")
        return command


class CodexRunResult(BaseModel):
    """Immutable, untrusted process output retained only until it is sanitized."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    exit_code: int
    artifact_references: tuple[str, ...]
    jsonl_lines: tuple[str, ...]


class CodexRunner(Protocol):
    async def run(
        self,
        authorized: AuthorizedCodexRun,
    ) -> CodexRunResult: ...


class CodexExecutionAuthorizer(Protocol):
    def authorize(self, request: CodexRunRequest) -> AuthorizedCodexRun: ...


class GatewayCodexEvidenceWriter(Protocol):
    async def record_codex_session(
        self,
        batch_id: str,
        claim_token: str,
        *,
        iteration: int,
        session_id: str,
    ) -> None: ...

    async def record_codex_process(
        self,
        batch_id: str,
        claim_token: str,
        *,
        iteration: int,
        process_id: str,
        state: Literal["started", "heartbeat", "exited", "cancelled"],
        command_digest: str,
    ) -> None: ...

    async def record_codex_event(
        self,
        batch_id: str,
        claim_token: str,
        *,
        iteration: int,
        session_id: str,
        event: CodexProcessEvent | CodexParseWarning,
    ) -> None: ...


class CodexSupervisor:
    def __init__(
        self,
        *,
        runner: CodexRunner,
        gateway: GatewayCodexEvidenceWriter,
        policy: CodexExecutionAuthorizer,
        repository: CodexRunRepository | None = None,
    ) -> None:
        self._runner = runner
        self._gateway = gateway
        self._policy = policy
        self._repository = repository

    async def run(self, request: CodexRunRequest) -> PackageExecutionResult:
        authorized = self._policy.authorize(request)

        command_digest = hashlib.sha256(
            "\0".join(authorized.command).encode("utf-8")
        ).hexdigest()
        process_id = f"codex-{request.session_id}"
        if self._repository is not None:
            try:
                await self._repository.start(request)
            except Exception:
                return self._result(
                    request,
                    PackageExecutionStatus.FAILED,
                    error="codex execution evidence could not be recorded",
                )
        else:
            return await self._run_legacy(request, authorized, process_id, command_digest)

        return await self._run_repository(request, authorized)

    async def _run_legacy(
        self,
        request: CodexRunRequest,
        authorized: AuthorizedCodexRun,
        process_id: str,
        command_digest: str,
    ) -> PackageExecutionResult:
        try:
            await self._gateway.record_codex_session(
                request.batch_id,
                request.claim_token,
                iteration=request.iteration,
                session_id=request.session_id,
            )
            await self._record_process(request, process_id, "started", command_digest)
        except Exception:
            return self._result(
                request,
                PackageExecutionStatus.FAILED,
                error="codex execution evidence could not be recorded",
            )
        try:
            run_result = await self._runner.run(authorized)
        except Exception:
            try:
                await self._record_process(
                    request, process_id, "cancelled", command_digest
                )
            except Exception:
                pass
            return self._result(
                request,
                PackageExecutionStatus.FAILED,
                error="codex process could not be started",
            )

        try:
            events = tuple(parse_codex_jsonl(line) for line in run_result.jsonl_lines)
            for event in events:
                await self._gateway.record_codex_event(
                    request.batch_id,
                    request.claim_token,
                    iteration=request.iteration,
                    session_id=request.session_id,
                    event=event,
                )
            await self._record_process(request, process_id, "exited", command_digest)
        except Exception:
            return self._result(
                request,
                PackageExecutionStatus.FAILED,
                error="codex execution evidence could not be recorded",
            )
        if run_result.exit_code:
            return self._result(
                request,
                PackageExecutionStatus.FAILED,
                error=f"codex process exited with code {run_result.exit_code}",
            )
        if any(
            isinstance(event, CodexParseWarning)
            or event.lifecycle == "failed"
            for event in events
        ):
            return self._result(
                request,
                PackageExecutionStatus.FAILED,
                error="codex JSONL evidence is incomplete",
            )
        return self._result(
            request,
            PackageExecutionStatus.SUCCEEDED,
            artifacts=run_result.artifact_references,
        )

    async def _run_repository(
        self,
        request: CodexRunRequest,
        authorized: AuthorizedCodexRun,
    ) -> PackageExecutionResult:
        from agenten.delivery.codex_runs import CodexOutcome

        assert self._repository is not None
        try:
            run_result = await self._runner.run(authorized)
        except Exception:
            if not await self._finish_repository(
                request.session_id,
                CodexOutcome(classification="infrastructure_failure"),
            ):
                return self._result(
                    request,
                    PackageExecutionStatus.FAILED,
                    error="codex execution evidence could not be recorded",
                )
            return self._result(
                request,
                PackageExecutionStatus.FAILED,
                error="codex process could not be started",
            )

        try:
            events = tuple(parse_codex_jsonl(line) for line in run_result.jsonl_lines)
            for event in events:
                await self._repository.append(event)
        except Exception:
            return self._result(
                request,
                PackageExecutionStatus.FAILED,
                error="codex execution evidence could not be recorded",
            )

        if run_result.exit_code:
            outcome = CodexOutcome(
                classification="infrastructure_failure",
                exit_code=run_result.exit_code,
            )
            error = f"codex process exited with code {run_result.exit_code}"
        elif any(
            isinstance(event, CodexParseWarning) or event.lifecycle == "failed"
            for event in events
        ):
            outcome = CodexOutcome(
                classification="behavioral_failure",
                behavioral_repair_increment=1,
            )
            error = "codex JSONL evidence is incomplete"
        else:
            outcome = CodexOutcome(classification="succeeded")
            error = None
        if not await self._finish_repository(request.session_id, outcome):
            return self._result(
                request,
                PackageExecutionStatus.FAILED,
                error="codex execution evidence could not be recorded",
            )
        if error is not None:
            return self._result(
                request,
                PackageExecutionStatus.FAILED,
                error=error,
            )
        return self._result(
            request,
            PackageExecutionStatus.SUCCEEDED,
            artifacts=run_result.artifact_references,
        )

    async def _finish_repository(
        self, session_id: str, outcome: CodexOutcome
    ) -> bool:
        assert self._repository is not None
        try:
            await self._repository.finish(session_id, outcome)
        except Exception:
            return False
        return True

    async def _record_process(
        self,
        request: CodexRunRequest,
        process_id: str,
        state: Literal["started", "heartbeat", "exited", "cancelled"],
        command_digest: str,
    ) -> None:
        await self._gateway.record_codex_process(
            request.batch_id,
            request.claim_token,
            iteration=request.iteration,
            process_id=process_id,
            state=state,
            command_digest=command_digest,
        )

    @staticmethod
    def _result(
        request: CodexRunRequest,
        status: PackageExecutionStatus,
        *,
        artifacts: tuple[str, ...] = (),
        error: str | None = None,
    ) -> PackageExecutionResult:
        return PackageExecutionResult(
            run_id=request.run_id,
            trace_id=request.trace_id,
            codex_session_id=request.session_id,
            batch_id=request.batch_id,
            worker_id=request.worker_id,
            status=status,
            artifact_refs=artifacts,
            artifact_versions=tuple(1 for _ in artifacts),
            error=error,
        )
