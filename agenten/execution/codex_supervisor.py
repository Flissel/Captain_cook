"""Gateway-fenced, injected Codex process supervision."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
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



class PowerShellCodexRunner:
    """Execute an authorized Codex request through the session-bound PS7 launcher."""

    def __init__(
        self,
        *,
        pwsh_path: Path,
        script_path: Path,
        codex_path: Path,
        session_id: str,
        state_path: Path,
        artifact_references: tuple[str, ...],
        codex_home: Path,
        timeout_seconds: float = 600,
    ) -> None:
        self._pwsh_path = pwsh_path.resolve(strict=True)
        self._script_path = script_path.resolve(strict=True)
        self._codex_path = codex_path.resolve(strict=True)
        self._session_id = session_id
        self._state_path = state_path.resolve()
        self._artifact_references = artifact_references
        self._codex_home = codex_home.resolve(strict=True)
        self._timeout_seconds = timeout_seconds

    async def run(self, authorized: AuthorizedCodexRun) -> CodexRunResult:
        if len(authorized.command) != 4:
            raise ValueError("PowerShell Codex runner requires one prompt argument")
        child_environment = authorized.child_environment()
        child_environment["CODEX_HOME"] = str(self._codex_home)
        process = await asyncio.create_subprocess_exec(
            str(self._pwsh_path),
            "-NoProfile",
            "-File",
            str(self._script_path),
            "-Workspace",
            str(authorized.workspace),
            "-Prompt",
            authorized.command[3],
            "-CodexPath",
            str(self._codex_path),
            "-SessionId",
            self._session_id,
            "-StatePath",
            str(self._state_path),
            cwd=authorized.workspace,
            env=child_environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(
                process.communicate(),
                timeout=self._timeout_seconds,
            )
        except TimeoutError:
            cancelled = await self._cancel_timed_out_process()
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except TimeoutError:
                process.kill()
                await process.wait()
            if not cancelled:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
                raise RuntimeError(
                    "Codex process exceeded timeout and tree cancellation failed"
                ) from None
            return CodexRunResult(
                exit_code=124,
                artifact_references=(),
                jsonl_lines=(),
            )
        return CodexRunResult(
            exit_code=process.returncode,
            artifact_references=self._artifact_references,
            jsonl_lines=tuple(
                line for line in stdout.decode("utf-8", errors="replace").splitlines()
                if line.strip()
            ),
        )



    async def _cancel_timed_out_process(self) -> bool:
        if not self._state_path.is_file():
            return False
        cancellation = await asyncio.create_subprocess_exec(
            str(self._pwsh_path),
            "-NoProfile",
            "-File",
            str(self._script_path),
            "-CancelStatePath",
            str(self._state_path),
            "-SessionId",
            self._session_id,
            "-CancellationReason",
            "timeout",
            env=self._cancellation_environment(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(
                cancellation.communicate(),
                timeout=15,
            )
        except TimeoutError:
            cancellation.kill()
            await cancellation.wait()
            return False
        if cancellation.returncode != 0:
            return False
        try:
            result = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return False
        return result == {
            "session_id": self._session_id,
            "outcome": "cancelled",
            "cancellation_reason": "timeout",
        }



    @staticmethod
    def _cancellation_environment() -> dict[str, str]:
        allowed = {
            "systemroot",
            "windir",
            "path",
            "pathext",
            "temp",
            "tmp",
            "comspec",
        }
        return {
            name: value
            for name, value in os.environ.items()
            if name.lower() in allowed
        }


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
            events = tuple(
                parse_codex_jsonl(line).model_copy(
                    update={"source_sequence": source_sequence}
                )
                for source_sequence, line in enumerate(run_result.jsonl_lines)
            )
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
            events = tuple(
                parse_codex_jsonl(line).model_copy(
                    update={"source_sequence": source_sequence}
                )
                for source_sequence, line in enumerate(run_result.jsonl_lines)
            )
            for event in events:
                await self._repository.append(event)
        except Exception:
            terminal_persisted = await self._finish_repository(
                request.session_id,
                CodexOutcome(classification="infrastructure_failure"),
            )
            if not terminal_persisted:
                return self._result(
                    request,
                    PackageExecutionStatus.EVIDENCE_UNRESOLVED,
                    error="codex terminal evidence requires recovery",
                )
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
