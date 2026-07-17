"""Gateway-fenced, injected Codex process supervision."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agenten.execution.process import PackageExecutionResult, PackageExecutionStatus


Identifier = str


class CodexRunRequest(BaseModel):
    """Fully validated immutable input for one supervised Codex process."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    run_id: Identifier = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    trace_id: Identifier = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    batch_id: Identifier = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,31}$")
    worker_id: Identifier = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    session_id: Identifier = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,121}$")
    claim_token: str = Field(min_length=1)
    iteration: int = Field(ge=1)
    command: tuple[str, ...] = Field(min_length=1)
    workspace: Path

    @field_validator("command")
    @classmethod
    def command_is_an_argument_vector(cls, command: tuple[str, ...]) -> tuple[str, ...]:
        if any(not argument for argument in command):
            raise ValueError("command arguments must not be empty")
        return command


class CodexRunner(Protocol):
    async def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
    ) -> tuple[int, tuple[str, ...]]: ...


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


class CodexSupervisor:
    _ALLOWED_ENVIRONMENT_NAMES = frozenset(
        {"PATH", "HOME", "USERPROFILE", "LANG", "LC_ALL", "TERM", "NO_COLOR"}
    )

    def __init__(
        self,
        *,
        runner: CodexRunner,
        gateway: GatewayCodexEvidenceWriter,
        workspace_root: Path,
        environment: Mapping[str, str],
    ) -> None:
        self._runner = runner
        self._gateway = gateway
        self._workspace_root = workspace_root.resolve()
        self._environment = {
            name: value
            for name, value in environment.items()
            if name in self._ALLOWED_ENVIRONMENT_NAMES
        }

    async def run(self, request: CodexRunRequest) -> PackageExecutionResult:
        workspace = request.workspace.resolve()
        if workspace != self._workspace_root and self._workspace_root not in workspace.parents:
            raise ValueError("workspace is outside the approved root")

        command_digest = hashlib.sha256(
            "\0".join(request.command).encode("utf-8")
        ).hexdigest()
        process_id = f"codex-{request.session_id}"
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
            exit_code, artifacts = await self._runner.run(
                request.command,
                cwd=workspace,
                env=self._environment,
            )
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
            await self._record_process(request, process_id, "exited", command_digest)
        except Exception:
            return self._result(
                request,
                PackageExecutionStatus.FAILED,
                error="codex execution evidence could not be recorded",
            )
        if exit_code:
            return self._result(
                request,
                PackageExecutionStatus.FAILED,
                error=f"codex process exited with code {exit_code}",
            )
        return self._result(
            request,
            PackageExecutionStatus.SUCCEEDED,
            artifacts=artifacts,
        )

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
