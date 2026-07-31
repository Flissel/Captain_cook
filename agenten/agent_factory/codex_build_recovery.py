"""Private monotonic checkpoints for one Captain-owned Codex build."""

from __future__ import annotations

import hashlib
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
from agenten.agent_factory.skill_workflow_contracts import (
    CodexBuildEvidenceV1,
    FactorySkillInvocationV1,
)


FactoryCodexBuildPhase = Literal[
    "scaffold_ready",
    "implementation_running",
    "implementation_interrupted",
    "implementation_complete",
    "sealed",
]

_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_REVISION_PATTERN = re.compile(r"^[0-9a-f]{40,64}$")
_CODEX_THREAD_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_OUTPUT_MANIFEST_URI_PATTERN = re.compile(
    r"^artifact://factory/codex-output-manifest/(?P<sha256>[0-9a-f]{64})$"
)
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
    scaffold_manifest_sha256: str
    phase: FactoryCodexBuildPhase
    resume_ordinal: int = Field(ge=0)
    terminal_receipt_sha256: str | None = None
    parent_terminal_receipt_sha256: str | None = None
    parent_journal_sha256: str | None = None
    parent_codex_thread_id: str | None = None
    runtime_retry_authorization_uri: str | None = None
    runtime_retry_authorization_sha256: str | None = None
    runtime_retry_authorization_binding_sha256: str | None = None
    output_manifest_uri: str | None = None
    output_manifest_sha256: str | None = None
    sealed_evidence_sha256: str | None = None
    sealed_build_receipt_uri: str | None = None
    sealed_build_receipt_sha256: str | None = None
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

    @field_validator(
        "brief_sha256",
        "scaffold_manifest_sha256",
        "sealed_evidence_sha256",
        "sealed_build_receipt_sha256",
        "runtime_retry_authorization_sha256",
        "runtime_retry_authorization_binding_sha256",
        "output_manifest_sha256",
        "parent_terminal_receipt_sha256",
        "parent_journal_sha256",
    )
    @classmethod
    def _require_sha256(cls, value: str | None) -> str | None:
        if value is not None and _DIGEST_PATTERN.fullmatch(value) is None:
            raise ValueError("checkpoint digest must be a SHA-256 digest")
        return value

    @field_validator("terminal_receipt_sha256")
    @classmethod
    def _require_receipt_digest(cls, value: str | None) -> str | None:
        if value is not None and _DIGEST_PATTERN.fullmatch(value) is None:
            raise ValueError("terminal_receipt_sha256 must be a SHA-256 digest")
        return value

    @field_validator("parent_codex_thread_id")
    @classmethod
    def _require_safe_parent_thread_id(cls, value: str | None) -> str | None:
        if value is not None and _CODEX_THREAD_ID_PATTERN.fullmatch(value) is None:
            raise ValueError("parent Codex thread ID is invalid")
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
        output_values = (
            self.output_manifest_uri,
            self.output_manifest_sha256,
        )
        output_required = self.phase in {"implementation_complete", "sealed"}
        if output_required and any(value is None for value in output_values):
            raise ValueError(
                "completed checkpoint requires original output manifest binding"
            )
        if not output_required and any(value is not None for value in output_values):
            raise ValueError(
                "incomplete checkpoint cannot bind an output manifest"
            )
        if self.output_manifest_uri is not None:
            match = _OUTPUT_MANIFEST_URI_PATTERN.fullmatch(self.output_manifest_uri)
            if (
                match is None
                or match.group("sha256") != self.output_manifest_sha256
            ):
                raise ValueError("checkpoint output manifest reference is invalid")
        sealed_values = (
            self.sealed_evidence_sha256,
            self.sealed_build_receipt_uri,
            self.sealed_build_receipt_sha256,
        )
        if self.phase == "sealed":
            if any(value is None for value in sealed_values):
                raise ValueError("sealed checkpoint requires original evidence binding")
        elif any(value is not None for value in sealed_values):
            raise ValueError("unsealed checkpoint cannot bind sealed evidence")
        retry_values = (
            self.runtime_retry_authorization_uri,
            self.runtime_retry_authorization_sha256,
            self.runtime_retry_authorization_binding_sha256,
        )
        if any(value is not None for value in retry_values) and any(
            value is None for value in retry_values
        ):
            raise ValueError("runtime retry checkpoint binding is incomplete")
        if self.resume_ordinal == 0 and any(value is not None for value in retry_values):
            raise ValueError("original runtime checkpoint cannot bind retry authority")
        if self.resume_ordinal > 0 and any(value is None for value in retry_values):
            raise ValueError("resumed runtime checkpoint requires retry authority")
        parent_lineage = (
            self.parent_terminal_receipt_sha256,
            self.parent_journal_sha256,
        )
        if self.resume_ordinal == 0 and (
            any(value is not None for value in parent_lineage)
            or self.parent_codex_thread_id is not None
        ):
            raise ValueError("original checkpoint cannot bind parent lineage")
        if self.resume_ordinal > 0 and any(value is None for value in parent_lineage):
            raise ValueError("resumed checkpoint requires parent lineage")
        return self


