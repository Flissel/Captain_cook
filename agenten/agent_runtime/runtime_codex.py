"""Redacted, resumable Codex CLI execution for Captain runtime ports."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agenten.agent_factory.codex_build_execution import write_once_codex_evidence
from agenten.agent_factory.orchestration import FactoryDispatchError
from agenten.agent_runtime.contracts import ArtifactRef
from agenten.execution.codex_supervisor import (
    CODEX_JSONL_READ_CHUNK_BYTES,
    DEFAULT_MAX_CODEX_JSONL_RECORD_BYTES,
    canonical_codex_event_type,
)


RuntimeCodexTerminalStatus = Literal[
    "succeeded",
    "failed",
    "timed_out",
    "cancelled",
]
RuntimeCodexProcessCleanupStatus = Literal[
    "not_required",
    "verified_cancelled",
    "unresolved",
]


@dataclass(frozen=True)
class RuntimeCodexInvocation:
    """Private execution input with one stable effect identity across resume."""

    command_id: UUID
    correlation_id: UUID
    command_identity_sha256: str
    session_id: str
    workspace: Path
    prompt: str
    timeout_seconds: int
    resume: bool = False


@dataclass(frozen=True)
class RuntimeCodexProcessResult:
    """Bounded process outcome before Captain reduces it to public evidence."""

    exit_code: int
    terminal_status: RuntimeCodexTerminalStatus
    process_cleanup_status: RuntimeCodexProcessCleanupStatus
    elapsed_ms: int
    session_id: str
    jsonl_events: tuple[dict[str, object], ...]

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
        if self.elapsed_ms < 0:
            raise ValueError("runtime Codex elapsed duration must not be negative")
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", self.session_id) is None:
            raise ValueError("runtime Codex session identity is invalid")


class RuntimeCodexTerminalEvidenceV1(BaseModel):
    """Redacted terminal evidence retained independently of provider output."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        populate_by_name=True,
    )

    schema_name: Literal["captain.runtime-codex-terminal-evidence.v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    command_id: UUID
    correlation_id: UUID
    command_identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    session_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    status: RuntimeCodexTerminalStatus
    exit_code: int
    elapsed_ms: int = Field(ge=0, strict=True)
    last_event_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    event_count: int = Field(ge=0, strict=True)
    event_types: tuple[str, ...] = ()
    process_cleanup_status: RuntimeCodexProcessCleanupStatus
    resumable_checkpoint: ArtifactRef | None = None

    @field_validator("event_types")
    @classmethod
    def require_unique_event_types(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if tuple(sorted(set(value))) != value:
            raise ValueError("runtime Codex event types must be sorted and unique")
        return value

    @model_validator(mode="after")
    def require_timeout_checkpoint(self) -> "RuntimeCodexTerminalEvidenceV1":
        if self.exit_code == 124:
            if self.status != "timed_out" or self.resumable_checkpoint is None:
                raise ValueError("runtime Codex timeout requires a resumable checkpoint")
        elif self.status == "timed_out":
            raise ValueError("runtime Codex timeout status requires exit 124")
        return self


RuntimeCodexObserver = Callable[[dict[str, object]], Awaitable[None]]


class RuntimeCodexProcessRunner(Protocol):
    async def run(
        self,
        invocation: RuntimeCodexInvocation,
        observer: RuntimeCodexObserver,
    ) -> RuntimeCodexProcessResult: ...

    async def cancel(self, session_id: str) -> None: ...


class RuntimeCodexExecution:
    """Small runtime execution/recovery seam over an injected process runner."""

    def __init__(
        self,
        *,
        runner: RuntimeCodexProcessRunner,
        checkpoint_root: Path,
    ) -> None:
        self._runner = runner
        self._checkpoint_root = checkpoint_root.resolve()
        self._terminal: dict[UUID, RuntimeCodexTerminalEvidenceV1] = {}
        self._identity_index: dict[tuple[UUID, str, str], UUID] = {}
        self._identities: dict[UUID, str] = {}
        self._sessions: dict[UUID, str] = {}
        self._active_events: dict[UUID, list[dict[str, object]]] = {}
        self._cancelled: set[UUID] = set()
        self._cancel_lock = asyncio.Lock()

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
    ) -> RuntimeCodexTerminalEvidenceV1:
        identity = _runtime_codex_command_identity(
            correlation_id=correlation_id,
            subject_id=subject_id,
            prompt_sha256=prompt_sha256,
            workspace_ref=workspace_ref,
        )
        key = (correlation_id, subject_id, prompt_sha256)
        self._identity_index[key] = command_id
        self._identities[command_id] = identity
        session_id = f"runtime-{identity[:24]}"
        self._sessions[command_id] = session_id
        return await self._run(
            RuntimeCodexInvocation(
                command_id=command_id,
                correlation_id=correlation_id,
                command_identity_sha256=identity,
                session_id=session_id,
                workspace=workspace,
                prompt=prompt,
                timeout_seconds=timeout_seconds,
            ),
            observer=observer,
        )

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
    ) -> RuntimeCodexTerminalEvidenceV1:
        key = (correlation_id, subject_id, prompt_sha256)
        command_id = causation_id or self._identity_index.get(key)
        if command_id is None:
            raise FactoryDispatchError("Codex resume checkpoint is unavailable")
        prior = self._terminal.get(command_id) or self._load_checkpoint(command_id)
        if (
            prior is None
            or prior.correlation_id != correlation_id
            or prior.status not in {"timed_out", "cancelled", "failed"}
        ):
            raise FactoryDispatchError("Codex resume checkpoint is not resumable")
        expected_identity = _runtime_codex_command_identity(
            correlation_id=correlation_id,
            subject_id=subject_id,
            prompt_sha256=prompt_sha256,
            workspace_ref=workspace_ref,
        )
        if prior.command_identity_sha256 != expected_identity:
            raise FactoryDispatchError("Codex resume command identity changed")
        self._sessions[command_id] = prior.session_id
        self._identities[command_id] = expected_identity
        self._cancelled.discard(command_id)
        return await self._run(
            RuntimeCodexInvocation(
                command_id=command_id,
                correlation_id=correlation_id,
                command_identity_sha256=expected_identity,
                session_id=prior.session_id,
                workspace=workspace,
                prompt=prompt,
                timeout_seconds=timeout_seconds,
                resume=True,
            ),
            observer=observer,
        )

    async def cancel(
        self,
        *,
        correlation_id: UUID,
        causation_id: UUID | None,
        subject_id: str,
        prompt_sha256: str,
    ) -> RuntimeCodexTerminalEvidenceV1:
        key = (correlation_id, subject_id, prompt_sha256)
        command_id = causation_id or self._identity_index.get(key)
        if command_id is None or command_id not in self._sessions:
            raise FactoryDispatchError("Codex cancellation target is unavailable")
        async with self._cancel_lock:
            existing = self._terminal.get(command_id)
            if command_id not in self._cancelled:
                await self._runner.cancel(self._sessions[command_id])
                self._cancelled.add(command_id)
            if existing is not None and existing.status == "cancelled":
                return existing
            events = self._active_events.get(command_id, [])
            evidence = self._terminal_evidence(
                command_id=command_id,
                correlation_id=correlation_id,
                command_identity_sha256=self._identities[command_id],
                session_id=self._sessions[command_id],
                result=RuntimeCodexProcessResult(
                    exit_code=130,
                    terminal_status="cancelled",
                    process_cleanup_status="verified_cancelled",
                    elapsed_ms=0,
                    session_id=self._sessions[command_id],
                    jsonl_events=tuple(events),
                ),
                events=events,
            )
            self._terminal[command_id] = evidence
            return evidence

    def terminal_evidence(
        self,
        command_id: UUID,
    ) -> RuntimeCodexTerminalEvidenceV1 | None:
        return self._terminal.get(command_id) or self._load_checkpoint(command_id)

    def find_terminal(
        self,
        *,
        correlation_id: UUID,
        subject_id: str,
        prompt_sha256: str,
    ) -> RuntimeCodexTerminalEvidenceV1 | None:
        command_id = self._identity_index.get(
            (correlation_id, subject_id, prompt_sha256)
        )
        return self.terminal_evidence(command_id) if command_id is not None else None

    async def _run(
        self,
        invocation: RuntimeCodexInvocation,
        *,
        observer: RuntimeCodexObserver,
    ) -> RuntimeCodexTerminalEvidenceV1:
        observed: list[dict[str, object]] = []
        self._active_events[invocation.command_id] = observed

        async def observe(event: dict[str, object]) -> None:
            canonical = _runtime_codex_event(event)
            observed.append(canonical)
            await observer(dict(canonical))

        try:
            result = await self._runner.run(invocation, observe)
        finally:
            self._active_events.pop(invocation.command_id, None)
        if tuple(observed) != result.jsonl_events:
            raise FactoryDispatchError(
                "Codex process result does not match streamed JSONL events"
            )
        self._sessions[invocation.command_id] = result.session_id
        evidence = self._terminal_evidence(
            command_id=invocation.command_id,
            correlation_id=invocation.correlation_id,
            command_identity_sha256=invocation.command_identity_sha256,
            session_id=result.session_id,
            result=result,
            events=observed,
        )
        self._terminal[invocation.command_id] = evidence
        return evidence

    def _terminal_evidence(
        self,
        *,
        command_id: UUID,
        correlation_id: UUID,
        command_identity_sha256: str,
        session_id: str,
        result: RuntimeCodexProcessResult,
        events: list[dict[str, object]],
    ) -> RuntimeCodexTerminalEvidenceV1:
        event_types = tuple(
            sorted(
                {
                    canonical_codex_event_type(event.get("type"))
                    for event in events
                }
            )
        )
        last_event_sha256 = (
            hashlib.sha256(_runtime_codex_json(events[-1])).hexdigest()
            if events
            else None
        )
        terminal_record = self._persist_checkpoint(
            {
                "schema": "captain.runtime-codex-checkpoint.v1",
                "command_id": str(command_id),
                "correlation_id": str(correlation_id),
                "command_identity_sha256": command_identity_sha256,
                "session_id": session_id,
                "status": result.terminal_status,
                "exit_code": result.exit_code,
                "elapsed_ms": result.elapsed_ms,
                "last_event_sha256": last_event_sha256,
                "event_count": len(events),
                "event_types": list(event_types),
                "process_cleanup_status": result.process_cleanup_status,
            }
        )
        checkpoint = (
            terminal_record
            if result.terminal_status in {"timed_out", "cancelled", "failed"}
            else None
        )
        evidence = RuntimeCodexTerminalEvidenceV1(
            schema_name="captain.runtime-codex-terminal-evidence.v1",
            command_id=command_id,
            correlation_id=correlation_id,
            command_identity_sha256=command_identity_sha256,
            session_id=session_id,
            status=result.terminal_status,
            exit_code=result.exit_code,
            elapsed_ms=result.elapsed_ms,
            last_event_sha256=last_event_sha256,
            event_count=len(events),
            event_types=event_types,
            process_cleanup_status=result.process_cleanup_status,
            resumable_checkpoint=checkpoint,
        )
        self._write_checkpoint_index(command_id, terminal_record)
        return evidence

    def _persist_checkpoint(self, payload: Mapping[str, object]) -> ArtifactRef:
        content = _runtime_codex_json(payload)
        digest = hashlib.sha256(content).hexdigest()
        target = self._checkpoint_root / digest[:2] / f"{digest}.json"
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_once_codex_evidence(
            target,
            content,
            conflict="runtime Codex checkpoint replay conflicts",
        )
        return ArtifactRef(
            uri=f"artifact://runtime-codex-checkpoint/{digest}",
            sha256=digest,
            media_type="application/json",
        )

    def _write_checkpoint_index(self, command_id: UUID, reference: ArtifactRef) -> None:
        target = self._checkpoint_root / "by-command" / f"{command_id}.json"
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        content = _runtime_codex_json(reference.model_dump(mode="json"))
        if target.exists():
            try:
                if target.read_bytes() == content:
                    return
            except OSError:
                pass
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            write_once_codex_evidence(
                temporary,
                content,
                conflict="runtime Codex checkpoint index conflicts",
            )
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def _load_checkpoint(
        self,
        command_id: UUID,
    ) -> RuntimeCodexTerminalEvidenceV1 | None:
        index = self._checkpoint_root / "by-command" / f"{command_id}.json"
        try:
            reference = ArtifactRef.model_validate_json(index.read_bytes())
            content = (
                self._checkpoint_root
                / reference.sha256[:2]
                / f"{reference.sha256}.json"
            ).read_bytes()
            if hashlib.sha256(content).hexdigest() != reference.sha256:
                raise ValueError
            payload = json.loads(content)
            if not isinstance(payload, dict):
                raise ValueError
            status = str(payload["status"])
            return RuntimeCodexTerminalEvidenceV1(
                schema_name="captain.runtime-codex-terminal-evidence.v1",
                command_id=UUID(str(payload["command_id"])),
                correlation_id=UUID(str(payload["correlation_id"])),
                command_identity_sha256=str(payload["command_identity_sha256"]),
                session_id=str(payload["session_id"]),
                status=status,
                exit_code=int(payload["exit_code"]),
                elapsed_ms=int(payload["elapsed_ms"]),
                last_event_sha256=payload.get("last_event_sha256"),
                event_count=int(payload["event_count"]),
                event_types=tuple(payload["event_types"]),
                process_cleanup_status=str(payload["process_cleanup_status"]),
                resumable_checkpoint=(
                    reference
                    if status in {"timed_out", "cancelled", "failed"}
                    else None
                ),
            )
        except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
            return None


