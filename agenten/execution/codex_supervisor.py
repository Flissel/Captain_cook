"""Gateway-fenced, injected Codex process supervision."""

from __future__ import annotations

import asyncio
import hashlib
from io import BytesIO
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
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
DEFAULT_MAX_CODEX_JSONL_RECORD_BYTES = 1024 * 1024
CODEX_JSONL_READ_CHUNK_BYTES = 64 * 1024
DEFAULT_MAX_CODEX_JOURNAL_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_CODEX_JOURNAL_RECORDS = 65_536
CODEX_RECEIPT_EVENT_TYPES = frozenset(
    {
        "error",
        "item.completed",
        "item.started",
        "item.updated",
        "thread.started",
        "turn.completed",
        "turn.started",
    }
)


def canonical_codex_event_type(value: object) -> str:
    """Return a bounded receipt-safe type without retaining provider text."""

    if isinstance(value, str) and value in CODEX_RECEIPT_EVENT_TYPES:
        return value
    return "unknown"


def canonical_codex_event_types(values: tuple[object, ...]) -> tuple[str, ...]:
    """Deduplicate and sort the finite receipt-safe event vocabulary."""

    return tuple(sorted({canonical_codex_event_type(value) for value in values}))


CodexOutputFailureKind = Literal[
    "journal_persistence_failed",
    "output_read_failed",
    "invalid_json_object",
    "record_size_limit_exceeded",
    "unterminated_record",
    "journal_size_limit_exceeded",
    "journal_record_count_exceeded",
]


class CodexOutputEvidenceError(RuntimeError):
    """Codex process output could not be retained as trustworthy evidence."""

    failure_kind: CodexOutputFailureKind

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.process_cleanup_status: CodexProcessCleanupStatus | None = None
        self.journal_path: Path | None = None
        self.journal_sha256: str | None = None
        self.journal_byte_count: int | None = None
        self.event_count: int | None = None
        self.event_types: tuple[str, ...] | None = None

    def bind_terminal_evidence(
        self,
        *,
        process_cleanup_status: CodexProcessCleanupStatus,
        journal_path: Path,
        journal_sha256: str,
        journal_byte_count: int,
        event_count: int,
        event_types: tuple[str, ...],
    ) -> None:
        if self.process_cleanup_status is not None:
            raise ValueError("Codex output failure evidence is already bound")
        resolved = journal_path.resolve()
        if not resolved.is_absolute():
            raise ValueError("Codex output failure journal path must be absolute")
        if len(journal_sha256) != 64 or any(
            value not in "0123456789abcdef" for value in journal_sha256
        ):
            raise ValueError("Codex output failure journal digest is invalid")
        if not 0 <= journal_byte_count <= DEFAULT_MAX_CODEX_JOURNAL_BYTES:
            raise ValueError("Codex output failure journal size is invalid")
        if not 0 <= event_count <= DEFAULT_MAX_CODEX_JOURNAL_RECORDS:
            raise ValueError("Codex output failure event metadata is invalid")
        self.process_cleanup_status = process_cleanup_status
        self.journal_path = resolved
        self.journal_sha256 = journal_sha256
        self.journal_byte_count = journal_byte_count
        self.event_count = event_count
        self.event_types = canonical_codex_event_types(event_types)


class CodexJournalPersistenceError(CodexOutputEvidenceError):
    """The private Codex JSONL journal could not be durably persisted."""

    failure_kind: CodexOutputFailureKind = "journal_persistence_failed"


class CodexOutputReadError(CodexOutputEvidenceError):
    """The Codex process output stream could not be read."""

    failure_kind: CodexOutputFailureKind = "output_read_failed"


class CodexJsonlInvalidObjectError(CodexOutputEvidenceError):
    """One complete Codex JSONL record was not a valid JSON object."""

    failure_kind: CodexOutputFailureKind = "invalid_json_object"


class CodexJsonlRecordTooLargeError(CodexOutputEvidenceError):
    """One Codex JSONL record exceeded the bounded evidence envelope."""

    failure_kind: CodexOutputFailureKind = "record_size_limit_exceeded"