class FactoryCodexScaffoldFileV1(BaseModel):
    """One exact file in the immutable Captain scaffold."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    filename: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]{0,127}$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class FactoryCodexScaffoldManifestV1(BaseModel):
    """Original caller bindings and bytes admitted before workspace mutation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_name: Literal["captain.factory-codex-scaffold-manifest.v1"] = Field(
        default="captain.factory-codex-scaffold-manifest.v1",
        alias="schema",
        serialization_alias="schema",
    )
    job_id: UUID
    correlation_id: UUID
    attempt: int = Field(ge=1)
    invocation_id: UUID
    workspace_ref: str = Field(min_length=1)
    files: tuple[FactoryCodexScaffoldFileV1, ...] = Field(min_length=1)

    @field_validator("files")
    @classmethod
    def _require_sorted_unique_files(
        cls,
        value: tuple[FactoryCodexScaffoldFileV1, ...],
    ) -> tuple[FactoryCodexScaffoldFileV1, ...]:
        names = tuple(item.filename for item in value)
        if names != tuple(sorted(names)) or len(names) != len(set(names)):
            raise ValueError("scaffold files must be sorted and unique")
        return value


FactoryCodexOutputPath = Literal[
    "candidate.zip",
    "factory-candidate.json",
    "test-evidence.json",
]


class FactoryCodexOutputArtifactV1(BaseModel):
    """One required output captured at successful Codex termination."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    relative_path: FactoryCodexOutputPath
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0)


class FactoryCodexOutputManifestV1(BaseModel):
    """Immutable content bindings for the exact three seal inputs."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_name: Literal["captain.factory-codex-output-manifest.v1"] = Field(
        default="captain.factory-codex-output-manifest.v1",
        alias="schema",
        serialization_alias="schema",
    )
    job_id: UUID
    correlation_id: UUID
    attempt: int = Field(ge=1)
    invocation_id: UUID
    workspace_ref: str = Field(min_length=1)
    resume_ordinal: int = Field(ge=0)
    terminal_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifacts: tuple[FactoryCodexOutputArtifactV1, ...] = Field(min_length=3, max_length=3)

    @field_validator("artifacts")
    @classmethod
    def _require_exact_sorted_artifacts(
        cls,
        value: tuple[FactoryCodexOutputArtifactV1, ...],
    ) -> tuple[FactoryCodexOutputArtifactV1, ...]:
        paths = tuple(item.relative_path for item in value)
        if paths != (
            "candidate.zip",
            "factory-candidate.json",
            "test-evidence.json",
        ):
            raise ValueError(
                "output manifest must bind the exact sorted required artifacts"
            )
        return value