def _runtime_codex_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _runtime_codex_event(value: Mapping[str, object]) -> dict[str, object]:
    try:
        encoded = _runtime_codex_json(dict(value))
        decoded = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise FactoryDispatchError("Codex JSONL event is invalid") from exc
    if not isinstance(decoded, dict):
        raise FactoryDispatchError("Codex JSONL event is invalid")
    return decoded


def _runtime_codex_command_identity(
    *,
    correlation_id: UUID,
    subject_id: str,
    prompt_sha256: str,
    workspace_ref: str,
) -> str:
    return hashlib.sha256(
        _runtime_codex_json(
            {
                "correlation_id": str(correlation_id),
                "subject_id": subject_id,
                "prompt_sha256": prompt_sha256,
                "workspace_ref": workspace_ref,
            }
        )
    ).hexdigest()


class SubprocessRuntimeCodexRunner:
    """Stream one Codex CLI JSONL process without retaining raw public evidence."""

    def __init__(
        self,
        *,
        executable: str,
        environ: Mapping[str, str],
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._executable = executable
        self._environ = dict(environ)
        self._monotonic = monotonic or time.monotonic
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._cancelled: set[str] = set()

    async def run(
        self,
        invocation: RuntimeCodexInvocation,
        observer: RuntimeCodexObserver,
    ) -> RuntimeCodexProcessResult:
        self._cancelled.discard(invocation.session_id)
        arguments = [self._executable, "exec"]
        if invocation.resume:
            arguments.extend(("resume", "--json", invocation.session_id))
        else:
            arguments.append("--json")
        arguments.append(invocation.prompt)
        process_options: dict[str, object] = {}
        if os.name == "nt":
            process_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            process_options["start_new_session"] = True
        try:
            process = await asyncio.create_subprocess_exec(
                *arguments,
                cwd=invocation.workspace,
                env=self._environ,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **process_options,
            )
        except (FileNotFoundError, OSError) as exc:
            raise FactoryDispatchError("Codex CLI executable is unavailable") from exc
        self._processes[invocation.session_id] = process
        assert process.stdout is not None
        assert process.stderr is not None
        events: list[dict[str, object]] = []
        started = self._monotonic()

        async def read_stdout() -> None:
            while True:
                try:
                    line = await process.stdout.readline()
                except OSError as exc:
                    raise FactoryDispatchError("Codex JSONL stream failed") from exc
                if not line:
                    return
                if len(line) > DEFAULT_MAX_CODEX_JSONL_RECORD_BYTES + 1:
                    raise FactoryDispatchError("Codex JSONL record exceeds size limit")
                try:
                    value = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise FactoryDispatchError("Codex JSONL event is invalid") from exc
                if not isinstance(value, dict):
                    raise FactoryDispatchError("Codex JSONL event is invalid")
                event = _runtime_codex_event(value)
                events.append(event)
                await observer(dict(event))

        async def drain_stderr() -> None:
            while await process.stderr.read(CODEX_JSONL_READ_CHUNK_BYTES):
                pass

        timed_out = False
        try:
            await asyncio.wait_for(
                asyncio.gather(process.wait(), read_stdout(), drain_stderr()),
                timeout=invocation.timeout_seconds,
            )
        except TimeoutError:
            timed_out = True
            await self.cancel(invocation.session_id)
            await process.wait()
        except BaseException:
            await asyncio.shield(self.cancel(invocation.session_id))
            await asyncio.shield(process.wait())
            raise
        finally:
            self._processes.pop(invocation.session_id, None)
        elapsed_ms = max(0, int((self._monotonic() - started) * 1000))
        externally_cancelled = invocation.session_id in self._cancelled
        exit_code = (
            124
            if timed_out
            else 130
            if externally_cancelled
            else process.returncode
        )
        if exit_code is None:
            raise FactoryDispatchError("Codex process has no terminal exit code")
        status: RuntimeCodexTerminalStatus = (
            "timed_out"
            if exit_code == 124
            else "cancelled"
            if exit_code == 130
            else "succeeded"
            if exit_code == 0
            else "failed"
        )
        session_id = invocation.session_id
        for event in events:
            if event.get("type") == "thread.started":
                candidate = event.get("thread_id")
                if isinstance(candidate, str) and re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", candidate
                ):
                    session_id = candidate
                    break
        return RuntimeCodexProcessResult(
            exit_code=exit_code,
            terminal_status=status,
            process_cleanup_status=(
                "verified_cancelled"
                if exit_code in {124, 130}
                else "not_required"
            ),
            elapsed_ms=elapsed_ms,
            session_id=session_id,
            jsonl_events=tuple(events),
        )

    async def cancel(self, session_id: str) -> None:
        if session_id in self._cancelled:
            return
        self._cancelled.add(session_id)
        process = self._processes.get(session_id)
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            process.kill()
            await process.wait()

__all__ = [
    "RuntimeCodexExecution",
    "RuntimeCodexInvocation",
    "RuntimeCodexProcessResult",
    "RuntimeCodexTerminalEvidenceV1",
    "SubprocessRuntimeCodexRunner",
]