class CodexJsonlUnterminatedRecordError(CodexOutputEvidenceError):
    """The Codex output ended with an incomplete JSONL record."""

    failure_kind: CodexOutputFailureKind = "unterminated_record"


class CodexJournalSizeLimitError(CodexOutputEvidenceError):
    """The aggregate Codex JSONL journal exceeded its safe envelope."""

    failure_kind: CodexOutputFailureKind = "journal_size_limit_exceeded"


class CodexJournalRecordCountLimitError(CodexOutputEvidenceError):
    """The aggregate Codex JSONL journal contained too many records."""

    failure_kind: CodexOutputFailureKind = "journal_record_count_exceeded"


@dataclass
class _JournalEvidenceState:
    persisted_bytes: bytearray = field(default_factory=bytearray)
    record_count: int = 0


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
    recovery_run: bool = False

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
        max_jsonl_record_bytes: int = DEFAULT_MAX_CODEX_JSONL_RECORD_BYTES,
        max_journal_bytes: int = DEFAULT_MAX_CODEX_JOURNAL_BYTES,
        max_journal_records: int = DEFAULT_MAX_CODEX_JOURNAL_RECORDS,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        if (
            not isinstance(deadline_at, datetime)
            or deadline_at.tzinfo is None
            or deadline_at.utcoffset() != timezone.utc.utcoffset(deadline_at)
        ):
            raise ValueError("Codex runner deadline must be a UTC timestamp")
        if (
            isinstance(max_jsonl_record_bytes, bool)
            or not isinstance(max_jsonl_record_bytes, int)
            or not 1 <= max_jsonl_record_bytes <= DEFAULT_MAX_CODEX_JSONL_RECORD_BYTES
        ):
            raise ValueError(
                "Codex JSONL record limit must be between 1 byte and 1 MiB"
            )
        if (
            isinstance(max_journal_bytes, bool)
            or not isinstance(max_journal_bytes, int)
            or not 1 <= max_journal_bytes <= DEFAULT_MAX_CODEX_JOURNAL_BYTES
        ):
            raise ValueError(
                "Codex JSONL journal limit must be between 1 byte and 64 MiB"
            )
        if (
            isinstance(max_journal_records, bool)
            or not isinstance(max_journal_records, int)
            or not 1
            <= max_journal_records
            <= DEFAULT_MAX_CODEX_JOURNAL_RECORDS
        ):
            raise ValueError(
                "Codex JSONL journal record limit must be between 1 and 65536"
            )
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
        self._max_jsonl_record_bytes = max_jsonl_record_bytes
        self._max_journal_bytes = max_journal_bytes
        self._max_journal_records = max_journal_records
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic or time.monotonic

    async def run(self, authorized: AuthorizedCodexRun) -> CodexRunResult:
        standard_command = (
            len(authorized.command) == 4
            and authorized.command[:3] == ("codex", "exec", "--json")
        )
        resume_command = (
            len(authorized.command) == 6
            and authorized.command[:4] == ("codex", "exec", "resume", "--json")
        )
        if not standard_command and not resume_command:
            raise ValueError("PowerShell Codex runner command is invalid")
        prompt = authorized.command[-1]
        resume_thread_id = authorized.command[-2] if resume_command else None
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
        wrapper_stop_at = self._monotonic() + wrapper_timeout_seconds
        launcher_arguments = [
            str(self._pwsh_path),
            "-NoProfile",
            "-File",
            str(self._script_path),
            "-Workspace",
            str(authorized.workspace),
            "-Prompt",
            prompt,
            "-CodexPath",
            str(self._codex_path),
            "-SessionId",
            self._session_id,
            "-StatePath",
            str(self._state_path),
            "-DeadlineAt",
            self._deadline_at.isoformat(),
        ]
        if resume_thread_id is not None:
            launcher_arguments.extend(("-ResumeThreadId", resume_thread_id))
        process = await asyncio.create_subprocess_exec(
            *launcher_arguments,
            cwd=authorized.workspace,
            env=child_environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert process.stdout is not None
        assert process.stderr is not None
        timed_out = False
        evidence_failure: CodexOutputEvidenceError | None = None
        process_cleanup_status: CodexProcessCleanupStatus = "not_required"
        wrapper_expired_during_launch = self._monotonic() >= wrapper_stop_at
        journal_state = _JournalEvidenceState()
        journal_bytes = b""
        journal_lines: tuple[str, ...] = ()
        with self._journal_path.open("a+b") as journal:
            process_waiter = asyncio.create_task(process.wait())
            stdout_reader = asyncio.create_task(
                self._journal_stdout(process.stdout, journal, journal_state)
            )
            stderr_reader = asyncio.create_task(self._drain_stderr(process.stderr))
            try:
                wrapper_timeout_remaining = wrapper_stop_at - self._monotonic()
                if wrapper_expired_during_launch or wrapper_timeout_remaining <= 0:
                    raise TimeoutError
                await asyncio.wait_for(
                    self._observe_process_and_readers(
                        process_waiter,
                        stdout_reader,
                        stderr_reader,
                    ),
                    timeout=wrapper_timeout_remaining,
                )
            except TimeoutError:
                timed_out = True
                cancelled = await self._attempt_verified_timeout_cancellation()
                wrapper_stopped = await self._settle_timed_out_wrapper(process)
                process_cleanup_status = (
                    "verified_cancelled"
                    if cancelled and wrapper_stopped
                    else "unresolved"
                )
            except CodexOutputEvidenceError as exc:
                evidence_failure = exc
                if process.returncode is None:
                    cancelled = (
                        await self._attempt_verified_evidence_failure_cancellation()
                    )
                    wrapper_stopped = await self._settle_timed_out_wrapper(process)
                    process_cleanup_status = (
                        "verified_cancelled"
                        if cancelled and wrapper_stopped
                        else "unresolved"
                    )
            finally:
                await self._finish_process_waiter(process_waiter)
                try:
                    await self._finish_readers(
                        stdout_reader,
                        stderr_reader,
                        allow_bounded_cancellation=(
                            timed_out or evidence_failure is not None
                        ),
                    )
                except BaseException as exc:
                    if evidence_failure is None:
                        evidence_failure = self._redact_reader_failure(exc)
            try:
                journal_bytes = self._read_bounded_journal(journal)
            except CodexOutputEvidenceError as exc:
                if evidence_failure is None:
                    evidence_failure = exc
                journal_bytes = bytes(journal_state.persisted_bytes)
            try:
                journal_lines, event_types = self._parse_journal_snapshot(
                    journal_bytes,
                    tolerate_partial=evidence_failure is not None,
                )
            except CodexOutputEvidenceError as exc:
                if evidence_failure is None:
                    evidence_failure = exc
                journal_lines, event_types = self._parse_journal_snapshot(
                    bytes(journal_state.persisted_bytes),
                    tolerate_partial=True,
                )
                journal_bytes = bytes(journal_state.persisted_bytes)
            if evidence_failure is not None:
                evidence_failure.bind_terminal_evidence(
                    process_cleanup_status=process_cleanup_status,
                    journal_path=self._journal_path,
                    journal_sha256=hashlib.sha256(journal_bytes).hexdigest(),
                    journal_byte_count=len(journal_bytes),
                    event_count=len(journal_lines),
                    event_types=event_types,
                )

        if evidence_failure is not None:
            raise evidence_failure

        exit_code = 124 if timed_out else process.returncode
        assert exit_code is not None
        terminal_status: CodexRunTerminalStatus
        if timed_out or exit_code == 124:
            terminal_status = "timed_out"
        elif exit_code == 130:
            terminal_status = "cancelled"
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
            jsonl_lines=journal_lines,
        )

    def _prelaunch_timeout_result(self) -> CodexRunResult:
        try:
            with self._journal_path.open("rb") as journal:
                journal_bytes = self._read_bounded_journal(journal)
        except CodexOutputEvidenceError as exc:
            journal_bytes = b""
            exc.bind_terminal_evidence(
                process_cleanup_status="not_required",
                journal_path=self._journal_path,
                journal_sha256=hashlib.sha256(journal_bytes).hexdigest(),
                journal_byte_count=0,
                event_count=0,
                event_types=(),
            )
            raise
        return CodexRunResult(
            exit_code=124,
            terminal_status="timed_out",
            process_cleanup_status="not_required",
            journal_path=self._journal_path,
            journal_sha256=hashlib.sha256(journal_bytes).hexdigest(),
            artifact_references=(),
            jsonl_lines=(),
        )

    async def _journal_stdout(
        self,
        stdout: asyncio.StreamReader,
        journal: BinaryIO,
        state: _JournalEvidenceState,
    ) -> None:
        pending = bytearray()
        while True:
            try:
                chunk = await stdout.read(CODEX_JSONL_READ_CHUNK_BYTES)
            except OSError:
                raise CodexOutputReadError(
                    "Codex process output stream could not be read"
                ) from None
            if not chunk:
                break
            cursor = 0
            while True:
                newline = chunk.find(b"\n", cursor)
                if newline < 0:
                    self._extend_jsonl_record(pending, chunk[cursor:])
                    break
                self._extend_jsonl_record(
                    pending,
                    chunk[cursor:newline],
                )
                record = bytes(pending)
                pending.clear()
                if record.endswith(b"\r"):
                    record = record[:-1]
                if record.strip():
                    self._require_json_object(record)
                    self._persist_jsonl_record(journal, record, state)
                cursor = newline + 1
                if cursor == len(chunk):
                    break
        if pending:
            raise CodexJsonlUnterminatedRecordError(
                "Codex JSONL stream ended with an unterminated record"
            )

    def _extend_jsonl_record(
        self,
        pending: bytearray,
        segment: bytes,
    ) -> None:
        candidate_length = len(pending) + len(segment)
        candidate_ends_with_cr = (
            segment.endswith(b"\r") if segment else pending.endswith(b"\r")
        )
        content_length = candidate_length - int(candidate_ends_with_cr)
        if content_length > self._max_jsonl_record_bytes:
            raise CodexJsonlRecordTooLargeError(
                "Codex JSONL record exceeds configured safe limit"
            )
        pending.extend(segment)

    def _persist_jsonl_record(
        self,
        journal: BinaryIO,
        record: bytes,
        state: _JournalEvidenceState,
    ) -> None:
        serialized = record + b"\n"
        if state.record_count >= self._max_journal_records:
            raise CodexJournalRecordCountLimitError(
                "Codex JSONL journal exceeds configured record count limit"
            )
        if len(state.persisted_bytes) + len(serialized) > self._max_journal_bytes:
            raise CodexJournalSizeLimitError(
                "Codex JSONL journal exceeds configured safe limit"
            )
        try:
            written = journal.write(serialized)
            if written != len(serialized):
                raise OSError
            journal.flush()
            os.fsync(journal.fileno())
        except OSError:
            raise CodexJournalPersistenceError(
                "Codex JSONL journal persistence failed"
            ) from None
        state.persisted_bytes.extend(serialized)
        state.record_count += 1

    def _read_bounded_journal(self, journal: BinaryIO) -> bytes:
        try:
            journal.flush()
            size_before = os.fstat(journal.fileno()).st_size
        except OSError:
            raise CodexJournalPersistenceError(
                "Codex JSONL journal snapshot could not be stabilized"
            ) from None
        if size_before > self._max_journal_bytes:
            raise CodexJournalSizeLimitError(
                "Codex JSONL journal exceeds configured safe limit"
            )
        try:
            journal.seek(0)
            snapshot = journal.read(self._max_journal_bytes + 1)
            size_after = os.fstat(journal.fileno()).st_size
        except OSError:
            raise CodexOutputReadError(
                "Codex JSONL journal snapshot could not be read"
            ) from None
        if (
            len(snapshot) > self._max_journal_bytes
            or size_before != size_after
            or len(snapshot) != size_after
        ):
            raise CodexJournalSizeLimitError(
                "Codex JSONL journal snapshot changed or exceeds safe limit"
            )
        return snapshot

    def _parse_journal_snapshot(
        self,
        journal_bytes: bytes,
        *,
        tolerate_partial: bool,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        records: list[str] = []
        event_types: set[str] = set()
        stream = BytesIO(journal_bytes)
        while True:
            raw_record = stream.readline(self._max_jsonl_record_bytes + 2)
            if not raw_record:
                break
            if not raw_record.endswith(b"\n"):
                if tolerate_partial:
                    break
                if len(raw_record) > self._max_jsonl_record_bytes:
                    raise CodexJsonlRecordTooLargeError(
                        "Codex JSONL record exceeds configured safe limit"
                    )
                raise CodexJsonlUnterminatedRecordError(
                    "Codex JSONL stream ended with an unterminated record"
                )
            record = raw_record[:-1]
            if record.endswith(b"\r"):
                record = record[:-1]
            if len(record) > self._max_jsonl_record_bytes:
                if tolerate_partial:
                    break
                raise CodexJsonlRecordTooLargeError(
                    "Codex JSONL record exceeds configured safe limit"
                )
            if not record.strip():
                continue
            if len(records) >= self._max_journal_records:
                if tolerate_partial:
                    break
                raise CodexJournalRecordCountLimitError(
                    "Codex JSONL journal exceeds configured record count limit"
                )
            event = self._require_json_object(record)
            records.append(record.decode("utf-8"))
            event_types.add(canonical_codex_event_type(event.get("type")))
        return tuple(records), tuple(sorted(event_types))

    @staticmethod
    def _require_json_object(record: bytes) -> dict[str, object]:
        try:
            event = json.loads(record)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise CodexJsonlInvalidObjectError(
                "Codex JSONL record is not a valid JSON object"
            ) from None
        if not isinstance(event, dict):
            raise CodexJsonlInvalidObjectError(
                "Codex JSONL record is not a valid JSON object"
            )
        return event

    @staticmethod
    async def _drain_stderr(stderr: asyncio.StreamReader) -> None:
        try:
            while await stderr.read(CODEX_JSONL_READ_CHUNK_BYTES):
                pass
        except OSError:
            raise CodexOutputReadError(
                "Codex process output stream could not be read"
            ) from None

    @staticmethod
    async def _observe_process_and_readers(
        process_waiter: asyncio.Task[int],
        stdout_reader: asyncio.Task[None],
        stderr_reader: asyncio.Task[None],
    ) -> None:
        readers = frozenset((stdout_reader, stderr_reader))
        pending: set[asyncio.Task[object]] = {
            process_waiter,
            stdout_reader,
            stderr_reader,
        }
        while process_waiter in pending:
            done, _ = await asyncio.wait(
                pending,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for reader in readers.intersection(done):
                if reader.cancelled():
                    raise CodexOutputReadError(
                        "Codex process output stream ended unexpectedly"
                    )
                failure = reader.exception()
                if failure is not None:
                    raise PowerShellCodexRunner._redact_reader_failure(failure)
            if process_waiter in done:
                await process_waiter
                return
            pending.difference_update(done)

    @staticmethod
    def _redact_reader_failure(exc: BaseException) -> CodexOutputEvidenceError:
        if isinstance(exc, CodexOutputEvidenceError):
            return exc
        return CodexOutputReadError("Codex process output stream could not be read")

    async def _attempt_verified_timeout_cancellation(self) -> bool:
        try:
            return await asyncio.wait_for(
                self._cancel_timed_out_process(),
                timeout=20,
            )
        except Exception:
            return False

    async def _attempt_verified_evidence_failure_cancellation(self) -> bool:
        try:
            return await asyncio.wait_for(
                self._cancel_evidence_failure_process(),
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

    @staticmethod
    async def _finish_process_waiter(process_waiter: asyncio.Task[int]) -> None:
        if not process_waiter.done():
            process_waiter.cancel()
        await asyncio.gather(process_waiter, return_exceptions=True)

    async def _cancel_timed_out_process(self) -> bool:
        return await self._cancel_process("timeout")

    async def _cancel_evidence_failure_process(self) -> bool:
        return await self._cancel_process("operator")

    async def _cancel_process(self, reason: Literal["operator", "timeout"]) -> bool:
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
            reason,
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
            "cancellation_reason": reason,
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
