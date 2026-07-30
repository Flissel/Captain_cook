"""Gateway-fenced, injected Codex process supervision."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Literal, Protocol

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
CodexRunTerminalStatus = Literal["succeeded", "failed", "timed_out", "cancelled"]
CodexProcessCleanupStatus = Literal[
    "not_required",
    "verified_cancelled",
    "unresolved",
]


class CodexJournalPersistenceError(RuntimeError):
    """The private Codex JSONL journal could not be durably persisted."""


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
    terminal_status: CodexRunTerminalStatus
    process_cleanup_status: CodexProcessCleanupStatus
    journal_path: Path
    journal_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    artifact_references: tuple[str, ...]
    jsonl_lines: tuple[str, ...]


class CodexRunner(Protocol):
    async def run(
        self,
        authorized: AuthorizedCodexRun,
    ) -> CodexRunResult: ...


class CodexRunCancellationMonitor(Protocol):
    """Wait until Captain withdraws the capability for this exact run."""

    async def wait(self) -> None: ...


class CodexRunCanceller(Protocol):
    """Cancel the verified process tree and persist terminal evidence."""

    async def cancel(self) -> None: ...



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
        journal_path: Path,
        artifact_references: tuple[str, ...],
        codex_home: Path,
        deadline_at: datetime,
        timeout_seconds: float = 600,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        if (
            not isinstance(deadline_at, datetime)
            or deadline_at.tzinfo is None
            or deadline_at.utcoffset() != timezone.utc.utcoffset(deadline_at)
        ):
            raise ValueError("Codex runner deadline must be a UTC timestamp")
        self._pwsh_path = pwsh_path.resolve(strict=True)
        self._script_path = script_path.resolve(strict=True)
        self._codex_path = codex_path.resolve(strict=True)
        self._session_id = session_id
        self._state_path = state_path.resolve()
        self._journal_path = journal_path.resolve()
        self._artifact_references = artifact_references
        self._codex_home = codex_home.resolve(strict=True)
        self._deadline_at = deadline_at
        self._timeout_seconds = timeout_seconds
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic or time.monotonic

    async def run(self, authorized: AuthorizedCodexRun) -> CodexRunResult:
        if len(authorized.command) != 4:
            raise ValueError("PowerShell Codex runner requires one prompt argument")
        self._journal_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        journal_descriptor = os.open(
            self._journal_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
        os.close(journal_descriptor)
        child_environment = authorized.child_environment()
        child_environment["CODEX_HOME"] = str(self._codex_home)
        wrapper_launch_at = self._clock()
        deadline_remaining_seconds = (
            self._deadline_at - wrapper_launch_at
        ).total_seconds()
        if deadline_remaining_seconds <= 0:
            return self._prelaunch_timeout_result()
        wrapper_timeout_seconds = min(
            self._timeout_seconds,
            deadline_remaining_seconds,
        )
        wrapper_launch_started = self._monotonic()
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
            "-DeadlineAt",
            self._deadline_at.isoformat(),
            cwd=authorized.workspace,
            env=child_environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert process.stdout is not None
        assert process.stderr is not None
        timed_out = False
        process_cleanup_status: CodexProcessCleanupStatus = "not_required"
        wrapper_timeout_remaining = wrapper_timeout_seconds - max(
            0.0,
            self._monotonic() - wrapper_launch_started,
        )
        with self._journal_path.open("ab") as journal:
            stdout_reader = asyncio.create_task(
                self._journal_stdout(process.stdout, journal)
            )
            stderr_reader = asyncio.create_task(self._drain_stderr(process.stderr))
            try:
                if wrapper_timeout_remaining <= 0:
                    raise TimeoutError
                await asyncio.wait_for(process.wait(), timeout=wrapper_timeout_remaining)
            except TimeoutError:
                timed_out = True
                cancelled = await self._attempt_verified_timeout_cancellation()
                wrapper_stopped = await self._settle_timed_out_wrapper(process)
                process_cleanup_status = (
                    "verified_cancelled"
                    if cancelled and wrapper_stopped
                    else "unresolved"
                )
            finally:
                await self._finish_readers(
                    stdout_reader,
                    stderr_reader,
                    allow_bounded_cancellation=timed_out,
                )

        journal_bytes = self._journal_path.read_bytes()
        exit_code = 124 if timed_out else process.returncode
        assert exit_code is not None
        terminal_status: CodexRunTerminalStatus
        if timed_out or exit_code == 124:
            terminal_status = "timed_out"
        elif exit_code == 0:
            terminal_status = "succeeded"
        else:
            terminal_status = "failed"
        return CodexRunResult(
            exit_code=exit_code,
            terminal_status=terminal_status,
            process_cleanup_status=process_cleanup_status,
            journal_path=self._journal_path,
            journal_sha256=hashlib.sha256(journal_bytes).hexdigest(),
            artifact_references=(
                () if terminal_status == "timed_out" else self._artifact_references
            ),
            jsonl_lines=tuple(
                line.decode("utf-8", errors="replace")
                for line in journal_bytes.splitlines()
                if line.strip()
            ),
        )

    def _prelaunch_timeout_result(self) -> CodexRunResult:
        journal_bytes = self._journal_path.read_bytes()
        return CodexRunResult(
            exit_code=124,
            terminal_status="timed_out",
            process_cleanup_status="not_required",
            journal_path=self._journal_path,
            journal_sha256=hashlib.sha256(journal_bytes).hexdigest(),
            artifact_references=(),
            jsonl_lines=(),
        )

    @staticmethod
    async def _journal_stdout(
        stdout: asyncio.StreamReader,
        journal: BinaryIO,
    ) -> None:
        while raw_line := await stdout.readline():
            line = raw_line.rstrip(b"\r\n")
            if not line.strip():
                continue
            try:
                journal.write(line + b"\n")
                journal.flush()
                os.fsync(journal.fileno())
            except OSError:
                raise CodexJournalPersistenceError(
                    "Codex JSONL journal persistence failed"
                ) from None

    @staticmethod
    async def _drain_stderr(stderr: asyncio.StreamReader) -> None:
        while await stderr.read(64 * 1024):
            pass

    async def _attempt_verified_timeout_cancellation(self) -> bool:
        try:
            return await asyncio.wait_for(
                self._cancel_timed_out_process(),
                timeout=20,
            )
        except Exception:
            return False

    @staticmethod
    async def _settle_timed_out_wrapper(
        process: asyncio.subprocess.Process,
    ) -> bool:
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
            return True
        except TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                return True
            except Exception:
                return False
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
                return True
            except Exception:
                return False
        except Exception:
            return False

    @staticmethod
    async def _finish_readers(
        *readers: asyncio.Task[None],
        allow_bounded_cancellation: bool,
    ) -> None:
        _, pending = await asyncio.wait(readers, timeout=5)
        cancelled_by_cleanup = frozenset(pending)
        for reader in pending:
            reader.cancel()
        results = await asyncio.gather(*readers, return_exceptions=True)
        for reader, result in zip(readers, results, strict=True):
            if not isinstance(result, BaseException):
                continue
            expected_cancellation = (
                allow_bounded_cancellation
                and reader in cancelled_by_cleanup
                and isinstance(result, asyncio.CancelledError)
            )
            if not expected_cancellation:
                raise result
        if pending and not allow_bounded_cancellation:
            raise CodexJournalPersistenceError(
                "Codex process output evidence did not settle"
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
        cancellation_monitor: CodexRunCancellationMonitor | None = None,
        canceller: CodexRunCanceller | None = None,
    ) -> None:
        if (cancellation_monitor is None) != (canceller is None):
            raise ValueError("Codex cancellation monitor and canceller must be paired")
        self._runner = runner
        self._gateway = gateway
        self._policy = policy
        self._repository = repository
        self._cancellation_monitor = cancellation_monitor
        self._canceller = canceller

    async def run(self, request: CodexRunRequest) -> PackageExecutionResult:
        authorized = self._policy.authorize(request)

        command_digest = hashlib.sha256(
            "\0".join(authorized.command).encode("utf-8")
        ).hexdigest()
        process_id = f"codex-{request.session_id}"
        if self._repository is not None:
            try:
                await self._repository.start(request)
            except Exception as exc:
                from agenten.delivery.codex_runs import ActiveCodexSessionRecoveryRequired

                if isinstance(exc, ActiveCodexSessionRecoveryRequired):
                    return self._result(
                        request,
                        PackageExecutionStatus.EVIDENCE_UNRESOLVED,
                        error="active Codex session requires recovery before retry",
                    )
                return self._result(
                    request,
                    PackageExecutionStatus.FAILED,
                    error="codex execution evidence could not be recorded",
                )
        else:
            return await self._run_legacy(request, authorized, process_id, command_digest)

        if self._cancellation_monitor is None:
            return await self._run_repository(request, authorized)
        return await self._run_repository_until_revoked(request, authorized)

    async def _run_repository_until_revoked(
        self,
        request: CodexRunRequest,
        authorized: AuthorizedCodexRun,
    ) -> PackageExecutionResult:
        """Race one active run against Captain's durable lease revocation.

        The canceller owns the OS process tree and its terminal Gateway record.
        This supervisor only converts the resulting cancellation into a
        fail-closed package result; it never overwrites the cancellation with a
        later local process exit.
        """

        assert self._cancellation_monitor is not None
        assert self._canceller is not None
        execution = asyncio.create_task(self._run_repository(request, authorized))
        revoked = asyncio.create_task(self._cancellation_monitor.wait())
        try:
            done, _ = await asyncio.wait(
                {execution, revoked}, return_when=asyncio.FIRST_COMPLETED
            )
            if execution in done:
                return await execution

            try:
                await revoked
                await self._canceller.cancel()
            except Exception:
                # The lease is revoked but we cannot prove the process tree is
                # gone. Never turn this ambiguous state into local success.
                execution.add_done_callback(_consume_background_result)
                return self._result(
                    request,
                    PackageExecutionStatus.EVIDENCE_UNRESOLVED,
                    error="revoked Codex session cancellation requires recovery",
                )

            try:
                await execution
            except Exception:
                # The terminal cancellation has already been persisted by the
                # session-bound canceller, so a local runner exception does not
                # change the authoritative outcome.
                pass
            return self._result(
                request,
                PackageExecutionStatus.FAILED,
                error="Codex capability grant was revoked by Captain",
            )
        finally:
            if not revoked.done():
                revoked.cancel()
            if revoked.done() and not revoked.cancelled():
                try:
                    revoked.exception()
                except Exception:
                    pass
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


def _consume_background_result(task: asyncio.Task[object]) -> None:
    """Retrieve a detached task exception after an evidence-unresolved return."""

    try:
        task.exception()
    except (asyncio.CancelledError, Exception):
        pass