class FilesystemFactoryCodexOutputManifestStore:
    """Content-addressed, write-once output manifests in Captain private state."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def persist(self, manifest: FactoryCodexOutputManifestV1) -> tuple[str, str]:
        content = canonical_factory_codex_model(manifest)
        digest = hashlib.sha256(content).hexdigest()
        _atomic_write_once(
            self._path(digest),
            content,
            conflict="Factory Codex output manifest conflicts",
        )
        return (
            f"artifact://factory/codex-output-manifest/{digest}",
            digest,
        )

    def load(
        self,
        invocation: FactorySkillInvocationV1,
        *,
        uri: str,
        sha256: str,
    ) -> FactoryCodexOutputManifestV1:
        match = _OUTPUT_MANIFEST_URI_PATTERN.fullmatch(uri)
        if (
            match is None
            or match.group("sha256") != sha256
            or _DIGEST_PATTERN.fullmatch(sha256) is None
        ):
            raise FactoryDispatchError(
                "Factory Codex output manifest reference is invalid"
            )
        path = self._path(sha256)
        try:
            content = path.read_bytes()
            manifest = FactoryCodexOutputManifestV1.model_validate_json(content)
        except (OSError, ValidationError, ValueError):
            raise FactoryDispatchError(
                "Factory Codex output manifest is missing or invalid"
            ) from None
        if (
            hashlib.sha256(content).hexdigest() != sha256
            or content != canonical_factory_codex_model(manifest)
        ):
            raise FactoryDispatchError(
                "Factory Codex output manifest digest changed"
            )
        if (
            manifest.invocation_id != invocation.invocation_id
            or manifest.job_id != invocation.job_id
            or manifest.correlation_id != invocation.correlation_id
            or manifest.attempt != invocation.attempt
            or manifest.workspace_ref != invocation.lease.workspace_ref
        ):
            raise FactoryDispatchError(
                "Factory Codex output manifest binding changed"
            )
        return manifest

    def _path(self, sha256: str) -> Path:
        return self._root / f"{sha256}.json"


class FilesystemFactoryCodexScaffoldManifestStore:
    """Write-once original scaffold bindings for one invocation."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def persist(self, manifest: FactoryCodexScaffoldManifestV1) -> str:
        content = canonical_factory_codex_model(manifest)
        _atomic_write_once(
            self._path(manifest.invocation_id),
            content,
            conflict="Factory Codex original scaffold manifest conflicts",
        )
        return hashlib.sha256(content).hexdigest()

    def load(
        self,
        invocation: FactorySkillInvocationV1,
    ) -> FactoryCodexScaffoldManifestV1 | None:
        path = self._path(invocation.invocation_id)
        if not path.exists():
            return None
        try:
            content = path.read_bytes()
            manifest = FactoryCodexScaffoldManifestV1.model_validate_json(content)
        except (OSError, ValidationError, ValueError):
            raise FactoryDispatchError(
                "Factory Codex original scaffold manifest is invalid"
            ) from None
        if content != canonical_factory_codex_model(manifest):
            raise FactoryDispatchError(
                "Factory Codex original scaffold manifest is not canonical"
            )
        if (
            manifest.invocation_id != invocation.invocation_id
            or manifest.job_id != invocation.job_id
            or manifest.correlation_id != invocation.correlation_id
            or manifest.attempt != invocation.attempt
            or manifest.workspace_ref != invocation.lease.workspace_ref
        ):
            raise FactoryDispatchError(
                "Factory Codex original scaffold manifest binding changed"
            )
        return manifest

    def _path(self, invocation_id: UUID) -> Path:
        return self._root / f"{invocation_id.hex}.json"


