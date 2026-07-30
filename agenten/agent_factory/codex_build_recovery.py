"""Private monotonic checkpoints for one Captain-owned Codex build."""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from agenten.agent_factory.orchestration import FactoryDispatchError
from agenten.agent_factory.skill_workflow_contracts import FactorySkillInvocationV1


FactoryCodexBuildPhase = Literal[
    "scaffold_ready",
    "implementation_running",
    "implementation_interrupted",
    "implementation_complete",
    "sealed",
]

_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_REVISION_PATTERN = re.compile(r"^[0-9a-f]{40,64}$")
_TRANSITIONS: frozenset[tuple[FactoryCodexBuildPhase, FactoryCodexBuildPhase]] = (
    frozenset(
        {
            ("scaffold_ready", "implementation_running"),
            ("implementation_running", "implementation_interrupted"),
            ("implementation_running", "implementation_complete"),
            ("implementation_interrupted", "implementation_running"),
            ("implementation_complete", "sealed"),
        }
    )
)


class FactoryCodexBuildCheckpointV1(BaseModel):
    """Immutable binding and phase for one exact Codex seal invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    job_id: UUID
    correlation_id: UUID
    attempt: int = Field(ge=1)
    invocation_id: UUID
    workspace_ref: str = Field(min_length=1)
    workspace_root: Path
    base_revision: str
    brief_sha256: str
    phase: FactoryCodexBuildPhase
    resume_ordinal: int = Field(ge=0)
    terminal_receipt_sha256: str | None = None
    updated_at: datetime

    @field_validator("workspace_root")
    @classmethod
    def _require_absolute_workspace_root(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("workspace_root must be absolute")
        return value

    @field_validator("base_revision")
    @classmethod
    def _require_revision(cls, value: str) -> str:
        if _REVISION_PATTERN.fullmatch(value) is None:
            raise ValueError("base_revision must be a lowercase Git revision")
        return value

    @field_validator("brief_sha256")
    @classmethod
    def _require_brief_digest(cls, value: str) -> str:
        if _DIGEST_PATTERN.fullmatch(value) is None:
            raise ValueError("brief_sha256 must be a SHA-256 digest")
        return value

    @field_validator("terminal_receipt_sha256")
    @classmethod
    def _require_receipt_digest(cls, value: str | None) -> str | None:
        if value is not None and _DIGEST_PATTERN.fullmatch(value) is None:
            raise ValueError("terminal_receipt_sha256 must be a SHA-256 digest")
        return value

    @field_validator("updated_at")
    @classmethod
    def _require_utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
            raise ValueError("updated_at must be UTC")
        return value

    @model_validator(mode="after")
    def _require_phase_receipt_shape(self) -> FactoryCodexBuildCheckpointV1:
        receipt_required = self.phase in {
            "implementation_interrupted",
            "implementation_complete",
            "sealed",
        }
        if receipt_required != (self.terminal_receipt_sha256 is not None):
            raise ValueError("terminal receipt does not match checkpoint phase")
        return self


class FilesystemFactoryCodexBuildCheckpointStore:
    """Atomically persist one exact, monotonic checkpoint per invocation."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def load(
        self,
        invocation: FactorySkillInvocationV1,
    ) -> FactoryCodexBuildCheckpointV1 | None:
        path = self._path(invocation.invocation_id)
        lock = self._acquire_file_lock(self._lock_path(invocation.invocation_id))
        try:
            checkpoint = self._read(path)
        finally:
            self._release_file_lock(lock)
        if checkpoint is None:
            return None
        if (
            checkpoint.invocation_id != invocation.invocation_id
            or checkpoint.job_id != invocation.job_id
            or checkpoint.correlation_id != invocation.correlation_id
            or checkpoint.attempt != invocation.attempt
            or checkpoint.workspace_ref != invocation.lease.workspace_ref
        ):
            raise FactoryDispatchError(
                "Factory Codex checkpoint binding does not match invocation"
            )
        return checkpoint

    def advance(
        self,
        previous: FactoryCodexBuildCheckpointV1 | None,
        next_checkpoint: FactoryCodexBuildCheckpointV1,
    ) -> FactoryCodexBuildCheckpointV1:
        target = _validated_checkpoint(next_checkpoint)
        prior = _validated_checkpoint(previous) if previous is not None else None
        if prior is not None:
            _require_immutable_bindings(prior, target)
        path = self._path(target.invocation_id)
        lock = self._acquire_file_lock(self._lock_path(target.invocation_id))
        try:
            actual_bytes = path.read_bytes() if path.exists() else None
            target_bytes = _canonical_checkpoint(target)
            if actual_bytes == target_bytes:
                return target
            if prior is None:
                if actual_bytes is not None:
                    raise FactoryDispatchError(
                        "Factory Codex checkpoint initial creation conflicts"
                    )
                _require_initial_checkpoint(target)
                self._atomic_create(path, target_bytes)
                return target

            previous_bytes = _canonical_checkpoint(prior)
            if actual_bytes != previous_bytes:
                raise FactoryDispatchError(
                    "Factory Codex checkpoint does not match exact previous bytes"
                )
            _require_transition(prior, target)
            self._atomic_replace(path, previous_bytes, target_bytes)
            return target
        except OSError as exc:
            raise FactoryDispatchError(
                "Factory Codex checkpoint filesystem operation failed"
            ) from exc
        finally:
            self._release_file_lock(lock)

    def _path(self, invocation_id: UUID) -> Path:
        return self._root / f"{invocation_id.hex}.json"

    def _lock_path(self, invocation_id: UUID) -> Path:
        return self._root / ".locks" / f"{invocation_id.hex}.lock"

    @staticmethod
    def _read(path: Path) -> FactoryCodexBuildCheckpointV1 | None:
        if not path.exists():
            return None
        try:
            raw = path.read_bytes()
            checkpoint = FactoryCodexBuildCheckpointV1.model_validate_json(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError):
            raise FactoryDispatchError("Factory Codex checkpoint is invalid") from None
        if raw != _canonical_checkpoint(checkpoint):
            raise FactoryDispatchError("Factory Codex checkpoint is not canonical")
        return checkpoint

    def _atomic_create(self, path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._write_temporary(path, content)
        try:
            os.link(temporary, path)
        except FileExistsError:
            raise FactoryDispatchError(
                "Factory Codex checkpoint initial creation conflicts"
            ) from None
        finally:
            temporary.unlink(missing_ok=True)

    def _atomic_replace(
        self,
        path: Path,
        expected_previous: bytes,
        content: bytes,
    ) -> None:
        temporary = self._write_temporary(path, content)
        try:
            if path.read_bytes() != expected_previous:
                raise FactoryDispatchError(
                    "Factory Codex checkpoint does not match exact previous bytes"
                )
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _write_temporary(path: Path, content: bytes) -> Path:
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        descriptor = os.open(
            temporary,
            getattr(os, "O_BINARY", 0) | os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        return temporary

    @staticmethod
    def _acquire_file_lock(path: Path) -> BinaryIO:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                while True:
                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        time.sleep(0.01)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            return handle
        except BaseException:
            handle.close()
            raise

    @staticmethod
    def _release_file_lock(handle: BinaryIO) -> None:
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _validated_checkpoint(
    checkpoint: FactoryCodexBuildCheckpointV1,
) -> FactoryCodexBuildCheckpointV1:
    try:
        return FactoryCodexBuildCheckpointV1.model_validate(
            checkpoint.model_dump()
        )
    except (ValidationError, ValueError, TypeError):
        raise FactoryDispatchError("Factory Codex checkpoint is invalid") from None


def _canonical_checkpoint(checkpoint: FactoryCodexBuildCheckpointV1) -> bytes:
    return json.dumps(
        checkpoint.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _require_initial_checkpoint(checkpoint: FactoryCodexBuildCheckpointV1) -> None:
    if checkpoint.phase != "scaffold_ready" or checkpoint.resume_ordinal != 0:
        raise FactoryDispatchError(
            "Factory Codex checkpoint initial phase must be scaffold_ready"
        )


def _require_immutable_bindings(
    previous: FactoryCodexBuildCheckpointV1,
    next_checkpoint: FactoryCodexBuildCheckpointV1,
) -> None:
    fields = (
        "job_id",
        "correlation_id",
        "attempt",
        "invocation_id",
        "workspace_ref",
        "workspace_root",
        "base_revision",
        "brief_sha256",
    )
    if any(getattr(previous, field) != getattr(next_checkpoint, field) for field in fields):
        raise FactoryDispatchError("Factory Codex checkpoint immutable binding changed")


def _require_transition(
    previous: FactoryCodexBuildCheckpointV1,
    next_checkpoint: FactoryCodexBuildCheckpointV1,
) -> None:
    transition = (previous.phase, next_checkpoint.phase)
    if transition not in _TRANSITIONS:
        raise FactoryDispatchError("Factory Codex checkpoint phase transition is invalid")
    if next_checkpoint.updated_at <= previous.updated_at:
        raise FactoryDispatchError("Factory Codex checkpoint timestamp is not monotonic")
    expected_ordinal = previous.resume_ordinal + (
        1 if transition == ("implementation_interrupted", "implementation_running") else 0
    )
    if next_checkpoint.resume_ordinal != expected_ordinal:
        raise FactoryDispatchError("Factory Codex checkpoint resume ordinal is invalid")
    if transition == ("implementation_complete", "sealed") and (
        next_checkpoint.terminal_receipt_sha256
        != previous.terminal_receipt_sha256
    ):
        raise FactoryDispatchError("Factory Codex checkpoint receipt binding changed")


__all__ = [
    "FactoryCodexBuildCheckpointV1",
    "FactoryCodexBuildPhase",
    "FilesystemFactoryCodexBuildCheckpointStore",
]