class FilesystemFactoryCodexSealedEvidenceStore:
    """Write-once canonical workflow evidence used for byte-stable seal replay."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def persist(self, evidence: CodexBuildEvidenceV1) -> str:
        content = canonical_factory_codex_model(evidence)
        _atomic_write_once(
            self._path(evidence.invocation_id),
            content,
            conflict="Factory Codex sealed evidence replay conflicts",
        )
        return hashlib.sha256(content).hexdigest()

    def load(
        self,
        invocation: FactorySkillInvocationV1,
    ) -> CodexBuildEvidenceV1 | None:
        path = self._path(invocation.invocation_id)
        if not path.exists():
            return None
        try:
            content = path.read_bytes()
            evidence = CodexBuildEvidenceV1.model_validate_json(content)
        except (OSError, ValidationError, ValueError):
            raise FactoryDispatchError("Factory Codex sealed evidence is invalid") from None
        if content != canonical_factory_codex_model(evidence):
            raise FactoryDispatchError("Factory Codex sealed evidence is not canonical")
        if evidence.invocation != invocation:
            raise FactoryDispatchError("Factory Codex sealed evidence invocation changed")
        return evidence

    def digest(self, evidence: CodexBuildEvidenceV1) -> str:
        return hashlib.sha256(canonical_factory_codex_model(evidence)).hexdigest()

    def _path(self, invocation_id: UUID) -> Path:
        return self._root / f"{invocation_id.hex}.json"


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
    return canonical_factory_codex_model(checkpoint)


def canonical_factory_codex_model(model: BaseModel) -> bytes:
    """Return the one canonical JSON representation used by recovery stores."""

    return json.dumps(
        model.model_dump(mode="json", by_alias=True),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _atomic_write_once(path: Path, content: bytes, *, conflict: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    descriptor = os.open(
        temporary,
        getattr(os, "O_BINARY", 0) | os.O_CREAT | os.O_EXCL | os.O_WRONLY,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            try:
                existing = path.read_bytes()
            except OSError as exc:
                raise FactoryDispatchError(conflict) from exc
            if existing != content:
                raise FactoryDispatchError(conflict)
    finally:
        temporary.unlink(missing_ok=True)


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
        "scaffold_manifest_sha256",
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
    previous_retry = (
        previous.runtime_retry_authorization_uri,
        previous.runtime_retry_authorization_sha256,
        previous.runtime_retry_authorization_binding_sha256,
    )
    next_retry = (
        next_checkpoint.runtime_retry_authorization_uri,
        next_checkpoint.runtime_retry_authorization_sha256,
        next_checkpoint.runtime_retry_authorization_binding_sha256,
    )
    if transition != ("implementation_interrupted", "implementation_running") and (
        previous_retry != next_retry
    ):
        raise FactoryDispatchError(
            "Factory Codex checkpoint retry authority binding changed"
        )
    previous_parent = (
        previous.parent_terminal_receipt_sha256,
        previous.parent_journal_sha256,
        previous.parent_codex_thread_id,
    )
    next_parent = (
        next_checkpoint.parent_terminal_receipt_sha256,
        next_checkpoint.parent_journal_sha256,
        next_checkpoint.parent_codex_thread_id,
    )
    if transition == ("implementation_interrupted", "implementation_running"):
        if (
            previous.terminal_receipt_sha256 is None
            or next_checkpoint.parent_terminal_receipt_sha256
            != previous.terminal_receipt_sha256
        ):
            raise FactoryDispatchError(
                "Factory Codex checkpoint parent lineage conflicts"
            )
    elif previous_parent != next_parent:
        raise FactoryDispatchError("Factory Codex checkpoint parent lineage changed")
    previous_output = (
        previous.output_manifest_uri,
        previous.output_manifest_sha256,
    )
    next_output = (
        next_checkpoint.output_manifest_uri,
        next_checkpoint.output_manifest_sha256,
    )
    if transition == ("implementation_running", "implementation_complete"):
        if any(value is not None for value in previous_output) or any(
            value is None for value in next_output
        ):
            raise FactoryDispatchError(
                "Factory Codex checkpoint output manifest binding conflicts"
            )
    elif previous_output != next_output:
        raise FactoryDispatchError(
            "Factory Codex checkpoint output manifest binding changed"
        )
    if transition == ("implementation_complete", "sealed") and (
        next_checkpoint.terminal_receipt_sha256
        != previous.terminal_receipt_sha256
    ):
        raise FactoryDispatchError("Factory Codex checkpoint receipt binding changed")


__all__ = [
    "FactoryCodexBuildCheckpointV1",
    "FactoryCodexBuildPhase",
    "FactoryCodexScaffoldFileV1",
    "FactoryCodexScaffoldManifestV1",
    "FactoryCodexOutputArtifactV1",
    "FactoryCodexOutputManifestV1",
    "FilesystemFactoryCodexBuildCheckpointStore",
    "FilesystemFactoryCodexOutputManifestStore",
    "FilesystemFactoryCodexScaffoldManifestStore",
    "FilesystemFactoryCodexSealedEvidenceStore",
    "canonical_factory_codex_model",
]
